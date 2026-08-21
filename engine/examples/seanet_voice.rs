use anyhow::{Context, Result};
use gtts_engine::{TtsEngine, catalog::Catalog, paths, wav};

const NATURAL_PACKAGE: &str = "hi-in-x-multi-seanet";
const TEXT: &str = "नमस्ते, यह एक परीक्षण है।";

fn main() -> Result<()> {
	let catalog = Catalog::load_default()?;
	let mut engine =
		TtsEngine::new(&paths::wasm_path().to_string_lossy(), &paths::vfs_root("seanet").to_string_lossy())?;
	println!("Engine instantiated OK.");

	for package in catalog.install_order(NATURAL_PACKAGE)? {
		let zvoice = paths::voice_package(&package.id);
		let bytes = std::fs::read(&zvoice)
			.with_context(|| format!("no voice package at {} — install it in NVDA first", zvoice.display()))?;
		engine.install_voice(&package.voice_dir(), &bytes)?;
		println!(">>> Installed {} at {} ({} bytes).", package.id, package.voice_dir(), bytes.len());
	}

	let natural = catalog.get(NATURAL_PACKAGE)?;
	let start = std::time::Instant::now();
	engine.init(&natural.voice_dir())?;
	println!(">>> Pipeline initialized in {:.2}s.", start.elapsed().as_secs_f64());

	for speaker in &natural.speakers {
		let t = std::time::Instant::now();
		let pcm = engine.synthesize(TEXT, 1.0, 1.0, &speaker.code)?;
		println!(
			"  {} ({}): {} bytes, {:.2}s audio in {:.2}s wall",
			speaker.code,
			speaker.name,
			pcm.len(),
			wav::duration_secs(&pcm, 24000),
			t.elapsed().as_secs_f64(),
		);
		let pcm = gtts_engine::apply_volume_gain(&pcm, gtts_engine::volume_percent_to_gain(100));
		wav::write_pcm16_mono(paths::out_dir().join(format!("seanet-{}.wav", speaker.code)), 24000, &pcm)?;
	}
	println!("Wrote seanet-<speaker>.wav for all {} Natural speakers.", natural.speakers.len());
	Ok(())
}
