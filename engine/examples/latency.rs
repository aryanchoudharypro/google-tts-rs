use std::time::Instant;

use anyhow::{Context, Result};
use gtts_engine::{TtsEngine, catalog::Catalog, paths};

const BASE: &str = "en-in-x-multi";
const BASE_SPEAKER: &str = "cxx";
const NATURAL: &str = "hi-in-x-multi-seanet";
const NATURAL_SPEAKER: &str = "cfn";

fn read_package(package_id: &str) -> Result<Vec<u8>> {
	let path = paths::voice_package(package_id);
	std::fs::read(&path).with_context(|| format!("no voice package at {}", path.display()))
}

fn first_chunk_latency(engine: &mut TtsEngine, text: &str, speaker: &str) -> Result<(f64, f64)> {
	let start = Instant::now();
	let mut first: Option<f64> = None;
	let mut total = 0usize;
	engine.synthesize_streaming(
		text,
		1.0,
		1.0,
		speaker,
		|chunk| {
			if first.is_none() && !chunk.is_empty() {
				first = Some(start.elapsed().as_secs_f64() * 1000.0);
			}
			total += chunk.len();
		},
		|| false,
	)?;
	let _ = total;
	Ok((first.unwrap_or(f64::NAN), start.elapsed().as_secs_f64() * 1000.0))
}

fn main() -> Result<()> {
	let catalog = Catalog::load_default()?;

	let t = Instant::now();
	let mut engine =
		TtsEngine::new(&paths::wasm_path().to_string_lossy(), &paths::vfs_root("latency").to_string_lossy())?;
	println!("engine construction (compile + instantiate + ctors): {:>8.0} ms", t.elapsed().as_secs_f64() * 1000.0);

	for package_id in [BASE, NATURAL] {
		for package in catalog.install_order(package_id)? {
			let t = Instant::now();
			engine.install_voice(&package.voice_dir(), &read_package(&package.id)?)?;
			println!("install {:<24} {:>8.0} ms", package.id, t.elapsed().as_secs_f64() * 1000.0);
		}
	}

	println!();
	for (label, package_id, speaker) in [
		("base #1", BASE, BASE_SPEAKER),
		("natural #1", NATURAL, NATURAL_SPEAKER),
		("base #2", BASE, BASE_SPEAKER),
		("natural #2", NATURAL, NATURAL_SPEAKER),
		("base #3", BASE, BASE_SPEAKER),
		("natural #3", NATURAL, NATURAL_SPEAKER),
	] {
		let t = Instant::now();
		engine.init(&catalog.get(package_id)?.voice_dir())?;
		println!("init {:<26} {:>8.0} ms", label, t.elapsed().as_secs_f64() * 1000.0);

		for text in ["Hello.", "the quick brown fox jumped over the lazy dog and kept running"] {
			let (first, whole) = first_chunk_latency(&mut engine, text, speaker)?;
			println!("   first audio {:>7.0} ms   whole utterance {:>7.0} ms   {:?}", first, whole, text);
		}
	}
	Ok(())
}
