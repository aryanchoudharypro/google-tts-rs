fn write_varint(out: &mut Vec<u8>, mut v: u64) {
	loop {
		let mut b = (v & 0x7f) as u8;
		v >>= 7;
		if v != 0 {
			b |= 0x80;
		}
		out.push(b);
		if v == 0 {
			break;
		}
	}
}

fn write_tag(out: &mut Vec<u8>, field: u32, wire: u32) {
	write_varint(out, ((field as u64) << 3) | wire as u64);
}

fn write_len_delim(out: &mut Vec<u8>, field: u32, payload: &[u8]) {
	write_tag(out, field, 2);
	write_varint(out, payload.len() as u64);
	out.extend_from_slice(payload);
}

fn write_string(out: &mut Vec<u8>, field: u32, s: &str) {
	write_len_delim(out, field, s.as_bytes());
}

fn write_float(out: &mut Vec<u8>, field: u32, v: f32) {
	write_tag(out, field, 5);
	out.extend_from_slice(&v.to_le_bytes());
}

pub fn pitch_to_wire(pitch: f32) -> f32 {
	2f32.powf((5.0 / 3.0) * (pitch - 1.0))
}

pub fn build_request(text: &str, rate: f32, pitch: f32) -> Vec<u8> {
	let mut level3 = Vec::new();
	write_string(&mut level3, 2, text);
	let mut level2 = Vec::new();
	write_len_delim(&mut level2, 1, &level3);
	let mut text_wrap = Vec::new();
	write_len_delim(&mut text_wrap, 2, &level2);

	let mut params_inner = Vec::new();
	write_float(&mut params_inner, 1, rate);
	write_float(&mut params_inner, 6, pitch_to_wire(pitch));
	let mut params_wrap = Vec::new();
	write_len_delim(&mut params_wrap, 3, &params_inner);

	let mut utterance = Vec::new();
	write_len_delim(&mut utterance, 1, &text_wrap);
	write_len_delim(&mut utterance, 2, &params_wrap);

	let mut top = Vec::new();
	write_len_delim(&mut top, 2, &utterance);
	top
}

pub fn build_config(speaker_code: &str) -> Vec<u8> {
	let mut out = Vec::new();
	write_string(&mut out, 1, speaker_code);
	out
}

#[cfg(test)]
mod tests {
	use super::*;

	const TEXT: &str = "Testing one two three.";
	const CAPTURE_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures");

	fn check_capture(file_prefix: &str, rate: f32, pitch: f32) {
		let req = std::fs::read(format!("{CAPTURE_DIR}\\{file_prefix}-req.bin")).unwrap();
		assert_eq!(build_request(TEXT, rate, pitch), req, "{file_prefix} request mismatch");
	}

	#[test]
	fn matches_capture_rate1_pitch1() {
		check_capture("session-1787096651", 1.0, 1.0);
	}

	#[test]
	fn matches_capture_rate2_pitch1() {
		check_capture("session-1787097164-rate2p1", 2.0, 1.0);
	}

	#[test]
	fn matches_capture_rate1_pitch0p5() {
		check_capture("session-1787097170-r1p05", 1.0, 0.5);
	}

	#[test]
	fn matches_capture_rate1p6_pitch2() {
		check_capture("session-1787097208-r16p20", 1.6, 2.0);
	}
}
