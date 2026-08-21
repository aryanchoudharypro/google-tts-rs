pub mod catalog;
pub mod modcache;
pub mod paths;
pub mod proto;
mod vfs;
pub mod wav;

use anyhow::{Result, bail};
use vfs::{Vfs, read_cstr, write_stat64};
use wasmtime::*;

pub struct HostState {
	mem: SharedMemory,
	vfs: Vfs,
	start: std::time::Instant,
}

unsafe fn mem_bytes(mem: &SharedMemory) -> &mut [u8] {
	let cells = mem.data();
	unsafe { std::slice::from_raw_parts_mut(cells.as_ptr() as *mut u8, cells.len()) }
}

fn read_varint(buf: &[u8], pos: &mut usize) -> Option<u64> {
	let mut result = 0u64;
	let mut shift = 0;
	loop {
		let b = *buf.get(*pos)?;
		*pos += 1;
		result |= ((b & 0x7f) as u64) << shift;
		if b & 0x80 == 0 {
			return Some(result);
		}
		shift += 7;
		if shift > 63 {
			return None;
		}
	}
}

fn try_extract_audio(buf: &[u8]) -> Option<&[u8]> {
	let mut pos = 0usize;
	let mut pcm: Option<&[u8]> = None;
	let mut rate: Option<u64> = None;
	while pos < buf.len() {
		let tag = read_varint(buf, &mut pos)?;
		let field = tag >> 3;
		match tag & 0x7 {
			0 => {
				let v = read_varint(buf, &mut pos)?;
				if field == 2 {
					rate = Some(v);
				}
			}
			2 => {
				let len = read_varint(buf, &mut pos)? as usize;
				let payload = buf.get(pos..pos + len)?;
				pos += len;
				if field == 1 {
					pcm = Some(payload);
				}
			}
			1 => pos += 8,
			5 => pos += 4,
			_ => return None,
		}
	}
	if rate == Some(24000) { pcm } else { None }
}

fn find_audio_chunks(buf: &[u8], out: &mut Vec<u8>) {
	walk_audio_chunks(buf, &mut |pcm| out.extend_from_slice(pcm));
}

fn walk_audio_chunks(buf: &[u8], on_chunk: &mut dyn FnMut(&[u8])) {
	let mut pos = 0usize;
	while pos < buf.len() {
		let Some(tag) = read_varint(buf, &mut pos) else { return };
		match tag & 0x7 {
			0 => {
				if read_varint(buf, &mut pos).is_none() {
					return;
				}
			}
			2 => {
				let Some(len) = read_varint(buf, &mut pos) else { return };
				let len = len as usize;
				let Some(payload) = buf.get(pos..pos + len) else { return };
				pos += len;
				match try_extract_audio(payload) {
					Some(pcm) => on_chunk(pcm),
					None => walk_audio_chunks(payload, on_chunk),
				}
			}
			1 => pos += 8,
			5 => pos += 4,
			_ => return,
		}
	}
}

pub fn volume_percent_to_gain(volume_percent: i32) -> f32 {
	(volume_percent as f32 / 50.0).clamp(0.0, 2.0)
}

pub fn apply_volume_gain(pcm: &[u8], gain: f32) -> Vec<u8> {
	let mut out = Vec::with_capacity(pcm.len());
	for chunk in pcm.chunks_exact(2) {
		let sample = i16::from_le_bytes([chunk[0], chunk[1]]);
		let f = ((sample as f32 / 32768.0) * gain).clamp(-1.0, 1.0);
		let scaled = if f < 0.0 { f * 32768.0 } else { f * 32767.0 };
		out.extend_from_slice(&(scaled as i16).to_le_bytes());
	}
	out
}

#[cfg(test)]
mod gain_tests {
	use super::*;

	#[test]
	fn zero_volume_is_silence() {
		let pcm = [0x00, 0x40, 0x00, 0xC0];
		let out = apply_volume_gain(&pcm, volume_percent_to_gain(0));
		assert_eq!(out, [0, 0, 0, 0]);
	}

