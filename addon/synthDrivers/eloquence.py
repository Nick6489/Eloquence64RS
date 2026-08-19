# Copyright (C) 2009-2019 eloquence fans
# synthDrivers/eci.py
# todo: possibly add to this
import gui
import wx
import ctypes
import winsound
import hashlib
import threading

try:
	from speech import (
		IndexCommand,
		CharacterModeCommand,
		LangChangeCommand,
		BreakCommand,
		PitchCommand,
		RateCommand,
		VolumeCommand,
		PhonemeCommand,
	)
except ImportError:
	from speech.commands import (
		IndexCommand,
		CharacterModeCommand,
		LangChangeCommand,
		BreakCommand,
		PitchCommand,
		RateCommand,
		VolumeCommand,
		PhonemeCommand,
	)

try:
	from driverHandler import NumericDriverSetting, BooleanDriverSetting, DriverSetting
except ImportError:
	from autoSettingsUtils.driverSetting import (
		BooleanDriverSetting,
		DriverSetting,
		NumericDriverSetting,
	)

try:
	from autoSettingsUtils.utils import StringParameterInfo
except ImportError:

	class StringParameterInfo:
		def __init__(self, value, label):
			self.value = value
			self.label = label


from ctypes import *
import ctypes.wintypes
import synthDriverHandler
import os
import config
import logging
import core
import globalVars
from synthDriverHandler import (
	SynthDriver,
	synthIndexReached,
	synthDoneSpeaking,
)
from . import _eloquence
from . import _eloquence_dictionaries
from . import _eloquence_text
from collections import OrderedDict
import addonHandler

addonHandler.initTranslation()

try:
	# Use NVDA's logger so informational driver lifecycle diagnostics reach nvda.log.
	from logHandler import log
except ImportError:
	# Keep the module importable in the standalone unit-test harness.
	log = logging.getLogger(__name__)


minRate = 40
maxRate = 150
VOICE_BCP47 = {
	"enu": "en-US",
	"eng": "en-GB",
	"esp": "es-ES",
	"esm": "es-419",
	"ptb": "pt-BR",
	"fra": "fr-FR",
	"frc": "fr-CA",
	"deu": "de-DE",
	"ita": "it-IT",
	"fin": "fi-FI",
	"chs": "zh-CN",  # Simplified Chinese
	"jpn": "ja-JP",  # Japanese
	"kor": "ko-KR",  # Korean
}

VOICE_CODE_TO_ID = {code: str(info[0]) for code, info in _eloquence.langs.items()}
VOICE_ID_TO_BCP47 = {
	voice_id: VOICE_BCP47.get(code) for code, voice_id in VOICE_CODE_TO_ID.items() if VOICE_BCP47.get(code)
}
LANGUAGE_TO_VOICE_ID = {
	lang.lower(): VOICE_CODE_TO_ID[code] for code, lang in VOICE_BCP47.items() if code in VOICE_CODE_TO_ID
}
PRIMARY_LANGUAGE_TO_VOICE_IDS = {}
for code, lang in VOICE_BCP47.items():
	voice_id = VOICE_CODE_TO_ID.get(code)
	if not voice_id:
		continue
	primary = lang.split("-", 1)[0].lower()
	PRIMARY_LANGUAGE_TO_VOICE_IDS.setdefault(primary, []).append(voice_id)

variants = {
	1: "Reed",
	2: "Shelly",
	3: "Bobby",
	4: "Rocko",
	5: "Glen",
	6: "Sandy",
	7: "Grandma",
	8: "Grandpa",
}

_system_config_host_notice_shown = False
_system_config_host_notice_timer = None

# NVDA can recreate a synth while a Voice settings panel still contains the
# outgoing driver's values.  When the audio device also changes, those stale
# values can be written into the incoming profile's in-memory configuration
# after the profile stack itself has switched.  Keep an Eloquence-only snapshot
# per complete profile stack so the engine and NVDA's active configuration can
# be reconciled without requiring an NVDA core patch.
_PROFILE_SETTING_IDS = (
	"voice",
	"variant",
	"rate",
	"pitch",
	"inflection",
	"volume",
	"hsz",
	"rgh",
	"bth",
	"backquoteVoiceTags",
	"ABRDICT",
	"phrasePrediction",
	"pauseMode",
	"audioQuality",
)
_BOOLEAN_PROFILE_SETTING_IDS = frozenset(("backquoteVoiceTags", "ABRDICT", "phrasePrediction"))
_INTEGER_PROFILE_SETTING_IDS = frozenset(("rate", "pitch", "inflection", "volume", "hsz", "rgh", "bth"))
_profile_settings_by_stack = {}
_pending_profile_setting_changes = {}
_current_profile_stack = None
_profile_switch_handler_registered = False
_profile_snapshots_preloaded = False
_MISSING_PROFILE_SETTING = object()


def _active_profile_stack():
	profiles = getattr(config.conf, "profiles", ())
	return tuple(getattr(profile, "name", None) or "normal configuration" for profile in profiles)


def _eloquence_settings_from(conf):
	try:
		settings = conf["speech"]["eloquence"]
	except (KeyError, TypeError):
		return {}
	return {setting_id: settings[setting_id] for setting_id in _PROFILE_SETTING_IDS if setting_id in settings}


def _coerce_boolean_setting(value):
	if isinstance(value, str):
		return value.strip().casefold() in {"1", "true", "yes", "on"}
	return bool(value)


def _coerce_raw_profile_setting(setting_id, value):
	if setting_id in _BOOLEAN_PROFILE_SETTING_IDS:
		return _coerce_boolean_setting(value)
	if setting_id in _INTEGER_PROFILE_SETTING_IDS:
		return int(value)
	return str(value)


def _eloquence_settings_from_profile_layers(profiles):
	"""Merge explicit Eloquence values directly from NVDA's raw profile layers."""
	settings = {}
	for profile in profiles:
		if not profile:
			continue
		try:
			profile_settings = profile["speech"]["eloquence"]
		except (KeyError, TypeError):
			continue
		for setting_id in _PROFILE_SETTING_IDS:
			if setting_id in profile_settings:
				try:
					settings[setting_id] = _coerce_raw_profile_setting(
						setting_id,
						profile_settings[setting_id],
					)
				except (TypeError, ValueError):
					log.warning(
						"Ignoring invalid raw Eloquence setting %s=%r in profile %r",
						setting_id,
						profile_settings[setting_id],
						getattr(profile, "name", None),
					)
	return settings


def _capture_unseen_active_profile(profile_stack):
	"""Capture an incoming profile before synth loading can contaminate its merged view."""
	if profile_stack in _profile_settings_by_stack:
		return
	settings = _eloquence_settings_from_profile_layers(getattr(config.conf, "profiles", ()))
	if not settings:
		settings = _eloquence_settings_from(config.conf)
	if settings:
		_profile_settings_by_stack[profile_stack] = settings
		log.debug(
			"Eloquence captured raw incoming profile snapshot: profiles=%s, rate=%s",
			profile_stack,
			settings.get("rate"),
		)


