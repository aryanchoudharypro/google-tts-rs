use anyhow::{Context, Result};
use gtts_engine::{TtsEngine, catalog::Catalog, paths, wav};

const PACKAGE: &str = "hi-in-x-multi";
const TEXT: &str = "नमस्ते, यह एक परीक्षण है।";

fn main() -> Result<()> {
	let catalog = Catalog::load_default()?;
	let package = catalog.get(PACKAGE)?;

	let mut engine = TtsEngine::new(&paths::wasm_path().to_string_lossy(), &paths::vfs_root("hi").to_string_lossy())?;
	println!("Engine instantiated OK.");

	let zvoice = paths::voice_package(PACKAGE);
	let voice_bytes = std::fs::read(&zvoice)
		.with_context(|| format!("no voice package at {} — install it in NVDA first", zvoice.display()))?;
	engine.install_voice(&package.voice_dir(), &voice_bytes)?;
	println!(">>> Installed {PACKAGE} at {} ({} bytes).", package.voice_dir(), voice_bytes.len());

	engine.init(&package.voice_dir())?;
	println!(">>> Pipeline initialized.");

	for speaker in &package.speakers {
		let pcm = engine.synthesize(TEXT, 1.0, 1.0, &speaker.code)?;
		println!("  {} ({}): {:.2}s audio", speaker.code, speaker.name, wav::duration_secs(&pcm, 24000));
		let pcm = gtts_engine::apply_volume_gain(&pcm, gtts_engine::volume_percent_to_gain(100));
		wav::write_pcm16_mono(paths::out_dir().join(format!("{PACKAGE}-{}.wav", speaker.code)), 24000, &pcm)?;
	}
	println!("Wrote {PACKAGE}-<speaker>.wav for all {} speakers.", package.speakers.len());
	Ok(())
}