	#[test]
	fn full_scale_clamps_at_double_gain() {
		let pcm = i16::MAX.to_le_bytes().into_iter().chain(i16::MIN.to_le_bytes()).collect::<Vec<u8>>();
		let out = apply_volume_gain(&pcm, volume_percent_to_gain(100));
		let hi = i16::from_le_bytes([out[0], out[1]]);
		let lo = i16::from_le_bytes([out[2], out[3]]);
		assert_eq!(hi, i16::MAX);
		assert_eq!(lo, i16::MIN);
	}

	#[test]
	fn volume_to_gain_matches_bridge_py_formula() {
		assert_eq!(volume_percent_to_gain(0), 0.0);
		assert_eq!(volume_percent_to_gain(50), 1.0);
		assert_eq!(volume_percent_to_gain(100), 2.0);
		assert_eq!(volume_percent_to_gain(200), 2.0);
	}
}

fn run_pthread(
	engine: &Engine,
	module: &Module,
	shared_mem: &SharedMemory,
	vfs_root: &str,
	trace_stubs: bool,
	pthread_ptr: i32,
	stack_high: i32,
	stack_low: i32,
	start_routine: i32,
	arg: i32,
) -> Result<()> {
	let debug = std::env::var_os("GTTS_TRACE_PTHREAD").is_some();
	macro_rules! dbg_pt { ($($arg:tt)*) => { if debug { eprintln!("[pthread {pthread_ptr:#x}] {}", format!($($arg)*)); } } }

	dbg_pt!("spawned, building instance");
	let (mut store, instance) = build_instance(engine, module, shared_mem, vfs_root, trace_stubs)?;

	instance
		.get_typed_func::<(i32, i32), ()>(&mut store, "emscripten_stack_set_limits")?
		.call(&mut store, (stack_high, stack_low))?;
	instance.get_typed_func::<i32, ()>(&mut store, "_emscripten_stack_restore")?.call(&mut store, stack_high)?;
	dbg_pt!("stack set up (high={stack_high:#x} low={stack_low:#x})");
	instance
		.get_typed_func::<(i32, i32, i32, i32, i32, i32), ()>(&mut store, "_emscripten_thread_init")?
		.call(&mut store, (pthread_ptr, 0, 0, 1, 0, 0))?;
	instance.get_typed_func::<(), i32>(&mut store, "_emscripten_tls_init")?.call(&mut store, ())?;
	dbg_pt!("thread_init + tls_init done, entering start_routine={start_routine:#x} arg={arg:#x}");

	let table = match instance.get_export(&mut store, "__indirect_function_table") {
		Some(Extern::Table(t)) => t,
		_ => bail!("module has no __indirect_function_table export"),
	};
	let entry_val = table
		.get(&mut store, start_routine as u64)
		.ok_or_else(|| anyhow::anyhow!("start_routine {start_routine} out of table bounds"))?;
	let Ref::Func(Some(entry_func)) = entry_val else {
		bail!("no function at table index {start_routine}");
	};
	let result = entry_func.typed::<i32, i32>(&store)?.call(&mut store, arg)?;
	dbg_pt!("start_routine returned {result:#x}, calling _emscripten_thread_exit");

	instance.get_typed_func::<i32, ()>(&mut store, "_emscripten_thread_exit")?.call(&mut store, result)?;
	dbg_pt!("_emscripten_thread_exit returned, thread finished");
	Ok(())
}

