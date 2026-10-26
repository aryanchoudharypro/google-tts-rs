# Google TTS RS

> [!WARNING]
> **Vibe-coded.** This project was written with AI assistance. It has been tested (Rust unit tests,
> Python driver tests, and end-to-end runs against real voices in NVDA), but **the code has not been
> reviewed line by line by a human**. Expect rough edges, and read it before you trust it with anything
> that matters to you. Bug reports and pull requests are welcome.

Google's WASM text-to-speech voices as an NVDA synthesizer, driven by a native Rust engine.

No Chrome. No Node.js. One ~16 MB executable that loads the same speech engine Chrome ships, runs it
on [wasmtime](https://wasmtime.dev/), and streams PCM straight into NVDA's audio player.

The high quality **"Natural"** (seanet) voices work, which needed real POSIX threads inside the wasm
module and a faithful reimplementation of Emscripten's threading host interface — see
[docs/engineering-notes.md](docs/engineering-notes.md).

## What it is

An NVDA synthesizer add-on that speaks with Google's offline text-to-speech voices, the same ones
Chrome and Android use, including the high quality "Natural" (seanet) voices. Voice packs are
downloaded from Google on demand through a built-in Voice Manager and run entirely on your machine.

## How it differs from the original

[Google TTS For NVDA](https://github.com/nguyenanhduc09/Google-TTS-For-NVDA) drives Google's speech
engine by launching a headless Chrome instance per session and talking to it over the Chrome DevTools
Protocol. That works, but a whole browser is a heavy price for a screen reader's speech, especially on
the low-spec machines NVDA users often run. This add-on replaces the browser with a single native
process written in Rust.

| | Original (Chrome bridge) | Google TTS RS |
|---|---|---|
| Runtime dependency | Headless Chrome | none, one bundled exe |
| Processes at idle | Chrome + renderer + GPU | one |
| "Natural" voices | yes | yes |
| Synthesis speed | n/a | ~12x realtime on a laptop CPU |
| Cold start | n/a | ~30 ms (compiled module is cached) |

What is new here: the Rust engine, which runs Google's unmodified WASM module on
[wasmtime](https://wasmtime.dev/); a reimplementation of Emscripten's threading host interface so the
Natural voices work; a cache for the compiled module; and a playback path tuned for responsiveness.
What is kept from the original: the Voice Manager, the voice catalog and the packaging layout.

## Layout

```
addon/     the NVDA add-on (Python)
engine/    the native speech engine (Rust)
tests/     tests that run without NVDA, plus one end-to-end test that needs the engine built
docs/      how the engine was reverse-engineered, and why it is built the way it is
```

The two halves talk over a small framed protocol on TCP loopback: the add-on starts `server.exe`,
which prints `READY <mainPort> <controlPort>` and then serves install-voice / init / speak requests,
streaming audio chunks back. A second connection exists purely so a cancel can interrupt a synthesis
that is currently blocking the main connection.

## Building

Needs a stable Rust toolchain and Python 3.11+ (only for the tests and the syntax check).

```powershell
.\build.ps1
```

That builds the engine, runs the Rust and Python tests, bundles `server.exe` into the add-on tree and
writes `dist/googleTtsRs-<version>.nvda-addon`. Use `-SkipTests` for a quick repackage and `-Clean` to
start from scratch.

## Developing

```powershell
cd engine
cargo test --release                          # protobuf encoder vs. real captured Chrome bytes
cargo run --release                           # synthesize one line, write a wav
cargo run --release --example seanet_voice    # every "Natural" speaker, end to end
```

```powershell
python tests\test_driver.py         # text chunking and audio feeding, no NVDA needed
python tests\test_native_bridge.py  # real bridge against a real engine process (build first)
python tests\test_continuity.py 20  # say-all timing; reports any gap in playback
```

```powershell
cargo run --release --example latency      # where the time actually goes
```

Examples read voice packs from your NVDA configuration directory, falling back to the ones the
upstream add-on downloaded. Override with `GTTS_VOICES_DIR`. `GTTS_WASM_PATH`, `GTTS_VFS_ROOT` and
`GTTS_OUT_DIR` override the other locations.

The engine caches its compiled wasm module under `%LOCALAPPDATA%\googleTtsRs\modcache`, which is what
turns a ~2.4 s cold start into ~30 ms. `GTTS_CACHE_DIR` moves it; `GTTS_NO_MODULE_CACHE` disables it.

Rust is formatted with `cargo +nightly fmt` (the config uses nightly-only import options). Python
follows NVDA's house style: tabs, and `camelCase` for locals in add-on code.

## Installing alongside the original

This add-on uses its own identifier (`googleTtsRs`), so it installs next to Google TTS For NVDA rather
than replacing it, and both appear separately in NVDA's synthesizer list. It reuses any voice packs the
older add-on already downloaded instead of fetching them again — and it will refuse to delete those
shared copies, since they belong to the other add-on.

Its voice manager is on `NVDA+control+shift+R`, not the original's `NVDA+control+shift+G`.

## Credits and licence

Maintained by Aryan Choudhary.

Forked from [Google TTS For NVDA](https://github.com/nguyenanhduc09/Google-TTS-For-NVDA) by Dao Duc
Trung and Nguyen Anh Duc, and licensed under the **GNU General Public License version 2** — see
[LICENSE](LICENSE). The voice manager, voice catalog and packaging layout are theirs; the Rust engine,
the threading work that made Natural voices run, and the gapless playback path are new here.

`addon/synthDrivers/googleTtsRs/WasmTtsEngine/` contains Google's speech engine, redistributed
unmodified from the Chrome extension the upstream add-on sourced it from. It is Google's work, not
covered by this project's licence, and no voice data is included — voice packs are downloaded from
Google at runtime.