def _preload_named_profile_snapshots():
	"""Read named profiles before a settings dialog/audio switch can mutate them."""
	global _profile_snapshots_preloaded
	if _profile_snapshots_preloaded:
		return
	list_profiles = getattr(config.conf, "listProfiles", None)
	get_profile = getattr(config.conf, "_getProfile", None)
	profiles = getattr(config.conf, "profiles", ())
	if not callable(list_profiles) or not callable(get_profile) or not profiles:
		return
	base_profile = profiles[0]
	for profile_name in list_profiles():
		profile_stack = ("normal configuration", profile_name)
		if profile_stack in _profile_settings_by_stack:
			continue
		try:
			profile = get_profile(profile_name)
			settings = _eloquence_settings_from_profile_layers((base_profile, profile))
		except Exception:
			log.exception("Could not preload Eloquence snapshot for profile %s", profile_name)
			continue
		if not settings:
			continue
		_profile_settings_by_stack[profile_stack] = settings
		log.debug(
			"Eloquence preloaded named profile snapshot: profiles=%s, rate=%s",
			profile_stack,
			settings.get("rate"),
		)
	_profile_snapshots_preloaded = True


def _remember_current_profile_setting(driver, setting_id, value):
	if getattr(driver, "_loading_profile_settings", False) or _current_profile_stack is None:
		return
	profile_stack = _current_profile_stack
	settings = _profile_settings_by_stack.setdefault(profile_stack, {})
	pending_key = (profile_stack, setting_id)
	_pending_profile_setting_changes.setdefault(
		pending_key,
		settings.get(setting_id, _MISSING_PROFILE_SETTING),
	)
	settings[setting_id] = value

	def confirm_if_config_was_updated():
		# Settings-ring changes write config immediately after calling the driver
		# setter.  Dialog controls do not write until Apply/OK.  Checking on the
		# next wx turn distinguishes a committed ring change from a pending dialog
		# edit while retaining NVDA's live-preview behavior.
		if _active_profile_stack() != profile_stack:
			return
		try:
			configured = config.conf["speech"]["eloquence"][setting_id]
		except (KeyError, TypeError):
			return
		if str(configured) == str(value):
			_pending_profile_setting_changes.pop(pending_key, None)

	wx.CallAfter(confirm_if_config_was_updated)


def _discard_pending_profile_settings(profile_stack):
	settings = _profile_settings_by_stack.get(profile_stack)
	if settings is None:
		return
	for pending_key, previous in list(_pending_profile_setting_changes.items()):
		stack, setting_id = pending_key
		if stack != profile_stack:
			continue
		if previous is _MISSING_PROFILE_SETTING:
			settings.pop(setting_id, None)
		else:
			settings[setting_id] = previous
		del _pending_profile_setting_changes[pending_key]


def _commit_current_profile_settings(driver):
	if _current_profile_stack is None:
		return
	settings = {}
	for setting_id in _PROFILE_SETTING_IDS:
		try:
			settings[setting_id] = getattr(driver, setting_id)
		except Exception:
			log.exception("Could not snapshot committed Eloquence setting %s", setting_id)
	_profile_settings_by_stack[_current_profile_stack] = settings
	for pending_key in list(_pending_profile_setting_changes):
		if pending_key[0] == _current_profile_stack:
			del _pending_profile_setting_changes[pending_key]


def _sync_profile_snapshot_to_config(profile_stack, settings):
	"""Keep NVDA's active profile cache aligned with the restored engine state."""
	if _active_profile_stack() != profile_stack:
		log.warning(
			"Not persisting Eloquence snapshot for inactive profile stack %s",
			profile_stack,
		)
		return
	try:
		configured_settings = config.conf["speech"]["eloquence"]
	except (KeyError, TypeError):
		log.exception("Could not access the active Eloquence configuration section")
		return
	for setting_id, value in settings.items():
		try:
			configured_settings[setting_id] = value
		except Exception:
			log.exception(
				"Could not persist restored Eloquence setting %s for profile stack %s",
				setting_id,
				profile_stack,
			)


def _apply_profile_snapshot(driver, profile_stack, settings):
	was_loading = getattr(driver, "_loading_profile_settings", False)
	corrected_settings = []
	driver._loading_profile_settings = True
	try:
		for setting_id in _PROFILE_SETTING_IDS:
			if setting_id not in settings:
				continue
			try:
				current_value = getattr(driver, setting_id)
			except Exception:
				corrected_settings.append(setting_id)
			else:
				if str(current_value) != str(settings[setting_id]):
					corrected_settings.append(setting_id)
			try:
				setattr(driver, setting_id, settings[setting_id])
			except Exception:
				log.exception(
					"Could not restore Eloquence setting %s for profile stack %s",
					setting_id,
					profile_stack,
				)
	finally:
		driver._loading_profile_settings = was_loading
	# The engine and NVDA's aggregated configuration must agree.  Otherwise an
	# audio-device-driven synth recreation can look correct for the rest of the
	# session while NVDA later writes the crossed values to disk on shutdown.
	_sync_profile_snapshot_to_config(profile_stack, settings)
	log_profile_restore = log.info if corrected_settings else log.debug
	log_profile_restore(
		"Eloquence corrected isolated profile settings: profiles=%s, settings=%s, "
		"rate=%s, raw ECI rate=%s",
		profile_stack,
		corrected_settings,
		getattr(driver, "rate", None),
		driver.getVParam(_eloquence.rate),
	)


def _handle_profile_switch(prevConf=None, **_kwargs):
	global _current_profile_stack
	incoming_stack = _active_profile_stack()
	_current_profile_stack = incoming_stack
	snapshot = _profile_settings_by_stack.get(incoming_stack)
	if not snapshot:
		return
	driver = synthDriverHandler.getSynth()
	if driver is None or getattr(driver, "name", None) != "eloquence":
		return
	_apply_profile_snapshot(driver, incoming_stack, snapshot)


def _ensure_profile_isolation_handler():
	global _current_profile_stack, _profile_switch_handler_registered
	if _current_profile_stack is None:
		_current_profile_stack = _active_profile_stack()
		_profile_settings_by_stack[_current_profile_stack] = _eloquence_settings_from(config.conf)
	if not _profile_switch_handler_registered:
		# This handler deliberately lives for the lifetime of the loaded module.
		# Unregistering it from SynthDriver.terminate would mutate NVDA's Action
		# while that Action is notifying listeners during an audio-device switch.
		config.post_configProfileSwitch.register(_handle_profile_switch)
		_profile_switch_handler_registered = True


def _sha256_file(path):
	hasher = hashlib.sha256()
	with open(path, "rb") as file:
		for chunk in iter(lambda: file.read(1024 * 1024), b""):
			hasher.update(chunk)
	return hasher.hexdigest()


def _system_config_host_path():
	prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
	return os.path.normpath(
		os.path.join(
			prog_files,
			"NVDA",
			"systemConfig",
			"addons",
			"Eloquence",
			"synthDrivers",
			"eloquence_host32.exe",
		)
	)