pub fn build_instance(
	engine: &Engine,
	module: &Module,
	shared_mem: &SharedMemory,
	vfs_root: &str,
	trace_stubs: bool,
) -> Result<(Store<HostState>, Instance)> {
	let vfs = Vfs::new(vfs_root.into());
	let mut store: Store<HostState> =
		Store::new(engine, HostState { mem: shared_mem.clone(), vfs, start: std::time::Instant::now() });
	let mut linker: Linker<HostState> = Linker::new(engine);
	linker.allow_shadowing(true);

	for imp in module.imports() {
		let modname = imp.module().to_string();
		let name = imp.name().to_string();
		match imp.ty() {
			ExternType::Func(fty) => {
				let trace_name = format!("{modname}.{name}");
				linker.func_new(&modname, &name, fty.clone(), move |_caller, params, results| {
					if trace_stubs {
						println!("[stub] {trace_name}({params:?})");
					}
					for (slot, ty) in results.iter_mut().zip(fty.results()) {
						*slot = Val::default_for_ty(&ty).expect("concrete result type always has a default");
					}
					Ok(())
				})?;
			}
			ExternType::Memory(_) => {
				linker.define(&store, &modname, &name, Extern::SharedMemory(shared_mem.clone()))?;
			}
			_ => {}
		}
	}

	linker.func_wrap("env", "EnsureDir", |mut caller: Caller<'_, HostState>, path_ptr: u32| {
		let state = caller.data_mut();
		let mem = unsafe { mem_bytes(&state.mem) };
		let path = read_cstr(mem, path_ptr);
		state.vfs.ensure_dir(&path).ok();
	})?;

	linker.func_wrap(
		"env",
		"__syscall_openat",
		|mut caller: Caller<'_, HostState>, _dirfd: i32, path_ptr: u32, flags: i32, mode: i32| -> i32 {
			let state = caller.data_mut();
			let mem = unsafe { mem_bytes(&state.mem) };
			let path = read_cstr(mem, path_ptr);
			state.vfs.openat(&path, flags, mode)
		},
	)?;

	linker.func_wrap(
		"env",
		"__syscall_fstat64",
		|mut caller: Caller<'_, HostState>, fd: i32, buf_ptr: u32| -> i32 {
			let state = caller.data_mut();
			match state.vfs.stat_fd(fd) {
				Some((size, is_dir)) => {
					let mem = unsafe { mem_bytes(&state.mem) };
					write_stat64(mem, buf_ptr, size, is_dir);
					0
				}
				None => -9,
			}
		},
	)?;

	for name in ["__syscall_stat64", "__syscall_lstat64"] {
		linker.func_wrap("env", name, |mut caller: Caller<'_, HostState>, path_ptr: u32, buf_ptr: u32| -> i32 {
			let state = caller.data_mut();
			let mem = unsafe { mem_bytes(&state.mem) };
			let path = read_cstr(mem, path_ptr);
			match state.vfs.stat_path(&path) {
				Some((size, is_dir)) => {
					write_stat64(mem, buf_ptr, size, is_dir);
					0
				}
				None => -2,
			}
		})?;
	}

	linker.func_wrap(
		"env",
		"__syscall_newfstatat",
		|mut caller: Caller<'_, HostState>, _dirfd: i32, path_ptr: u32, buf_ptr: u32, _flags: i32| -> i32 {
			let state = caller.data_mut();
			let mem = unsafe { mem_bytes(&state.mem) };
			let path = read_cstr(mem, path_ptr);
			match state.vfs.stat_path(&path) {
				Some((size, is_dir)) => {
					write_stat64(mem, buf_ptr, size, is_dir);
					0
				}
				None => -2,
			}
		},
	)?;

	linker.func_wrap("wasi_snapshot_preview1", "fd_close", |mut caller: Caller<'_, HostState>, fd: i32| -> i32 {
		caller.data_mut().vfs.close(fd)
	})?;

	linker.func_wrap(
		"wasi_snapshot_preview1",
		"fd_read",
		|mut caller: Caller<'_, HostState>, fd: i32, iovs_ptr: u32, iovs_len: u32, nread_ptr: u32| -> i32 {
			let state = caller.data_mut();
			let mem = unsafe { mem_bytes(&state.mem) };
			let mut total = 0i64;
			for i in 0..iovs_len {
				let base = iovs_ptr as usize + (i as usize) * 8;
				let buf_ptr = u32::from_le_bytes(mem[base..base + 4].try_into().unwrap());
				let buf_len = u32::from_le_bytes(mem[base + 4..base + 8].try_into().unwrap());
				if buf_len == 0 {
					continue;
				}
				let n = state.vfs.read(fd, &mut mem[buf_ptr as usize..(buf_ptr + buf_len) as usize]);
				if n < 0 {
					return -(n as i32);
				}
				total += n;
				if (n as u32) < buf_len {
					break;
				}
			}
			mem[nread_ptr as usize..nread_ptr as usize + 4].copy_from_slice(&(total as u32).to_le_bytes());
			0
		},
	)?;

	linker.func_wrap(
		"wasi_snapshot_preview1",
		"fd_write",
		|mut caller: Caller<'_, HostState>, fd: i32, iovs_ptr: u32, iovs_len: u32, nwritten_ptr: u32| -> i32 {
			let state = caller.data_mut();
			let mem = unsafe { mem_bytes(&state.mem) };
			let mut total = 0i64;
			for i in 0..iovs_len {
				let base = iovs_ptr as usize + (i as usize) * 8;
				let buf_ptr = u32::from_le_bytes(mem[base..base + 4].try_into().unwrap());
				let buf_len = u32::from_le_bytes(mem[base + 4..base + 8].try_into().unwrap());
				if buf_len == 0 {
					continue;
				}
				let n = state.vfs.write(fd, &mem[buf_ptr as usize..(buf_ptr + buf_len) as usize]);
				if n < 0 {
					return -(n as i32);
				}
				total += n;
			}
			mem[nwritten_ptr as usize..nwritten_ptr as usize + 4].copy_from_slice(&(total as u32).to_le_bytes());
			0
		},
	)?;

	linker.func_wrap(
		"wasi_snapshot_preview1",
		"fd_seek",
		|mut caller: Caller<'_, HostState>,
		 fd: i32,
		 offset_lo: i32,
		 offset_hi: i32,
		 whence: i32,
		 newoffset_ptr: u32|
		 -> i32 {
			let state = caller.data_mut();
			let offset = ((offset_hi as i64) << 32) | (offset_lo as u32 as i64);
			let newpos = state.vfs.seek(fd, offset, whence);
			if newpos < 0 {
				return -(newpos as i32);
			}
			let mem = unsafe { mem_bytes(&state.mem) };
			mem[newoffset_ptr as usize..newoffset_ptr as usize + 8].copy_from_slice(&newpos.to_le_bytes());
			0
		},
	)?;

	linker.func_wrap(
		"wasi_snapshot_preview1",
		"environ_sizes_get",
		|mut caller: Caller<'_, HostState>, count_ptr: u32, bufsize_ptr: u32| -> i32 {
			let mem = unsafe { mem_bytes(&caller.data_mut().mem) };
			mem[count_ptr as usize..count_ptr as usize + 4].copy_from_slice(&0u32.to_le_bytes());
			mem[bufsize_ptr as usize..bufsize_ptr as usize + 4].copy_from_slice(&0u32.to_le_bytes());
			0
		},
	)?;
	linker.func_wrap(
		"wasi_snapshot_preview1",
		"environ_get",
		|_caller: Caller<'_, HostState>, _e: u32, _b: u32| -> i32 { 0 },
	)?;

	linker.func_wrap(
		"wasi_snapshot_preview1",
		"random_get",
		|mut caller: Caller<'_, HostState>, buf_ptr: u32, buf_len: u32| -> i32 {
			let mem = unsafe { mem_bytes(&caller.data_mut().mem) };
			for b in &mut mem[buf_ptr as usize..(buf_ptr + buf_len) as usize] {
				*b = (std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().subsec_nanos() & 0xff)
					as u8;
			}
			0
		},
	)?;

	linker.func_wrap(
		"wasi_snapshot_preview1",
		"clock_time_get",
		|mut caller: Caller<'_, HostState>, _clock_id: i32, _prec_lo: i32, _prec_hi: i32, out_ptr: u32| -> i32 {
			let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap();
			let nanos = now.as_nanos() as i64;
			let mem = unsafe { mem_bytes(&caller.data_mut().mem) };
			mem[out_ptr as usize..out_ptr as usize + 8].copy_from_slice(&nanos.to_le_bytes());
			0
		},
	)?;

	linker.func_wrap("env", "emscripten_get_now", |caller: Caller<'_, HostState>| -> f64 {
		caller.data().start.elapsed().as_secs_f64() * 1000.0
	})?;

	linker.func_wrap(
		"env",
		"_mmap_js",
		|mut caller: Caller<'_, HostState>,
		 len: i32,
		 _prot: i32,
		 _flags: i32,
		 fd: i32,
		 offset_lo: i32,
		 offset_hi: i32,
		 allocated_ptr: u32,
		 addr_ptr: u32|
		 -> i32 {
			let offset = ((offset_hi as i64) << 32) | (offset_lo as u32 as i64);
			let malloc_func = match caller.get_export("malloc") {
				Some(Extern::Func(f)) => f,
				_ => return -14,
			};
			let malloc_typed = match malloc_func.typed::<i32, i32>(&caller) {
				Ok(f) => f,
				Err(_) => return -14,
			};
			let ptr = match malloc_typed.call(&mut caller, len) {
				Ok(p) if p != 0 => p,
				_ => return -12,
			};

			let state = caller.data_mut();
			state.vfs.seek(fd, offset, 0);
			let mut file_buf = vec![0u8; len as usize];
			let n = state.vfs.read(fd, &mut file_buf).max(0) as usize;

			let mem = unsafe { mem_bytes(&state.mem) };
			mem[ptr as usize..ptr as usize + n].copy_from_slice(&file_buf[..n]);
			for b in &mut mem[ptr as usize + n..ptr as usize + len as usize] {
				*b = 0;
			}
			mem[allocated_ptr as usize..allocated_ptr as usize + 4].copy_from_slice(&1i32.to_le_bytes());
			mem[addr_ptr as usize..addr_ptr as usize + 4].copy_from_slice(&(ptr as u32).to_le_bytes());
			0
		},
	)?;

	linker.func_wrap("wasi_snapshot_preview1", "proc_exit", |code: i32| -> () {
		std::process::exit(code);
	})?;

	{
		let engine2 = engine.clone();
		let module2 = module.clone();
		let shared_mem2 = shared_mem.clone();
		let vfs_root2 = vfs_root.to_string();
		linker.func_wrap(
            "env",
            "__pthread_create_js",
            move |mut caller: Caller<'_, HostState>, pthread_ptr: i32, _attr: i32, start_routine: i32, arg: i32| -> i32 {
                let state = caller.data_mut();
                let mem = unsafe { mem_bytes(&state.mem) };
                let base = pthread_ptr as usize;
                let stack_high = u32::from_le_bytes(mem[base + 48..base + 52].try_into().unwrap()) as i32;
                let stack_size = u32::from_le_bytes(mem[base + 52..base + 56].try_into().unwrap()) as i32;
                let stack_low = stack_high - stack_size;

                if std::env::var_os("GTTS_TRACE_PTHREAD").is_some() {
                    eprintln!("[pthread_create_js] pthread_ptr={pthread_ptr:#x} start_routine={start_routine:#x} arg={arg:#x} stack_high={stack_high:#x} stack_low={stack_low:#x}");
                }
                let engine3 = engine2.clone();
                let module3 = module2.clone();
                let shared_mem3 = shared_mem2.clone();
                let vfs_root3 = vfs_root2.clone();
                let spawned = std::thread::Builder::new().name(format!("gtts-pthread-{pthread_ptr:#x}")).spawn(move || {
                    if let Err(e) = run_pthread(
                        &engine3, &module3, &shared_mem3, &vfs_root3, trace_stubs, pthread_ptr, stack_high, stack_low,
                        start_routine, arg,
                    ) {
                        eprintln!("[pthread {pthread_ptr:#x}] failed: {e:#}");
                    }
                });
                match spawned {
                    Ok(_) => 0,
                    Err(e) => {
                        eprintln!("[pthread_create_js] OS thread spawn failed: {e}");
                        11
                    }
                }
            },
        )?;
	}

	linker.func_wrap("env", "emscripten_num_logical_cores", || -> i32 {
		std::thread::available_parallelism().map(|n| n.get() as i32).unwrap_or(1)
	})?;
	linker.func_wrap("env", "hardware_concurrency", || -> i32 {
		std::thread::available_parallelism().map(|n| n.get() as i32).unwrap_or(1)
	})?;

	linker.func_wrap("env", "_emscripten_init_main_thread_js", |mut caller: Caller<'_, HostState>, tb: i32| {
		let debug = std::env::var_os("GTTS_TRACE_PTHREAD").is_some();
		let can_block = i32::from(std::env::var_os("GTTS_MAIN_CAN_BLOCK").is_some());
		if debug {
			eprintln!("[main_thread_init] tb={tb:#x} can_block={can_block}");
		}
		let Some(Extern::Func(thread_init)) = caller.get_export("_emscripten_thread_init") else {
			eprintln!("[main_thread_init] module has no _emscripten_thread_init export");
			return;
		};
		match thread_init.typed::<(i32, i32, i32, i32, i32, i32), ()>(&caller) {
			Ok(f) => {
				if let Err(e) = f.call(&mut caller, (tb, 1, 1, can_block, 65536, 0)) {
					eprintln!("[main_thread_init] _emscripten_thread_init failed: {e:#}");
					return;
				}
			}
			Err(e) => {
				eprintln!("[main_thread_init] _emscripten_thread_init has unexpected type: {e:#}");
				return;
			}
		}
		if let Some(Extern::Func(tls_init)) = caller.get_export("_emscripten_tls_init") {
			match tls_init.typed::<(), i32>(&caller) {
				Ok(f) => match f.call(&mut caller, ()) {
					Ok(base) if debug => eprintln!("[main_thread_init] tls_init -> {base:#x}"),
					Ok(_) => {}
					Err(e) => eprintln!("[main_thread_init] _emscripten_tls_init failed: {e:#}"),
				},
				Err(e) => eprintln!("[main_thread_init] _emscripten_tls_init has unexpected type: {e:#}"),
			}
		}
	})?;

	linker.func_wrap(
		"env",
		"_emscripten_notify_mailbox_postmessage",
		|mut caller: Caller<'_, HostState>, target_thread: i32, curr_thread: i32| {
			let debug = std::env::var_os("GTTS_TRACE_PTHREAD").is_some();
			if target_thread == curr_thread {
				if debug {
					eprintln!("[mailbox] same-thread notify (thread={curr_thread:#x}), draining now");
				}
				if let Some(Extern::Func(f)) = caller.get_export("_emscripten_check_mailbox") {
					if let Ok(typed) = f.typed::<(), ()>(&caller) {
						if let Err(e) = typed.call(&mut caller, ()) {
							eprintln!("[mailbox] _emscripten_check_mailbox failed: {e:#}");
						}
					}
				}
			} else if debug {
				eprintln!(
					"[mailbox] cross-thread notify target={target_thread:#x} curr={curr_thread:#x} — NOT handled, queue will not drain"
				);
			}
		},
	)?;

	let instance = linker.instantiate(&mut store, module)?;
	Ok((store, instance))
}

