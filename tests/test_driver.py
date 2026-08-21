"""Unit tests for the synth driver's pure logic — no NVDA, no engine process.

Run with: python -m pytest tests/test_driver.py      (or: python tests/test_driver.py)
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nvda_stubs

driver = nvda_stubs.load_driver()


SAMPLE_RATE = 24000


def _ms(milliseconds: int) -> bytes:
	return b"\x00" * (SAMPLE_RATE * 2 * milliseconds // 1000)


def _feed_with_lookahead(lookaheadMs: int = 250):
	instance, player = nvda_stubs.new_driver_instance(driver)
	feed = driver._AudioFeed(player, lookaheadMs=lookaheadMs)
	instance._audioFeed = feed
	return instance, player, feed


def _fed_bytes(player) -> int:
	return sum(len(data) for data, _ in player.feeds)


def test_audio_starts_flowing_before_the_utterance_ends():
	"""Playback must not wait for the whole utterance: anything beyond two
	lookahead windows is handed to the device as it arrives."""
	_instance, player, feed = _feed_with_lookahead(250)
	for _ in range(10):
		feed.feed(_ms(100))
	assert _fed_bytes(player) > 0, "long utterances must stream, not buffer"
	assert _fed_bytes(player) <= len(_ms(1000)) - len(_ms(500))


def test_index_is_reported_one_lookahead_window_early():
	"""The whole point: the callback rides a chunk that leaves ~250ms of audio
	behind it, so NVDA sends the next utterance while this one still plays."""
	_instance, player, feed = _feed_with_lookahead(250)
	for _ in range(10):
		feed.feed(_ms(100))
	fired = []
	feed.mark(lambda: fired.append("index"))
	feed.flush()

	assert player.syncCount == 0, "reporting an index must never drain the device"
	assert _fed_bytes(player) == len(_ms(1000)), "all audio must reach the device"
	assert fired == [], "the callback belongs to the device, not to this thread"

	withCallback = [i for i, (_data, onDone) in enumerate(player.feeds) if onDone is not None]
	assert len(withCallback) == 1, "exactly one chunk carries the index"
	trailing = sum(len(data) for data, _ in player.feeds[withCallback[0] + 1 :])
	assert abs(trailing - len(_ms(250))) <= len(_ms(100)), f"expected ~250ms after the index, got {trailing} bytes"


def test_flush_reports_when_the_last_chunk_has_played():
	"""The driver idles the audio device only once everything has drained, and
	learns that from a callback on the very last chunk."""
	_instance, player, feed = _feed_with_lookahead(250)
	for _ in range(10):
		feed.feed(_ms(100))
	feed.mark(lambda: None)
	drained = []
	feed.flush(onDone=lambda: drained.append("drained"))

	assert drained == [], "draining is signalled by the device, not immediately"
	assert player.feeds[-1][1] is not None, "the final chunk must carry the drain callback"
	player.run_done_callbacks()
	assert drained == ["drained"]


def test_short_utterance_still_reports_its_index():
	"""Shorter than one window, so there is nothing to hang the callback on.
	Reporting immediately is slightly early; losing the index would stall
	NVDA's queue outright."""
	_instance, player, feed = _feed_with_lookahead(250)
	feed.feed(_ms(50))
	fired = []
	feed.mark(lambda: fired.append("index"))
	assert fired == ["index"]
	feed.flush()
	assert _fed_bytes(player) == len(_ms(50))


def test_mark_with_no_audio_at_all_still_reports():
	_instance, player, feed = _feed_with_lookahead(250)
	fired = []
	feed.mark(lambda: fired.append("index"))
	assert fired == ["index"]
	assert player.feeds == []


def test_flush_releases_everything_held_back():
	_instance, player, feed = _feed_with_lookahead(250)
	feed.feed(_ms(100))
	feed.flush()
	assert _fed_bytes(player) == len(_ms(100))


def test_reset_drops_held_audio():
	_instance, player, feed = _feed_with_lookahead(250)
	feed.feed(_ms(100))
	feed.reset()
	feed.flush()
	assert player.feeds == [], "held-back audio must not leak into the next utterance"


