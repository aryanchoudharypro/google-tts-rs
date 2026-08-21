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


def test_feed_holds_one_chunk_back():
	"""Audio is fed as it streams, one chunk behind, so a mark always has a
	real chunk to attach its callback to."""
	instance, player = nvda_stubs.new_driver_instance(driver)
	feed = instance._audioFeed
	feed.feed(b"aa")
	assert player.feeds == [], "first chunk should be held back"
	feed.feed(b"bb")
	assert [data for data, _ in player.feeds] == [b"aa"]
	feed.feed(b"cc")
	assert [data for data, _ in player.feeds] == [b"aa", b"bb"]
	feed.flush()
	assert [data for data, _ in player.feeds] == [b"aa", b"bb", b"cc"]


def test_mark_attaches_callback_without_draining():
	"""The whole point: an index is reported via WavePlayer's onDone rather
	than by blocking on sync(), which is what used to stall NVDA's pipeline
	and leave a full-stop-sized gap at every utterance boundary."""
	instance, player = nvda_stubs.new_driver_instance(driver)
	feed = instance._audioFeed
	fired = []
	feed.feed(b"aa")
	feed.feed(b"bb")
	feed.mark(lambda: fired.append("index"))

	assert player.syncCount == 0, "marking an index must not drain the device"
	assert [data for data, _ in player.feeds] == [b"aa", b"bb"]
	assert player.feeds[-1][1] is not None, "callback must ride on the last chunk before the mark"
	assert fired == [], "callback fires from the audio device, not immediately"

	player.run_done_callbacks()
	assert fired == ["index"]


def test_mark_with_nothing_pending_reports_immediately():
	"""A swallowed index would stall NVDA's queue outright, so when there is
	no audio to attach to, report right away rather than dropping it."""
	instance, player = nvda_stubs.new_driver_instance(driver)
	fired = []
	instance._audioFeed.mark(lambda: fired.append("index"))
	assert fired == ["index"]
	assert player.feeds == []


def test_reset_drops_held_audio():
	instance, player = nvda_stubs.new_driver_instance(driver)
	instance._audioFeed.feed(b"aa")
	instance._audioFeed.reset()
	instance._audioFeed.flush()
	assert player.feeds == [], "held-back audio must not leak into the next utterance"


def test_empty_chunks_are_ignored():
	instance, player = nvda_stubs.new_driver_instance(driver)
	instance._audioFeed.feed(b"")
	instance._audioFeed.flush()
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
