from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
ENGINE_VERSION = "20260625.1"
ENGINE_DIR = BASE_DIR / "WasmTtsEngine" / ENGINE_VERSION
CATALOG_PATH = ENGINE_DIR / "voices.json"


@dataclass(frozen=True)
class VoicePackage:
	id: str
	fileId: str
	url: str
	sha256Checksum: str
	compressedSize: int
	remote: bool
	speakers: tuple[dict[str, str], ...]
	dependentVoiceId: str | None = None

	@property
	def fileName(self) -> str:
		return f"{self.id}.zvoice"

	@property
	def language(self) -> str:
		return package_id_to_language(self.id)

	@property
	def displayName(self) -> str:
		return f"{self.language} ({len(self.speakers)} voices)"


@dataclass(frozen=True)
class Speaker:
	id: str
	name: str
	language: str
	packageId: str
	speaker: str
	gender: str


def package_id_to_language(packageId: str) -> str:
	match = re.match(r"^([a-z]{2,3})-([a-z]{2})(?:-|$)", packageId, re.I)
	if not match:
		return packageId
	return f"{match.group(1).lower()}-{match.group(2).upper()}"


def _safe_str(value: Any, default: str = "") -> str:
	if value is None:
		return default
	return str(value)


class VoiceCatalog:
	def __init__(self, packages: list[VoicePackage]) -> None:
		self.packages = sorted(packages, key=lambda pkg: (pkg.language.lower(), pkg.id.lower()))
		self._packageById = {package.id: package for package in self.packages}
		self.speakers = self._build_speakers()
		self._speakerById = {speaker.id: speaker for speaker in self.speakers}

	@classmethod
	def load(cls, path: Path | None = None) -> "VoiceCatalog":
		catalogPath = path or CATALOG_PATH
		raw = json.loads(catalogPath.read_text(encoding="utf-8"))
		packages: list[VoicePackage] = []
		for item in raw:
			if not isinstance(item, dict):
				continue
			speakers = item.get("speakers")
			if not isinstance(speakers, list):
				speakers = []
			packages.append(
				VoicePackage(
					id=_safe_str(item.get("id")),
					fileId=_safe_str(item.get("fileId")),
					url=_safe_str(item.get("url")),
					sha256Checksum=_safe_str(item.get("sha256Checksum")),
					compressedSize=int(item.get("compressedSize") or 0),
					remote=bool(item.get("remote", True)),
					speakers=tuple(s for s in speakers if isinstance(s, dict)),
					dependentVoiceId=(_safe_str(item.get("dependentVoiceId")) or None),
				),
			)
		return cls(packages)

	def _build_speakers(self) -> list[Speaker]:
		speakers: list[Speaker] = []
		seen: set[str] = set()
		for package in self.packages:
			for rawSpeaker in package.speakers:
				speakerCode = _safe_str(rawSpeaker.get("speaker"))
				name = _safe_str(rawSpeaker.get("name"), speakerCode or package.id)
				gender = _safe_str(rawSpeaker.get("gender"))
				speakerId = f"{package.id}:{speakerCode or name}"
				if speakerId in seen:
					continue
				seen.add(speakerId)
				speakers.append(
					Speaker(
						id=speakerId,
						name=name,
						language=package.language,
						packageId=package.id,
						speaker=speakerCode,
						gender=gender,
					),
				)
		return speakers

	def package_for_voice(self, voiceId: str) -> VoicePackage:
		speaker = self._speakerById[voiceId]
		return self._packageById[speaker.packageId]

	def speaker_for_voice(self, voiceId: str) -> Speaker:
		return self._speakerById[voiceId]

	def package_by_id(self, packageId: str) -> VoicePackage:
		return self._packageById[packageId]

	def language_for_voice(self, voiceId: str) -> str:
		return self._speakerById[voiceId].language

	def to_runtime_json(self) -> str:
		runtimePackages: list[dict[str, Any]] = []
		for package in self.packages:
			runtimePackages.append(
				{
					"id": package.id,
					"fileId": package.fileId,
					"url": f"/{package.fileName}",
					"sha256Checksum": package.sha256Checksum,
					"compressedSize": package.compressedSize,
					"speakers": list(package.speakers),
					"remote": False,
				},
			)
		return json.dumps(runtimePackages, ensure_ascii=False)

	def voices_by_language(self) -> "OrderedDict[str, list[Speaker]]":
		grouped: OrderedDict[str, list[Speaker]] = OrderedDict()
		for speaker in self.speakers:
			grouped.setdefault(speaker.language, []).append(speaker)
		return grouped
