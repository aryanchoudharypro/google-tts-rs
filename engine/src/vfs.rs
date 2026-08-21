use std::{
	collections::HashMap,
	fs::{File, OpenOptions},
	io::{Read, Seek, SeekFrom, Write},
	path::PathBuf,
};

pub struct FdEntry {
	pub file: File,
}

pub struct Vfs {
	pub root: PathBuf,
	pub fds: HashMap<i32, FdEntry>,
	pub next_fd: i32,
}

impl Vfs {
	pub fn new(root: PathBuf) -> Self {
		std::fs::create_dir_all(&root).ok();
		Vfs { root, fds: HashMap::new(), next_fd: 4 }
	}

	pub fn resolve(&self, wasm_path: &str) -> PathBuf {
		let trimmed = wasm_path.trim_start_matches('/');
		let mut out = self.root.clone();
		for part in trimmed.split('/') {
			if part.is_empty() || part == "." || part == ".." {
				continue;
			}
			out.push(part);
		}
		out
	}

	pub fn ensure_dir(&self, wasm_path: &str) -> std::io::Result<()> {
		let p = self.resolve(wasm_path);
		std::fs::create_dir_all(p)
	}

	pub fn openat(&mut self, wasm_path: &str, flags: i32, _mode: i32) -> i32 {
		const O_WRONLY: i32 = 1;
		const O_RDWR: i32 = 2;
		const O_CREAT: i32 = 0x40;
		const O_TRUNC: i32 = 0x200;
		const O_DIRECTORY: i32 = 0x10000;

		let path = self.resolve(wasm_path);
		if flags & O_DIRECTORY != 0 {
			if !path.is_dir() {
				return -2;
			}
			let fd = self.next_fd;
			self.next_fd += 1;
			if let Ok(f) = File::open(&path) {
				self.fds.insert(fd, FdEntry { file: f });
				return fd;
			}
			return -2;
		}

		if let Some(parent) = path.parent() {
			if flags & O_CREAT != 0 {
				let _ = std::fs::create_dir_all(parent);
			}
		}

		let mut opts = OpenOptions::new();
		opts.read(true);
		if flags & (O_WRONLY | O_RDWR) != 0 {
			opts.write(true);
		}
		if flags & O_CREAT != 0 {
			opts.create(true);
		}
		if flags & O_TRUNC != 0 {
			opts.truncate(true);
		}

		match opts.open(&path) {
			Ok(f) => {
				let fd = self.next_fd;
				self.next_fd += 1;
				self.fds.insert(fd, FdEntry { file: f });
				fd
			}
			Err(e) => {
				eprintln!("[vfs] openat({wasm_path:?}, flags={flags:#x}) -> {e}");
				-2
			}
		}
	}

	pub fn close(&mut self, fd: i32) -> i32 {
		if self.fds.remove(&fd).is_some() { 0 } else { -9 }
	}

	pub fn read(&mut self, fd: i32, buf: &mut [u8]) -> i64 {
		match self.fds.get_mut(&fd) {
			Some(e) => match e.file.read(buf) {
				Ok(n) => n as i64,
				Err(_) => -5,
			},
			None => -9,
		}
	}

	pub fn write(&mut self, fd: i32, buf: &[u8]) -> i64 {
		if fd == 1 || fd == 2 {
			let _ = std::io::stdout().write_all(buf);
			return buf.len() as i64;
		}
		match self.fds.get_mut(&fd) {
			Some(e) => match e.file.write(buf) {
				Ok(n) => n as i64,
				Err(_) => -5,
			},
			None => -9,
		}
	}

	pub fn seek(&mut self, fd: i32, offset: i64, whence: i32) -> i64 {
		let from = match whence {
			0 => SeekFrom::Start(offset as u64),
			1 => SeekFrom::Current(offset),
			2 => SeekFrom::End(offset),
			_ => return -22,
		};
		match self.fds.get_mut(&fd) {
			Some(e) => match e.file.seek(from) {
				Ok(pos) => pos as i64,
				Err(_) => -29,
			},
			None => -9,
		}
	}

	pub fn stat_path(&self, wasm_path: &str) -> Option<(u64, bool)> {
		let path = self.resolve(wasm_path);
		std::fs::metadata(&path).ok().map(|m| (m.len(), m.is_dir()))
	}

	pub fn stat_fd(&self, fd: i32) -> Option<(u64, bool)> {
		self.fds.get(&fd).and_then(|e| e.file.metadata().ok()).map(|m| (m.len(), m.is_dir()))
	}
}

pub fn read_cstr(mem: &[u8], ptr: u32) -> String {
	let start = ptr as usize;
	let end = mem[start..].iter().position(|&b| b == 0).map(|i| start + i).unwrap_or(mem.len());
	String::from_utf8_lossy(&mem[start..end]).into_owned()
}

pub fn write_stat64(mem: &mut [u8], buf: u32, size: u64, is_dir: bool) {
	let b = buf as usize;
	let mode: u32 = if is_dir { 0o040755 } else { 0o100644 };
	write_u32(mem, b, 1);
	write_u32(mem, b + 4, mode);
	write_u32(mem, b + 8, 1);
	write_u32(mem, b + 12, 0);
	write_u32(mem, b + 16, 0);
	write_u32(mem, b + 20, 0);
	write_i64(mem, b + 24, size as i64);
	write_u32(mem, b + 32, 4096);
	write_u32(mem, b + 36, ((size + 511) / 512) as u32);
	write_i32(mem, b + 40, 0);
	write_i32(mem, b + 44, 0);
	write_i32(mem, b + 48, 0);
	write_i32(mem, b + 52, 0);
	write_i32(mem, b + 56, 0);
	write_i32(mem, b + 60, 0);
	write_i64(mem, b + 64, 1);
}

fn write_u32(mem: &mut [u8], off: usize, v: u32) {
	mem[off..off + 4].copy_from_slice(&v.to_le_bytes());
}
fn write_i32(mem: &mut [u8], off: usize, v: i32) {
	mem[off..off + 4].copy_from_slice(&v.to_le_bytes());
}
fn write_i64(mem: &mut [u8], off: usize, v: i64) {
	mem[off..off + 8].copy_from_slice(&v.to_le_bytes());
}
