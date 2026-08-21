use anyhow::{Context, Result};
use gtts_engine::{TtsEngine, catalog::Catalog, paths, wav};

const DEFAULT_PACKAGE: &str = "en-in-x-multi";
const DEFAULT_SPEAKER: &str = "cxx";
const DEFAULT_TEXT: &str = "Hello from a native Rust engine, no Chrome involved.";

fn main() -> Result<()> {
	let mut args = std::env::args().skip(1);
	let package_id = args.next().unwrap_or_else(|| DEFAULT_PACKAGE.to_string());
	let speaker = args.next().unwrap_or_else(|| DEFAULT_SPEAKER.to_string());
	let text = args.next().unwrap_or_else(|| DEFAULT_TEXT.to_string());

	let wasm_path = paths::wasm_path();
	println!("Compiling {} ...", wasm_path.display());
	let mut engine = TtsEngine::new(&wasm_path.to_string_lossy(), &paths::vfs_root("cli").to_string_lossy())?;
	println!("Engine instantiated OK.");

	let catalog = Catalog::load_default()?;
	for package in catalog.install_order(&package_id)? {
		let zvoice = paths::voice_package(&package.id);
		let bytes = std::fs::read(&zvoice)
			.with_context(|| format!("no voice package at {} — install it in NVDA first", zvoice.display()))?;
		engine.install_voice(&package.voice_dir(), &bytes)?;
		println!(">>> Installed {} at {} ({} bytes).", package.id, package.voice_dir(), bytes.len());
	}
	engine.init(&catalog.get(&package_id)?.voice_dir())?;
	println!(">>> Pipeline initialized.");

	{
		let fixtures = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests").join("fixtures");
		let ref_req = std::fs::read(fixtures.join("session-1787096651-req.bin"))?;
		let ref_cfg = std::fs::read(fixtures.join("session-1787096651-cfg.bin"))?;
		anyhow::ensure!(
			gtts_engine::proto::build_request("Testing one two three.", 1.0, 1.0) == ref_req,
			"request proto encoder mismatch vs real capture",
		);
		anyhow::ensure!(
			gtts_engine::proto::build_config("cxx") == ref_cfg,
			"config proto encoder mismatch vs real capture",
		);
		println!("Proto encoder verified byte-identical to real Chrome capture.");
	}

	let pcm = engine.synthesize(&text, 1.0, 1.0, &speaker)?;
	println!("Extracted {} bytes of 24kHz 16-bit mono PCM.", pcm.len());

	let pcm = gtts_engine::apply_volume_gain(&pcm, gtts_engine::volume_percent_to_gain(100));

	let out = paths::out_dir().join(format!("{package_id}-{speaker}.wav"));
	wav::write_pcm16_mono(&out, 24000, &pcm)?;
	println!("Wrote {} ({:.2}s)", out.display(), wav::duration_secs(&pcm, 24000));
	Ok(())
}