def _detect_system_config_host_mismatch(addon_dir=None):
	addon_dir = addon_dir or os.path.abspath(os.path.dirname(__file__))
	source_file = os.path.normpath(os.path.join(addon_dir, "eloquence_host32.exe"))
	target_file = _system_config_host_path()
	if os.path.normcase(os.path.abspath(source_file)) == os.path.normcase(os.path.abspath(target_file)):
		return None
	if not os.path.isfile(source_file) or not os.path.isfile(target_file):
		return None
	source_hash = _sha256_file(source_file)
	target_hash = _sha256_file(target_file)
	if source_hash == target_hash:
		return None
	return {
		"source": source_file,
		"target": target_file,
		"sourceHash": source_hash,
		"targetHash": target_hash,
	}


def _show_system_config_host_mismatch_notice():
	log.warning("Showing Eloquence helper mismatch dialog")
	gui.messageBox(
		_(
			"The Eloquence helper copied to NVDA's systemConfig does not match the helper "
			"shipped with this add-on.\n\n"
			"This can happen after updating Eloquence64RS or switching from another "
			"Eloquence add-on. Eloquence may fail on secure screens until the helper is "
			"updated.\n\n"
			"Go to NVDA Settings > Eloquence and choose "
			"'Copy Helper to System Config (for Logon Screen)' to copy the current helper."
		),
		_("Eloquence Helper Mismatch"),
		wx.OK | wx.ICON_WARNING,
	)


def _queue_system_config_host_mismatch_notice():
	global _system_config_host_notice_timer
	log.warning("Scheduling Eloquence helper mismatch dialog with threading.Timer")

	def _timer_callback():
		log.warning("Queueing Eloquence helper mismatch dialog on wx event loop")
		wx.CallAfter(_show_system_config_host_mismatch_notice)

	_system_config_host_notice_timer = threading.Timer(1.0, _timer_callback)
	_system_config_host_notice_timer.daemon = True
	_system_config_host_notice_timer.start()


def _schedule_system_config_host_mismatch_notice():
	global _system_config_host_notice_shown
	try:
		if getattr(getattr(globalVars, "appArgs", None), "secure", False):
			return
		if _system_config_host_notice_shown:
			return
		mismatch = _detect_system_config_host_mismatch()
		if not mismatch:
			return
		log.warning(
			"Eloquence systemConfig helper hash mismatch: packaged %s has sha256 %s; "
			"systemConfig %s has sha256 %s",
			mismatch["source"],
			mismatch["sourceHash"],
			mismatch["target"],
			mismatch["targetHash"],
		)
		_system_config_host_notice_shown = True
		_queue_system_config_host_mismatch_notice()
	except Exception:
		log.debug("Could not check Eloquence systemConfig helper hash", exc_info=True)


