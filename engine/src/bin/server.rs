use std::{
	collections::HashSet,
	io::{Read, Write},
	net::{TcpListener, TcpStream},
	sync::{
		Arc,
		atomic::{AtomicBool, Ordering},
	},
};

use anyhow::{Context, Result};
use gtts_engine::{SynthesisOutcome, TtsEngine, apply_volume_gain, paths};
use serde::{Deserialize, Serialize};

const TAG_INSTALL_VOICE: u8 = 1;
const TAG_INIT: u8 = 2;
const TAG_SPEAK: u8 = 3;

const TAG_ERROR: u8 = 0x80;
const TAG_ACK: u8 = 0x81;
const TAG_AUDIO_CHUNK: u8 = 0x82;
const TAG_SPEAK_DONE: u8 = 0x83;
const TAG_SPEAK_CANCELLED: u8 = 0x84;

#[derive(Deserialize)]
struct InstallVoiceReq {
	voice_dir: String,
	zvoice_path: String,
}

#[derive(Deserialize)]
struct InitReq {
	voice_dir: String,
}

#[derive(Deserialize)]
struct SpeakReq {
	text: String,
	rate: f32,
	pitch: f32,
	speaker: String,
	gain: f32,
}

#[derive(Serialize)]
struct ErrorResp<'a> {
	message: &'a str,
}

fn read_frame(stream: &mut TcpStream) -> Result<(u8, Vec<u8>)> {
	let mut header = [0u8; 5];
	stream.read_exact(&mut header)?;
	let tag = header[0];
	let len = u32::from_le_bytes(header[1..5].try_into().unwrap()) as usize;
	let mut payload = vec![0u8; len];
	stream.read_exact(&mut payload)?;
	Ok((tag, payload))
}

fn write_frame(stream: &mut TcpStream, tag: u8, payload: &[u8]) -> Result<()> {
	stream.write_all(&[tag])?;
	stream.write_all(&(payload.len() as u32).to_le_bytes())?;
	stream.write_all(payload)?;
	stream.flush()?;
	Ok(())
}

fn write_error(stream: &mut TcpStream, message: &str) -> Result<()> {
	write_frame(stream, TAG_ERROR, &serde_json::to_vec(&ErrorResp { message })?)
}

fn main() -> Result<()> {
	let wasm_path = paths::wasm_path().to_string_lossy().into_owned();
	let vfs_root = paths::vfs_root("server").to_string_lossy().into_owned();

	let main_listener = TcpListener::bind("127.0.0.1:0")?;
	let control_listener = TcpListener::bind("127.0.0.1:0")?;
	let main_port = main_listener.local_addr()?.port();
	let control_port = control_listener.local_addr()?.port();
	println!("READY {main_port} {control_port}");
	std::io::stdout().flush()?;

	let cancel_flag = Arc::new(AtomicBool::new(false));
	{
		let cancel_flag = cancel_flag.clone();
		std::thread::spawn(move || {
			let Ok((mut stream, _)) = control_listener.accept() else { return };
			let mut buf = [0u8; 1];
			while stream.read(&mut buf).unwrap_or(0) > 0 {
				cancel_flag.store(true, Ordering::SeqCst);
			}
		});
	}

	eprintln!("Loading engine from {wasm_path} ...");
	let mut engine = TtsEngine::new(&wasm_path, &vfs_root).context("failed to start TtsEngine")?;
	eprintln!("Engine ready.");

	let (mut stream, _) = main_listener.accept()?;
	let mut installed_voice_dirs: HashSet<String> = HashSet::new();
	let mut current_voice_dir: Option<String> = None;

	loop {
		let (tag, payload) = match read_frame(&mut stream) {
			Ok(v) => v,
			Err(_) => break,
		};
		match tag {
			TAG_INSTALL_VOICE => {
				let result: Result<()> = (|| {
					let req: InstallVoiceReq = serde_json::from_slice(&payload)?;
					if installed_voice_dirs.contains(&req.voice_dir) {
						return Ok(());
					}
					let zvoice_bytes = std::fs::read(&req.zvoice_path)?;
					engine.install_voice(&req.voice_dir, &zvoice_bytes)?;
					installed_voice_dirs.insert(req.voice_dir);
					Ok(())
				})();
				match result {
					Ok(()) => write_frame(&mut stream, TAG_ACK, b"{}")?,
					Err(e) => write_error(&mut stream, &e.to_string())?,
				}
			}
			TAG_INIT => {
				let result: Result<()> = (|| {
					let req: InitReq = serde_json::from_slice(&payload)?;
					if current_voice_dir.as_deref() != Some(req.voice_dir.as_str()) {
						engine.init(&req.voice_dir)?;
						current_voice_dir = Some(req.voice_dir);
					}
					Ok(())
				})();
				match result {
					Ok(()) => write_frame(&mut stream, TAG_ACK, b"{}")?,
					Err(e) => write_error(&mut stream, &e.to_string())?,
				}
			}
			TAG_SPEAK => {
				let req: SpeakReq = match serde_json::from_slice(&payload) {
					Ok(r) => r,
					Err(e) => {
						write_error(&mut stream, &e.to_string())?;
						continue;
					}
				};
				cancel_flag.store(false, Ordering::SeqCst);
				let gain = req.gain;
				let outcome = engine.synthesize_streaming(
					&req.text,
					req.rate,
					req.pitch,
					&req.speaker,
					|chunk| {
						let scaled = apply_volume_gain(chunk, gain);
						let _ = write_frame(&mut stream, TAG_AUDIO_CHUNK, &scaled);
					},
					|| cancel_flag.load(Ordering::SeqCst),
				);
				match outcome {
					Ok(SynthesisOutcome::Completed) => write_frame(&mut stream, TAG_SPEAK_DONE, b"{}")?,
					Ok(SynthesisOutcome::Cancelled) => write_frame(&mut stream, TAG_SPEAK_CANCELLED, b"{}")?,
					Err(e) => write_error(&mut stream, &e.to_string())?,
				}
			}
			_ => {
				write_error(&mut stream, "unknown frame tag")?;
			}
		}
	}

	Ok(())
}
