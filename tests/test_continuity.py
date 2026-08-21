# -*- coding: utf-8 -*-
"""Reproduce NVDA's say-all timing against the real engine and measure whether
the audio stream actually stays continuous.

NVDA's speech manager hands the synth one utterance at a time and only sends
the next one once the previous utterance's end-of-utterance index has been
reported. This drives the real SynthDriver exactly that way, with a player that
models a playback clock, and reports every moment the stream ran dry.

Needs the engine built (`cargo build --release`) and a voice pack installed.

Run with: python tests/test_continuity.py [lines] [voice-substring]
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nvda_stubs  # noqa: E402
import sim_player  # noqa: E402

LINES = [
	"The morning light came in through the tall windows and lay across the floor",
	"in long pale stripes that shifted as the branches outside moved",
	"Nobody had opened the shutters in weeks and the dust hung in the air",
	"turning slowly wherever the light touched it",
	"She set the cup down on the table without drinking from it",
	"and listened to the house settle around her",
]


class FakeSpeechManager:
	"""The half of NVDA that matters here: send an utterance, wait for its
	index, send the next one."""

	def __init__(self, driver, indexNotifier) -> None:
		self.driver = driver
		self.commands = sys.modules["speech.commands"]
		self._seen = threading.Event()
		self._waitingFor: int | None = None
		self.indexTimes: list[float] = []
		self._start = time.monotonic()
		indexNotifier.handler = self._on_index

	def _on_index(self, synth=None, index=None) -> None:
		self.indexTimes.append(time.monotonic() - self._start)
		if index == self._waitingFor:
			self._seen.set()

	def speak_lines(self, lines: list[str], player, timeout: float = 30.0) -> None:
		for number, line in enumerate(lines, 1):
			self._waitingFor = number
			self._seen.clear()
			before = player.feedCount
			sent = time.monotonic() - self._start
			self.driver.speak([line, self.commands.IndexCommand(number)])
			while player.feedCount == before:
				if time.monotonic() - self._start - sent > timeout:
					raise TimeoutError(f"no audio for line {number}")
				time.sleep(0.001)
			firstAudio = time.monotonic() - self._start
			if not self._seen.wait(timeout):
				raise TimeoutError(f"index {number} never reported")
			with player._lock:
				buffered = player._streamEnd - player._now()
			print(
				f"  line {number}: sent at {sent:6.2f}s, first audio +{(firstAudio - sent) * 1000:5.0f}ms, "
				f"index at {self.indexTimes[-1]:6.2f}s with {buffered * 1000:5.0f}ms still buffered",
			)


def main() -> int:
	count = int(sys.argv[1]) if len(sys.argv) > 1 else len(LINES)
	lines = (LINES * ((count // len(LINES)) + 1))[:count]

	nvdaConfig = Path.home() / "AppData" / "Roaming" / "nvda"
	stubs = nvda_stubs.install_stubs(nvdaConfig if nvdaConfig.is_dir() else None)

	player = sim_player.SimPlayer()
	stubs["nvwave"].WavePlayer = lambda **kwargs: player

	driver_module = nvda_stubs.load_driver_module()
	driver = driver_module.SynthDriver()
	try:
		wanted = sys.argv[2].lower() if len(sys.argv) > 2 else None
		if wanted:
			matches = [
				voiceId
				for voiceId, voice in driver.availableVoices.items()
				if wanted in voiceId.lower() or wanted in voice.displayName.lower()
			]
			if not matches:
				print(f"no voice matching {wanted!r}; available: {', '.join(driver.availableVoices)}")
				return 1
			driver.voice = matches[0]
			print(f"using voice {driver.voice}")
		print(f"warming up, then speaking {len(lines)} lines the way NVDA's say-all would...")
		manager = FakeSpeechManager(driver, stubs["synthDriverHandler"].synthIndexReached)
		driver.speak(["Warm up."])
		time.sleep(3.0)
		player.underruns.clear()
		player.fedSeconds = 0.0
		player.feedCount = 0

		started = time.monotonic()
		manager.speak_lines(lines, player)
		player.sync()
		wall = time.monotonic() - started

		print()
		print(player.report())
		print(f"wall clock {wall:.2f}s for {player.fedSeconds:.2f}s of audio")
		gaps = [u for u in player.underruns]
		if gaps:
			worst = max(u.length for u in gaps)
			print(f"worst underrun {worst * 1000:.0f}ms")
			return 1
		return 0
	finally:
		driver.terminate()
		player.close()


if __name__ == "__main__":
	raise SystemExit(main())
