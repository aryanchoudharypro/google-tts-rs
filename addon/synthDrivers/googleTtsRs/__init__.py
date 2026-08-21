from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Iterator
from contextlib import suppress
import re
import threading
from typing import Any

import addonHandler
import config
import synthDriverHandler
import wx
from logHandler import log
from nvwave import WavePlayer
from speech.commands import BreakCommand, IndexCommand, LangChangeCommand, PitchCommand, RateCommand, VolumeCommand
from synthDriverHandler import VoiceInfo, synthDoneSpeaking, synthIndexReached

from .bridge import BridgeCancelled, NativeTtsBridge, SAMPLE_RATE
from .catalog import VoiceCatalog
from . import voice_store


addonHandler.initTranslation()


_CLAUSE_TARGET_CHARS = 220
_CLAUSE_MAX_CHARS = 360
_FAST_FIRST_CLAUSE_TARGET_CHARS = 100
_FAST_FIRST_CLAUSE_MAX_CHARS = 160
_FAST_FIRST_CLAUSE_MIN_CHARS = 30
_SHORT_CACHE_MAX_CHARS = 200
_SHORT_CACHE_MAX_ITEMS = 256
_SHORT_CACHE_MAX_BYTES = 12 * 1024 * 1024
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?;:])\s+")
_CLAUSE_BOUNDARY_RE = re.compile(r"[,;:]\s+")
_SpeechRequest = tuple[list[Any], str, int, int, int, threading.Event]


class _AudioFeed:
	"""Feeds PCM to NVDA's WavePlayer while keeping index reporting accurately
	timed *without* draining the audio device.

	Why this exists: NVDA's speech manager pushes the next utterance to the
	synth the moment the previous utterance's end-of-utterance index is
	reported (`_handleIndex` -> `_pushNextSpeech`), not when
	`synthDoneSpeaking` fires. A driver that blocks on `WavePlayer.sync()`
	before reporting an index therefore reports it only once the audio has
	fully drained, which stalls the pipeline and leaves an audible
	full-stop-like gap at every utterance boundary — most noticeable when
	reading continuously across lines that have no sentence punctuation.

	`WavePlayer.feed()` takes an `onDone` callback that fires when that
	specific chunk finishes playing; this is how NVDA's own oneCore and
	eSpeak drivers report indexes. Reporting an index accurately therefore
	means attaching the callback to the last chunk of audio *before* it, so
	this class holds each chunk back until the next one arrives and hands
	the held-back chunk to `feed()` together with the callback when a mark
	lands. Holding one chunk back costs roughly one chunk of extra startup
	latency (~40ms at this engine's chunk size) and buys gapless playback
	across utterances.
	"""

	def __init__(self, player: Any) -> None:
		self._player = player
		self._lock = threading.Lock()
		self._pending: bytes | None = None

	def feed(self, pcm: bytes) -> None:
		if not pcm:
			return
		with self._lock:
			previous = self._pending
			self._pending = pcm
		if previous:
			self._player.feed(previous)

	def mark(self, onDone: "Any") -> None:
		"""Report a position in the stream once the audio fed before it has
		actually been played."""
		with self._lock:
			previous = self._pending
			self._pending = None
		if previous:
			self._player.feed(previous, onDone=onDone)
			return
		onDone()

	def flush(self) -> None:
		with self._lock:
			previous = self._pending
			self._pending = None
		if previous:
			self._player.feed(previous)

	def reset(self) -> None:
		"""Drop held-back audio on cancellation. `WavePlayer.stop()` already
		discards its own queued audio and pending onDone callbacks, so this
		just makes sure we don't feed a stale chunk into the next utterance."""
		with self._lock:
			self._pending = None