pub struct TtsEngine {
	store: Store<HostState>,
	malloc: TypedFunc<i32, i32>,
	install_voice_fn: TypedFunc<(i32, i32, i32), i32>,
	init_fn: TypedFunc<(i32, i32), i32>,
	init_buffered_fn: TypedFunc<(i32, i32, i32, i32), i32>,
	read_buffered_fn: TypedFunc<(), i32>,
	finalize_buffered_fn: TypedFunc<(), ()>,
	get_event_ptr_fn: TypedFunc<(), i32>,
	get_event_len_fn: TypedFunc<(), i32>,
}

impl TtsEngine {
	pub fn new(wasm_path: &str, vfs_root: &str) -> Result<Self> {
		let trace_stubs = std::env::var_os("GTTS_TRACE_STUBS").is_some();

		let mut config = Config::new();
		config.wasm_threads(true);
		config.shared_memory(true);
		let engine = Engine::new(&config)?;

		let bytes = std::fs::read(wasm_path)?;
		let module = modcache::load(&engine, std::path::Path::new(wasm_path), &bytes)?;

		let mem_ty = module
			.imports()
			.find_map(|imp| if let ExternType::Memory(m) = imp.ty() { Some(m) } else { None })
			.expect("module should import a memory");
		let shared_mem = SharedMemory::new(&engine, mem_ty)?;

		let (mut store, instance) = build_instance(&engine, &module, &shared_mem, vfs_root, trace_stubs)?;

		if let Ok(ctors) = instance.get_typed_func::<(), ()>(&mut store, "__wasm_call_ctors") {
			ctors.call(&mut store, ())?;
		}

		let malloc = instance.get_typed_func::<i32, i32>(&mut store, "malloc")?;
		let install_voice_fn = instance.get_typed_func::<(i32, i32, i32), i32>(&mut store, "GoogleTtsInstallVoice")?;
		let init_fn = instance.get_typed_func::<(i32, i32), i32>(&mut store, "GoogleTtsInit")?;
		let init_buffered_fn =
			instance.get_typed_func::<(i32, i32, i32, i32), i32>(&mut store, "GoogleTtsInitBuffered")?;
		let read_buffered_fn = instance.get_typed_func::<(), i32>(&mut store, "GoogleTtsReadBuffered")?;
		let finalize_buffered_fn = instance.get_typed_func::<(), ()>(&mut store, "GoogleTtsFinalizeBuffered")?;
		let get_event_ptr_fn = instance.get_typed_func::<(), i32>(&mut store, "GoogleTtsGetEventBufferPtr")?;
		let get_event_len_fn = instance.get_typed_func::<(), i32>(&mut store, "GoogleTtsGetEventBufferLen")?;

		Ok(TtsEngine {
			store,
			malloc,
			install_voice_fn,
			init_fn,
			init_buffered_fn,
			read_buffered_fn,
			finalize_buffered_fn,
			get_event_ptr_fn,
			get_event_len_fn,
		})
	}

