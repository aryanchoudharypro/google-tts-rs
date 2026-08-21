use std::{
	hash::{Hash, Hasher},
	path::{Path, PathBuf},
};

use anyhow::Result;
use wasmtime::{Engine, Module};

fn cache_dir() -> PathBuf {
	if let Some(dir) = std::env::var_os("GTTS_CACHE_DIR").filter(|v| !v.is_empty()) {
		return PathBuf::from(dir);
	}
	let base = std::env::var_os("LOCALAPPDATA")
		.filter(|v| !v.is_empty())
		.map(PathBuf::from)
		.unwrap_or_else(std::env::temp_dir);
	base.join("googleTtsRs").join("modcache")
}

fn fingerprint(engine: &Engine, wasm_path: &Path, wasm: &[u8]) -> String {
	let mut hash: u64 = 0xcbf29ce484222325;
	let mut mix = |bytes: &[u8]| {
		for byte in bytes {
			hash ^= u64::from(*byte);
			hash = hash.wrapping_mul(0x100000001b3);
		}
	};
	mix(&(wasm.len() as u64).to_le_bytes());
	let step = (wasm.len() / 4096).max(1);
	for chunk in wasm.chunks(step) {
		mix(&chunk[..chunk.len().min(64)]);
	}
	let mut compat = std::collections::hash_map::DefaultHasher::new();
	engine.precompile_compatibility_hash().hash(&mut compat);
	mix(&compat.finish().to_le_bytes());
	mix(wasm_path.to_string_lossy().as_bytes());
	format!("{hash:016x}")
}

pub fn load(engine: &Engine, wasm_path: &Path, wasm: &[u8]) -> Result<Module> {
	if std::env::var_os("GTTS_NO_MODULE_CACHE").is_some() {
		return Ok(Module::new(engine, wasm)?);
	}
	let dir = cache_dir();
	let path = dir.join(format!("{}.cwasm", fingerprint(engine, wasm_path, wasm)));

	if path.is_file() {
		match unsafe { Module::deserialize_file(engine, &path) } {
			Ok(module) => return Ok(module),
			Err(error) => {
				eprintln!("[modcache] ignoring unusable cache entry {}: {error}", path.display());
				let _ = std::fs::remove_file(&path);
			}
		}
	}

	let module = Module::new(engine, wasm)?;
	if let Err(error) = write_entry(&dir, &path, &module) {
		eprintln!("[modcache] could not write {}: {error}", path.display());
	}
	Ok(module)
}

fn write_entry(dir: &Path, path: &Path, module: &Module) -> Result<()> {
	std::fs::create_dir_all(dir)?;
	let serialized = module.serialize()?;
	let temporary = path.with_extension(format!("tmp{}", std::process::id()));
	std::fs::write(&temporary, serialized)?;
	std::fs::rename(&temporary, path)?;
	prune(dir, path);
	Ok(())
}

fn prune(dir: &Path, keep: &Path) {
	let Ok(entries) = std::fs::read_dir(dir) else { return };
	for entry in entries.flatten() {
		let path = entry.path();
		if path != keep && path.extension().is_some_and(|ext| ext == "cwasm") {
			let _ = std::fs::remove_file(path);
		}
	}
}
