import builtins
import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest import mock


class _Command:
	def __init__(self, **kwargs):
		self.__dict__.update(kwargs)


class IndexCommand(_Command):
	def __init__(self, index):
		super().__init__(index=index)


class LangChangeCommand(_Command):
	def __init__(self, lang):
		super().__init__(lang=lang)


class BreakCommand(_Command):
	def __init__(self, time):
		super().__init__(time=time)


class CharacterModeCommand(_Command):
	pass


class PitchCommand(_Command):
	pass


class RateCommand(_Command):
	pass


class VolumeCommand(_Command):
	pass


class PhonemeCommand(_Command):
	pass


class _ImmediateTimer:
	def __init__(self, _interval, function):
		self.function = function
		self.daemon = False

	def start(self):
		self.function()


class _Notification:
	def __init__(self):
		self.calls = []

	def notify(self, **kwargs):
		self.calls.append(kwargs)


class _SynthBase:
	@staticmethod
	def VoiceSetting():
		return types.SimpleNamespace(id="voice")

	@staticmethod
	def VariantSetting():
		return types.SimpleNamespace(id="variant")

	@staticmethod
	def RateSetting():
		return types.SimpleNamespace(id="rate")

	@staticmethod
	def PitchSetting():
		return types.SimpleNamespace(id="pitch")

	@staticmethod
	def InflectionSetting():
		return types.SimpleNamespace(id="inflection")

	@staticmethod
	def VolumeSetting():
		return types.SimpleNamespace(id="volume")

	def _percentToParam(self, value, min_value, max_value):
		return int(min_value + (max_value - min_value) * value / 100)

	def _paramToPercent(self, value, min_value, max_value):
		return int((value - min_value) * 100 / (max_value - min_value))

	def terminate(self):
		self.base_terminated = True

	def loadSettings(self, onlyChanged=False):
		self.base_load_settings_calls = getattr(self, "base_load_settings_calls", [])
		self.base_load_settings_calls.append(onlyChanged)
		if hasattr(self, "settings_lifecycle_events"):
			self.settings_lifecycle_events.append("settings")


class _SpeechQueue:
	def __init__(self):
		self.items = []

	def put(self, item):
		self.items.append(item)


class _AggregatedConfigSection:
	"""NVDA configuration view: readable/writable, but keys cannot be deleted."""

	def __init__(self, values=None):
		self.values = dict(values or {})

	def __getitem__(self, key):
		return self.values[key]

	def __setitem__(self, key, value):
		self.values[key] = value

	def get(self, key, default=None):
		return self.values.get(key, default)


class _ConfigRoot(dict):
	def __init__(self, *args, profiles=(), **kwargs):
		super().__init__(*args, **kwargs)
		self.profiles = list(profiles)


class _Choice:
	def __init__(self, items, selection):
		self.items = list(items)
		self.selection = selection

	def GetSelection(self):
		return self.selection

	def SetItems(self, items):
		self.items = list(items)
		self.selection = -1

	def SetSelection(self, selection):
		self.selection = selection


class _EloquenceStub(types.ModuleType):
	def __init__(self):
		super().__init__("addon.synthDrivers._eloquence")
		self.params = {9: 262144}
		self.voice_params = {7: 80}
		self.lastindex = None
		self.hsz = 1
		self.pitch = 2
		self.fluctuation = 3
		self.rgh = 4
		self.bth = 5
		self.rate = 6
		self.vlm = 7
		self.eciPath = "C:\\eci.dll"
		self.langs = {
			"enu": (65536, "American English"),
			"eng": (65537, "British English"),
			"deu": (262144, "German"),
			"esp": (131072, "Castilian Spanish"),
			"esm": (131073, "Latin American Spanish"),
			"fra": (196608, "French"),
			"frc": (196609, "French Canadian"),
			"ptb": (458752, "Brazilian Portuguese"),
			"fin": (589824, "Finnish"),
			"ita": (327680, "Italian"),
			"chs": (393216, "Mandarin Chinese"),
			"jpn": (524288, "Japanese"),
			"kor": (655360, "Korean"),
		}
		self.synth_queue = _SpeechQueue()
		self._client = types.SimpleNamespace(_sequence=1)
		self.stopped = False
		self.processed = False
		self.immediate_calls = []
		self.presence_contour_calls = []
		self.sample_rate_calls = []
		self.terminated = False

	def cmdProsody(self, *args):
		self.immediate_calls.append(("cmdProsody", args))

	def set_voice(self, voice_id):
		self.params[9] = int(voice_id)
		self.immediate_calls.append(("set_voice", (int(voice_id),)))

	def speak(self, text):
		self.immediate_calls.append(("speak", (text,)))

	def index(self, index):
		self.immediate_calls.append(("index", (index,)))

	def synth(self):
		self.immediate_calls.append(("synth", ()))

	def process(self):
		self.processed = True

	def stop(self):
		self.stopped = True

	def set_presence_contour(self, enabled):
		self.presence_contour_calls.append(bool(enabled))

	def set_sample_rate(self, rate):
		self.sample_rate_calls.append(int(rate))

	def getVParam(self, param):
		return self.voice_params.get(param, 0)

	def terminate(self):
		self.terminated = True


