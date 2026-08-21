use std::path::Path;

use anyhow::Result;

pub fn write_pcm16_mono(path: impl AsRef<Path>, sample_rate: u32, pcm: &[u8]) -> Result<()> {
	let path = path.as_ref();
	if let Some(parent) = path.parent() {
		std::fs::create_dir_all(parent)?;
	}
	let data_len = pcm.len() as u32;
	let mut out = Vec::with_capacity(44 + pcm.len());
	out.extend_from_slice(b"RIFF");
	out.extend_from_slice(&(36 + data_len).to_le_bytes());
	out.extend_from_slice(b"WAVE");
	out.extend_from_slice(b"fmt ");
	out.extend_from_slice(&16u32.to_le_bytes());
	out.extend_from_slice(&1u16.to_le_bytes());
	out.extend_from_slice(&1u16.to_le_bytes());
	out.extend_from_slice(&sample_rate.to_le_bytes());
	out.extend_from_slice(&(sample_rate * 2).to_le_bytes());
	out.extend_from_slice(&2u16.to_le_bytes());
	out.extend_from_slice(&16u16.to_le_bytes());
	out.extend_from_slice(b"data");
	out.extend_from_slice(&data_len.to_le_bytes());
	out.extend_from_slice(pcm);
	std::fs::write(path, out)?;
	Ok(())
}

pub fn duration_secs(pcm: &[u8], sample_rate: u32) -> f64 {
	pcm.len() as f64 / 2.0 / f64::from(sample_rate)
}