def test_empty_chunks_are_ignored():
	_instance, player, feed = _feed_with_lookahead(250)
	feed.feed(b"")
	feed.flush()
	assert player.feeds == []


def _segments(text: str, fastFirst: bool = False) -> list[str]:
	instance, _player = nvda_stubs.new_driver_instance(driver)
	return list(instance._iter_text_segments_for_latency(text, fastFirst))


def test_newline_is_not_a_sentence_boundary():
	"""The reported bug: a line break with no punctuation should read straight
	through, the way IBMTTS does, not be split into two utterances (which the
	engine renders with a full-stop-like pause between them)."""
	assert _segments("the quick brown fox\njumped over the lazy dog") == [
		"the quick brown fox\njumped over the lazy dog",
	]


def test_blank_line_is_not_a_sentence_boundary():
	assert _segments("first line\n\nsecond line") == ["first line\n\nsecond line"]


def test_real_punctuation_still_splits():
	assert _segments("First sentence. Second sentence.") == ["First sentence.", "Second sentence."]


def test_long_text_is_split_into_speakable_segments():
	"""Long runs are broken up so the first audio starts sooner, but every
	piece must stay under the hard cap the splitter promises."""
	text = ("word " * 60).strip() + ", and then some more text follows here to push past the limit"
	segments = _segments(text)
	assert len(segments) > 1
	assert all(segment.strip() for segment in segments)
	assert max(len(segment) for segment in segments) <= driver._CLAUSE_MAX_CHARS


def test_splitting_preserves_all_words():
	text = " ".join(f"word{i}" for i in range(200)) + "."
	segments = _segments(text)
	rejoined = " ".join(segments)
	assert rejoined.split() == text.split()


def test_empty_and_whitespace_only_text_yields_nothing():
	assert _segments("") == []
	assert _segments("   \n\t  ") == []


def test_sequence_joins_adjacent_strings_into_one_utterance():
	"""NVDA hands over a sequence of fragments; joining them before splitting
	is what keeps a line break inside one utterance."""
	instance, _player = nvda_stubs.new_driver_instance(driver)
	instance._speech_options = lambda rate, pitch, volume, voice=None: {"voiceId": voice or "v"}
	chunks = list(
		instance._iter_speech_chunks(["first line", "\n", "second line"], "v", 50, 50, 100, threading.Event()),
	)
	texts = [payload[0] for kind, payload in chunks if kind == "text"]
	assert texts == ["first line\nsecond line"]


def test_index_commands_are_emitted_in_order():
	commands = sys.modules["speech.commands"]
	instance, _player = nvda_stubs.new_driver_instance(driver)
	instance._speech_options = lambda rate, pitch, volume, voice=None: {"voiceId": voice or "v"}
	sequence = ["Hello.", commands.IndexCommand(1), "World.", commands.IndexCommand(2)]
	kinds = [
		(kind, payload if kind != "text" else payload[0])
		for kind, payload in instance._iter_speech_chunks(sequence, "v", 50, 50, 100, threading.Event())
	]
	assert kinds == [("text", "Hello."), ("index", 1), ("text", "World."), ("index", 2)]


def test_cancelled_sequence_stops_early():
	instance, _player = nvda_stubs.new_driver_instance(driver)
	instance._speech_options = lambda rate, pitch, volume, voice=None: {"voiceId": voice or "v"}
	cancelEvent = threading.Event()
	cancelEvent.set()
	assert list(instance._iter_speech_chunks(["anything"], "v", 50, 50, 100, cancelEvent)) == []


def _run() -> int:
	failures = 0
	for name, func in sorted(globals().items()):
		if not name.startswith("test_") or not callable(func):
			continue
		try:
			func()
		except Exception as exc:
			failures += 1
			print(f"FAIL {name}: {exc.__class__.__name__}: {exc}")
		else:
			print(f"ok   {name}")
	print(f"\n{failures} failure(s)")
	return 1 if failures else 0


if __name__ == "__main__":
	raise SystemExit(_run())