def _install_nvda_stubs():
	builtins._ = lambda text: text
	speech = types.ModuleType("speech")
	speech.IndexCommand = IndexCommand
	speech.CharacterModeCommand = CharacterModeCommand
	speech.LangChangeCommand = LangChangeCommand
	speech.BreakCommand = BreakCommand
	speech.PitchCommand = PitchCommand
	speech.RateCommand = RateCommand
	speech.VolumeCommand = VolumeCommand
	speech.PhonemeCommand = PhonemeCommand
	sys.modules["speech"] = speech

	driver_handler = types.ModuleType("driverHandler")
	driver_handler.NumericDriverSetting = lambda setting_id, *args, **kwargs: types.SimpleNamespace(id=setting_id)
	driver_handler.BooleanDriverSetting = lambda setting_id, *args, **kwargs: types.SimpleNamespace(id=setting_id)
	driver_handler.DriverSetting = lambda setting_id, *args, **kwargs: types.SimpleNamespace(id=setting_id)
	sys.modules["driverHandler"] = driver_handler

	synth_driver_handler = types.ModuleType("synthDriverHandler")
	synth_driver_handler.SynthDriver = _SynthBase
	synth_driver_handler.synthIndexReached = _Notification()
	synth_driver_handler.synthDoneSpeaking = _Notification()
	synth_driver_handler.getSynth = lambda: None
	synth_driver_handler.VoiceInfo = lambda *args, **kwargs: types.SimpleNamespace(
		id=args[0] if args else None,
		name=args[1] if len(args) > 1 else None,
		language=args[2] if len(args) > 2 else None,
	)
	sys.modules["synthDriverHandler"] = synth_driver_handler

	gui = types.ModuleType("gui")
	gui.settingsDialogs = types.SimpleNamespace(SettingsPanel=object)
	gui.guiHelper = types.SimpleNamespace()
	gui.messageBoxCalls = []
	gui.messageBox = lambda *args, **kwargs: gui.messageBoxCalls.append((args, kwargs))
	sys.modules["gui"] = gui
	wx = types.ModuleType("wx")
	wx.OK = 1
	wx.ICON_WARNING = 2
	wx.ICON_INFORMATION = 4
	wx.ICON_ERROR = 8
	wx.CallAfter = mock.Mock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
	wx.CallLater = mock.Mock(side_effect=lambda _delay, func, *args, **kwargs: func(*args, **kwargs))
	sys.modules["wx"] = wx
	sys.modules["winsound"] = types.ModuleType("winsound")
	sys.modules["config"] = types.SimpleNamespace(
		conf={},
		save=lambda: None,
		post_configProfileSwitch=types.SimpleNamespace(register=mock.Mock()),
	)
	sys.modules["core"] = types.SimpleNamespace(postNvdaStartup=types.SimpleNamespace(register=mock.Mock()))
	sys.modules["globalVars"] = types.SimpleNamespace(appArgs=types.SimpleNamespace(secure=False))
	sys.modules["addonHandler"] = types.SimpleNamespace(initTranslation=lambda: None)


def _load_driver():
	_install_nvda_stubs()
	eloquence_stub = _EloquenceStub()
	sys.modules["addon.synthDrivers._eloquence"] = eloquence_stub
	sys.modules.pop("addon.synthDrivers.eloquence", None)
	module = importlib.import_module("addon.synthDrivers.eloquence")
	preprocess_calls = []

	def preprocess(text, voice_id):
		preprocess_calls.append((text, voice_id))
		return f"{voice_id}:{text}"

	module._eloquence_text._text_preprocessing.preprocess = preprocess
	return module, eloquence_stub, preprocess_calls