class SynthDriver(synthDriverHandler.SynthDriver):
	name = "googleTtsRs"
	description = "Google TTS RS"
	supportedSettings = (
		synthDriverHandler.SynthDriver.VoiceSetting(),
		synthDriverHandler.SynthDriver.RateSetting(),
		synthDriverHandler.SynthDriver.RateBoostSetting(),
		synthDriverHandler.SynthDriver.PitchSetting(),
		synthDriverHandler.SynthDriver.VolumeSetting(),
	)
	supportedCommands = {
		BreakCommand,
		IndexCommand,
		LangChangeCommand,
		RateCommand,
		PitchCommand,
		VolumeCommand,
	}
	supportedNotifications = {synthIndexReached, synthDoneSpeaking}
	cachePropertiesByDefault = False

	@classmethod
	def check(cls) -> bool:
		return True

	def __init__(self) -> None:
		super().__init__()
		fullCatalog = VoiceCatalog.load()
		installedPackages = voice_store.installed_packages(fullCatalog)
		installedIds = {package.id for package in installedPackages}
		installedPackages = [
			package
			for package in installedPackages
			if not package.dependentVoiceId or package.dependentVoiceId in installedIds
		]
		if not installedPackages:
			wx.CallAfter(self._prompt_for_voice_install)
			raise RuntimeError("No Google TTS voice packages are installed.")
		self.catalog = VoiceCatalog(installedPackages)
		if not self.catalog.speakers:
			raise RuntimeError("Installed Google TTS voice packages do not contain usable voices.")
		self.availableVoices = self._build_available_voices()
		self.availableLanguages = {speaker.language for speaker in self.catalog.speakers}
		self._bridge = NativeTtsBridge(self.catalog)
		self._player = WavePlayer(channels=1, samplesPerSec=SAMPLE_RATE, bitsPerSample=16)
		self._audioFeed = _AudioFeed(self._player)
		self._speechCondition = threading.Condition()
		self._speechQueue: deque[_SpeechRequest] = deque()
		self._activeCancelEvent: threading.Event | None = None
		self._shutdownEvent = threading.Event()
		self._cacheLock = threading.RLock()
		self._shortAudioCache: OrderedDict[tuple[Any, ...], bytes] = OrderedDict()
		self._shortAudioCacheBytes = 0
		self._worker = threading.Thread(
			name="googleTtsRs.speech",
			target=self._speech_loop,
			daemon=True,
		)
		self._worker.start()
		self.__voice = self._initial_voice()
		self._rate = 50
		self._rateBoost = False
		self._pitch = 50
		self._volume = 100
		self._warmupThread: threading.Thread | None = None
		self._warmupCancelEvent = threading.Event()
		self._warm_current_voice_async()

	def _prompt_for_voice_install(self) -> None:
		def open_when_ready(retries: int = 200) -> None:
			if retries <= 0:
				return
			for win in wx.GetTopLevelWindows():
				if not win.IsShown():
					continue
				clsName = win.__class__.__name__
				if "MessageDialog" in clsName:
					wx.CallLater(150, open_when_ready, retries - 1)
					return
				if isinstance(win, wx.Dialog) and getattr(win, "IsModal", lambda: False)():
					if not any(known in clsName for known in ("SettingsDialog", "SynthesizerDialog", "VoiceManagerDialog")):
						wx.CallLater(150, open_when_ready, retries - 1)
						return
			try:
				from globalPlugins.googleTtsRs import open_voice_manager_download_tab

				open_voice_manager_download_tab()
			except Exception:
				log.exception("Could not open Google TTS voice manager.", exc_info=True)

		wx.CallLater(250, open_when_ready)

	def terminate(self) -> None:
		self.cancel()
		self._shutdownEvent.set()
		with self._speechCondition:
			self._speechCondition.notify_all()
		with suppress(Exception):
			self._bridge.terminate()
		with suppress(Exception):
			self._player.close()

	def speak(self, speechSequence: list[Any]) -> None:
		with suppress(Exception):
			self._warmupCancelEvent.set()
		sequence = list(speechSequence)
		cancelEvent = threading.Event()
		voice = self.__voice
		rate = self._rate
		pitch = self._pitch
		volume = self._volume
		with self._speechCondition:
			if self._shutdownEvent.is_set():
				return
			self._speechQueue.append((sequence, voice, rate, pitch, volume, cancelEvent))
			self._speechCondition.notify()

	def cancel(self) -> None:
		with self._speechCondition:
			if self._activeCancelEvent is not None:
				self._activeCancelEvent.set()
			for request in self._speechQueue:
				request[-1].set()
			self._speechQueue.clear()
			self._speechCondition.notify_all()
		with suppress(Exception):
			self._warmupCancelEvent.set()
		with suppress(Exception):
			self._player.stop()
		with suppress(Exception):
			self._audioFeed.reset()
		with suppress(Exception):
			self._bridge.cancel_current()

	def pause(self, switch: bool) -> None:
		self._player.pause(switch)

	def _build_available_voices(self) -> "OrderedDict[str, VoiceInfo]":
		voices: OrderedDict[str, VoiceInfo] = OrderedDict()
		for speaker in self.catalog.speakers:
			label = f"{speaker.name} ({speaker.language})"
			voices[speaker.id] = VoiceInfo(speaker.id, label, speaker.language)
		return voices

	def _initial_voice(self) -> str:
		try:
			configured = config.conf["speech"][self.name]["voice"]
			if configured in self.availableVoices:
				return configured
		except Exception:
			pass
		for speaker in self.catalog.speakers:
			if speaker.language == "en-US":
				return speaker.id
		return next(iter(self.availableVoices))

	def _iter_speech_chunks(
		self,
		speechSequence: list[Any],
		voice: str,
		rate: int,
		pitch: int,
		volume: int,
		cancelEvent: threading.Event,
	) -> Iterator[tuple[str, Any]]:
		textParts: list[str] = []
		firstTextSegment = True
		activeVoice = voice

		def flush_text() -> Iterator[tuple[str, Any]]:
			nonlocal firstTextSegment
			text = "".join(textParts).strip()
			textParts.clear()
			if not text:
				return
			options = self._speech_options(rate, pitch, volume, activeVoice)
			for segment in self._iter_text_segments_for_latency(text, firstTextSegment):
				if cancelEvent.is_set():
					return
				firstTextSegment = False
				yield ("text", (segment, options))

		for item in speechSequence:
			if cancelEvent.is_set():
				return
			itemType = type(item)
			if itemType is str:
				textParts.append(item)
			elif itemType is BreakCommand:
				yield from flush_text()
				if cancelEvent.is_set():
					return
				yield ("break", max(0, int(item.time)))
			elif itemType is IndexCommand:
				yield from flush_text()
				if cancelEvent.is_set():
					return
				yield ("index", item.index)
			elif itemType is LangChangeCommand:
				yield from flush_text()
				if cancelEvent.is_set():
					return
				activeVoice = self._voice_for_language(item.lang, voice)
			elif itemType is RateCommand:
				yield from flush_text()
				if cancelEvent.is_set():
					return
				rate = int(item.newValue)
			elif itemType is PitchCommand:
				yield from flush_text()
				if cancelEvent.is_set():
					return
				pitch = int(item.newValue)
			elif itemType is VolumeCommand:
				yield from flush_text()
				if cancelEvent.is_set():
					return
				volume = int(item.newValue)
		yield from flush_text()

	def _split_text_for_latency(self, text: str) -> list[str]:
		return list(self._iter_text_segments_for_latency(text, False))

	def _iter_text_segments_for_latency(self, text: str, fastFirstSegment: bool) -> Iterator[str]:
		text = text.strip()
		if not text:
			return
		useFastFirstSegment = fastFirstSegment
		for sentence in self._iter_sentence_units(text):
			if useFastFirstSegment and len(sentence) > _CLAUSE_TARGET_CHARS:
				cut = self._find_punctuation_cut(
					sentence,
					_FAST_FIRST_CLAUSE_TARGET_CHARS,
					_FAST_FIRST_CLAUSE_MAX_CHARS,
					_FAST_FIRST_CLAUSE_MIN_CHARS,
				)
				if cut <= 0 and len(sentence) > _CLAUSE_MAX_CHARS:
					cut = self._find_hard_word_cut(sentence, _FAST_FIRST_CLAUSE_MAX_CHARS)
				if 0 < cut < len(sentence):
					segment = sentence[:cut].strip()
					if segment:
						yield segment
					sentence = sentence[cut:].strip()
			useFastFirstSegment = False
			if not sentence:
				continue
			if len(sentence) > _CLAUSE_TARGET_CHARS:
				for segment in self._iter_long_text_segments(sentence):
					yield segment
				continue
			yield sentence

	def _iter_sentence_units(self, text: str) -> Iterator[str]:
		start = 0
		for match in _SENTENCE_BOUNDARY_RE.finditer(text):
			sentence = text[start : match.start()].strip()
			if sentence:
				yield sentence
			start = match.end()
		sentence = text[start:].strip()
		if sentence:
			yield sentence

	def _iter_long_text_segments(self, text: str) -> Iterator[str]:
		remaining = text.strip()
		while remaining:
			if len(remaining) <= _CLAUSE_TARGET_CHARS:
				yield remaining
				return
			cut = self._find_punctuation_cut(remaining, _CLAUSE_TARGET_CHARS, _CLAUSE_MAX_CHARS)
			if cut <= 0:
				if len(remaining) <= _CLAUSE_MAX_CHARS:
					yield remaining
					return
				cut = self._find_hard_word_cut(remaining, _CLAUSE_MAX_CHARS)
			segment = remaining[:cut].strip()
			if segment:
				yield segment
			remaining = remaining[cut:].strip()

	def _find_punctuation_cut(self, text: str, targetChars: int, maxChars: int, minChars: int | None = None) -> int:
		best = -1
		minChars = targetChars // 2 if minChars is None else minChars
		for match in _CLAUSE_BOUNDARY_RE.finditer(text):
			cut = match.start() + 1
			if cut < minChars or cut > maxChars:
				continue
			if best < 0 or abs(cut - targetChars) <= abs(best - targetChars):
				best = cut
		return best

	def _find_hard_word_cut(self, text: str, maxChars: int) -> int:
		if len(text) <= maxChars:
			return len(text)
		window = text[:maxChars]
		cut = window.rfind(" ")
		if cut > max(1, _CLAUSE_TARGET_CHARS // 2):
			return cut
		return maxChars

	def _speech_loop(self) -> None:
		while not self._shutdownEvent.is_set():
			with self._speechCondition:
				while not self._speechQueue and not self._shutdownEvent.is_set():
					self._speechCondition.wait()
				if self._shutdownEvent.is_set():
					return
				request = self._speechQueue.popleft()
				self._activeCancelEvent = request[-1]
			try:
				self._speak_worker(*request)
			finally:
				with self._speechCondition:
					if self._activeCancelEvent is request[-1]:
						self._activeCancelEvent = None

	def _speak_worker(
		self,
		speechSequence: list[Any],
		voice: str,
		rate: int,
		pitch: int,
		volume: int,
		cancelEvent: threading.Event,
	) -> None:
		try:
			for kind, payload in self._iter_speech_chunks(
				speechSequence,
				voice,
				rate,
				pitch,
				volume,
				cancelEvent,
			):
				if cancelEvent.is_set():
					return
				if kind == "text":
					text, options = payload
					self._speak_text(text, options, cancelEvent)
				elif kind == "break":
					self._feed_silence(payload)
				elif kind == "index":
					self._mark_index(payload, cancelEvent)
			if not cancelEvent.is_set():
				self._finish_request_audio()
			if not cancelEvent.is_set():
				synthDoneSpeaking.notify(synth=self)
		except BridgeCancelled:
			log.debug("Google TTS speech cancelled.")
		except Exception:
			log.exception("Google TTS speech failed.", exc_info=True)
			if not cancelEvent.is_set():
				synthDoneSpeaking.notify(synth=self)

	def _speak_text(self, text: str, options: dict[str, Any], cancelEvent: threading.Event) -> None:
		cacheKey = self._short_cache_key(text, options)
		if cacheKey is not None:
			cached = self._get_cached_audio(cacheKey)
			if cached is not None:
				if not cancelEvent.is_set():
					self._feed_audio(cached)
				return
		audioParts: list[bytes] = []

		def on_audio(pcm: bytes) -> None:
			if cancelEvent.is_set():
				return
			if cacheKey is not None and pcm:
				audioParts.append(pcm)
			self._feed_audio(pcm)

		self._bridge.speak(text, options, on_audio, cancelEvent)
		if cacheKey is not None and audioParts and not cancelEvent.is_set():
			self._put_cached_audio(cacheKey, b"".join(audioParts))

	def _feed_audio(self, pcm: bytes) -> None:
		if pcm:
			self._audioFeed.feed(pcm)

	def _mark_index(self, index: int, cancelEvent: threading.Event) -> None:
		"""Report an index once the audio preceding it has been played, without
		blocking this thread. Reporting promptly is what lets NVDA hand us the
		next utterance while the current one is still playing."""

		def notify() -> None:
			if not cancelEvent.is_set():
				synthIndexReached.notify(synth=self, index=index)

		self._audioFeed.mark(notify)

	def _has_queued_speech(self) -> bool:
		with self._speechCondition:
			return bool(self._speechQueue)

	def _finish_request_audio(self) -> None:
		self._audioFeed.flush()
		if self._has_queued_speech():
			return
		self._player.idle()

	def _short_cache_key(self, text: str, options: dict[str, Any]) -> tuple[Any, ...] | None:
		if len(text) > _SHORT_CACHE_MAX_CHARS:
			return None
		return (
			text,
			options.get("voiceId"),
			options.get("rate"),
			options.get("pitch"),
			options.get("volume"),
			options.get("outputGain"),
		)

	def _get_cached_audio(self, key: tuple[Any, ...]) -> bytes | None:
		with self._cacheLock:
			audio = self._shortAudioCache.get(key)
			if audio is None:
				return None
			self._shortAudioCache.move_to_end(key)
			return audio

	def _put_cached_audio(self, key: tuple[Any, ...], audio: bytes) -> None:
		if not audio:
			return
		if len(audio) > _SHORT_CACHE_MAX_BYTES:
			return
		with self._cacheLock:
			oldAudio = self._shortAudioCache.pop(key, None)
			if oldAudio is not None:
				self._shortAudioCacheBytes -= len(oldAudio)
			self._shortAudioCache[key] = audio
			self._shortAudioCacheBytes += len(audio)
			self._shortAudioCache.move_to_end(key)
			while (
				len(self._shortAudioCache) > _SHORT_CACHE_MAX_ITEMS
				or self._shortAudioCacheBytes > _SHORT_CACHE_MAX_BYTES
			):
				_, removedAudio = self._shortAudioCache.popitem(last=False)
				self._shortAudioCacheBytes -= len(removedAudio)

	def _feed_silence(self, milliseconds: int) -> None:
		if milliseconds <= 0:
			return
		frameCount = int(SAMPLE_RATE * milliseconds / 1000)
		self._audioFeed.feed(b"\x00\x00" * frameCount)

	def _speech_options(self, rate: int, pitch: int, volume: int, voice: str | None = None) -> dict[str, Any]:
		speaker = self.catalog.speaker_for_voice(voice or self.__voice)
		return {
			"voiceId": speaker.id,
			"voiceName": speaker.name,
			"lang": speaker.language,
			"rate": self._rate_to_engine(rate),
			"pitch": self._pitch_to_engine(pitch),
			"volume": max(0.0, min(1.0, volume / 100.0)),
			"outputGain": max(0.0, min(2.0, volume / 50.0)),
		}

	def _voice_for_language(self, lang: str | None, fallbackVoice: str) -> str:
		if not lang:
			return fallbackVoice
		normalizedLang = self._normalize_language(lang)
		if not normalizedLang:
			return fallbackVoice
		fallbackSpeaker = self.catalog.speaker_for_voice(fallbackVoice)
		if self._normalize_language(fallbackSpeaker.language) == normalizedLang:
			return fallbackVoice
		for speaker in self.catalog.speakers:
			if self._normalize_language(speaker.language) == normalizedLang:
				return speaker.id
		rootLang = normalizedLang.split("-", 1)[0]
		if self._normalize_language(fallbackSpeaker.language).split("-", 1)[0] == rootLang:
			return fallbackVoice
		for speaker in self.catalog.speakers:
			if self._normalize_language(speaker.language).split("-", 1)[0] == rootLang:
				return speaker.id
		return fallbackVoice

	def _normalize_language(self, lang: str | None) -> str:
		return str(lang or "").replace("_", "-").lower()

	def _warm_current_voice_async(self) -> None:
		options = self._speech_options(self._rate, self._pitch, 0)
		with suppress(Exception):
			self._warmupCancelEvent.set()
		cancelEvent = threading.Event()
		self._warmupCancelEvent = cancelEvent

		def warm() -> None:
			try:
				self._bridge.preload_voice(options, cancelEvent)
			except BridgeCancelled:
				log.debug("Google TTS voice preload cancelled.")
			except Exception:
				log.debug("Google TTS voice preload failed.", exc_info=True)

		thread = threading.Thread(name="googleTtsRs.preload", target=warm, daemon=True)
		self._warmupThread = thread
		thread.start()

	def _rate_to_engine(self, value: int) -> float:
		percent = max(0, min(100, value)) / 100.0
		rate = 0.35 + (2.0 - 0.35) * percent
		if self._rateBoost:
			rate *= 2
		return round(max(0.1, min(10.0, rate)), 3)

	def _pitch_to_engine(self, pitch: int) -> float:
		pitchSemitones = -12.0 + 24.0 * max(0, min(100, pitch)) / 100.0
		return round(max(0.1, min(3.0, 1.0 + pitchSemitones / 20.0)), 3)

	def _get_voice(self) -> str:
		return self.__voice

	def _set_voice(self, value: str) -> None:
		if value not in self.availableVoices:
			value = next(iter(self.availableVoices))
		self.__voice = value
		self._warm_current_voice_async()

	def _get_language(self) -> str:
		return self.catalog.language_for_voice(self.__voice)

	def _get_rate(self) -> int:
		return self._rate

	def _set_rate(self, value: int) -> None:
		self._rate = max(0, min(100, int(value)))

	def _get_rateBoost(self) -> bool:
		return self._rateBoost

	def _set_rateBoost(self, value: bool) -> None:
		self._rateBoost = bool(value)

	def _get_pitch(self) -> int:
		return self._pitch

	def _set_pitch(self, value: int) -> None:
		self._pitch = max(0, min(100, int(value)))

	def _get_volume(self) -> int:
		return self._volume

	def _set_volume(self, value: int) -> None:
		self._volume = max(0, min(100, int(value)))
		with suppress(Exception):
			self._player.setVolume(all=1.0)
