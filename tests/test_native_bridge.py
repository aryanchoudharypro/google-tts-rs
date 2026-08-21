"""End-to-end test of the real NativeTtsBridge from the add-on tree.

Unlike test_driver.py this starts the actual engine process and synthesizes
real audio, so it needs `cargo build --release` to have run and the voice
packages below to be installed (NVDA's Voice Manager, or the upstream add-on's
voices directory, which is reused automatically).

Run with: python tests/test_native_bridge.py
"""

from __future__ import annotations

import sys
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nvda_stubs

OUT_DIR = nvda_stubs.REPO_ROOT / "out"

BASE_VOICE = "en-in-x-multi:cxx"
HINDI_VOICE = "hi-in-x-multi:cfn"
NATURAL_VOICE = "hi-in-x-multi-seanet:cfn"


def write_wav(path: Path, pcm: bytes) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with wave.open(str(path), "wb") as stream:
		stream.setnchannels(1)
		stream.setsampwidth(2)
		stream.setframerate(24000)
		stream.writeframes(pcm)


def speech_options(catalog_mod, voiceId: str, rate: float = 1.0, pitch: float = 1.0, volume: float = 1.0) -> dict:
	speaker = catalog_mod.VoiceCatalog.load().speaker_for_voice(voiceId)
	return {
		"voiceId": speaker.id,
		"voiceName": speaker.name,
		"lang": speaker.language,
		"rate": rate,
		"pitch": pitch,
		"volume": volume,
		"outputGain": max(0.0, min(2.0, volume * 2.0)),
	}


def main() -> int:
	nvdaConfig = Path(r"C:\Users\aryan\AppData\Roaming\nvda")
	nvda_stubs.install_stubs(nvdaConfig if nvdaConfig.is_dir() else None)
	import types

	pkg = types.ModuleType(nvda_stubs.PKG_NAME)
	pkg.__path__ = [str(nvda_stubs.ADDON_SYNTH_DIR)]
	sys.modules[nvda_stubs.PKG_NAME] = pkg
	catalog_mod = nvda_stubs._load_module("catalog", pkg)
	nvda_stubs._load_module("voice_store", pkg)
	bridge = nvda_stubs._load_module("bridge", pkg)

	bridgeObj = bridge.NativeTtsBridge()
	try:
		print("preload_voice(en)...")
		options = speech_options(catalog_mod, BASE_VOICE)
		bridgeObj.preload_voice(options)
		print("  ok")

		print("speak(en)...")
		chunks: list[bytes] = []
		result = bridgeObj.speak("Testing the real bridge class, not a reimplementation.", options, chunks.append)
		total = sum(len(c) for c in chunks)
		print(f"  -> {result}, {total} bytes in {len(chunks)} chunks")
		assert result.get("success") and total > 0
		write_wav(OUT_DIR / "bridge-en.wav", b"".join(chunks))

		print("Switching voice and speaking Hindi...")
		hiOptions = speech_options(catalog_mod, HINDI_VOICE)
		chunks2: list[bytes] = []
		result2 = bridgeObj.speak("नमस्ते, यह असली ब्रिज क्लास है।", hiOptions, chunks2.append)
		print(f"  -> {result2}, {sum(len(c) for c in chunks2)} bytes")
		assert result2.get("success") and chunks2
		write_wav(OUT_DIR / "bridge-hi.wav", b"".join(chunks2))

		print("Switching to a Natural (seanet) voice...")
		naturalOptions = speech_options(catalog_mod, NATURAL_VOICE)
		chunksNat: list[bytes] = []
		started = time.monotonic()
		resultNat = bridgeObj.speak("नमस्ते, यह नैचुरल आवाज़ है।", naturalOptions, chunksNat.append)
		totalNat = sum(len(c) for c in chunksNat)
		print(f"  -> {resultNat}, {totalNat} bytes, {time.monotonic() - started:.2f}s")
		assert resultNat.get("success") and totalNat > 0
		write_wav(OUT_DIR / "bridge-hi-natural.wav", b"".join(chunksNat))

		print("Switching back from Natural to a base voice...")
		chunksBack: list[bytes] = []
		resultBack = bridgeObj.speak("Back on a base voice.", options, chunksBack.append)
		print(f"  -> {resultBack}, {sum(len(c) for c in chunksBack)} bytes")
		assert resultBack.get("success") and chunksBack

		print("Cancelling a real in-flight speak()...")
		longText = "This sentence repeats several times so a mid-utterance cancel is observable. " * 4
		cancelEvent = threading.Event()
		outcome: dict = {}

		def do_speak() -> None:
			try:
				outcome["result"] = bridgeObj.speak(longText, options, lambda pcm: None, cancelEvent)
			except bridge.BridgeCancelled:
				outcome["cancelled"] = True

		worker = threading.Thread(target=do_speak)
		worker.start()
		time.sleep(0.15)
		cancelEvent.set()
		bridgeObj.cancel_current()
		worker.join(timeout=30)
		print(f"  -> {outcome}")
		assert outcome.get("cancelled") is True, outcome

		print("Confirming the bridge still works after cancel...")
		chunks3: list[bytes] = []
		result3 = bridgeObj.speak("Still working after cancel.", options, chunks3.append)
		print(f"  -> {result3}, {sum(len(c) for c in chunks3)} bytes")
		assert result3.get("success") and chunks3

		print("\nAll checks passed.")
		return 0
	finally:
		bridgeObj.terminate()


if __name__ == "__main__":
	raise SystemExit(main())
