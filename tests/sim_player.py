# -*- coding: utf-8 -*-
"""A WavePlayer stand-in with a real playback clock.

FakePlayer records calls but has no notion of time, so it cannot show whether
the audio stream actually runs dry between utterances. This one models the
device: fed audio occupies a span on a timeline, `onDone` callbacks fire when
their chunk finishes playing, and any moment where the stream had nothing left
to play is recorded as an underrun.

Two different underruns matter and they are reported separately:

  gap      the stream ran dry and later resumed, i.e. silence the listener
           hears between utterances
  stutter  the stream ran dry in the middle of an utterance's own audio

Nothing here talks to a sound card; it is a clock and a list of spans.
"""

from __future__ import annotations

import threading
import time

SAMPLE_RATE = 24000
BYTES_PER_SECOND = SAMPLE_RATE * 2


class Underrun:
	def __init__(self, at: float, length: float, sinceLastFeed: bool) -> None:
		self.at = at
		self.length = length
		self.sinceLastFeed = sinceLastFeed

	def __repr__(self) -> str:
		return f"Underrun(at={self.at:.3f}s, length={self.length * 1000:.0f}ms)"


class SimPlayer:
	def __init__(self) -> None:
		self._lock = threading.RLock()
		self._start = time.monotonic()
		self._streamEnd = 0.0
		self._pending: list[tuple[float, object]] = []
		self._stop = threading.Event()
		self.underruns: list[Underrun] = []
		self.fedSeconds = 0.0
		self.feedCount = 0
		self.syncCount = 0
		self.idleCount = 0
		self.stopCount = 0
		self._timer = threading.Thread(target=self._run, name="simPlayer", daemon=True)
		self._timer.start()

	def _now(self) -> float:
		return time.monotonic() - self._start

	def _run(self) -> None:
		while not self._stop.is_set():
			due = []
			with self._lock:
				now = self._now()
				keep = []
				for when, callback in self._pending:
					(due if when <= now else keep).append((when, callback))
				self._pending = keep
			for _when, callback in due:
				try:
					callback()
				except Exception as exc:
					print(f"  ! onDone raised: {exc!r}")
			time.sleep(0.002)

	def feed(self, data, size=None, onDone=None) -> None:
		length = (len(data) if size is None else size) / BYTES_PER_SECOND
		with self._lock:
			now = self._now()
			if self._streamEnd < now:
				silence = now - self._streamEnd
				if self.feedCount and silence > 0.005:
					self.underruns.append(Underrun(self._streamEnd, silence, True))
				self._streamEnd = now
			self._streamEnd += length
			self.fedSeconds += length
			self.feedCount += 1
			if onDone is not None:
				self._pending.append((self._streamEnd, onDone))

	def sync(self) -> None:
		self.syncCount += 1
		while True:
			with self._lock:
				remaining = self._streamEnd - self._now()
			if remaining <= 0:
				return
			time.sleep(min(remaining, 0.01))

	def idle(self) -> None:
		self.idleCount += 1
		self.sync()

	def stop(self) -> None:
		with self._lock:
			self.stopCount += 1
			self._streamEnd = 0.0
			self._pending.clear()

	def pause(self, switch: bool) -> None:
		pass

	def close(self) -> None:
		self._stop.set()

	def setVolume(self, **kwargs) -> None:
		pass

	def report(self) -> str:
		lines = [
			f"fed {self.fedSeconds:.2f}s of audio in {self.feedCount} chunks, "
			f"sync={self.syncCount} idle={self.idleCount} stop={self.stopCount}",
		]
		if not self.underruns:
			lines.append("no underruns: playback was continuous")
		else:
			total = sum(u.length for u in self.underruns)
			lines.append(f"{len(self.underruns)} underrun(s), {total * 1000:.0f}ms of silence total:")
			for underrun in self.underruns:
				lines.append(f"    {underrun}")
		return "\n".join(lines)