def _new_driver(module):
	driver = module.SynthDriver.__new__(module.SynthDriver)
	driver._defaultVoice = "262144"
	driver.curvoice = "262144"
	driver._lastEngineVoice = "262144"
	driver._languageOverrideActive = False
	driver._pause_mode = 1
	driver._sampleRate = "11025"
	driver._presenceContour = False
	driver._backquoteVoiceTags = False
	driver._ABRDICT = False
	driver._phrasePrediction = False
	driver.rate = 50
	return driver


def _queued_calls(eloquence_stub):
	return eloquence_stub.synth_queue.items[-1][0]


def _queued_voice_ids(calls, eloquence_stub):
	return [
		args[0]
		for func, args in calls
		if getattr(func, "__self__", None) is eloquence_stub and getattr(func, "__name__", "") == "set_voice"
	]


def _queued_text(calls, eloquence_stub):
	return [
		args[0]
		for func, args in calls
		if getattr(func, "__self__", None) is eloquence_stub and getattr(func, "__name__", "") == "speak"
	]


class LanguageScopeTests(unittest.TestCase):
	def setUp(self):
		from addon.synthDrivers import _text_preprocessing

		self._original_preprocess = _text_preprocessing.preprocess

	def tearDown(self):
		from addon.synthDrivers import _text_preprocessing

		_text_preprocessing.preprocess = self._original_preprocess

	def test_dictionary_download_refreshes_choice_label_without_reopening_settings(self):
		module, eloquence_stub, _preprocess_calls = _load_driver()
		module.wx.MessageBox = mock.Mock()
		eloquence_stub.set_dictionary_directory = mock.Mock()
		panel = module.EloquenceSettingsPanel.__new__(module.EloquenceSettingsPanel)
		panel.dictionaryProfiles = [
			module._eloquence_dictionaries.BUILTIN_PROFILE,
			module._eloquence_dictionaries.ALTERNATIVE_PROFILE,
			module._eloquence_dictionaries.COMMUNITY_PROFILE,
		]
		panel.dictionaryChoice = _Choice(
			[
				"Built-in Eloquence pronunciations (no custom dictionary)",
				"Alternative dictionaries (minimal; not downloaded)",
				"Community dictionaries (extensive; not downloaded)",
			],
			selection=1,
		)
		module.config.conf = {"eloquence": {}}
		alternative = module._eloquence_dictionaries.ALTERNATIVE_PROFILE

		with (
			mock.patch.object(
				module._eloquence_dictionaries,
				"update_profile",
				return_value={"files": 1, "entries": 2},
			),
			mock.patch.object(
				module._eloquence_dictionaries,
				"active_directory",
				side_effect=lambda _data_directory, profile: (
					r"C:\dictionaries\alternative" if profile == alternative else None
				),
			),
		):
			panel.onUpdate(None)

		self.assertEqual(panel.dictionaryChoice.selection, 1)
		self.assertEqual(panel.dictionaryChoice.items[1], "Alternative dictionaries (minimal)")
		self.assertEqual(
			panel.dictionaryChoice.items[2],
			"Community dictionaries (extensive; not downloaded)",
		)

	def test_dictionary_profile_save_supports_nvda_aggregated_config_section(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		section = _AggregatedConfigSection(
			{
				"dictionary_name": "Alternative IBM TTS Dictionaries",
				"dictionary_url": "https://example.invalid/old-source",
			}
		)
		module.config.conf = {"eloquence": section}
		panel = module.EloquenceSettingsPanel.__new__(module.EloquenceSettingsPanel)
		panel.dictionaryProfiles = [module._eloquence_dictionaries.BUILTIN_PROFILE]
		panel.dictionaryChoice = types.SimpleNamespace(GetSelection=lambda: 0)

		profile = panel._storeSelectedDictionaryProfile()

		self.assertEqual(profile, module._eloquence_dictionaries.BUILTIN_PROFILE)
		self.assertEqual(section["dictionary_profile"], profile)
		self.assertIn("dictionary_name", section.values)

	def test_presence_contour_uses_a_new_boolean_schema_key(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()

		setting_ids = [setting.id for setting in module.SynthDriver.supportedSettings]
		self.assertIn("presenceContour", setting_ids)
		self.assertIn("sampleRate", setting_ids)
		self.assertNotIn("audioQuality", setting_ids)

	def test_presence_contour_setting_updates_backend_once(self):
		module, eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)

		driver._set_presenceContour(True)
		driver._set_presenceContour(True)

		self.assertIs(driver._get_presenceContour(), True)
		self.assertEqual(eloquence_stub.presence_contour_calls, [True])

	def test_native_sample_rate_setting_updates_backend_once(self):
		module, eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)

		driver._set_sampleRate("16000")
		driver._set_sampleRate("16000")

		self.assertEqual(driver._get_sampleRate(), "16000")
		self.assertEqual(eloquence_stub.sample_rate_calls, [16000])

	def test_sample_rate_exposes_the_property_name_nvda_requests(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)

		self.assertEqual(
			driver._get_availableSamplerates(),
			module.SynthDriver._sampleRates,
		)
		self.assertFalse(hasattr(module.SynthDriver, "_get_availableSampleRates"))
		self.assertEqual(
			[option.label for option in driver._get_availableSamplerates().values()],
			["11.025 kHz", "16 kHz"],
		)

	def test_profile_snapshot_coerces_legacy_audio_quality_values(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		profiles = [{"speech": {"eloquence": {"audioQuality": "enhanced"}}}]

		self.assertEqual(
			module._eloquence_settings_from_profile_layers(profiles),
			{"presenceContour": True},
		)

	def test_new_presence_contour_value_wins_over_legacy_value_in_same_profile(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		profiles = [
			{
				"speech": {
					"eloquence": {
						"audioQuality": "enhanced",
						"presenceContour": False,
					},
				},
			},
		]

		self.assertEqual(
			module._eloquence_settings_from_profile_layers(profiles),
			{"presenceContour": False},
		)

	def test_named_legacy_value_overrides_base_presence_contour(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		profiles = [
			{"speech": {"eloquence": {"presenceContour": False}}},
			{"speech": {"eloquence": {"audioQuality": "enhanced"}}},
		]

		self.assertEqual(
			module._eloquence_settings_from_profile_layers(profiles),
			{"presenceContour": True},
		)

	def test_initialization_keeps_legacy_value_captured_before_boolean_default(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		base_profile = {
			"speech": {
				"eloquence": {
					"audioQuality": "enhanced",
				},
			},
		}
		module.config.conf = _ConfigRoot(
			{
				"speech": {
					"eloquence": {
						"audioQuality": "enhanced",
						# Model the default NVDA may cache while loading settings.
						"presenceContour": False,
					},
				},
			},
			profiles=[base_profile],
		)
		stack = ("normal configuration",)

		module._capture_unseen_active_profile(stack)
		module._ensure_profile_isolation_handler()

		self.assertEqual(
			module._profile_settings_by_stack[stack]["presenceContour"],
			True,
		)

	def test_system_config_host_hash_match_is_not_a_mismatch(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		with tempfile.TemporaryDirectory() as root:
			addon_dir = os.path.join(root, "addon", "synthDrivers")
			system_config_dir = os.path.join(
				root,
				"NVDA",
				"systemConfig",
				"addons",
				"Eloquence",
				"synthDrivers",
			)
			os.makedirs(addon_dir)
			os.makedirs(system_config_dir)
			helper_bytes = b"current host"
			with open(os.path.join(addon_dir, "eloquence_host32.exe"), "wb") as file:
				file.write(helper_bytes)
			with open(os.path.join(system_config_dir, "eloquence_host32.exe"), "wb") as file:
				file.write(helper_bytes)

			with mock.patch.dict(os.environ, {"ProgramFiles": root}):
				self.assertIsNone(module._detect_system_config_host_mismatch(addon_dir))

	def test_system_config_host_hash_mismatch_is_detected(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		with tempfile.TemporaryDirectory() as root:
			addon_dir = os.path.join(root, "addon", "synthDrivers")
			system_config_dir = os.path.join(
				root,
				"NVDA",
				"systemConfig",
				"addons",
				"Eloquence",
				"synthDrivers",
			)
			os.makedirs(addon_dir)
			os.makedirs(system_config_dir)
			source_file = os.path.join(addon_dir, "eloquence_host32.exe")
			target_file = os.path.join(system_config_dir, "eloquence_host32.exe")
			with open(source_file, "wb") as file:
				file.write(b"current host")
			with open(target_file, "wb") as file:
				file.write(b"old host")

			with mock.patch.dict(os.environ, {"ProgramFiles": root}):
				mismatch = module._detect_system_config_host_mismatch(addon_dir)

		self.assertEqual(mismatch["source"], source_file)
		self.assertEqual(mismatch["target"], target_file)
		self.assertNotEqual(mismatch["sourceHash"], mismatch["targetHash"])

	def test_system_config_host_mismatch_notice_is_scheduled_immediately(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		module.config.conf["eloquence"] = {
			"system_config_host_mismatch_notice_sha256": "new",
		}
		mismatch = {
			"source": r"C:\Users\nick\AppData\Roaming\nvda\addons\Eloquence\synthDrivers\eloquence_host32.exe",
			"target": r"C:\Program Files\NVDA\systemConfig\addons\Eloquence\synthDrivers\eloquence_host32.exe",
			"sourceHash": "new",
			"targetHash": "old",
		}
		with (
			mock.patch.object(module, "_detect_system_config_host_mismatch", return_value=mismatch),
			mock.patch.object(module.threading, "Timer", _ImmediateTimer),
		):
			module._schedule_system_config_host_mismatch_notice()

		self.assertEqual(len(module.gui.messageBoxCalls), 1)
		module.wx.CallAfter.assert_called_once()
		module.core.postNvdaStartup.register.assert_not_called()

	def test_system_config_host_mismatch_notice_only_shows_once_per_process(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		mismatch = {
			"source": r"C:\Users\nick\AppData\Roaming\nvda\addons\Eloquence\synthDrivers\eloquence_host32.exe",
			"target": r"C:\Program Files\NVDA\systemConfig\addons\Eloquence\synthDrivers\eloquence_host32.exe",
			"sourceHash": "new",
			"targetHash": "old",
		}
		with (
			mock.patch.object(module, "_detect_system_config_host_mismatch", return_value=mismatch),
			mock.patch.object(module.threading, "Timer", _ImmediateTimer),
		):
			module._schedule_system_config_host_mismatch_notice()
			module._schedule_system_config_host_mismatch_notice()

		self.assertEqual(len(module.gui.messageBoxCalls), 1)

	def test_driver_terminate_releases_native_host(self):
		module, eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)

		driver.terminate()

		self.assertTrue(eloquence_stub.terminated)
		self.assertTrue(driver.base_terminated)

	def test_termination_does_not_defer_settings_panel_cleanup(self):
		module, eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)
		module.gui.settingsDialogs.NVDASettingsDialog = types.SimpleNamespace(
			categoryClasses=[module.EloquenceSettingsPanel]
		)
		module.wx.CallAfter.reset_mock()

		driver.terminate()

		self.assertTrue(eloquence_stub.terminated)
		module.wx.CallAfter.assert_not_called()
		self.assertIn(
			module.EloquenceSettingsPanel,
			module.gui.settingsDialogs.NVDASettingsDialog.categoryClasses,
		)

	def test_load_settings_activates_configuration_profiles_dictionary(self):
		module, eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)
		driver.settings_lifecycle_events = []
		module.config.conf = {
			"eloquence": {"dictionary_profile": "community"},
			"speech": {"eloquence": {"rate": 64}},
		}
		eloquence_stub.set_dictionary_directory = mock.Mock(
			side_effect=lambda _directory: driver.settings_lifecycle_events.append("dictionary")
		)

		with mock.patch.object(
			module._eloquence_dictionaries,
			"active_directory",
			return_value=r"C:\dictionaries\community",
		) as active_directory:
			driver.loadSettings(onlyChanged=True)

		self.assertEqual(driver.base_load_settings_calls, [True])
		self.assertEqual(driver.settings_lifecycle_events, ["dictionary", "settings"])
		active_directory.assert_called_once_with("C:\\", "community")
		eloquence_stub.set_dictionary_directory.assert_called_once_with(r"C:\dictionaries\community")

	def test_upgrade_seeds_missing_sample_rate_before_nvda_loads_settings(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)
		base = {"speech": {"eloquence": {"rate": 64}}}
		module.config.conf = _ConfigRoot(
			{"speech": {"eloquence": {"rate": 64}}, "eloquence": {}},
			profiles=[base],
		)

		def nvda_generic_loader(_instance, onlyChanged=False):
			self.assertEqual(
				module.config.conf["speech"]["eloquence"]["sampleRate"],
				"11025",
			)

		with mock.patch.object(
			_SynthBase,
			"loadSettings",
			autospec=True,
			side_effect=nvda_generic_loader,
		):
			driver.loadSettings(onlyChanged=False)

		self.assertEqual(base["speech"]["eloquence"]["sampleRate"], "11025")

	def test_profile_snapshot_restores_incoming_engine_and_repairs_config(self):
		module, eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)
		driver.name = "eloquence"
		module.synthDriverHandler.getSynth = lambda: driver
		module._current_profile_stack = ("normal configuration", "Test Profile")
		module._profile_settings_by_stack = {
			("normal configuration",): {"rate": 64},
		}
		module.config.conf = _ConfigRoot(
			{"speech": {"eloquence": {"rate": 44}}},
			profiles=[types.SimpleNamespace(name=None)],
		)
		driver.rate = 44
		module._remember_current_profile_setting(driver, "rate", 44)

		module._handle_profile_switch(prevConf={"speech": {"eloquence": {"rate": 44}}})

		self.assertEqual(driver.rate, 64)
		self.assertEqual(module.config.conf["speech"]["eloquence"]["rate"], 64)
		self.assertEqual(
			module._profile_settings_by_stack[("normal configuration", "Test Profile")]["rate"],
			44,
		)
		self.assertEqual(module._current_profile_stack, ("normal configuration",))

	def test_profile_snapshot_repairs_named_profile_config_for_restart(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)
		driver.name = "eloquence"
		module.synthDriverHandler.getSynth = lambda: driver
		module._current_profile_stack = ("normal configuration",)
		module._profile_settings_by_stack = {
			("normal configuration", "Test Profile"): {"rate": 44, "volume": 92},
		}
		module.config.conf = _ConfigRoot(
			{"speech": {"eloquence": {"rate": 64, "volume": 62}}},
			profiles=[types.SimpleNamespace(name=None), types.SimpleNamespace(name="Test Profile")],
		)

		module._handle_profile_switch(prevConf={"speech": {"eloquence": {"rate": 64}}})

		self.assertEqual(driver.rate, 44)
		self.assertEqual(driver.volume, 92)
		self.assertEqual(module.config.conf["speech"]["eloquence"]["rate"], 44)
		self.assertEqual(module.config.conf["speech"]["eloquence"]["volume"], 92)

	def test_unseen_profile_uses_raw_layers_before_merged_config_is_contaminated(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)
		module._current_profile_stack = ("normal configuration", "British Eloquence")
		module._profile_settings_by_stack = {}

		class Profile(dict):
			def __init__(self, name, values):
				super().__init__(values)
				self.name = name

		base = Profile(
			None,
			{
				"speech": {
					"eloquence": {
						"voice": "65536",
						"rate": "64",
						"volume": "90",
						"phrasePrediction": "False",
					}
				}
			},
		)
		test_profile = Profile(
			"Test Profile",
			{
				"speech": {
					"eloquence": {
						"voice": "65536",
						"rate": "44",
						"volume": "70",
						"phrasePrediction": "False",
					}
				}
			},
		)
		module.config.conf = _ConfigRoot(
			{"speech": {"eloquence": {"voice": "65537", "rate": 54, "volume": 90}}, "eloquence": {}},
			profiles=[base, test_profile],
		)

		def load_contaminated_british_values(instance, onlyChanged=False):
			instance.voice = "65537"
			instance.rate = 54
			instance.volume = 90

		with mock.patch.object(
			_SynthBase,
			"loadSettings",
			autospec=True,
			side_effect=load_contaminated_british_values,
		):
			driver.loadSettings(onlyChanged=False)

		incoming_stack = ("normal configuration", "Test Profile")
		self.assertEqual(module._profile_settings_by_stack[incoming_stack]["rate"], 44)
		module.synthDriverHandler.getSynth = lambda: driver
		module._handle_profile_switch()
		self.assertEqual(driver.voice, "65536")
		self.assertEqual(driver.rate, 44)
		self.assertEqual(driver.volume, 70)
		self.assertIs(driver.phrasePrediction, False)
		self.assertEqual(module.config.conf["speech"]["eloquence"]["rate"], 44)

	def test_named_profile_is_preloaded_before_its_live_layer_is_contaminated(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()

		class Profile(dict):
			def __init__(self, name, values):
				super().__init__(values)
				self.name = name

		base = Profile(
			None,
			{"speech": {"eloquence": {"voice": "65536", "rate": "64"}}},
		)
		test_profile = Profile(
			"Test Profile",
			{"speech": {"eloquence": {"voice": "65536", "rate": "44"}}},
		)
		conf = _ConfigRoot(
			{"speech": {"eloquence": {"voice": "65536", "rate": 64}}},
			profiles=[base],
		)
		conf.listProfiles = lambda: ["Test Profile"]
		conf._getProfile = lambda name: test_profile
		module.config.conf = conf

		module._preload_named_profile_snapshots()
		test_profile["speech"]["eloquence"]["rate"] = "64"

		self.assertEqual(
			module._profile_settings_by_stack[("normal configuration", "Test Profile")]["rate"],
			44,
		)

	def test_trigger_and_manual_profile_layers_merge_in_activation_order(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()

		class Profile(dict):
			def __init__(self, name, values):
				super().__init__(values)
				self.name = name

		base = Profile(
			None,
			{"speech": {"eloquence": {"voice": "65536", "rate": "64", "volume": "90"}}},
		)
		trigger = Profile(
			"Sonos",
			{"speech": {"eloquence": {"rate": "48", "volume": "80"}}},
		)
		manual = Profile(
			"British Eloquence",
			{"speech": {"eloquence": {"voice": "65537", "rate": "54"}}},
		)
		stack = ("normal configuration", "Sonos", "British Eloquence")
		module.config.conf = _ConfigRoot(
			{"speech": {"eloquence": {"voice": "65537", "rate": 54, "volume": 80}}},
			profiles=[base, trigger, manual],
		)

		module._capture_unseen_active_profile(stack)

		self.assertEqual(
			module._profile_settings_by_stack[stack],
			{"voice": "65537", "rate": 54, "volume": 80},
		)

	def test_profile_load_and_corrupt_prev_conf_do_not_poison_snapshot(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)
		driver._loading_profile_settings = True
		module._current_profile_stack = ("normal configuration",)
		module._profile_settings_by_stack = {("normal configuration",): {"rate": 64}}
		module.config.conf = _ConfigRoot(
			{"speech": {"eloquence": {"rate": 54}}},
			profiles=[types.SimpleNamespace(name=None), types.SimpleNamespace(name="British Eloquence")],
		)

		module._remember_current_profile_setting(driver, "rate", 54)
		module._handle_profile_switch(prevConf={"speech": {"eloquence": {"rate": 44}}})

		self.assertEqual(module._profile_settings_by_stack[("normal configuration",)]["rate"], 64)

	def test_same_profile_reload_restores_committed_snapshot(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)
		module._current_profile_stack = ("normal configuration",)
		module._profile_settings_by_stack = {("normal configuration",): {"rate": 64}}
		module.config.conf = _ConfigRoot(
			{"speech": {"eloquence": {"rate": 44}}, "eloquence": {}},
			profiles=[types.SimpleNamespace(name=None)],
		)

		def load_contaminated_rate(instance, onlyChanged=False):
			instance.rate = 44

		with mock.patch.object(_SynthBase, "loadSettings", autospec=True, side_effect=load_contaminated_rate):
			driver.loadSettings(onlyChanged=False)

		self.assertEqual(driver.rate, 64)
		self.assertEqual(module.config.conf["speech"]["eloquence"]["rate"], 64)

	def test_same_profile_reload_discards_uncommitted_dialog_preview(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)
		module._current_profile_stack = ("normal configuration",)
		module._profile_settings_by_stack = {("normal configuration",): {"rate": 64}}
		module.config.conf = _ConfigRoot(
			{"speech": {"eloquence": {"rate": 44}}, "eloquence": {}},
			profiles=[types.SimpleNamespace(name=None)],
		)
		module._remember_current_profile_setting(driver, "rate", 70)

		def load_contaminated_rate(instance, onlyChanged=False):
			instance.rate = 44

		with mock.patch.object(_SynthBase, "loadSettings", autospec=True, side_effect=load_contaminated_rate):
			driver.loadSettings(onlyChanged=False)

		self.assertEqual(driver.rate, 64)
		self.assertEqual(module._profile_settings_by_stack[("normal configuration",)]["rate"], 64)

	def test_settings_ring_change_commits_after_nvda_updates_config(self):
		module, _eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)
		module._current_profile_stack = ("normal configuration",)
		module._profile_settings_by_stack = {("normal configuration",): {"rate": 64}}
		module.config.conf = _ConfigRoot(
			{"speech": {"eloquence": {"rate": 64}}},
			profiles=[types.SimpleNamespace(name=None)],
		)
		callbacks = []
		module.wx.CallAfter.side_effect = lambda callback, *args, **kwargs: callbacks.append(
			lambda: callback(*args, **kwargs)
		)

		module._remember_current_profile_setting(driver, "rate", 65)
		module.config.conf["speech"]["eloquence"]["rate"] = 65
		callbacks.pop()()

		self.assertEqual(module._profile_settings_by_stack[("normal configuration",)]["rate"], 65)
		self.assertNotIn(
			(("normal configuration",), "rate"),
			module._pending_profile_setting_changes,
		)

	def test_english_document_language_does_not_replace_default_voice(self):
		module, eloquence_stub, preprocess_calls = _load_driver()
		driver = _new_driver(module)

		driver.speak([LangChangeCommand("en-US"), "Collapse tree"])

		calls = _queued_calls(eloquence_stub)
		self.assertEqual(_queued_voice_ids(calls, eloquence_stub), [65536])
		self.assertEqual(preprocess_calls, [("Collapse tree", 65536)])
		self.assertEqual(driver._get_voice(), "262144")
		self.assertEqual(driver.curvoice, "262144")

	def test_cancel_does_not_suppress_next_english_language_command(self):
		module, eloquence_stub, preprocess_calls = _load_driver()
		driver = _new_driver(module)

		driver.speak([LangChangeCommand("en-US"), "Repository"])
		driver.cancel()
		driver.speak([LangChangeCommand("en-US"), "Collapse tree"])

		second_calls = eloquence_stub.synth_queue.items[-1][0]
		self.assertEqual(_queued_voice_ids(second_calls, eloquence_stub), [262144, 65536])
		self.assertEqual(preprocess_calls[-1], ("Collapse tree", 65536))
		self.assertEqual(driver._get_voice(), "262144")

	def test_german_ui_without_language_command_uses_default_after_english_content(self):
		module, eloquence_stub, preprocess_calls = _load_driver()
		driver = _new_driver(module)

		driver.speak([LangChangeCommand("en-US"), "Repository"])
		driver.speak(["Desktop"])

		second_calls = eloquence_stub.synth_queue.items[-1][0]
		self.assertEqual(_queued_voice_ids(second_calls, eloquence_stub), [262144])
		self.assertEqual(preprocess_calls[-1], ("Desktop", 262144))
		self.assertEqual(driver._get_voice(), "262144")

	def test_language_reset_uses_default_for_say_all_structural_text(self):
		module, eloquence_stub, preprocess_calls = _load_driver()
		driver = _new_driver(module)

		driver.speak([LangChangeCommand("en-US"), "README", LangChangeCommand(None), "list end"])

		calls = _queued_calls(eloquence_stub)
		self.assertEqual(_queued_voice_ids(calls, eloquence_stub), [65536, 262144])
		self.assertEqual(
			preprocess_calls,
			[("README", 65536), ("list end", 262144)],
		)
		self.assertEqual(driver._get_voice(), "262144")

	def test_index_only_sequence_still_notifies_progress_and_done(self):
		module, eloquence_stub, _preprocess_calls = _load_driver()
		driver = _new_driver(module)

		driver.speak([IndexCommand(42)])

		self.assertEqual(module.synthIndexReached.calls, [{"synth": driver, "index": 42}])
		self.assertEqual(module.synthDoneSpeaking.calls, [{"synth": driver}])
		self.assertEqual(eloquence_stub.synth_queue.items, [])
		self.assertFalse(eloquence_stub.processed)


if __name__ == "__main__":
	unittest.main()
