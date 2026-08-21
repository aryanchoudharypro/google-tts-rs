# google-tts-native — status / handoff

## Goal

Replace the Chrome/CDP bridge in the third-party NVDA addon
`Google-TTS-For-NVDA` (the user's own local git clone at
`D:\dev\Google-TTS-For-NVDA` — editing it directly is authorized, see part 3;
the "read-only reference" note here was self-imposed early caution) with a
lightweight native Rust host that
runs the same Emscripten-compiled Google WASM TTS engine directly, with no
Chrome and no Node.js. Motivation: launching a full headless Chrome instance
per synthesis session is slow and heavy, especially on the low-spec machines
NVDA users tend to run.

User's stated constraints: no Node.js (RAM cost on low-spec machines is the
explicit objection), Rust or Go preferred, incremental "take it slow but
checked" pace — verify each step against ground truth before building on it.

## 2026-08-21 update, part 6: **Natural / seanet voices work.** Root cause was the stubbed `_emscripten_init_main_thread_js`

**Status: solved and shipped.** All six Hindi Natural speakers and the
English Natural voice now install, initialize and synthesize end to end
through the real `bridge.py`, at roughly 12x realtime. Natural voices are
un-hidden in the addon again and packaged as
`dist/googleTtsForNvda-0.2.1.nvda-addon`.

### Method: stopped reading disassembly, read Emscripten's own source instead

Part 5 hit the limit of static disassembly. The unlock was that the emsdk
installed at the end of part 5 ships the full C source for Emscripten's
threading runtime (`emsdk/upstream/emscripten/system/lib/pthread/*.c`,
`system/lib/libc/musl/src/internal/pthread_impl.h`, `src/lib/libpthread.js`,
`src/struct_info_generated.json`). Every field offset and every host-import
contract part 5 had to infer from compiled code is written down there in
readable form. Cross-checked against the real module before trusting it: the
`pthread` struct layout in `struct_info_generated.json` (`stack`=48,
`stack_size`=52, `profilerBlock`=96, `tls_base`=100, `mailbox`=104,
`mailbox_refcount`=108, `waiting_async`=112, `wait_addr`=116, `__size__`=120)
matches every offset part 5 had reverse-engineered by hand, so the installed
emsdk's runtime is layout-compatible with whatever built `bindings_main.wasm`.

That immediately identified the mystery field: `pthread_ptr+108` is
`mailbox_refcount`, and the "infinite wait" inside `_emscripten_thread_exit`
is `_emscripten_thread_mailbox_shutdown()` — a correct, deliberate wait for
every in-flight cross-thread sender to finish. Not a bug in it; something
else was deadlocked while holding a mailbox reference.

### Root cause

`env._emscripten_init_main_thread_js` is a real import of this module and was
still a generic no-op stub. It is called from a **static constructor**
(`_emscripten_init_main_thread`, `__attribute__((constructor(48)))`, so it
runs inside our `__wasm_call_ctors` call) and it is the **only** place the
main thread's four per-instance thread-state wasm globals are ever set —
`thread_ptr`, `is_main_thread`, `is_runtime_thread`, `supports_wait` (see
`system/lib/pthread/emscripten_thread_state.S`).

Leaving it stubbed meant that on the main thread:

- `pthread_self()` / `__get_tp()` returned **NULL**,
- `emscripten_is_main_runtime_thread()` returned **false**, and
- main-thread TLS was never initialized (`PThread.threadInitTLS()`, i.e.
  `_emscripten_tls_init()`, never ran).

The fatal one is the second. Every blocking wait in Emscripten funnels
through `emscripten_futex_wait` -> `_emscripten_yield()`, and
`_emscripten_yield` only calls `emscripten_main_thread_process_queued_calls()`
— which is what actually executes work other threads proxied to the main
thread — `if (emscripten_is_main_runtime_thread())`. With that global stuck at
0, the main thread blocked forever without ever draining its proxy queue, so
the pipeline's worker thread waited on a task the main thread would never run,
while holding the mailbox reference that `_emscripten_thread_exit` was waiting
to see released. Classic deadlock, and it needed a *third* piece of state (the
un-set global) to explain why neither of the two visible waits ever progressed.

Base voices never tripped this because their pipeline is single-threaded and
never proxies anything.

### The fix (`engine/src/lib.rs`)

A real implementation of `_emscripten_init_main_thread_js(tb)` that
reentrantly calls the module's own exports, mirroring `bindings_main.js`'s
`__emscripten_init_main_thread_js` exactly:

```
_emscripten_thread_init(tb, is_main=1, is_runtime=1, can_block, 65536, 0)
_emscripten_tls_init()
```

`can_block` is the subtle argument. In Chrome the addon runs in an *offscreen
document*, where `Atomics.wait` throws, so the glue resolves `can_block` to
**false** — and that is what routes the main thread's futex waits into musl's
`futex_wait_main_browser_thread` busy-spin, the path that pumps proxied calls
on every iteration. We deliberately reproduce Chrome's answer rather than the
"we're a native host, our main thread *can* block" one, because a genuinely
blocking main thread cannot service cross-thread work and re-deadlocks.
`GTTS_MAIN_CAN_BLOCK=1` flips it back for A/B testing. (This is also why the
whole thing works without a JS event loop: the busy-spin *is* the event loop.)

`_emscripten_thread_mailbox_await` is intentionally left as a no-op, and that
is a supported configuration rather than a remaining gap — the JS reference
only does anything when `Atomics.waitAsync` exists, and its sole effect is to
set `pthread->waiting_async = 1` so senders switch from the
`_emscripten_notify_mailbox_postmessage` import (which we do implement) to a
bare `memory.atomic.notify` whose wakeup would be delivered on an event loop
we don't have. Staying on the postMessage path is the `waitAsyncPolyfilled`
branch of that same reference code.

Part 5's `_emscripten_notify_mailbox_postmessage` implementation and the whole
`__pthread_create_js`/`run_pthread` machinery were correct and are unchanged;
they were necessary but not sufficient.

### Verified

- `cargo test --release`: 7/7 pass.
- Base voice (`cargo run --release --bin gtts_engine`): unchanged, still 3.13s
  of audio in `out.wav`, proto encoder still byte-identical to the Chrome
  capture.
- `cargo run --release --example seanet_voice` (new, permanent): installs the
  base dependency, installs the Natural package, `GoogleTtsInit` in **1.53s**,
  then synthesizes all six Hindi Natural speakers
  (`cfn`/`hia`/`hib`/`hic`/`hid`/`hie`) — ~2.2s of audio each in ~0.17s wall,
  written to `out_seanet_<speaker>.wav`. TFLite's XNNPACK delegate now applies
  successfully for the seanet model (301/301 nodes).
- `examples/seanet_repro.rs` (the old hang repro) now runs to completion
  instead of hanging: `>>> init: OK`, `outcome: Ok(Completed)`.
- `pyclient/test_native_bridge.py` (drives the *real*, unmodified
  `bridge.py`): extended with a Natural-voice case plus a switch back to a
  base voice afterwards. All checks pass, including the pre-existing
  mid-utterance cancel test.
- English Natural (`en-in-x-multi-seanet:cxx`) checked separately through the
  same bridge — works.

### Addon changes shipped alongside (all uncommitted, git is the safety net)

- `bridge.py`: `NativeTtsBridge._ensure_voice_ready` now installs a Natural
  package's `dependentVoiceId` base package into the engine VFS first
  (install-only, never `GoogleTtsInit`'d), via new helpers
  `_dependency_package` / `_install_package` / `_raise_unless_installed`. This
  was next-steps item 9 from part 3 and is what makes the dependency mechanism
  work outside the hand-written repro.
- `synthDrivers/googleTtsForNvda/__init__.py`: part 4's stopgap (hide every
  `dependentVoiceId` package) is **removed**. Natural packages are now hidden
  only when their base package isn't installed, since that is the one case
  where they genuinely cannot initialize.