	fn malloc_copy(&mut self, bytes: &[u8]) -> Result<i32> {
		let ptr = self.malloc.call(&mut self.store, bytes.len() as i32)?;
		let mem = unsafe { mem_bytes(&self.store.data().mem) };
		mem[ptr as usize..ptr as usize + bytes.len()].copy_from_slice(bytes);
		Ok(ptr)
	}

	pub fn install_voice(&mut self, voice_dir: &str, zvoice_bytes: &[u8]) -> Result<()> {
		let dir_ptr = self.malloc_copy(format!("{voice_dir}\0").as_bytes())?;
		let data_ptr = self.malloc_copy(zvoice_bytes)?;
		let result = self.install_voice_fn.call(&mut self.store, (dir_ptr, data_ptr, zvoice_bytes.len() as i32))?;
		if result != 1 {
			bail!("GoogleTtsInstallVoice failed (returned {result})");
		}
		Ok(())
	}

	pub fn init(&mut self, voice_dir: &str) -> Result<()> {
		let pipeline_ptr = self.malloc_copy(format!("{voice_dir}/pipeline.pb\0").as_bytes())?;
		let voicedir_ptr = self.malloc_copy(format!("{voice_dir}\0").as_bytes())?;
		let result = self.init_fn.call(&mut self.store, (pipeline_ptr, voicedir_ptr))?;
		if result != 1 {
			bail!("GoogleTtsInit failed (returned {result})");
		}
		Ok(())
	}

