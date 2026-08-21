"""Import the add-on's synth driver outside NVDA.

The driver module imports a dozen NVDA-internal modules that only exist inside
a running NVDA process. Rather than restructure the add-on to suit the tests,
this installs the smallest stubs that let the module import and its pure logic
(text chunking, audio feeding) be exercised directly. Nothing here pretends to
emulate NVDA's behaviour — the stubs exist to satisfy imports and to record
calls so tests can assert on them.

The driver package itself is loaded under a synthetic package name: Python runs
a package's ``__init__.py`` on any submodule import, and for this add-on the
``__init__.py`` *is* the driver, so ``import synthDrivers.googleTtsRs.bridge``
would drag the whole thing in before the stubs are ready. Building the package
object by hand and loading each module into it with importlib keeps the files
on disk completely unmodified while letting their relative imports resolve.
"""

from __future__ import annotations

import builtins
import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_SYNTH_DIR = REPO_ROOT / "addon" / "synthDrivers" / "googleTtsRs"
PKG_NAME = "googleTtsRs_under_test"


class FakePlayer:
	"""Records what the driver feeds it, in order, so tests can assert on
	chunk boundaries and on when `onDone` callbacks were attached."""

	def __init__(self) -> None:
		self.feeds: list[tuple[bytes, object]] = []
		self.syncCount = 0
		self.idleCount = 0
		self.stopCount = 0

	def feed(self, data, size=None, onDone=None) -> None:
		self.feeds.append((bytes(data), onDone))

	def sync(self) -> None:
		self.syncCount += 1

	def idle(self) -> None:
		self.idleCount += 1
		self.syncCount += 1

	def stop(self) -> None:
		self.stopCount += 1

	def pause(self, switch: bool) -> None:
		pass

	def close(self) -> None:
		pass

	def setVolume(self, **kwargs) -> None:
		pass

	def run_done_callbacks(self) -> list[object]:
		"""Simulate the audio device finishing every fed chunk, in order.
		Returns the callbacks that fired."""
		fired = []
		for _data, onDone in self.feeds:
			if onDone is not None:
				onDone()
				fired.append(onDone)
		return fired


class _Notifier:
	def __init__(self, name: str) -> None:
		self.name = name
		self.calls: list[dict] = []
		self.handler = None

	def notify(self, **kwargs) -> None:
		self.calls.append(kwargs)
		if self.handler is not None:
			self.handler(**kwargs)

	def register(self, handler) -> None:
		pass


def _make_speech_commands() -> types.ModuleType:
	module = types.ModuleType("speech.commands")

	class _Command:
		def __init__(self, *args, **kwargs) -> None:
			for key, value in kwargs.items():
				setattr(self, key, value)

	class BreakCommand(_Command):
		def __init__(self, time=0) -> None:
			self.time = time

	class IndexCommand(_Command):
		def __init__(self, index=0) -> None:
			self.index = index

	class LangChangeCommand(_Command):
		def __init__(self, lang=None) -> None:
			self.lang = lang

	class _ValueCommand(_Command):
		def __init__(self, newValue=0) -> None:
			self.newValue = newValue

	class PitchCommand(_ValueCommand):
		pass

	class RateCommand(_ValueCommand):
		pass

	class VolumeCommand(_ValueCommand):
		pass

	for cls in (BreakCommand, IndexCommand, LangChangeCommand, PitchCommand, RateCommand, VolumeCommand):
		setattr(module, cls.__name__, cls)
	return module