- `globalPlugins/googleTtsForNvda/voiceManager.py`: downloading a Natural
  package now auto-includes its base package (`_with_dependencies`), so
  picking a Natural voice in the Voice Manager yields a working voice rather
  than a hidden one.
- `manifest.ini` bumped to 0.2.1; `native/server.exe` refreshed and repackaged
  as `dist/googleTtsForNvda-0.2.1.nvda-addon` (11.7MB).

### Known remaining rough edges

- Removing a base package from the Voice Manager while a Natural package that
  depends on it stays installed will silently drop those Natural voices from
  the synth's voice list. It degrades gracefully (no error, no hang) but a
  warning at removal time would be friendlier.
- The main thread busy-spins while waiting on the worker thread, because that
  is what `can_block=false` means. It matches Chrome's behavior and only
  happens while a synthesis is actually in flight, but on a low-spec machine
  it is a second core briefly at 100%. If that ever matters, the place to look
  is a host-side hook that yields inside the spin, not flipping `can_block`
  back to 1.
- Nothing in the addon has been committed. `git -C D:\dev\Google-TTS-For-NVDA
  status` still shows the whole native-bridge migration as working-tree
  changes.

## 2026-08-20 update, part 5: real pthread support — `__pthread_create_js` now works; still hanging (the "Asyncify" theory in this heading is WRONG, see this part's own follow-ups and part 6)

Picked up item 6 (real pthread support) in a fresh session. Reverse-engineered
the exact Emscripten pthread-worker bootstrap protocol by reading the
addon's own shipped `bindings_main.js` (the JS glue that would normally run
this — we don't execute it, but it's the ground truth for what each host
import/export is supposed to do), rather than guessing. Key findings, all
confirmed against the real wasm module via a throwaway `wasmtime`
introspection script (since deleted):

- The module's memory is **fixed-size** (8192/8192 pages = exactly 512MB,
  no growth) and its `__indirect_function_table` is an **export** (not an
  import), also fixed-size with no growth-related import. This matters:
  it means each pthread's own `Instance` can safely get its own
  independently-instantiated table (populated identically from the same
  module's static elem segments) — no need to explicitly share the table
  across threads, only the `SharedMemory`.
- `__pthread_create_js(pthread_ptr, attr, start_routine, arg) -> i32`: per
  `bindings_main.js`'s `spawnThread`, `attr` is never actually used (not
  even forwarded to the worker) — everything needed is either in the
  pthread struct already (written by musl's `pthread_create` before this
  import is called) or in the other three params. The struct has
  `stackHigh` at `+48` and `stackSize` at `+52` (confirmed against
  `establishStackSpace()`); `stackLow = stackHigh - stackSize`.
