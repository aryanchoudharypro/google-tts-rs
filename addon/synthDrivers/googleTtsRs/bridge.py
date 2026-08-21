from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import threading
import time
from typing import Any

try:
	from logHandler import log
except Exception:
	import logging

	log = logging.getLogger("googleTtsRs")

from .catalog import ENGINE_DIR, VoiceCatalog
from . import voice_store


BASE_DIR = Path(__file__).resolve().parent
SAMPLE_RATE = 24000
RECV_POLL_TIMEOUT = 0.005
STARTUP_POLL_INTERVAL = 0.05

TAG_INSTALL_VOICE = 1
TAG_INIT = 2
TAG_SPEAK = 3
TAG_ERROR = 0x80
TAG_ACK = 0x81
TAG_AUDIO_CHUNK = 0x82
TAG_SPEAK_DONE = 0x83
TAG_SPEAK_CANCELLED = 0x84

SERVER_STARTUP_TIMEOUT = 30.0

AudioCallback = Callable[[bytes], None]


class BridgeError(Exception):
	pass


class BridgeCancelled(Exception):
	pass


def _raise_if_cancelled(cancelEvent: threading.Event | None) -> None:
	if cancelEvent is not None and cancelEvent.is_set():
		raise BridgeCancelled()


class NativeTtsBridge:
	def __init__(self, catalog: VoiceCatalog | None = None) -> None:
		self.catalog = catalog or VoiceCatalog.load()
		self._process: subprocess.Popen[str] | None = None
		self._mainSock: socket.socket | None = None
		self._controlSock: socket.socket | None = None
		self._lock = threading.RLock()
		self._currentPackageId: str | None = None
		self._runtimeBusy = False

	@staticmethod
	def _find_server_exe() -> str | None:
		candidates = [
			os.environ.get("GTTS_NATIVE_SERVER_EXE", ""),
			str(BASE_DIR / "native" / "server.exe"),
			str(BASE_DIR.parents[2] / "engine" / "target" / "release" / "server.exe"),
		]
		for candidate in candidates:
			if candidate and Path(candidate).is_file():
				return candidate
		return None

	def ensure_connection(self, cancelEvent: threading.Event | None = None) -> None:
		with self._lock:
			_raise_if_cancelled(cancelEvent)
			if self._process is not None and self._process.poll() is None and self._mainSock is not None:
				return
			self._start_server(cancelEvent)

	def _start_server(self, cancelEvent: threading.Event | None = None) -> None:
		_raise_if_cancelled(cancelEvent)
		exePath = self._find_server_exe()
		if not exePath:
			raise BridgeError("The Google TTS engine (server.exe) was not found.")
		env = dict(os.environ)
		env.setdefault("GTTS_WASM_PATH", str(ENGINE_DIR / "bindings_main.wasm"))
		env.setdefault("GTTS_VFS_ROOT", str(voice_store.data_root() / "engineVfs"))
		self._process = subprocess.Popen(
			[exePath],
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
			env=env,
			text=True,
			creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
		)
		try:
			mainPort, controlPort = self._read_ready_line(cancelEvent)
			self._mainSock = socket.create_connection(("127.0.0.1", mainPort), timeout=15)
			self._mainSock.settimeout(RECV_POLL_TIMEOUT)
			self._controlSock = socket.create_connection(("127.0.0.1", controlPort), timeout=15)
		except BridgeCancelled:
			self._close_sockets()
			with suppress(Exception):
				self._process.terminate()
				self._process.wait(timeout=2)
			self._process = None
			raise

	def _read_ready_line(self, cancelEvent: threading.Event | None = None) -> tuple[int, int]:
		result: dict[str, str] = {}

		def read_line() -> None:
			assert self._process is not None and self._process.stdout is not None
			result["line"] = self._process.stdout.readline()

		reader = threading.Thread(name="googleTtsRs.ready", target=read_line, daemon=True)
		reader.start()
		deadline = time.monotonic() + SERVER_STARTUP_TIMEOUT
		while reader.is_alive():
			_raise_if_cancelled(cancelEvent)
			if self._process is not None and self._process.poll() is not None:
				raise BridgeError(f"The Google TTS engine exited before it was ready: {self._process.returncode}")
			if time.monotonic() > deadline:
				raise BridgeError("Timed out waiting for the Google TTS engine to start.")
			reader.join(STARTUP_POLL_INTERVAL)
		line = result.get("line", "").strip()
		parts = line.split()
		if len(parts) != 3 or parts[0] != "READY":
			raise BridgeError(f"The Google TTS engine sent an unexpected startup line: {line!r}")
		return int(parts[1]), int(parts[2])

	def _raise_unless_installed(self, package: Any) -> None:
		if not voice_store.is_package_installed(package):
			raise BridgeError(f"Voice package is not installed: {package.id}")
		dependency = self._dependency_package(package)
		if dependency is not None and not voice_store.is_package_installed(dependency):
			raise BridgeError(f"Voice package {package.id} also needs {dependency.id}, which is not installed.")

	def _dependency_package(self, package: Any) -> Any | None:
		dependentVoiceId = getattr(package, "dependentVoiceId", None)
		if not dependentVoiceId:
			return None
		try:
			return self.catalog.package_by_id(dependentVoiceId)
		except KeyError:
			raise BridgeError(
				f"Voice package {package.id} depends on {dependentVoiceId}, which is not available.",
			) from None

	def _install_package(self, package: Any, cancelEvent: threading.Event | None = None) -> None:
		self._request(
			TAG_INSTALL_VOICE,
			{"voice_dir": f"/voices/{package.fileId}", "zvoice_path": str(voice_store.package_file(package))},
			cancelEvent,
		)

	def _ensure_voice_ready(self, package: Any, cancelEvent: threading.Event | None = None) -> None:
		if self._currentPackageId == package.id:
			return
		dependency = self._dependency_package(package)
		if dependency is not None:
			self._install_package(dependency, cancelEvent)
		self._install_package(package, cancelEvent)
		self._request(TAG_INIT, {"voice_dir": f"/voices/{package.fileId}"}, cancelEvent)
		self._currentPackageId = package.id

	def _request(self, tag: int, obj: dict[str, Any], cancelEvent: threading.Event | None = None) -> None:
		_raise_if_cancelled(cancelEvent)
		assert self._mainSock is not None
		self._write_frame(self._mainSock, tag, json.dumps(obj, ensure_ascii=False).encode("utf-8"))
		respTag, respPayload = self._read_frame(self._mainSock)
		_raise_if_cancelled(cancelEvent)
		if respTag == TAG_ACK:
			return
		if respTag == TAG_ERROR:
			raise BridgeError(self._error_message(respPayload))
		raise BridgeError(f"Unexpected engine protocol frame tag: {respTag:#x}")

	def preload_voice(self, options: dict[str, Any], cancelEvent: threading.Event | None = None) -> dict[str, Any]:
		package = self.catalog.package_for_voice(str(options["voiceId"]))
		self._raise_unless_installed(package)
		self.ensure_connection(cancelEvent)
		with self._lock:
			self._ensure_voice_ready(package, cancelEvent)
		return {"success": True}

	def speak(
		self,
		text: str,
		options: dict[str, Any],
		onAudio: AudioCallback,
		cancelEvent: threading.Event | None = None,
	) -> dict[str, Any]:
		if not text.strip():
			return {"success": True, "empty": True}
		speaker = self.catalog.speaker_for_voice(str(options["voiceId"]))
		package = self.catalog.package_for_voice(str(options["voiceId"]))
		self._raise_unless_installed(package)
		self.ensure_connection(cancelEvent)

		with self._lock:
			self._ensure_voice_ready(package, cancelEvent)
			_raise_if_cancelled(cancelEvent)
			assert self._mainSock is not None
			request = {
				"text": text,
				"rate": options["rate"],
				"pitch": options["pitch"],
				"speaker": speaker.speaker,
				"gain": options.get("outputGain", options["volume"]),
			}
			self._runtimeBusy = True
			try:
				self._write_frame(self._mainSock, TAG_SPEAK, json.dumps(request, ensure_ascii=False).encode("utf-8"))
				audioChunks = 0
				cancelSent = False

				def onIdle() -> None:
					nonlocal cancelSent
					if not cancelSent and cancelEvent is not None and cancelEvent.is_set():
						self._send_cancel()
						cancelSent = True

				while True:
					tag, payload = self._read_frame(self._mainSock, onIdle)
					if tag == TAG_AUDIO_CHUNK:
						onIdle()
						if cancelSent:
							continue
						audioChunks += 1
						onAudio(payload)
					elif tag == TAG_SPEAK_DONE:
						return {"success": True, "audioChunks": audioChunks}
					elif tag == TAG_SPEAK_CANCELLED:
						raise BridgeCancelled()
					elif tag == TAG_ERROR:
						raise BridgeError(self._error_message(payload))
					else:
						raise BridgeError(f"Unexpected engine protocol frame tag: {tag:#x}")
			finally:
				self._runtimeBusy = False

	def cancel_current(self) -> None:
		if self._runtimeBusy:
			self._send_cancel()

	def _send_cancel(self) -> None:
		sock = self._controlSock
		if sock is None:
			return
		try:
			sock.sendall(b"\x01")
		except Exception:
			log.debug("Could not send the Google TTS cancel signal.", exc_info=True)

	def terminate(self) -> None:
		with self._lock:
			self._close_sockets()
			if self._process is not None and self._process.poll() is None:
				try:
					self._process.terminate()
					self._process.wait(timeout=5)
				except subprocess.TimeoutExpired:
					self._process.kill()
				except Exception:
					pass
			self._process = None
			self._currentPackageId = None

	def _close_sockets(self) -> None:
		for sock in (self._mainSock, self._controlSock):
			if sock is not None:
				with suppress(Exception):
					sock.close()
		self._mainSock = None
		self._controlSock = None

	@staticmethod
	def _error_message(payload: bytes) -> str:
		try:
			return str(json.loads(payload).get("message") or "Google TTS engine error.")
		except (json.JSONDecodeError, UnicodeDecodeError):
			return "Google TTS engine error."

	@staticmethod
	def _write_frame(sock: socket.socket, tag: int, payload: bytes) -> None:
		sock.sendall(bytes([tag]) + struct.pack("<I", len(payload)) + payload)

	@staticmethod
	def _read_frame(sock: socket.socket, onIdle: Callable[[], None] | None = None) -> tuple[int, bytes]:
		def recv_exact(n: int) -> bytes:
			buf = bytearray()
			while len(buf) < n:
				try:
					chunk = sock.recv(n - len(buf))
				except (TimeoutError, socket.timeout):
					if onIdle is not None:
						onIdle()
					continue
				if not chunk:
					raise BridgeError("The Google TTS engine connection closed unexpectedly.")
				buf.extend(chunk)
			return bytes(buf)

		header = recv_exact(5)
		tag = header[0]
		(length,) = struct.unpack("<I", header[1:5])
		payload = recv_exact(length) if length else b""
		return tag, payload