class EloquenceSettingsPanel(gui.settingsDialogs.SettingsPanel):
	# Translators: Name of the category for this add-on in the settings dialog
	title = _("Eloquence")

	def makeSettings(self, settings):
		try:
			sHelper = gui.guiHelper.BoxSizerHelper(self, sizer=settings)
			data_directory = os.path.dirname(_eloquence.eciPath)
			selected_profile = _eloquence_dictionaries.resolve_profile(
				config.conf.get("eloquence", {}),
				data_directory,
				migrate=not globalVars.appArgs.secure,
			)
			self.dictionaryProfiles = [
				_eloquence_dictionaries.BUILTIN_PROFILE,
				_eloquence_dictionaries.ALTERNATIVE_PROFILE,
				_eloquence_dictionaries.COMMUNITY_PROFILE,
			]
			if (
				_eloquence_dictionaries.active_directory(
					data_directory,
					_eloquence_dictionaries.LEGACY_PROFILE,
				)
				or selected_profile == _eloquence_dictionaries.LEGACY_PROFILE
			):
				self.dictionaryProfiles.append(_eloquence_dictionaries.LEGACY_PROFILE)
			profile_labels = self._dictionaryProfileLabels(data_directory)

			self.dictionaryChoice = sHelper.addLabeledControl(
				# Translators: Label of a combobox in the Eloquence category of the settings dialog
				_("Pronunciation dictionary:"),
				wx.Choice,
				choices=[profile_labels[profile] for profile in self.dictionaryProfiles],
			)
			self.dictionaryChoice.SetSelection(self.dictionaryProfiles.index(selected_profile))
			self.Bind(wx.EVT_CHOICE, self.onDictionaryChoice, self.dictionaryChoice)

			# Translators: Downloads a fresh snapshot of the selected pronunciation dictionary.
			self.updateButton = sHelper.addItem(wx.Button(self, label=_("Download or update dictionary")))
			self.Bind(wx.EVT_BUTTON, self.onUpdate, self.updateButton)
			# When NVDA is running in secure mode, one should not be able to save any setting to disk.
			self._updateDictionaryButtonState()

			# Copy the native host because NVDA excludes executables from its normal secure-screen copy.
			self.copyHelperButton = sHelper.addItem(
				# Translators: Label of a button in the Eloquence category of the settings dialog
				wx.Button(self, label=_("Copy Helper to System Config (for Logon Screen)"))
			)
			self.Bind(wx.EVT_BUTTON, self.onCopyHelper, self.copyHelperButton)
			# Copying helper from secure mode is not allowed, following "Use NVDA during sign-in" button behaviour in
			# NVDA's General settings category.
			if globalVars.appArgs.secure:
				self.copyHelperButton.Disable()

			# NEW: Auto-update addon button
			# Translators: Label of a button in the Eloquence category of the settings dialog
			self.addonUpdateButton = sHelper.addItem(wx.Button(self, label=_("Check for Add-on Updates")))
			self.Bind(wx.EVT_BUTTON, self.onCheckAddonUpdate, self.addonUpdateButton)
			# Add-on updates are not allowed in secure mode.
			if globalVars.appArgs.secure:
				self.addonUpdateButton.Disable()
		except Exception as e:
			log.error(f"Error creating Eloquence settings panel: {e}")
			# Panel creation failed, but don't crash - synth will still work

	def onCopyHelper(self, evt):
		"""Copy the native Eloquence host to NVDA's secure-screen configuration."""
		source_file = os.path.normpath(os.path.join(os.path.dirname(__file__), "eloquence_host32.exe"))
		prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
		target_addon_dir = os.path.normpath(
			os.path.join(prog_files, "NVDA", "systemConfig", "addons", "Eloquence")
		)

		# Security check: Ensure the target addon directory exists in systemConfig
		if not os.path.isdir(target_addon_dir):
			wx.MessageBox(
				_(
					# Translators: Text of a message dialog when copying the helper to system config
					"Eloquence folder not found in systemConfig.\n\nPlease go to NVDA Settings > General and click 'Use currently saved settings during sign-in' first to initialize folders."
				),
				# Translators: Title of a message dialog when copying the helper to system config
				_("Folder Missing"),
				wx.OK | wx.ICON_WARNING,
			)
			return

		dest_dir = os.path.normpath(os.path.join(target_addon_dir, "synthDrivers"))
		dest_file = os.path.normpath(os.path.join(dest_dir, "eloquence_host32.exe"))

		if not os.path.exists(source_file):
			wx.MessageBox(
				# Translators: Text of a message dialog when copying the helper to system config
				_("Source file not found at:\n{source_file}").format(source_file=source_file),
				# Translators: Title of a message dialog when copying the helper to system config
				_("Error"),
				wx.OK | wx.ICON_ERROR,
			)
			return

		# Prepare elevated command: ensure subdirectory exists and copy the helper
		cmd_params = f'/c mkdir "{dest_dir}" 2>nul & copy /y "{source_file}" "{dest_file}"'

		try:
			# Triggering UAC Elevation using ShellExecuteW's "runas" verb
			ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", cmd_params, None, 0)

			if ret > 32:
				# Play Windows Asterisk sound for confirmation of successful launch
				winsound.MessageBeep(winsound.MB_ICONASTERISK)
				wx.MessageBox(
					_(
						# Translators: text of a message dialog when copying the helper to system config
						"Successfully copied eloquence_host32.exe to systemConfig!\n\nEloquence should now load normally on logon screen, start-up, and other secure screens."
					),
					# Translators: Title of a message dialog when copying the helper to system config
					_("Success"),
					wx.OK | wx.ICON_INFORMATION,
				)
			elif ret == 5:
				# SE_ERR_ACCESSDENIED: Elevation prompt was declined
				wx.MessageBox(
					# Translators: Text of a message dialog when copying the helper to system config
					_("Copy process was cancelled or permission was denied by the user."),
					# Translators: Title of a message dialog when copying the helper to system config
					_("Cancelled"),
					wx.OK | wx.ICON_ERROR,
				)
			else:
				wx.MessageBox(
					# Translators: Text of a message dialog when copying the helper to system config
					_("An error occurred while attempting to copy the file. (Error Code: {ret})").format(
						ret=ret
					),
					# Translators: Title of a message dialog when copying the helper to system config
					_("Error"),
					wx.OK | wx.ICON_ERROR,
				)
		except Exception as e:
			wx.MessageBox(
				# Translators: Text of a message dialog when copying the helper to system config
				_("An unexpected error occurred: {e}").format(e=str(e)),
				# Translators: Title of a message dialog when copying the helper to system config
				_("Error"),
				wx.OK | wx.ICON_ERROR,
			)

	def onCheckAddonUpdate(self, evt):
		"""Check for and apply addon updates from GitHub"""
		import sys
		import os

		# Import the update manager
		addon_dir = os.path.abspath(os.path.dirname(__file__))
		update_manager_path = os.path.join(addon_dir, "_eloquence_updater.py")

		# Check if updater exists
		if not os.path.exists(update_manager_path):
			wx.MessageBox(
				# Translators: Text of a message dialog when updating the add-on
				_("Update manager not found. Please reinstall the add-on."),
				# Translators: Title of a message dialog when updating the add-on
				_("Error"),
				wx.OK | wx.ICON_ERROR,
			)
			return

		# Import update manager
		sys.path.insert(0, addon_dir)
		try:
			from _eloquence_updater import EloquenceUpdateManager
		except ImportError as e:
			wx.MessageBox(
				# Translators: Text of a message dialog when updating the add-on
				_("Failed to load update manager: {e}").format(e=e),
				# Translators: Title of a message dialog when updating the add-on
				_("Error"),
				wx.OK | wx.ICON_ERROR,
			)
			return
		finally:
			if addon_dir in sys.path:
				sys.path.remove(addon_dir)

		# Create progress dialog
		progress = wx.ProgressDialog(
			# Translators: Title of a progress dialog when updating the add-on
			_("Checking for Updates"),
			# Translators: Message of a progress dialog when updating the add-on
			_("Connecting to GitHub..."),
			maximum=100,
			parent=self,
			style=wx.PD_APP_MODAL | wx.PD_AUTO_HIDE,
		)

		try:
			# Initialize update manager
			manager = EloquenceUpdateManager(addon_dir)

			# Check for updates
			# Translators: Message of a progress dialog when updating the add-on
			progress.Update(10, _("Checking for updates..."))
			(
				has_update,
				latest_version,
				download_url,
				changelog,
			) = manager.check_for_updates()

			if not has_update:
				# Translators: Message of a progress dialog when updating the add-on
				progress.Update(100, _("No updates available"))
				progress.Destroy()
				wx.MessageBox(
					# Translators: Text of a message dialog when updating the add-on
					_("You are using the latest version!"),
					# Translators: Title of a message dialog when updating the add-on
					_("Up to Date"),
					wx.OK | wx.ICON_INFORMATION,
				)
				return

			# Show changelog
			# Translators: Text of a message dialog when updating the add-on
			progress.Update(20, _("Update available!"))
			progress.Destroy()

			changelog_dialog = wx.MessageDialog(
				self,
				_(
					# Translators: Text of a message dialog when updating the add-on
					"New version available: {latest_version}\n\n"
					"Current version: {currVersion}\n\n"
					"Changelog:\n{changelog}\n\n"
					"Would you like to download and review the update?"
				).format(
					latest_version=latest_version,
					currVersion=manager.CURRENT_VERSION,
					changelog=changelog[:500],
				),
				# Translators: Title of a message dialog when updating the add-on
				_("Update Available"),
				wx.YES_NO | wx.ICON_INFORMATION,
			)

			if changelog_dialog.ShowModal() != wx.ID_YES:
				return

			# Download update
			progress = wx.ProgressDialog(
				# Translators: Text of a progress dialog when updating the add-on
				_("Downloading Update"),
				# Translators: Title of a progress dialog when updating the add-on
				_("Downloading..."),
				maximum=100,
				parent=self,
				style=wx.PD_APP_MODAL | wx.PD_CAN_ABORT,
			)

			def download_progress(percent, message):
				cont, skip = progress.Update(percent, message)
				return cont

			addon_path = manager.download_update(download_url, download_progress)
			progress.Update(100, _("Download complete"))
			progress.Destroy()

			progress = wx.ProgressDialog(
				# Translators: Text of a progress dialog when updating the add-on
				_("Installing Update"),
				# Translators: Text of a progress dialog when updating the add-on
				_("Please wait..."),
				maximum=100,
				parent=self,
				style=wx.PD_APP_MODAL | wx.PD_AUTO_HIDE,
			)
			progress.Pulse(_("Installing add-on package..."))
			if not manager.install_update(addon_path, self):
				progress.Destroy()
				manager.cleanup()
				wx.MessageBox(
					# Translators: Text of a message dialog when updating the add-on
					_("Update cancelled."),
					# Translators: Text of a message dialog when updating the add-on
					_("Cancelled"),
					wx.OK | wx.ICON_INFORMATION,
				)
				return
			progress.Destroy()
			manager.cleanup()
			manager.prompt_for_restart()

		except Exception as e:
			progress.Destroy()
			log.error(f"Update failed: {e}")
			wx.MessageBox(
				_(
					# Translators: Text of a message dialog when updating the add-on
					"Update failed: {e}\n\nYour addon has not been modified."
				).format(e=str(e)),
				# Translators: Title of a message dialog when updating the add-on
				_("Update Failed"),
				wx.OK | wx.ICON_ERROR,
			)

	def _selectedDictionaryProfile(self):
		selection = self.dictionaryChoice.GetSelection()
		if selection < 0 or selection >= len(self.dictionaryProfiles):
			return _eloquence_dictionaries.BUILTIN_PROFILE
		return self.dictionaryProfiles[selection]

	def _dictionaryProfileLabels(self, data_directory=None):
		if data_directory is None:
			data_directory = os.path.dirname(_eloquence.eciPath)
		profile_labels = {
			# Translators: Eloquence's original pronunciation with no downloaded or custom dictionary.
			_eloquence_dictionaries.BUILTIN_PROFILE: _(
				"Built-in Eloquence pronunciations (no custom dictionary)"
			),
			# Translators: A small, conservative community pronunciation dictionary.
			_eloquence_dictionaries.ALTERNATIVE_PROFILE: _("Alternative dictionaries (minimal)"),
			# Translators: A large community pronunciation dictionary.
			_eloquence_dictionaries.COMMUNITY_PROFILE: _("Community dictionaries (extensive)"),
			# Translators: Dictionary files retained from an older version of the add-on.
			_eloquence_dictionaries.LEGACY_PROFILE: _("Existing custom dictionaries"),
		}
		if not _eloquence_dictionaries.active_directory(
			data_directory,
			_eloquence_dictionaries.ALTERNATIVE_PROFILE,
		):
			# Translators: The small dictionary profile is available but has not been downloaded yet.
			profile_labels[_eloquence_dictionaries.ALTERNATIVE_PROFILE] = _(
				"Alternative dictionaries (minimal; not downloaded)"
			)
		if not _eloquence_dictionaries.active_directory(
			data_directory,
			_eloquence_dictionaries.COMMUNITY_PROFILE,
		):
			# Translators: The large dictionary profile is available but has not been downloaded yet.
			profile_labels[_eloquence_dictionaries.COMMUNITY_PROFILE] = _(
				"Community dictionaries (extensive; not downloaded)"
			)
		return profile_labels

	def _refreshDictionaryChoiceLabels(self):
		selected_profile = self._selectedDictionaryProfile()
		profile_labels = self._dictionaryProfileLabels()
		self.dictionaryChoice.SetItems(
			[profile_labels[profile] for profile in self.dictionaryProfiles]
		)
		self.dictionaryChoice.SetSelection(self.dictionaryProfiles.index(selected_profile))

	def _updateDictionaryButtonState(self):
		can_update = (
			not globalVars.appArgs.secure
			and self._selectedDictionaryProfile() in _eloquence_dictionaries.PROVIDERS
		)
		self.updateButton.Enable(can_update)

	def onDictionaryChoice(self, evt):
		self._updateDictionaryButtonState()
		evt.Skip()

	def _activateDictionaryProfile(self, profile, *, reload=False):
		data_directory = os.path.dirname(_eloquence.eciPath)
		directory = _eloquence_dictionaries.active_directory(data_directory, profile)
		_eloquence.set_dictionary_directory(directory, reload=reload)

	def _storeSelectedDictionaryProfile(self):
		if "eloquence" not in config.conf:
			config.conf["eloquence"] = {}
		profile = self._selectedDictionaryProfile()
		config.conf["eloquence"]["dictionary_profile"] = profile
		# NVDA exposes this as an AggregatedSection. It supports assigning the
		# new stable profile ID, but deliberately does not implement key deletion.
		# Obsolete source/name keys are harmless and may remain in older configs.
		return profile

	def onSave(self):
		profile = self._storeSelectedDictionaryProfile()
		try:
			self._activateDictionaryProfile(profile)
		except Exception:
			log.exception("Could not activate Eloquence dictionary profile %s", profile)

	def onUpdate(self, evt):
		profile = self._selectedDictionaryProfile()
		if profile not in _eloquence_dictionaries.PROVIDERS:
			wx.MessageBox(
				# Translators: The built-in and retained custom dictionary choices cannot be downloaded.
				_("Select a downloadable dictionary first."),
				# Translators: Title of a message dialog when updating a dictionary
				_("Error"),
				wx.OK | wx.ICON_ERROR,
			)
			return

		try:
			self._storeSelectedDictionaryProfile()
			data_directory = os.path.dirname(_eloquence.eciPath)
			result = _eloquence_dictionaries.update_profile(data_directory, profile)
			self._activateDictionaryProfile(profile, reload=True)
			self._refreshDictionaryChoiceLabels()
			wx.MessageBox(
				_(
					# Translators: Reports the size of a newly downloaded pronunciation dictionary snapshot.
					"Dictionary update successful.\n\n"
					"Dictionary files: {files}\n"
					"Pronunciation entries: {entries}"
				).format(**result),
				# Translators: Title of a message dialog when updating a dictionary
				_("Success"),
				wx.OK | wx.ICON_INFORMATION,
			)
		except Exception as error:
			log.exception("Failed to update Eloquence dictionary profile %s", profile)
			wx.MessageBox(
				# Translators: Text of a message dialog when updating a dictionary
				_("An error occurred while updating the dictionary: {error}").format(error=error),
				# Translators: Title of a message dialog when updating a dictionary
				_("Error"),
				wx.OK | wx.ICON_ERROR,
			)