- The real worker bootstrap sequence (`bindings_main.js`'s `cmd==2` handler,
  replayed exactly): `emscripten_stack_set_limits(stackHigh, stackLow)` →
  `_emscripten_stack_restore(stackHigh)` → `_emscripten_thread_init(pthread_ptr,
  is_main=0, is_runtime=0, can_block=1, 0, 0)` → `_emscripten_tls_init()`
  (this is literally what `PThread.threadInitTLS()` reduces to for this
  module — confirmed only one function is ever registered via
  `registerTLSInit`) → call the wasm table entry at `start_routine` with
  `arg` (signature `i32 -> i32`, per Emscripten's own "thread entry points
  are always `void *ThreadMain(void *arg)`" comment) → `_emscripten_thread_exit(result)`.
  `__wasm_call_ctors` must **not** be re-run per thread — global
  constructors run exactly once, only for the first/main instantiation,
  since the static state they initialize lives in the one shared linear
  memory every instance sees.
- `emscripten_check_blocking_allowed` turned out to be a red herring: its
  wasm signature is `() -> ()` (no return value at all), and its real JS
  impl (`_emscripten_check_blocking_allowed` in the glue) only ever prints a
  console warning — it's advisory, not a gate. It does **not** decide
  between a real blocking wait and Asyncify-based cooperative waiting, so
  no override was needed there.

### Implemented: `engine/src/lib.rs` restructured around a reusable `build_instance`

The old `TtsEngine::new` did all linker/instantiate setup inline. Extracted
that into a free function `build_instance(engine, module, shared_mem,
vfs_root, trace_stubs) -> Result<(Store<HostState>, Instance)>`, used both
for the main instantiation and, recursively, for every new pthread's own
instantiation (so nested/multiple thread creation works automatically,
should the pipeline ever need more than one thread — unconfirmed either
way). Added a new free function `run_pthread(...)` that calls
`build_instance` then replays the bootstrap sequence above on a **real OS
thread** (`std::thread::Builder::spawn`), fire-and-forget — `__pthread_create_js`
returns `0` immediately after a successful spawn, exactly mirroring
`spawnThread`'s own postMessage-and-return-immediately shape; the actual
completion signal is whatever `_emscripten_thread_exit`'s *compiled* body
does at the wasm level (presumably a real `memory.atomic.notify` — wasmtime
executes wasm's native shared-memory atomics correctly with no host
involvement needed, since `wasm_threads`/`shared_memory` were already
enabled in `Config`). Also gave `emscripten_num_logical_cores`/
`hardware_concurrency` real implementations (`std::thread::available_parallelism`)
instead of the default-stubbed `0`, in case anything sizes a thread pool off
that.

**Verified this part works**: rebuilt `dump_thread_imports`-style
introspection confirmed the signatures used; `cargo test --release` (all 7
unit tests), `cargo run --release` (base `en-in-x-multi` voice), and
`cargo run --release --example hi_voice` (`hi-in-x-multi`) all still pass
byte-for-byte identical to before — **zero regression** on the working
path. Running `examples/seanet_repro.rs` (the Natural-voice repro) no longer
fails/traps early: it gets past `install_voice` and into `GoogleTtsInit`,
where — for the first time — Windows Task Manager–equivalent process
inspection (`Get-Process` CPU/thread count) showed **real multi-threaded
execution**: 8 OS threads, CPU time exceeding wall-clock time (i.e.
genuinely >1 core busy concurrently), which is only possible if
`__pthread_create_js` actually spawned and ran a second thread doing real
work — categorically different from the old behavior (a single thread
spinning forever on `emscripten_get_now`, fixed back in part 4).

### Not yet resolved: `GoogleTtsInit` still doesn't return for the Natural voice

Let it run for ~9 minutes (with `taskkill` after) — it never printed
`>>> init: OK` (or any error) from `seanet_repro.rs`. A `GTTS_TRACE_STUBS=1`
run showed the *calling* thread's wait-for-the-new-thread's-work is going
through a different, still-unimplemented path: repeated
`env.emscripten_stack_snapshot` / `env.emscripten_stack_unwind_buffer` /
`env.emscripten_check_blocking_allowed` calls — Emscripten's **Asyncify**
cooperative-suspend mechanism (save/restore the C call stack to yield back
to a JS-style event loop, normally resumed later via
`Atomics.waitAsync`/`checkMailbox`/`postMessage`). These three imports are
still generic no-op stubs (return default `0`s, don't actually save/restore
anything), so whatever loop this is never gets the "wake up, re-check, and
this time proceed" signal it's waiting for from a real JS event loop.

**This was not a naive busy-spin** (a 25s trace showed ~660 snapshot/unwind
pairs — about 26/sec, using dozens of distinct buffer addresses in no
obvious short cycle — not a millions/sec tight loop repeating 2-3 fixed
addresses), so it's genuinely ambiguous whether this is (a) a real infinite
loop that will *never* terminate without actual Asyncify support, cycling
through a bounded pool of scratch-buffer addresses forever, or (b)
legitimately very slow unaccelerated computation — the TFLite delegate
explicitly failed to attach for this model ("Attempting to use a delegate
that only supports static-sized tensors with a graph that has dynamic-sized
tensors... Ignoring the error, and continuing anyway"), so it's running on
slow reference CPU kernels, and Natural/seanet-style neural vocoders are
known to be far more compute-heavy than the base engine's parametric voices.
I did not find a way to distinguish these two possibilities without
disassembling the wasm around the calling code (no `.proto`/C++ source
exists for this either, same as everything else reverse-engineered so far).

**Decision: paused here, reported to the user rather than guessing further.**
`__pthread_create_js` is real, tested, and strictly better than before (base
voices unaffected, Natural voices no longer fail/trap instantly) — this is
being kept. But Natural voices are **still** not safe to re-enable in the
addon: an unbounded multi-minute "hang" with no progress indicator is worse
UX for an NVDA user than the current clean "voice not listed" stopgap from
part 4, which stays in place. The `dependentVoiceId` wiring into
`NativeTtsBridge._ensure_voice_ready` (next-steps item 9) still hasn't been
done, and shouldn't be until `GoogleTtsInit` can be shown to actually
terminate in bounded time for a Natural voice.

### Follow-up same session: disassembled the wasm — this is NOT Asyncify, it's a stuck task-queue refcount

User explicitly chose to disassemble rather than keep guessing. Installed
`wasm-tools` (`cargo install wasm-tools`, none of `wasm2wat`/`wasm-dis`/etc.
were already present) and ran `wasm-tools print bindings_main.wasm` to get
a full WAT disassembly (~248MB text, kept only in the session scratchpad,
not the repo — regenerate the same way if needed again). Findings, all from
reading real compiled code, no more guessing:

- `emscripten_check_blocking_allowed` is called from exactly two places:
  `__pthread_join` and `__pthread_cond_timedwait` — real, unmodified musl
  functions, not part of an Asyncify-suspend mechanism at all. The
  `emscripten_stack_snapshot`/`unwind_buffer` calls I'd assumed were part of
  the *same* loop turned out to belong to a **completely different, unrelated
  function**: `absl::GetStackTrace(void**, int, int)`, called (via
  `absl::PostSynchEvent`/`absl::DebugOnlyDeadlockCheck`) from Abseil's Mutex
  diagnostic/deadlock-detection logging path. My earlier "Asyncify
  cooperative-suspend loop" theory (part 5, first half) was simply wrong —
  these are two unrelated things interleaving in the trace output because
  both fire from real activity happening concurrently on different threads.
- Added `GTTS_TRACE_PTHREAD=1` (permanent, mirrors `GTTS_TRACE_STUBS`) —
  `eprintln!` checkpoints through `__pthread_create_js`/`run_pthread`'s
  bootstrap steps. **stderr isn't buffered the way stdout is, so this
  actually shows live progress**, unlike the stdout trace which only
  flushes in ~64KB-ish chunks. This revealed the real, precise state: the
  spawned pthread's `start_routine` **does return** (`returned 0x0`) — the
  actual TFLite/pipeline computation completes — but the following
  `_emscripten_thread_exit` call **never returns**. So `__pthread_create_js`
  and the real computation it enables are not the problem; something inside
  Emscripten's own thread-teardown path is.
- Disassembled `$_emscripten_thread_exit` directly: near the top it does
  `thread_ptr.atomic.rmw.sub(offset=108, 1)`, and if the result isn't `0`,
  loops calling `$_do_futex_wait(thread_ptr+108, that_value, f64::INF)` —
  **an infinite-timeout blocking wait** — until that field reads `0`. This
  is a real futex wait (backed by wasm's native shared-memory atomics,
  which wasmtime executes correctly), not a stub — it's genuinely,
  correctly blocked, waiting for something to decrement a refcount at
  `pthread_ptr+108` down to zero.
- Found the field's purpose by finding the only other place that touches
  it: `$do_proxy` (Emscripten's real cross-thread task-proxying
  implementation — the `+108` field is a live-senders refcount on a task
  queue: `do_proxy` CAS-increments it before enqueueing a task onto a
  target thread's queue via `em_task_queue_enqueue`, then unconditionally
  decrements it (+ `emscripten_futex_wake`s any waiter) right after,
  win or lose. If the state transition needs it, it also calls the
  still-unimplemented `_emscripten_notify_mailbox_postmessage(target,
  curr)` import to ask the target thread to drain its queue.
- **Implemented `_emscripten_notify_mailbox_postmessage` for real** (was a
  no-op stub): for the same-thread case (`target == curr`, which is what
  the JS reference does via `setTimeout(checkMailbox)` — "run it on this
  same thread's own next tick") it now reentrantly calls the module's own
  **exported** `_emscripten_check_mailbox` function right there in the host
  callback — a normal, safe wasmtime pattern (a host import callback
  calling back into the *same* store it was invoked from is just a nested
  call frame, not a cross-thread hazard). Deliberately did **not**
  hand-parse `em_task_queue_enqueue`'s task-entry layout to write a
  from-scratch host-side queue walker — the module already exports a
  function that correctly drains its own queue, so calling that is safer
  and much less risky than reimplementing the data structure ourselves.
  The cross-thread case (`target != curr`, needing a *different* running
  thread's Store to be reached) is explicitly not implemented; there's a
  `GTTS_TRACE_PTHREAD`-gated log line if that path is ever actually hit.
- **This fix did not resolve the hang** — reran with both
  `GTTS_TRACE_PTHREAD=1` and `GTTS_TRACE_STUBS=1`: `[mailbox]` never printed
  at all, meaning `do_proxy`'s notify branch is never reached in this
  scenario. The `_emscripten_thread_exit` wait still never returns. Since
  `do_proxy`'s own increment/decrement of `+108` are unconditionally paired
  within one call (short-circuiting to a no-op if the target's queue is
  already "dead"), a `do_proxy` call by itself can't be the reason `+108`
  stays elevated forever *unless* some `do_proxy` invocation is itself stuck
  mid-flight (e.g. blocked acquiring one of its two internal mutexes, or
  inside `em_task_queue_enqueue`, which wasn't disassembled this session) —
  or the field's value diverges from what real Emscripten thread-lifecycle
  bookkeeping would produce for some other, not-yet-identified reason.
  Kept the mailbox fix anyway: it's a correct, real implementation of a
  previously-fake import per the actual reference protocol, strictly better
  than a no-op stub, and worth having regardless of whether it closes this
  specific gap.

**This is the real limit of static disassembly for this bug.** Pinning down
*which* thread is stuck *where* inside `do_proxy`/`em_task_queue_enqueue`/
the mutex functions at the exact moment of the hang needs live inspection
(e.g. a debugger attached to both OS threads showing each one's current
wasm program counter / call stack) — reading compiled code by eye can show
what a function *can* do, not which of two concurrently-running threads is
currently paused where. Stopped here and reported back to the user rather
than continuing to add speculative fixes for a mechanism that isn't even
confirmed to be firing.

### Follow-up same session: minimal pthread-only repro — core plumbing is proven correct

User approved installing the Emscripten SDK (`git clone
https://github.com/emscripten-core/emsdk` into `google-tts-native/emsdk`,
then `emsdk install latest && emsdk activate latest` — not committed,
multi-GB, untracked like everything else in this project directory) to
build a from-scratch, minimal test case isolated from the real TFLite/Abseil
pipeline.

`examples/minimal_pthread_repro/minimal.c`: a plain C program exporting
`run_test()`, which does exactly `pthread_create` → (child prints and
returns `42`) → `pthread_join` → prints and returns the joined result.
Compiled with `emcc -O2 -pthread -sSHARED_MEMORY=1 -sIMPORTED_MEMORY=1
-sINITIAL_MEMORY=16MB -sEXPORTED_FUNCTIONS=[...] -sERROR_ON_UNDEFINED_SYMBOLS=0`
(no `-sSTANDALONE_WASM`, no JS glue is used or needed — only the `.wasm`
output matters, our Rust host provides every import). Made `HostState` and
`build_instance` `pub` in `lib.rs` so `examples/minimal_pthread_test.rs`
could reuse the exact same instantiation path the real engine uses, rather
than duplicating it.

**Result: complete success.** `run_test() returned 42 (expected 42)`,
exit code 0. The `GTTS_TRACE_PTHREAD=1` trace shows the full real lifecycle
completing cleanly: `__pthread_create_js` → spawn → stack setup → thread_init
→ TLS init → entry point runs and returns `0x2a` (42) → `_emscripten_thread_exit`
completes → back on the main thread, `pthread_join` returns → `run_test`
returns 42. **This conclusively proves the pthread plumbing implemented
this session (`__pthread_create_js`/`build_instance`/`run_pthread`, and the
`_emscripten_thread_exit` wait-on-`+108` mechanism) is not itself broken.**
The hang is specific to something the real TFLite/Abseil pipeline does that
this simple create+join test never exercises — most likely genuine use of
Emscripten's cross-thread task-proxying API (`do_proxy`/`em_task_queue_*`,
seen in disassembly but never exercised by a plain `pthread_join`), which a
minimal test would need to deliberately provoke (e.g. via
`emscripten/proxying.h`'s `emscripten_proxy_sync`/`emscripten_proxy_async`)
to reproduce. Not yet attempted — natural next step, and now cheap to try
since the toolchain is already set up.

### Next steps if this is picked up again

0. **Toolchain is now available**: `emsdk` is installed and activated at
   `google-tts-native/emsdk` (run `emsdk_env.ps1` in the same PowerShell
   invocation as any `emcc` call — env vars don't persist across separate
   tool calls in this harness). `HostState`/`build_instance` are `pub` in
   `gtts_engine` specifically to support ad-hoc isolated repros like this
   one; reuse that pattern for further narrowing.
1. Extend `minimal_pthread_repro` to deliberately call
   `emscripten_proxy_sync`/`emscripten_proxy_async` (from
   `emscripten/proxying.h`) from a spawned thread targeting the main
   thread's runtime thread id, while the main thread is blocked in
   `pthread_join` — this is the most direct way to confirm or rule out the
   `do_proxy`/task-queue theory from part 5's disassembly without needing
   the full TFLite/Abseil binary.
2. **Get live thread-state visibility**, not more static disassembly. Options,
   roughly in order of how much new tooling they need:
   - Add a lot more `GTTS_TRACE_PTHREAD`-gated `eprintln!` checkpoints
     directly around `do_proxy`'s two `__pthread_mutex_lock` calls and
     `em_task_queue_enqueue` — but that requires *also* disassembling and
     hand-instrumenting-by-proxy those functions (we can't add prints
     inside compiled wasm directly; the only lever we have is adding real
     implementations for still-stubbed *imports*, and none of the mutex/
     queue internals are imports — they're compiled musl/Emscripten code).
   - Build wasmtime with its debugging/DWARF support and attempt to attach
     a real debugger (`lldb`/`gdb`-over-wasmtime, or wasmtime's own
     `--debug` tooling) to see both threads' live call stacks — not
     attempted this session, unclear how well-supported this is on Windows
     with a Rust host embedding wasmtime rather than the `wasmtime` CLI
     itself running the module.
   - Alternatively: try reproducing against a *smaller*, more isolated
     wasm test case that exercises just `pthread_create`+`pthread_join`
     without the rest of the TTS pipeline, to see whether pthread
     lifecycle works in isolation on this host (would help separate "our
     pthread plumbing has a subtle bug" from "this specific TFLite/Abseil
     workload trips something pthread-adjacent but not pthread-primitive").
2. Consider whether the `attr` param `__pthread_create_js` currently
   ignores (see part 5 above) is really always irrelevant — re-check
   whether musl's `pthread_create` ever uses `PTHREAD_CREATE_DETACHED`
   (which changes join/exit refcounting semantics) for this specific
   thread; if so, that could very plausibly change what `+108`'s expected
   starting value even needs to be, independent of `do_proxy`.
3. If (1) and (2) don't converge in reasonable time, it may be more
   productive to step back and ask whether Natural/seanet voice support is
   worth this level of investment at all, versus the project's core goal
   (fast, low-RAM NVDA TTS) — base voices already work well without any of
   this.

## 2026-08-20 update, part 4: "Natural" (seanet) voices — root cause found, real fix deferred

User tested the packaged addon in real NVDA (first real-NVDA test this
project has had). Base voices worked. Switching to "Natural" voices (e.g.
"Google हिन्दी 1 (Natural)") failed. Investigated end to end; **two real bugs
found, one fixed, one substantial and deliberately deferred.**

### Bug 1 (fixed): missing `dependentVoiceId` dependency install

"Natural" voices live in a separate, much smaller package (e.g.
`hi-in-x-multi-seanet`, 3.7MB vs. the base `hi-in-x-multi`'s 17.6MB) that
the engine's own `voices.json` marks with `"dependentVoiceId":
"hi-in-x-multi"` — its pipeline needs the *base* package's files also
present in the wasm VFS (its `pipeline.pb` looks up shared config/model
files under the base package's own directory by a fixed relative path).
Confirmed by direct repro (`engine/examples/seanet_repro.rs`, kept in the
repo — useful for resuming this): installing *only* the seanet package and
calling `GoogleTtsInit` fails cleanly with
`openat("/voices/hi-in-x-multi-r62/burdock_frame_config.pb") -> not found`.

`catalog.py`'s `VoicePackage` never carried this field at all (silently
dropped it during JSON parsing) even though the JS glue
(`offscreen_compiled.js`'s `ph()`/`Sh()`/`yh()` functions) has always
handled it — installs the dependency package first, recursively, before the
requested one. Neither `NativeTtsBridge` nor the old `ChromeTtsBridge`/JS
path's Python side ever needed to know about it before, since Chrome's JS
runtime did this transparently.

**Fixed**: added `dependentVoiceId: str | None = None` to `VoicePackage` in
`catalog.py`, parsed from the source JSON. (Not yet wired into
`NativeTtsBridge._ensure_voice_ready` to actually install the dependency —
see "what's left" below; this fix alone isn't sufficient, bug 2 blocks it
regardless, so wiring it up now would be dead code until bug 2 is
resolved.)

### Bug 2 (found, NOT fixed — genuinely substantial): real threading required

After manually installing both packages (repro test), `GoogleTtsInit` no
longer fails — it **hangs forever**. Root cause, confirmed via two rounds of
`GTTS_TRACE_STUBS=1` tracing:

1. First trace showed a tight spin (280k+ calls in under a minute) purely
   on `env.emscripten_get_now`, which was stubbed to always return `0.0` —
   a frozen clock makes any wall-clock-timeout wait loop spin forever.
   **Fixed this regardless** (real `Instant`-based clock now in `lib.rs`,
   `HostState.start` + `env.emscripten_get_now` in the Pass-2 real-impl
   section) — harmless, strictly more correct, and already rebuilt into the
   bundled `server.exe`. **This did not fix the hang** — it just quieted
   that one symptom.
2. Second trace (much shorter, ~1300 lines) showed the real sequence:
   `emscripten_num_logical_cores` → `__pthread_create_js` (called for
   real, returns our stubbed fake-success) → `_emscripten_thread_mailbox_await`
   → then hundreds of `emscripten_stack_snapshot`/`emscripten_stack_unwind_buffer`
   pairs (Emscripten's **Asyncify** cooperative-stack-suspend mechanism,
   used so a thread that isn't allowed to block synchronously — Chrome's
   main thread — can cooperatively yield while polling an async
   `Atomics.wait`) → `emscripten_check_blocking_allowed`.
3. Symbol search in the wasm binary confirms this isn't vestigial:
   `greco::PipelineThreadManager::WaitAll()`, `greco::PipelineThread`,
   `thread::PermanentEvent` — a real C++ thread pool. The Natural pipeline
   genuinely dispatches work to a background thread and blocks waiting for
   it. Our `__pthread_create_js` is a no-op stub, so nothing ever runs on
   that "thread," and the wait can never be satisfied.

**Why this needs real design work, not a quick patch** (this is *why* it's
deferred, not just "ran out of time" — read this before attempting a fix):
wasmtime supports genuine shared-memory wasm threads natively (real
`atomic.wait`/`notify` work correctly across real OS threads with shared
`SharedMemory`, no JS-level polyfill needed) — and since our host has no
"must not block a UI thread" constraint the way a browser tab does, the
right fix is probably real OS threads each running their own `Store`/
`Instance` off the same `Module`+`SharedMemory`, *not* trying to
reimplement Chrome's Asyncify/mailbox JS protocol by guesswork. But: each
new `Instance`'s C stack pointer starts at the module's default location —
the SAME one the main thread already uses. Since memory is genuinely
shared, two threads' stacks collide immediately unless the new instance's
stack-pointer *global* is explicitly repointed at the pre-allocated stack
region Emscripten's musl libc already reserved in the `pthread_t`/TCB
structure passed into `pthread_create` (this is exactly what Emscripten's
real JS runtime does via `establishStackSpace` before invoking the entry
point). Getting this wrong doesn't fail cleanly — **it's silent memory
corruption** (garbled audio, unpredictable crashes), not an obvious error,
and it can't be fully verified here without something like ASan. There's
also unresolved scope beyond just the stack pointer: TLS setup per thread,
and whether the pool ever creates more than one thread (only one
`pthread_create` was observed before the hang — unconfirmed whether more
would follow once the first actually completes).

**Decision (explicit, from the user)**: don't attempt this now — it's real,
focused future work, not a same-session follow-on. Revisit deliberately.

### Stopgap shipped instead: hide Natural voices from selection

`SynthDriver.__init__` in `__init__.py` now filters `installedPackages` to
exclude any package with `dependentVoiceId` set, before the
"no voices installed" check:
```python
installedPackages = [package for package in installedPackages if not package.dependentVoiceId]
```
Rationale: a hang with **no cancellation path** (the wait happens inside a
single wasm call our `should_cancel` polling can't interrupt — confirmed
this in part 3's design notes already, now it's a concrete real
consequence) is a much worse user experience than a voice simply not being
listed. Verified: 104 total catalog packages, 51 correctly identified as
Natural/dependent and now excluded, 53 base packages unaffected; re-ran
`pyclient/test_native_bridge.py` after the `catalog.py` schema change to
confirm base-voice speak/cancel still works (it does). Repackaged
`dist\googleTtsForNvda-0.2.0.nvda-addon` with this fix and the updated
(clock-fixed) `server.exe` — this is the version to reinstall for further
testing.

## 2026-08-20 update, part 3: NativeTtsBridge wired into the addon

The user explicitly authorized editing `D:\dev\Google-TTS-For-NVDA` (it was
being treated as read-only out of caution, not because it's actually
off-limits — it's the user's own local git clone). Wrote the adapter class
from part 2's design and wired it in.

### What changed, both uncommitted (git is the safety net; nothing pushed)

- **`bridge.py`**: added `NativeTtsBridge`, a drop-in replacement for
  `ChromeTtsBridge` implementing the same public surface actually used by
  `__init__.py` (confirmed by grepping the whole addon repo for
  `_bridge\.`/`ChromeTtsBridge` — nothing outside `__init__.py` touches it,
  and `stop_runtime()` turned out to be dead code, not called anywhere, so
  it wasn't ported). `ChromeTtsBridge` itself is untouched, left in place as
  a fallback/reference. Key implementation points:
  - Spawns `server.exe` (found via `GTTS_NATIVE_SERVER_EXE` env var, a
    `native/server.exe` bundled-path for future packaging, or the dev build
    path, in that order), reads its `READY <mainPort> <controlPort>` line
    off stdout via a helper thread (plain `readline()` has no timeout, so a
    thread + polling loop is what makes this cancellable/boundable instead
    of hanging forever if the process never gets ready).
  - **Important protocol subtlety, worth remembering**: the main connection
    is a strict single-outstanding-request byte stream, not multiplexed
    like CDP. `ChromeTtsBridge.speak()` can bail out immediately on
    cancellation because a stray late CDP response just gets ignored by
    id-matching on the next request. `NativeTtsBridge.speak()` can't do
    that — bailing before the terminal frame arrives would desync frame
    boundaries for the *next* request on the same socket. So on cancel it
    sends the cancel byte on the control connection and keeps *reading and
    discarding* audio frames (not delivering to `onAudio`) until the
    server's terminal frame shows up, then raises `CdpCancelled`. Get this
    wrong and every request after the first cancelled one silently reads
    garbage frame headers.
  - `gain` is read from `options.get("outputGain", options["volume"])` —
    the addon's own `_speech_options` already computes this correctly, so
    the bridge doesn't recompute it (matches part 2's design note).
  - Voice switching reuses the same `_ensure_voice_ready` idempotency
    already built server-side (`current_voice_dir` tracking) but *also*
    tracks `self._currentPackageId` client-side, skipping the InstallVoice/
    Init round trip entirely when the package hasn't changed — a couple of
    JSON messages saved on every `preload_voice`/`speak` call for the
    common case (same voice repeatedly).
- **`__init__.py`**: `from .bridge import ChromeTtsBridge` →
  `NativeTtsBridge`; `self._bridge = NativeTtsBridge(self.catalog)`;
  deleted the `ChromeTtsBridge.find_chrome() is None` startup gate and the
  now-dead `_show_missing_chrome_error` method — the entire point of this
  project is that Chrome is no longer required, so that check needed to be
  removed, not ported. Diff is exactly as minimal as part 2 predicted: one
  import line, one instantiation line, one deleted gate. Nothing else in
  `__init__.py` (chunking, caching, the speech queue) changed at all.

### How this was validated without a running NVDA

`pyclient/test_native_bridge.py` imports the **real, unmodified** `bridge.py`
(not a reimplementation like part 2's `test_client.py`) by constructing a
synthetic package pointing at the addon's synth-driver directory and
loading `catalog.py`/`voice_store.py`/`bridge.py` into it via `importlib`
directly — this works because those three files have zero *hard* NVDA-only
imports (unlike `googleTtsForNvda/__init__.py`, the actual SynthDriver,
which does, and which Python would otherwise auto-run as a side effect of
importing any submodule of that package). One thing this exposed:
`voice_store.data_root()` reads `globalVars.appArgs.configPath` to find
NVDA's real config dir; without NVDA that silently falls back to a
throwaway temp directory with no voices "installed" in it. Fixed by
stubbing a minimal `globalVars` module pointing at the real config dir
(`C:\Users\aryan\AppData\Roaming\nvda`) before import — a real gap the test
caught, not a workaround for a code bug.

Ran it: preload + speak (English), voice-switch + speak (Hindi, same
`NativeTtsBridge` instance), a real `cancel_current()` against a real
in-flight `speak()` (confirmed `CdpCancelled` raised, confirmed the bridge
is still usable immediately after), all passed. Output in
`pyclient/out_bridge_{en,hi}.wav`, both confirmed real signal via RMS
(en clips at ±32768 as expected — that test used volume=1.0→gain=2.0).

### What's still genuinely left

1. **Never run inside real NVDA yet.** The above proves the actual bridge
   code path works correctly in isolation; it does not prove NVDA's speech
   thread, `WavePlayer` feeding, or the settings UI behave correctly with
   it. That needs an actual NVDA installation with this addon loaded as a
   synth driver — out of scope for this session (no NVDA runtime available
   here).
2. **`server.exe` isn't bundled with the addon** — `NativeTtsBridge`
   currently finds it via the dev build path
   (`D:\dev\google-tts-native\engine\target\release\server.exe`) as a
   last-resort fallback. A real add-on package needs the binary built and
   placed at `synthDrivers/googleTtsForNvda/native/server.exe` (or
   equivalent) as part of a packaging/CI step that doesn't exist yet.
3. Word-boundary/timepoint events and the pthread question (items 3/6 from
   part 1) are still untouched — the wire protocol only carries audio.
4. No supervision if `server.exe` crashes mid-session (flagged in part 2,
   still true).

## 2026-08-20 update, part 2: IPC server + NVDA integration groundwork

Started on item 5 (NVDA addon integration shape). Built and validated a
persistent local IPC server so the NVDA-side Python driver can talk to this
engine without embedding Rust or re-paying wasm-load cost per utterance —
the whole point of this project. **Did not touch anything under
`D:\dev\Google-TTS-For-NVDA`** — it's the read-only reference clone; nothing
here required editing it (see "what's genuinely left" below for why the
final wiring step will).

### What exists now

- **`engine/src/bin/server.rs`** (new binary target, `cargo build --release
  --bin server`, or it's built by a plain `cargo build --release` too): a
  single-client TCP server exposing `TtsEngine` over a small private framed
  protocol. Two 127.0.0.1 connections, both OS-assigned ports:
  - **main**: request/response + streamed audio. Frame = `[u8 tag][u32 LE
    len][payload]`. Client→server tags: `1`=InstallVoice (JSON
    `{voice_dir, zvoice_path}` — server reads the file itself, no need to
    ship 17MB voice blobs over the socket), `2`=Init (JSON `{voice_dir}`,
    no-ops if that voice dir is already the active pipeline — cheap to call
    on every voice-change like `preload_voice` already does), `3`=Speak
    (JSON `{text, rate, pitch, speaker, gain}` — `gain` is 0..2 and passed
    through as-is, not recomputed server-side, see below). Server→client
    tags: `0x80`=Error (JSON `{message}`), `0x81`=Ack, `0x82`=AudioChunk
    (raw gain-applied PCM, one frame per `GoogleTtsReadBuffered` iteration —
    real streaming, not batched), `0x83`=SpeakDone, `0x84`=SpeakCancelled.
  - **control**: a dedicated side channel purely for cancellation. Any
    single byte received on it sets an `AtomicBool` the main thread polls
    between `ReadBuffered` iterations (see `TtsEngine::synthesize_streaming`
    below) — needed because the main connection's thread is blocked
    generating audio and can't also be reading a "please stop" message on
    the *same* socket. This is the same problem Chrome's CDP approach solves
    via an inherently-async websocket; a second dedicated connection is the
    simplest fix for a synchronous request loop.
  - On startup prints `READY <mainPort> <controlPort>` to stdout, then
    serves. Engine wasm/vfs paths default to the same values `main.rs` uses,
    overridable via `GTTS_WASM_PATH`/`GTTS_VFS_ROOT` env vars.
- **`lib.rs`: `TtsEngine::synthesize_streaming`** (new, alongside the
  existing batch `synthesize`) — calls `on_chunk(&[u8])` per `ReadBuffered`
  iteration as soon as decoded (each iteration's raw bytes are already a
  self-contained protobuf message, confirmed earlier, so no cross-iteration
  buffering is needed to extract audio from it) and polls a `should_cancel`
  closure between iterations, returning `SynthesisOutcome::{Completed,
  Cancelled}`. `find_audio_chunks`'s recursive walk was factored out into
  `walk_audio_chunks` so both the batch and streaming paths share it.
- **`pyclient/test_client.py`**: a standalone (no NVDA, no addon edits)
  Python test proving the whole loop end-to-end: spawns `server.exe`,
  installs+inits **two different voice packages over the same connection**
  (en-in-x-multi then hi-in-x-multi — proves voice-switching works over IPC,
  beyond examples/hi_voice.rs's single-voice-per-process check), speaks
  both to completion, then starts a long speak and cancels it mid-flight via
  the control connection, confirming (a) it actually stops early
  (`SPEAK_CANCELLED`, not `SPEAK_DONE`) and (b) the main connection is still
  usable afterward (one more successful speak). Voice-ID → package/speaker
  resolution reuses the addon's own `catalog.py` (imported directly — it has
  zero relative/NVDA imports, so it's safe standalone) rather than
  reimplementing that mapping; `voice_store.py` could *not* be reused the
  same way since it does `from .catalog import ...`, a relative import that
  only works when imported as part of the `googleTtsForNvda` package, whose
  `__init__.py` hard-imports NVDA-only modules (`wx`, `addonHandler`, ...).
  The real voice-install directory is hardcoded in the test client instead.
  Run: `python D:\dev\google-tts-native\pyclient\test_client.py` (needs
  `server.exe` already built). All checks passed as of this writing; output
  in `pyclient/out_server_{en,hi}.wav`.
- **Gain is computed Python-side, not Rust-side, for this protocol.**
  `__init__.py`'s `_speech_options` already computes `outputGain` correctly
  (`max(0, min(2, volume/100))`... actually `volume/50`, see that method) —
  the Speak request's `gain` field is that value passed straight through.
  `volume_percent_to_gain` in `lib.rs` still exists and is still correct,
  just unused by this protocol — it's for standalone/CLI use where there's
  no NVDA-side options dict already carrying a computed gain.

### What's genuinely left before this is wired into NVDA

This is the real remaining gap, not just polish:

1. **A Python adapter class matching `ChromeTtsBridge`'s public interface**
   (`speak(text, options, onAudio, cancelEvent)`, `preload_voice(options,
   cancelEvent)`, `cancel_current()`, `terminate()`) that spawns
   `server.exe`, resolves `options["voiceId"]` via the *real* `catalog.py` +
   `voice_store.py` (both importable fine from inside the addon package,
   unlike the standalone test client's workaround), and speaks the
   protocol above. `__init__.py` in the addon would only need
   `from .bridge import ChromeTtsBridge` swapped for this new class — the
   rest of `__init__.py` (chunking, caching, the speech queue) shouldn't
   need to change at all, since it only calls that narrow interface. Also:
   `ChromeTtsBridge.find_chrome()` is called in `SynthDriver.__init__` to
   hard-fail with a "Chrome not found" dialog — the whole point of this
   project is that this check becomes unnecessary and should be deleted for
   the native path, not reimplemented.
2. **This is out of scope to build blind**: `D:\dev\Google-TTS-For-NVDA` is
   explicitly a read-only reference clone, not something to edit directly.
   Actually wiring the adapter in means either getting a writable copy of
   the addon or building a small patch/fork — a decision for the user, not
   something to just do silently.
3. Not addressed at all yet: word-boundary/timepoint events (STATUS has
   flagged this as unimplemented since the very first checkpoint — the
   protocol above only carries audio, no `IndexCommand`/word-boundary
   equivalent), and `BreakCommand`/silence gaps (currently handled entirely
   NVDA-side in `__init__.py._feed_silence`, doesn't touch the bridge at
   all, so probably fine as-is).
4. Server currently handles exactly one client connection then exits when
   it drops (fine for one NVDA process at a time, which is the only
   realistic case, but worth stating as a deliberate scope limit not an
   oversight).
5. No supervision/restart logic if `server.exe` crashes mid-session — the
   original Chrome path didn't really have this either (a wedged Chrome
   needed the user to notice), so not obviously worse, but worth deciding
   whether the adapter class should auto-restart the process on a broken
   pipe.

## 2026-08-20 update

Restructured `main.rs`'s linear smoke-test script into a real library
(`lib.rs`, crate `gtts_engine`) exposing `TtsEngine::new/install_voice/init/
synthesize`, each returning `anyhow::Result` and `bail!`ing on non-1/nonzero
wasm return codes instead of silently printing and continuing. `main.rs` is
now a thin CLI that calls the library and writes `out.wav`, reproducing the
prior checkpoint's exact output. Also:

- **Volume gain implemented and applied.** Traced the addon's own JS to find
  the exact algorithm (wasm never sees volume — confirmed, this was right):
  `offscreen_compiled.js` converts raw int16 PCM to float via `/32768`,
  `bridge.py`'s `_speech_options` maps NVDA volume 0..100 to
  `gain = clamp(volume/50.0, 0, 2)` (so NVDA's default of 100 is gain
  **2.0**, not 1.0 — don't assume unity gain at default settings),
  `bridgeHarness.js`'s `buffersToPcmBase64` multiplies by gain, clamps to
  [-1,1], then requantizes asymmetrically (`*0x8000` if negative, `*0x7fff`
  if positive — not a clean round-trip even at gain=1.0, but that's genuinely
  what Chrome does, so `apply_volume_gain`/`volume_percent_to_gain` in
  `lib.rs` reproduce it exactly). No captured ground truth exists for
  post-gain output (only pre-gain wasm event captures), so this is validated
  by source-reading fidelity, not byte-identical comparison. `main.rs` now
  applies it at volume=100 before writing `out.wav`.
- **Second voice confirmed working**: `examples/hi_voice.rs` installs
  `hi-in-x-multi.zvoice` (fileId `hi-in-x-multi-r62`, different from
  `en-in-x-multi-r65`) and synthesizes Devanagari text with speaker `"cfn"`
  ("Chrome OS हिन्दी 1", from `WasmTtsEngine/20260625.1/voices.json`'s
  per-voice `speakers` array — NOT `runtime/voices.json` or
  `verified_voices.json`, which don't carry speaker codes). Produced real
  audio (RMS ~6200, full dynamic range, 2.17s), proving speaker-code
  switching isn't accidentally hardcoded to `"cxx"`. Run with
  `cargo run --release --example hi_voice`.
- **Differential rate/pitch captures re-validated**, now as permanent unit
  tests in `proto.rs` (`cargo test`) rather than a one-off manual check.
  Note while doing this: the byte offset for locating the `rate` f32 field
  from the end of a request buffer is `len-9`, not `len-8` as a first
  attempt assumed (there's a 1-byte field tag between the two floats) — get
  this wrong and the decoded rate is a near-zero garbage value that looks
  plausible at a glance. All four captures (rate=1/pitch=1, rate=2/pitch=1,
  rate=1/pitch=0.5, rate=1.6/pitch=2) match byte-identical.
- Housekeeping item (orphaned `D:\dev\google-tts-native-src` clone) is now
  empty but this session's sandbox permissions blocked deleting it — still
  needs a manual `rmdir` from the user.

## Where things stand (current checkpoint)

**The full synthesis pipeline works end-to-end in pure Rust, for arbitrary
text, with output cross-validated against real Chrome-produced audio.**
Concretely, in `D:\dev\google-tts-native\engine` (`cargo run --release`,
must be run via the **PowerShell tool, not Bash** — see Gotchas):

1. Compiles/instantiates the real 23MB Emscripten pthreads module
   (`bindings_main.wasm`) in wasmtime with a from-scratch VFS + WASI/syscall
   host implementation.
2. `GoogleTtsInstallVoice` unpacks a real `.zvoice` package (57 files, ~24MB)
   into the VFS.
3. `GoogleTtsInit` loads the full voice pipeline (mmap-based model loading,
   ~10MB of FlatBuffer/binary models).
4. A hand-rolled, **validated** protobuf encoder (`src/proto.rs`) builds the
   request/config messages `GoogleTtsInitBuffered` expects from arbitrary
   text/rate/pitch — verified byte-identical to a real captured Chrome
   request before being trusted on new text.
5. `GoogleTtsReadBuffered`/`FinalizeBuffered` produce the real audio event
   stream; a small recursive protobuf walker (no schema needed) extracts the
   raw 24kHz 16-bit mono PCM and writes a `.wav`.
6. Synthesized a brand-new sentence never seen by Chrome
   ("Hello from a native Rust engine, no Chrome involved.") successfully —
   3.13s of real audio in `engine/out.wav`.

**Threading was assumed necessary but turned out not to be** — buffered
synthesis is synchronous in this build. `__pthread_create_js` etc. are still
unimplemented no-op stubs and nothing has needed them so far.

## Key files

- `engine/src/main.rs` — current state is a **linear smoke-test script**, not
  a library: instantiates the module, runs the install→init→synth→decode
  sequence once with hardcoded paths, dumps a lot of debug `println!`s
  (including a very verbose blanket `[stub] modname.name(params)` trace on
  every still-unimplemented import — useful for debugging missing host
  functions, but noisy; see "how to debug a new trap" below).
- `engine/src/vfs.rs` — the virtual filesystem backing Emscripten's
  `__syscall_*`/WASI functions. `write_stat64`'s struct layout is a
  best-effort classic Emscripten wasm32 72-byte layout, unverified in
  isolation but working in practice (real `.zvoice` install + pipeline load
  both succeeded, which exercises stat pretty thoroughly).
- `engine/src/proto.rs` — request/config protobuf encoder. Schema was
  reverse-engineered by hand from captured wire bytes (no `.proto` file
  ever seen) and is validated against real captures in `main.rs` at
  startup (asserts byte-identical output for the baseline text/rate/pitch).
- `capture/serve_and_capture.py` — standalone Python/Chrome-CDP capture
  harness (bypasses the NVDA addon's Python package, since its `__init__.py`
  hard-imports `addonHandler`/`wx`/NVDA-only modules). Used to produce the
  ground-truth fixtures below. Re-run this if new ground truth is ever
  needed (e.g. to validate the second voice, or timepoint/word-boundary
  events, which haven't been touched yet).
- `capture/output/session-1787096651-{req,cfg,event00..34}.bin` — the
  ground-truth capture used for all validation so far: text "Testing one two
  three.", rate=1.0, pitch=1.0, speaker "cxx" (`en-in-x-multi` package,
  "Chrome OS Indian English 1"). Also 3 differential captures
  (`rate2p1`, `r1p05`, `r16p20`) used earlier to disambiguate the rate/pitch
  wire encoding — not yet re-validated against the new encoder, would be a
  cheap next sanity check.

## Reverse-engineered protocol details (all confirmed against real bytes)

- **Voice install**: `GoogleTtsInstallVoice(dirPathPtr, dataPtr, len) -> i32`
  (1 = success). `dirPath` e.g. `/voices/en-in-x-multi-r65`, `data` = raw
  `.zvoice` file bytes.
- **Init**: `GoogleTtsInit(pipelinePathPtr, voiceDirPathPtr) -> i32` (1 =
  success). Paths are C strings under the installed voice dir, e.g.
  `/voices/en-in-x-multi-r65/pipeline.pb` and `/voices/en-in-x-multi-r65`.
- **Buffered synth**:
  `GoogleTtsInitBuffered(reqPtr, cfgPtr, reqLen, cfgLen) -> i32` (nonzero =
  success), then loop `GoogleTtsReadBuffered() -> i32` (-1 = error, 0 =
  done+call `GoogleTtsFinalizeBuffered()`, anything else = more data
  pending), each iteration also read `GoogleTtsGetEventBufferPtr/Len()` for
  that iteration's raw event bytes.
- **Request proto** (see `proto.rs` for exact byte layout):
  ```
  Request { field2: Utterance {
    field1: TextWrap { field2: { field1: { field2: string text } } }
    field2: Params   { field3: { field1: float rate, field6: float pitchWire } }
  }}
  ```
  `rate` is a direct linear passthrough of Chrome's rate slider value (1.0 =
  normal). `pitchWire = 2^((5/3)*(pitch-1))` where `pitch` is Chrome's 0..2
  pitch slider value (1.0 = normal) — verified to 5 decimal places against 3
  independent data points.
- **Config proto**: `{ field1: string speakerCode }`, e.g. `"cxx"` for
  "Chrome OS Indian English 1". Speaker codes live in the addon's
  `runtime/voices.json` (`C:\Users\aryan\AppData\Roaming\nvda\googleTtsForNvda\voices\`).
- **Event/audio stream**: each `ReadBuffered` call's raw bytes are a
  self-contained protobuf message; concatenating them back-to-back is a
  valid repeated-field stream (protobuf property exploited by
  `find_audio_chunks` in `main.rs`). Audio chunks decode as
  `{ field1: bytes rawPcm, field2: varint sampleRate }`, kept only when
  `sampleRate == 24000`; PCM is 16-bit signed LE mono. Volume is **not**
  sent to wasm at all — it's applied as a pure client-side sample multiply
  in the JS bridge (`currentOutputGain`), so the native host needs to do the
  same after decoding PCM (not yet implemented — currently raw/unscaled).
- **Host functions that needed real (not stub) implementations** to get this
  far: the filesystem stack (`EnsureDir`, `__syscall_openat/fstat64/stat64/
  lstat64/newfstatat`, `fd_close/read/write/seek`, `environ_*`,
  `random_get`, `clock_time_get`, `proc_exit` — all legacy split-i64 ABI
  where relevant) plus **`env._mmap_js`**, which is used for zero-copy model
  file loading and was the trickiest one to find (see "two real bugs found"
  below). Everything else is still a blanket generic no-op stub.

## Two real bugs found and fixed this session (worth knowing before touching this code again)

1. **`Func::new` result-slot bug** (`main.rs`, the Pass-1 generic stub
   loop): `results: &mut [Val]` passed to a `func_new` callback is **not**
   pre-filled with type-correct defaults — wasmtime primes it with a
   placeholder that fails the runtime type-check if left untouched for any
   stub with a non-empty result list. This caused a deep, misleading trap
   inside abseil/protobuf static-initializer code
   (`__wasm_call_ctors`) that looked like a C++ exception-handling problem
   but was actually just this. Fixed by writing `Val::default_for_ty(&ty)`
   into every result slot. **If you ever add a new blanket stub pass, remember
   this.**
2. **`_mmap_js` needed a real implementation**, not a stub: Emscripten uses
   it for zero-copy file loading of model files (`.fb`, `.far`, etc). No
   real OS mmap exists here, so it's faked by calling the module's own
   `malloc` export (via `Caller::get_export`, which only works
   post-instantiation) and filling the buffer from the file at the given
   offset. Exact signature confirmed by reading Emscripten's own JS glue in
   `bindings_main.js` (`function __mmap_js(len, prot, flags, fd, offset_low,
   offset_high, allocated, addr)`), not guessed.

## Environment gotchas

- **Always run `cargo build`/`cargo run` via the PowerShell tool, not Bash.**
  Git-Bash's PATH shadows MSVC's real `link.exe` with its own coreutil,
  breaking every Rust link step. This is a hard requirement, not a
  preference.
- Full trace of every still-stubbed import call can be dumped by
  temporarily re-adding a `println!` at the top of the Pass-1 generic stub
  closure in `main.rs` (search history for `[stub]` — it was added and later
  should probably be removed/gated behind a verbosity flag rather than left
  permanently on, since it's very high-volume: ~30,000 lines of trace for
  one synthesis run).

## Pending / next steps (in rough priority order)

1. ~~Restructure into a real library~~ **Done 2026-08-20** — see update above.
2. ~~Apply client-side volume gain~~ **Done 2026-08-20** — see update above.
3. ~~Test second installed voice~~ **Done 2026-08-20** — see update above.
4. ~~Re-validate differential rate/pitch captures~~ **Done 2026-08-20** — now
   permanent unit tests in `proto.rs`.
5. ~~Decide the actual integration shape for the NVDA addon~~ **Done
   2026-08-20** — IPC server + `NativeTtsBridge`, tested inside real NVDA.
6. **Real pthread support — now confirmed necessary, not hypothetical.**
   Was "flag if something needs it"; something does (Natural/seanet voices,
   part 4 above). Read part 4's "why this needs real design work" section
   in full before attempting — the stack-pointer-global correctness issue
   is a silent-memory-corruption risk, not just a hang, if rushed. Natural
   voices are hidden from selection in the meantime (also part 4), so
   there's no urgency pressure — take the time this needs.
7. Housekeeping: `D:\dev\google-tts-native-src` (an orphaned clone of an
   unrelated Chromium mirror repo) is now empty and just needs `rmdir` —
   this session's sandbox permissions blocked doing it directly.
8. Minor: `vfs.rs` has two genuinely-dead items flagged by `cargo build`
   warnings (`FdEntry::file`/`is_dir` fields read only via pattern match
   elsewhere so may be a false positive — recheck; `path_from` is fully
   unused) — harmless, low priority cleanup.
9. Once pthread support lands: wire `VoicePackage.dependentVoiceId` (added
   part 4, parsed but not yet consumed) into
   `NativeTtsBridge._ensure_voice_ready` — install the dependency package's
   `.zvoice` into its own `/voices/{fileId}` dir before the target
   package's, mirroring the JS glue's `yh()`/`Sh()` — and remove the
   `installedPackages` filter in `__init__.py` that hides Natural voices.

## How to resume

Read this file, then `engine/src/lib.rs` (the `TtsEngine` API and host
function implementations) and `engine/src/main.rs` (the thin CLI smoke test)
before changing anything. Re-run, all via the PowerShell tool in `engine/`:

- `cargo test --release` — 7 unit tests (proto encoder vs. 4 real captures,
  volume-gain formula/clamping), should all pass in well under a second.
- `cargo run --release` — should print "Proto encoder verified
  byte-identical..." then end with "Wrote out.wav (3.13s)".
- `cargo run --release --example hi_voice` — second-voice check, ends with
  "Wrote out_hi.wav (2.17s)".
- `python D:\dev\google-tts-native\pyclient\test_client.py` (after `cargo
  build --release` so `server.exe` exists) — IPC server end-to-end check,
  ends with "All checks passed."

- `python D:\dev\google-tts-native\pyclient\test_native_bridge.py` — same
  check but through the REAL `bridge.py` `NativeTtsBridge` class (see part 3
  below), ends with "All checks passed (real bridge.py NativeTtsBridge)."

`Google-TTS-For-NVDA` is no longer read-only — the user authorized editing
it directly (part 3 below), and `bridge.py`/`__init__.py` there now have
real, working, uncommitted changes. Next real step is part 3's "What's
still genuinely left": this has never been run inside an actual NVDA
process, and `server.exe` isn't bundled/packaged with the addon yet.
