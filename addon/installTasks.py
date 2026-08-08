import os
import shutil

import addonHandler
from logHandler import log


def _copy_user_dictionaries(source_dir, target_dir):
	if not os.path.isdir(source_dir):
		return 0

	copied = 0
	target_profiles = os.path.join(target_dir, "dictionaries")

	# Root-level files came from versions before dictionary profiles existed.
	legacy_target = os.path.join(target_profiles, "legacy")
	for filename in os.listdir(source_dir):
		if not filename.lower().endswith(".dic"):
			continue

		source_path = os.path.join(source_dir, filename)
		if not os.path.isfile(source_path):
			continue

		os.makedirs(legacy_target, exist_ok=True)
		shutil.copy2(source_path, os.path.join(legacy_target, filename))
		copied += 1

	# Root-level dictionaries are obsolete in the pending installation. Any
	# user copies are now safe in the legacy profile above.
	if os.path.isdir(target_dir):
		for filename in os.listdir(target_dir):
			target_path = os.path.join(target_dir, filename)
			if filename.lower().endswith(".dic") and os.path.isfile(target_path):
				os.remove(target_path)

	# Preserve every installed provider/legacy snapshot across add-on updates.
	source_profiles = os.path.join(source_dir, "dictionaries")
	if os.path.isdir(source_profiles):
		for root, _directories, filenames in os.walk(source_profiles):
			for filename in filenames:
				if not filename.lower().endswith(".dic"):
					continue
				source_path = os.path.join(root, filename)
				relative = os.path.relpath(source_path, source_profiles)
				target_path = os.path.join(target_profiles, relative)
				os.makedirs(os.path.dirname(target_path), exist_ok=True)
				shutil.copy2(source_path, target_path)
				copied += 1
	return copied


def onInstall():
	addon = addonHandler.getCodeAddon()
	if os.path.normcase(os.path.normpath(addon.path)) == os.path.normcase(
		os.path.normpath(addon.installPath)
	):
		return

	source_dir = os.path.join(addon.installPath, "synthDrivers", "eloquence")
	target_dir = os.path.join(addon.path, "synthDrivers", "eloquence")
	copied = _copy_user_dictionaries(source_dir, target_dir)
	if copied:
		log.info("Preserved %s Eloquence dictionary file(s) for pending add-on install", copied)