	pub fn synthesize(&mut self, text: &str, rate: f32, pitch: f32, speaker_code: &str) -> Result<Vec<u8>> {
		let req_bytes = proto::build_request(text, rate, pitch);
		let cfg_bytes = proto::build_config(speaker_code);
		let req_ptr = self.malloc_copy(&req_bytes)?;
		let cfg_ptr = self.malloc_copy(&cfg_bytes)?;

		let result = self
			.init_buffered_fn
			.call(&mut self.store, (req_ptr, cfg_ptr, req_bytes.len() as i32, cfg_bytes.len() as i32))?;
		if result == 0 {
			bail!("GoogleTtsInitBuffered failed (returned 0)");
		}

		let mut event_stream = Vec::new();
		const MAX_READ_ITERATIONS: usize = 100_000;
		for _ in 0..MAX_READ_ITERATIONS {
			let status = self.read_buffered_fn.call(&mut self.store, ())?;
			let ptr = self.get_event_ptr_fn.call(&mut self.store, ())?;
			let len = self.get_event_len_fn.call(&mut self.store, ())?;
			if len > 0 {
				let mem = unsafe { mem_bytes(&self.store.data().mem) };
				event_stream.extend_from_slice(&mem[ptr as usize..(ptr + len) as usize]);
			}
			if status == 0 {
				break;
			}
			if status == -1 {
				bail!("GoogleTtsReadBuffered returned an error");
			}
		}
		self.finalize_buffered_fn.call(&mut self.store, ())?;

		let mut pcm = Vec::new();
		find_audio_chunks(&event_stream, &mut pcm);
		Ok(pcm)
	}

