use std::path::{Path, PathBuf};

pub const ENGINE_VERSION: &str = "20260625.1";

fn env_path(key: &str) -> Option<PathBuf> {
	std::env::var_os(key).map(PathBuf::from).filter(|p| !p.as_os_str().is_empty())
}

pub fn wasm_path() -> PathBuf {
	if let Some(path) = env_path("GTTS_WASM_PATH") {
		return path;
	}
	let relative = Path::new("WasmTtsEngine").join(ENGINE_VERSION).join("bindings_main.wasm");
	if let Ok(exe) = std::env::current_exe() {
		if let Some(addon_dir) = exe.parent().and_then(Path::parent) {
			let candidate = addon_dir.join(&relative);
			if candidate.is_file() {
				return candidate;
			}
		}
	}
	repo_root().join("addon").join("synthDrivers").join("googleTtsRs").join(&relative)
}

pub const DATA_DIR_NAME: &str = "googleTtsRs";
pub const LEGACY_DATA_DIR_NAME: &str = "googleTtsForNvda";

pub fn voices_dir() -> PathBuf {
	if let Some(path) = env_path("GTTS_VOICES_DIR") {
		return path;
	}
	let appdata = env_path("APPDATA").unwrap_or_else(|| PathBuf::from("."));
	appdata.join("nvda").join(DATA_DIR_NAME).join("voices")
}

pub fn legacy_voices_dir() -> PathBuf {
	let appdata = env_path("APPDATA").unwrap_or_else(|| PathBuf::from("."));
	appdata.join("nvda").join(LEGACY_DATA_DIR_NAME).join("voices")
}

pub fn voice_package(package_id: &str) -> PathBuf {
	let name = format!("{package_id}.zvoice");
	let path = voices_dir().join(&name);
	if path.is_file() {
		return path;
	}
	let legacy = legacy_voices_dir().join(&name);
	if legacy.is_file() {
		return legacy;
	}
	path
}

pub fn vfs_root(name: &str) -> PathBuf {
	if let Some(path) = env_path("GTTS_VFS_ROOT") {
		return path;
	}
	std::env::temp_dir().join("google-tts-rs").join(name)
}

pub fn repo_root() -> PathBuf {
	Path::new(env!("CARGO_MANIFEST_DIR")).parent().map(Path::to_path_buf).unwrap_or_default()
}

pub fn out_dir() -> PathBuf {
	env_path("GTTS_OUT_DIR").unwrap_or_else(|| repo_root().join("out"))
}