class SynthDriver(synthDriverHandler.SynthDriver):
	settingsPanel = EloquenceSettingsPanel
	supportedSettings = (
		SynthDriver.VoiceSetting(),
		SynthDriver.VariantSetting(),
		SynthDriver.RateSetting(),
		SynthDriver.PitchSetting(),
		SynthDriver.InflectionSetting(),
		SynthDriver.VolumeSetting(),
		# Translators: A synth setting available in speech settings dialog
		NumericDriverSetting("hsz", _("Hea&d size")),
		# Translators: A synth setting available in speech settings dialog
		NumericDriverSetting("rgh", _("Rou&ghness")),
		# Translators: A synth setting available in speech settings dialog
		NumericDriverSetting("bth", _("Breathi&ness")),
		BooleanDriverSetting(
			# Translators: A synth setting available in speech settings dialog
			"backquoteVoiceTags",
			_("Enable backquote voice &tags"),
			True,
		),
		# Translators: A synth setting available in speech settings dialog
		BooleanDriverSetting("ABRDICT", _("Enable &abbreviation dictionary"), False),
		# Translators: A synth setting available in speech settings dialog
		BooleanDriverSetting("phrasePrediction", _("Enable phras&e prediction"), False),
		# Translators: A synth setting available in speech settings dialog
		DriverSetting("pauseMode", _("Shorten &pauses"), defaultVal="0"),
		# Translators: A synth setting available in speech settings dialog
		DriverSetting("audioQuality", _("Audio &quality"), defaultVal="standard"),
	)
	supportedCommands = {
		IndexCommand,
		CharacterModeCommand,
		LangChangeCommand,
		BreakCommand,
		PitchCommand,
		RateCommand,
		VolumeCommand,
		PhonemeCommand,
	}
	supportedNotifications = {synthIndexReached, synthDoneSpeaking}
	PROSODY_ATTRS = {
		PitchCommand: _eloquence.pitch,
		VolumeCommand: _eloquence.vlm,
		RateCommand: _eloquence.rate,
	}

	description = "ETI-Eloquence"
	name = "eloquence"

	# Initialize _pause_mode at class level to prevent issues with setting restoration
	_pause_mode = 0
	_audioQuality = "standard"

	@classmethod
	def check(cls):
		try:
			log.info("Eloquence: Running check() to verify synth is available")
			result = _eloquence.eciCheck()
			log.info(f"Eloquence: check() returned {result}")
			return result
		except Exception as e:
			log.error(f"Eloquence: check() failed with error: {e}", exc_info=True)
			return False

	def __init__(self):
		# Construction and NVDA's subsequent initSettings call belong to one
		# profile-load transaction.  Do not let those setter calls overwrite the
		# outgoing profile snapshot before our post-switch handler runs.
		self._loading_profile_settings = True
		# Safe settings panel registration - won't crash if API changes in different NVDA versions
		try:
			if hasattr(gui.settingsDialogs, "NVDASettingsDialog"):
				if hasattr(gui.settingsDialogs.NVDASettingsDialog, "categoryClasses"):
					if EloquenceSettingsPanel not in gui.settingsDialogs.NVDASettingsDialog.categoryClasses:
						gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(EloquenceSettingsPanel)
		except Exception as e:
			log.warning(f"Could not register Eloquence settings panel: {e}")
			# Continue initialization - synth will work without settings panel

		try:
			log.info("Eloquence: Starting initialization")
			_eloquence.initialize(self._onIndexReached)
			log.info("Eloquence: _eloquence.initialize completed successfully")
		except Exception as e:
			log.error(f"Eloquence: Failed to initialize _eloquence module: {e}", exc_info=True)
			raise

		try:
			voice_param = _eloquence.params.get(9)
			if voice_param is None:
				configured_voice = config.conf.get("speech", {}).get("eci", {}).get("voice", "enu")
				voice_info = _eloquence.langs.get(configured_voice) or _eloquence.langs.get("enu")
				voice_param = voice_info[0] if voice_info else 65536
			self._update_voice_state(voice_param, update_default=True)
			# Initialize _rate first before setting the rate property
			self._rate = self._percentToParam(50, minRate, maxRate)
			self.rate = 50
			self.variant = "1"
			self._pause_mode = 0
			log.info("Eloquence: Initialization completed successfully")
		except Exception as e:
			log.error(f"Eloquence: Failed during voice/parameter setup: {e}", exc_info=True)
			raise

		_schedule_system_config_host_mismatch_notice()

		# One-time migration notice for users updating from multiprocessing-based IPC
		try:
			eci_conf = config.conf.get("speech", {}).get("ibmeci")
			eloquence_conf = config.conf.get("eloquence", {})
			if eci_conf is not None and not eloquence_conf.get("ipc_migration_notice_shown", False):

				def _show_migration_notice():
					if "eloquence" not in config.conf:
						config.conf["eloquence"] = {}
					config.conf["eloquence"]["ipc_migration_notice_shown"] = True
					config.conf.save()
					wx.CallAfter(
						gui.messageBox,
						_(
							"Eloquence has been updated with a new communication system.\n\n"
							"If you use Eloquence on secure screens (logon screen, UAC prompts), "
							"please go to NVDA Settings > Eloquence and click "
							"'Copy Helper to System Config' to update the secure screen copy.\n\n"
							"This message will only appear once."
						),
						_("Eloquence Update Notice"),
						wx.OK | wx.ICON_INFORMATION,
					)

				self._migration_func = _show_migration_notice
				core.postNvdaStartup.register(self._migration_func)
		except Exception:
			pass  # Never let a notice prevent the synth from working

	def loadSettings(self, onlyChanged=False):
		"""Load NVDA's synth settings and the add-on's profile-scoped dictionary."""
		active_stack = _active_profile_stack()
		# On an audio-device profile change, NVDA can recreate the synth using a
		# merged configuration still carrying the outgoing profile's values.  The
		# raw ConfigObj layers already identify the incoming profile correctly, so
		# capture them before calling NVDA's loader for the first time this session.
		_capture_unseen_active_profile(active_stack)
		# Named profiles must be copied even earlier than their first activation.
		# NVDA can mutate the incoming profile layer before constructing this new
		# driver, so preload clean copies while the initial profile is still stable.
		_preload_named_profile_snapshots()
		if _current_profile_stack is not None:
			# A same-profile full load is NVDA's settings Cancel/reload path.  A
			# different active stack means a profile switch is in progress.  In both
			# cases, pending dialog previews were not committed and must not become a
			# durable profile snapshot.
			_discard_pending_profile_settings(_current_profile_stack)
		self._loading_profile_settings = True
		try:
			# Activate the dictionary before loading voice parameters.  ECI dictionary
			# activation can alter the current voice state, so rate, pitch, volume and
			# the other NVDA profile settings must be the final writes to the engine.
			try:
				data_directory = os.path.dirname(_eloquence.eciPath)
				profile = _eloquence_dictionaries.resolve_profile(
					config.conf.get("eloquence", {}),
					data_directory,
					migrate=not globalVars.appArgs.secure,
				)
				directory = _eloquence_dictionaries.active_directory(data_directory, profile)
				_eloquence.set_dictionary_directory(directory)
			except Exception:
				log.exception("Could not activate the Eloquence dictionary for the new configuration profile")
			# NVDA calls loadSettings for an in-place profile change and initSettings
			# calls it for a newly constructed driver.  Keeping both operations here
			# avoids a second post_configProfileSwitch handler while ensuring the
			# active profile's synth values win over all engine initialization.
			super().loadSettings(onlyChanged=onlyChanged)
		finally:
			self._loading_profile_settings = False
		_ensure_profile_isolation_handler()
		if active_stack == _current_profile_stack:
			snapshot = _profile_settings_by_stack.get(active_stack)
			if snapshot:
				_apply_profile_snapshot(self, active_stack, snapshot)
		try:
			configured_rate = config.conf["speech"][self.name]["rate"]
		except Exception:
			configured_rate = None
		active_profiles = [
			getattr(profile, "name", None) or "normal configuration"
			for profile in getattr(config.conf, "profiles", ())
		]
		log.debug(
			"Eloquence profile settings loaded: profiles=%s, configured rate=%s, "
			"applied rate=%s, raw ECI rate=%s, onlyChanged=%s",
			active_profiles,
			configured_rate,
			self.rate,
			self.getVParam(_eloquence.rate),
			onlyChanged,
		)

	def saveSettings(self):
		"""Save through NVDA, then commit the active Eloquence profile snapshot."""
		was_loading = getattr(self, "_loading_profile_settings", False)
		super().saveSettings()
		self._loading_profile_settings = False
		_ensure_profile_isolation_handler()
		if was_loading or synthDriverHandler.getSynth() is self:
			_commit_current_profile_settings(self)

	def terminate(self):
		# NVDA destroys the current driver before constructing the replacement.
		# Release the native host as well as its WavePlayer so that switching back
		# can create a fresh ECI engine immediately.
		_eloquence.terminate()
		# Do not remove or defer removal of EloquenceSettingsPanel here.  A bound
		# wx.CallAfter callback keeps this obsolete driver alive and prevents NVDA's
		# VoiceSettingsPanel weakref from rebuilding its controls for the replacement
		# synth.  Once registered, the add-on panel is safe to retain for this process.
		super(SynthDriver, self).terminate()

	def combine_adjacent_strings(self, lst):
		result = []
		current_string = ""
		for item in lst:
			if isinstance(item, str):
				current_string += item
			else:
				if current_string:
					result.append(current_string)
					current_string = ""
				result.append(item)
		if current_string:
			result.append(current_string)
		return result

	def _build_options(self):
		return _eloquence_text.BuildOptions(
			volume=self.getVParam(_eloquence.vlm),
			rate=self.rate,
			pause_mode=self._pause_mode,
			backquote_tags=self._backquoteVoiceTags,
			abbreviation_dict=self._ABRDICT,
			phrase_prediction=self._phrasePrediction,
		)

	def speak(self, speechSequence):
		last = None
		outlist = []
		pending_indexes = []
		queued_speech = False
		options = self._build_options()
		sequence_voice = getattr(self, "_defaultVoice", str(_eloquence.params.get(9, 65536)))
		last_queued_engine_voice = getattr(self, "_lastEngineVoice", None)

		# Reset prosody to baseline at the start of each utterance to prevent
		# state leaks from previous speech sequences (issue #59).
		for pr in (_eloquence.rate, _eloquence.pitch, _eloquence.vlm):
			outlist.append((_eloquence.cmdProsody, (pr, 1, 0)))

		if last_queued_engine_voice != sequence_voice:
			try:
				outlist.append((_eloquence.set_voice, (int(sequence_voice),)))
				last_queued_engine_voice = sequence_voice
			except (TypeError, ValueError):
				log.debug("Skipping default voice reset for invalid voice id %r", sequence_voice)

		# IBMTTS Logic: Combine strings before processing regex
		speechSequence = self.combine_adjacent_strings(speechSequence)

		for item in speechSequence:
			if isinstance(item, str):
				s = str(item)
				s = _eloquence_text.build(s, voice_id=sequence_voice, options=options)
				outlist.append((_eloquence.speak, (s,)))
				last = s
				queued_speech = True
			elif isinstance(item, IndexCommand):
				pending_indexes.append(item.index)
				outlist.append((_eloquence.index, (item.index,)))
			elif isinstance(item, BreakCommand):
				fragment = _eloquence_text.break_fragment(item.time, options)
				outlist.append((_eloquence.speak, (fragment,)))
				queued_speech = True
			elif isinstance(item, LangChangeCommand):
				voice_id = self._resolve_voice_for_language(item.lang)
				if voice_id is None:
					log.debug("No Eloquence voice mapped for language '%s'", item.lang)
					continue
				voice_str = str(voice_id)
				if voice_str == sequence_voice:
					continue
				try:
					queued_voice = int(voice_id)
				except (TypeError, ValueError):
					log.debug(
						"Skipping language change for '%s': invalid voice id %r",
						item.lang,
						voice_id,
					)
					continue
				outlist.append((_eloquence.set_voice, (queued_voice,)))
				sequence_voice = voice_str
				last_queued_engine_voice = voice_str
			elif type(item) in self.PROSODY_ATTRS:
				pr = self.PROSODY_ATTRS[type(item)]
				# Use the raw _offset/_multiplier values directly, NOT the
				# computed properties.  NVDA guarantees that only one of them
				# is specified (they are mutually exclusive).  The computed
				# .multiplier property already folds offset into a ratio
				# using the *current* defaultValue, so passing both would
				# double-count the change.  Raw values are stable constants
				# that do not depend on defaultValue and are safe to apply
				# later in the worker thread against the live base pitch.
				raw_offset = getattr(item, "_offset", 0)
				raw_multiplier = getattr(item, "_multiplier", 1)
				outlist.append(
					(
						_eloquence.cmdProsody,
						(pr, raw_multiplier, raw_offset),
					)
				)
		if not queued_speech:
			# No speech queued. Ensure any state changes apply and emit indexes immediately
			# so sayAll can advance even when there's nothing to speak.
			for func, args in outlist:
				if func is _eloquence.index:
					continue
				try:
					func(*args)
				except Exception:
					log.exception("Synthesis command failed")
			self._lastEngineVoice = last_queued_engine_voice
			for index in pending_indexes:
				synthIndexReached.notify(synth=self, index=index)
			synthDoneSpeaking.notify(synth=self)
			return

		if last is not None:
			trailing_pause = _eloquence_text.trailing_pause(last, options)
			if trailing_pause is not None:
				outlist.append((_eloquence.speak, (trailing_pause,)))

		outlist.append((_eloquence.index, (0xFFFF,)))
		outlist.append((_eloquence.synth, ()))
		self._lastEngineVoice = last_queued_engine_voice
		seq = _eloquence._client._sequence
		_eloquence.synth_queue.put((outlist, seq))
		_eloquence.process()

	# def cancel(self):
	#  self.dll.eciStop(self.handle)

	def pause(self, switch):
		_eloquence.pause(switch)
		#  self.dll.eciPause(self.handle,switch)

	# Pause Mode Definitions:
	# 0: Injects p0 at all punctuation for Legacy Speed.
	# 1: Standard timing with a p1 pause at the end of speech blocks only.
	# 2: Injects p1 at all punctuation for consistent Modern Shortening.
	_pauseModes = {
		# Translators: An option in the "Shorten pauses" combo box in speech settings
		"0": StringParameterInfo("0", _("Never")),
		# Translators: An option in the "Shorten pauses" combo box in speech settings
		"1": StringParameterInfo("1", _("At end of text only")),
		# Translators: An option in the "Shorten pauses" combo box in speech settings
		"2": StringParameterInfo("2", _("Always")),
	}

	def _get_availablePausemodes(self):
		return self._pauseModes

	def _set_pauseMode(self, val):
		self._pause_mode = int(val)
		_remember_current_profile_setting(self, "pauseMode", str(self._pause_mode))

	def _get_pauseMode(self):
		return str(self._pause_mode)

	_audioQualityOptions = {
		# Translators: An option in the "Audio quality" combo box in speech settings.
		"standard": StringParameterInfo("standard", _("Standard 11 kHz")),
		# Translators: An option in the "Audio quality" combo box in speech settings.
		"enhanced": StringParameterInfo("enhanced", _("Enhanced 22 kHz")),
	}

	def _get_availableAudioqualitys(self):
		# NVDA constructs this property name with setting.id.capitalize() + "s".
		return self._audioQualityOptions

	def _set_audioQuality(self, value):
		quality = "enhanced" if value == "enhanced" else "standard"
		if quality == self._audioQuality:
			return
		_eloquence.set_audio_quality(quality)
		self._audioQuality = quality
		_remember_current_profile_setting(self, "audioQuality", quality)

	def _get_audioQuality(self):
		return self._audioQuality

	_backquoteVoiceTags = False
	_ABRDICT = False
	_phrasePrediction = False

	def _get_backquoteVoiceTags(self):
		return self._backquoteVoiceTags

	def _set_backquoteVoiceTags(self, enable):
		enable = _coerce_boolean_setting(enable)
		if enable == self._backquoteVoiceTags:
			return
		self._backquoteVoiceTags = enable
		_remember_current_profile_setting(self, "backquoteVoiceTags", enable)

	def _get_ABRDICT(self):
		return self._ABRDICT

	def _set_ABRDICT(self, enable):
		enable = _coerce_boolean_setting(enable)
		if enable == self._ABRDICT:
			return
		self._ABRDICT = enable
		_remember_current_profile_setting(self, "ABRDICT", enable)

	def _get_phrasePrediction(self):
		return self._phrasePrediction

	def _set_phrasePrediction(self, enable):
		enable = _coerce_boolean_setting(enable)
		if enable == self._phrasePrediction:
			return
		self._phrasePrediction = enable
		_remember_current_profile_setting(self, "phrasePrediction", enable)

	def _get_rate(self):
		return self._paramToPercent(self.getVParam(_eloquence.rate), minRate, maxRate)

	def _set_rate(self, vl):
		self._rate = self._percentToParam(vl, minRate, maxRate)
		self.setVParam(_eloquence.rate, self._percentToParam(vl, minRate, maxRate))
		_remember_current_profile_setting(self, "rate", int(vl))

	def _get_pitch(self):
		return self.getVParam(_eloquence.pitch)

	def _set_pitch(self, vl):
		self.setVParam(_eloquence.pitch, vl)
		_remember_current_profile_setting(self, "pitch", int(vl))

	def _get_volume(self):
		return self.getVParam(_eloquence.vlm)

	def _set_volume(self, vl):
		self.setVParam(_eloquence.vlm, int(vl))
		_remember_current_profile_setting(self, "volume", int(vl))

	def _set_inflection(self, vl):
		vl = int(vl)
		self.setVParam(_eloquence.fluctuation, vl)
		_remember_current_profile_setting(self, "inflection", vl)

	def _get_inflection(self):
		return self.getVParam(_eloquence.fluctuation)

	def _set_hsz(self, vl):
		vl = int(vl)
		self.setVParam(_eloquence.hsz, vl)
		_remember_current_profile_setting(self, "hsz", vl)

	def _get_hsz(self):
		return self.getVParam(_eloquence.hsz)

	def _set_rgh(self, vl):
		vl = int(vl)
		self.setVParam(_eloquence.rgh, vl)
		_remember_current_profile_setting(self, "rgh", vl)

	def _get_rgh(self):
		return self.getVParam(_eloquence.rgh)

	def _set_bth(self, vl):
		vl = int(vl)
		self.setVParam(_eloquence.bth, vl)
		_remember_current_profile_setting(self, "bth", vl)

	def _get_bth(self):
		return self.getVParam(_eloquence.bth)

	def _getAvailableVoices(self):
		o = OrderedDict()
		for name in os.listdir(_eloquence.eciPath[:-8]):
			if not name.lower().endswith(".syn"):
				continue
			voice_code = name.lower()[:-4]
			info = _eloquence.langs[voice_code]
			language = VOICE_BCP47.get(voice_code)
			o[str(info[0])] = synthDriverHandler.VoiceInfo(str(info[0]), info[1], language)
		return o

	def _get_voice(self):
		return getattr(self, "_defaultVoice", str(_eloquence.params[9]))

	def _set_voice(self, vl):
		_eloquence.set_voice(vl)
		self._update_voice_state(vl, update_default=True)
		_remember_current_profile_setting(self, "voice", str(vl))

	def _update_voice_state(self, voice_id, update_default):
		voice_str = str(voice_id)
		try:
			_eloquence.params[9] = int(voice_str)
		except (TypeError, ValueError):
			log.debug("Unable to coerce Eloquence voice id '%s' to int", voice_id)
		if update_default or not getattr(self, "_defaultVoice", None):
			self._defaultVoice = voice_str
		self.curvoice = self._defaultVoice
		self._lastEngineVoice = voice_str
		self._languageOverrideActive = False

	def _resolve_voice_for_language(self, language):
		if not language:
			return getattr(self, "_defaultVoice", None)
		normalized = language.lower().replace("_", "-")
		voice_id = LANGUAGE_TO_VOICE_ID.get(normalized)
		if voice_id:
			return voice_id
		primary, _, region = normalized.partition("-")
		default_voice = getattr(self, "_defaultVoice", None)
		default_lang = VOICE_ID_TO_BCP47.get(default_voice) if default_voice else None
		if default_lang:
			default_primary, _, default_region = default_lang.lower().partition("-")
			if default_primary == primary and (not region or default_region == region):
				return default_voice
		candidates = PRIMARY_LANGUAGE_TO_VOICE_IDS.get(primary, [])
		if not candidates:
			return None
		if region:
			for candidate in candidates:
				candidate_tag = VOICE_ID_TO_BCP47.get(candidate)
				if not candidate_tag:
					continue
				cand_primary, _, cand_region = candidate_tag.lower().partition("-")
				if cand_primary == primary and cand_region == region:
					return candidate
			if primary == "es":
				for candidate in candidates:
					candidate_tag = VOICE_ID_TO_BCP47.get(candidate)
					if candidate_tag and candidate_tag.lower().endswith("-419"):
						return candidate
		if default_lang and default_lang.lower().partition("-")[0] == primary:
			return default_voice
		return candidates[0]

	def getVParam(self, pr):
		return _eloquence.getVParam(pr)

	def setVParam(self, pr, vl):
		_eloquence.setVParam(pr, vl)

	def _get_lastIndex(self):
		# fix?
		return _eloquence.lastindex

	def cancel(self):
		self._lastEngineVoice = None
		self._languageOverrideActive = False
		_eloquence.stop()

	def _getAvailableVariants(self):
		global variants
		return OrderedDict(
			(str(id), synthDriverHandler.VoiceInfo(str(id), name)) for id, name in variants.items()
		)

	def _set_variant(self, v):
		global variants
		self._variant = v if int(v) in variants else "1"
		_eloquence.setVariant(int(v))
		self.setVParam(_eloquence.rate, self._rate)
		_remember_current_profile_setting(self, "variant", str(self._variant))
		#  if 'eloquence' in config.conf['speech']:
		#   config.conf['speech']['eloquence']['pitch'] = self.pitch

	def _get_variant(self):
		return self._variant

	def _onIndexReached(self, index):
		if index is not None:
			synthIndexReached.notify(synth=self, index=index)
		else:
			synthDoneSpeaking.notify(synth=self)
