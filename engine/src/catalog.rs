use std::path::Path;

use anyhow::{Context, Result, bail};

#[derive(Debug, Clone)]
pub struct Speaker {
	pub code: String,
	pub name: String,
	pub gender: String,
}

#[derive(Debug, Clone)]
pub struct VoicePackage {
	pub id: String,
	pub file_id: String,
	pub speakers: Vec<Speaker>,
	pub dependent_voice_id: Option<String>,
}

impl VoicePackage {
	pub fn voice_dir(&self) -> String {
		format!("/voices/{}", self.file_id)
	}
}

#[derive(Debug, Clone)]
pub struct Catalog {
	pub packages: Vec<VoicePackage>,
}

impl Catalog {
	pub fn load_default() -> Result<Self> {
		let wasm = crate::paths::wasm_path();
		let dir = wasm.parent().context("wasm path has no parent directory")?;
		Self::load(dir.join("voices.json"))
	}

	pub fn load(path: impl AsRef<Path>) -> Result<Self> {
		let path = path.as_ref();
		let raw = std::fs::read_to_string(path)
			.with_context(|| format!("failed to read voice catalog at {}", path.display()))?;
		let value: serde_json::Value = serde_json::from_str(&raw)?;
		let items = value.as_array().context("voices.json should be a JSON array")?;
		let mut packages = Vec::with_capacity(items.len());
		for item in items {
			let Some(id) = item.get("id").and_then(|v| v.as_str()) else { continue };
			let file_id = item.get("fileId").and_then(|v| v.as_str()).unwrap_or(id);
			let speakers = item
				.get("speakers")
				.and_then(|v| v.as_array())
				.map(|list| {
					list.iter()
						.filter_map(|s| {
							Some(Speaker {
								code: s.get("speaker")?.as_str()?.to_string(),
								name: s.get("name").and_then(|v| v.as_str()).unwrap_or_default().to_string(),
								gender: s.get("gender").and_then(|v| v.as_str()).unwrap_or_default().to_string(),
							})
						})
						.collect()
				})
				.unwrap_or_default();
			packages.push(VoicePackage {
				id: id.to_string(),
				file_id: file_id.to_string(),
				speakers,
				dependent_voice_id: item
					.get("dependentVoiceId")
					.and_then(|v| v.as_str())
					.filter(|s| !s.is_empty())
					.map(str::to_string),
			});
		}
		Ok(Catalog { packages })
	}

	pub fn get(&self, package_id: &str) -> Result<&VoicePackage> {
		match self.packages.iter().find(|p| p.id == package_id) {
			Some(package) => Ok(package),
			None => bail!("no voice package {package_id:?} in the catalog"),
		}
	}

	pub fn install_order(&self, package_id: &str) -> Result<Vec<&VoicePackage>> {
		let package = self.get(package_id)?;
		let mut order = Vec::new();
		if let Some(dependency) = &package.dependent_voice_id {
			order.push(self.get(dependency)?);
		}
		order.push(package);
		Ok(order)
	}
}