def install_stubs(nvdaConfigDir: Path | None = None) -> dict[str, types.ModuleType]:
	"""Register stub modules in ``sys.modules``. Returns them for inspection."""
	builtins._ = lambda text: text

	addonHandler = types.ModuleType("addonHandler")
	addonHandler.initTranslation = lambda: None

	configModule = types.ModuleType("config")
	configModule.conf = {"speech": {}, "audio": {}}

	synthDriverHandler = types.ModuleType("synthDriverHandler")

	class _Setting:
		def __init__(self, *args, **kwargs) -> None:
			pass

	class SynthDriver:
		VoiceSetting = _Setting
		RateSetting = _Setting
		RateBoostSetting = _Setting
		PitchSetting = _Setting
		VolumeSetting = _Setting

		def __init__(self) -> None:
			pass

	class VoiceInfo:
		def __init__(self, voiceId, displayName, language=None) -> None:
			self.id = voiceId
			self.displayName = displayName
			self.language = language

	synthDriverHandler.SynthDriver = SynthDriver
	synthDriverHandler.VoiceInfo = VoiceInfo
	synthDriverHandler.synthIndexReached = _Notifier("synthIndexReached")
	synthDriverHandler.synthDoneSpeaking = _Notifier("synthDoneSpeaking")

	wxModule = types.ModuleType("wx")
	wxModule.CallAfter = lambda func, *a, **kw: None
	wxModule.CallLater = lambda millis, func, *a, **kw: None

	logHandler = types.ModuleType("logHandler")

	class _Log:
		def __getattr__(self, name):
			return lambda *a, **kw: None

	logHandler.log = _Log()

	nvwave = types.ModuleType("nvwave")
	nvwave.WavePlayer = lambda **kwargs: FakePlayer()

	speech = types.ModuleType("speech")
	speech.__path__ = []
	commands = _make_speech_commands()
	speech.commands = commands

	globalVars = types.ModuleType("globalVars")
	configPath = str(nvdaConfigDir) if nvdaConfigDir else str(REPO_ROOT / "tests" / "_fake_nvda_config")
	globalVars.appArgs = types.SimpleNamespace(configPath=configPath)

	stubs = {
		"addonHandler": addonHandler,
		"config": configModule,
		"synthDriverHandler": synthDriverHandler,
		"wx": wxModule,
		"logHandler": logHandler,
		"nvwave": nvwave,
		"speech": speech,
		"speech.commands": commands,
		"globalVars": globalVars,
	}
	sys.modules.update(stubs)
	return stubs


def _load_module(modname: str, pkg: types.ModuleType) -> types.ModuleType:
	spec = importlib.util.spec_from_file_location(f"{PKG_NAME}.{modname}", ADDON_SYNTH_DIR / f"{modname}.py")
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	setattr(pkg, modname, module)
	return module


def load_driver(nvdaConfigDir: Path | None = None) -> types.ModuleType:
	"""Import the add-on's synth driver (`__init__.py`) with stubs in place."""
	install_stubs(nvdaConfigDir)
	return load_driver_module()


def load_driver_module() -> types.ModuleType:
	"""Same, but assumes `install_stubs` has already run, so a caller can
	replace a stub (the audio player, say) before the driver imports it."""
	pkg = types.ModuleType(PKG_NAME)
	pkg.__path__ = [str(ADDON_SYNTH_DIR)]
	sys.modules[PKG_NAME] = pkg
	_load_module("catalog", pkg)
	_load_module("voice_store", pkg)
	_load_module("bridge", pkg)

	spec = importlib.util.spec_from_file_location(PKG_NAME, ADDON_SYNTH_DIR / "__init__.py", submodule_search_locations=[str(ADDON_SYNTH_DIR)])
	assert spec is not None and spec.loader is not None
	driver = importlib.util.module_from_spec(spec)
	driver.__path__ = [str(ADDON_SYNTH_DIR)]
	sys.modules[PKG_NAME] = driver
	for name in ("catalog", "voice_store", "bridge"):
		setattr(driver, name, sys.modules[f"{PKG_NAME}.{name}"])
	spec.loader.exec_module(driver)
	return driver


def new_driver_instance(driverModule: types.ModuleType):
	"""A SynthDriver whose pure-logic methods can be called without running
	its real __init__ (which would start threads and a subprocess)."""
	instance = object.__new__(driverModule.SynthDriver)
	player = FakePlayer()
	instance._player = player
	instance._audioFeed = driverModule._AudioFeed(player)
	return instance, player