	pub fn synthesize_streaming(
		&mut self,
		text: &str,
		rate: f32,
		pitch: f32,
		speaker_code: &str,
		mut on_chunk: impl FnMut(&[u8]),
		mut should_cancel: impl FnMut() -> bool,
	) -> Result<SynthesisOutcome> {
		let req_bytes = proto::build_request(text, rate, pitch);
		let cfg_bytes = proto::build_config(speaker_code);
		let req_ptr = self.malloc_copy(&req_bytes)?;
		let cfg_ptr = self.malloc_copy(&cfg_bytes)?;

		let result = self
			.init_buffered_fn
			.call(&mut self.store, (req_ptr, cfg_ptr, req_bytes.len() as i32, cfg_bytes.len() as i32))?;
		if result == 0 {
			bail!("GoogleTtsInitBuffered failed (returned 0)");
		}

		const MAX_READ_ITERATIONS: usize = 100_000;
		let mut cancelled = false;
		for _ in 0..MAX_READ_ITERATIONS {
			if should_cancel() {
				cancelled = true;
				break;
			}
			let status = self.read_buffered_fn.call(&mut self.store, ())?;
			let ptr = self.get_event_ptr_fn.call(&mut self.store, ())?;
			let len = self.get_event_len_fn.call(&mut self.store, ())?;
			if len > 0 {
				let mem = unsafe { mem_bytes(&self.store.data().mem) };
				let event_bytes = &mem[ptr as usize..(ptr + len) as usize];
				walk_audio_chunks(event_bytes, &mut on_chunk);
			}
			if status == 0 {
				break;
			}
			if status == -1 {
				bail!("GoogleTtsReadBuffered returned an error");
			}
		}
		self.finalize_buffered_fn.call(&mut self.store, ())?;

		Ok(if cancelled { SynthesisOutcome::Cancelled } else { SynthesisOutcome::Completed })
	}
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SynthesisOutcome {
	Completed,
	Cancelled,
}
