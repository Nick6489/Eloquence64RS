import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

from addon.synthDrivers import _eloquence_dictionaries as dictionaries


class DictionaryProfileTests(unittest.TestCase):
	def test_new_install_uses_builtin_eloquence_pronunciations(self):
		with tempfile.TemporaryDirectory() as data_directory:
			profile = dictionaries.resolve_profile({}, data_directory)
			self.assertEqual(profile, dictionaries.BUILTIN_PROFILE)
			self.assertIsNone(dictionaries.active_directory(data_directory, profile))

	def test_root_dictionary_files_migrate_to_a_legacy_profile(self):
		with tempfile.TemporaryDirectory() as data_directory:
			old_path = os.path.join(data_directory, "enumain.dic")
			with open(old_path, "wb") as dictionary:
				dictionary.write(b"old\tpronunciation\n")

			profile = dictionaries.resolve_profile({}, data_directory)

			self.assertEqual(profile, dictionaries.LEGACY_PROFILE)
			legacy_directory = dictionaries.profile_directory(data_directory, profile)
			self.assertEqual(dictionaries.dictionary_files(legacy_directory), ["enumain.dic"])
			self.assertFalse(os.path.exists(old_path))

	def test_explicit_builtin_profile_overrides_retained_legacy_files(self):
		with tempfile.TemporaryDirectory() as data_directory:
			legacy_directory = dictionaries.profile_directory(data_directory, dictionaries.LEGACY_PROFILE)
			os.makedirs(legacy_directory)
			with open(os.path.join(legacy_directory, "enumain.dic"), "wb") as dictionary:
				dictionary.write(b"word\ttranslation\n")

			profile = dictionaries.resolve_profile(
				{"dictionary_profile": dictionaries.BUILTIN_PROFILE},
				data_directory,
			)
			self.assertEqual(profile, dictionaries.BUILTIN_PROFILE)
			self.assertIsNone(dictionaries.active_directory(data_directory, profile))

	def test_provider_update_replaces_snapshot_and_prefers_repository_root_files(self):
		with tempfile.TemporaryDirectory() as data_directory, tempfile.TemporaryDirectory() as source:
			archive = os.path.join(source, "snapshot.zip")
			with zipfile.ZipFile(archive, "w") as package:
				package.writestr("repository-master/enumain.dic", b"root\tpreferred\n")
				package.writestr("repository-master/version/ENUmain.dic", b"nested\tnot used\n")
				package.writestr("repository-master/ENUroot.dic", b"rootword\ttranslation\n")
				package.writestr("repository-master/README.md", b"ignored")

			with mock.patch.object(
				dictionaries,
				"_download",
				side_effect=lambda _url, destination: shutil.copy2(archive, destination),
			):
				result = dictionaries.update_profile(data_directory, dictionaries.ALTERNATIVE_PROFILE)

			profile_directory = dictionaries.profile_directory(
				data_directory,
				dictionaries.ALTERNATIVE_PROFILE,
			)
			self.assertEqual(result, {"files": 2, "entries": 2})
			with open(os.path.join(profile_directory, "enumain.dic"), "rb") as dictionary:
				self.assertEqual(dictionary.read(), b"root\tpreferred\n")

			# A later snapshot is authoritative: removed entries and volumes disappear.
			with zipfile.ZipFile(archive, "w") as package:
				package.writestr("repository-master/enumain.dic", b"replacement\tentry\n")
			with mock.patch.object(
				dictionaries,
				"_download",
				side_effect=lambda _url, destination: shutil.copy2(archive, destination),
			):
				dictionaries.update_profile(data_directory, dictionaries.ALTERNATIVE_PROFILE)

			self.assertEqual(dictionaries.dictionary_files(profile_directory), ["enumain.dic"])
			with open(os.path.join(profile_directory, "enumain.dic"), "rb") as dictionary:
				self.assertEqual(dictionary.read(), b"replacement\tentry\n")

	def test_invalid_download_keeps_previous_snapshot(self):
		with tempfile.TemporaryDirectory() as data_directory, tempfile.TemporaryDirectory() as source:
			profile_directory = dictionaries.profile_directory(
				data_directory,
				dictionaries.COMMUNITY_PROFILE,
			)
			os.makedirs(profile_directory)
			with open(os.path.join(profile_directory, "enumain.dic"), "wb") as dictionary:
				dictionary.write(b"preserved\tentry\n")
			archive = os.path.join(source, "invalid.zip")
			with zipfile.ZipFile(archive, "w") as package:
				package.writestr("repository-master/README.md", b"no dictionaries")

			with mock.patch.object(
				dictionaries,
				"_download",
				side_effect=lambda _url, destination: shutil.copy2(archive, destination),
			):
				with self.assertRaises(ValueError):
					dictionaries.update_profile(data_directory, dictionaries.COMMUNITY_PROFILE)

			with open(os.path.join(profile_directory, "enumain.dic"), "rb") as dictionary:
				self.assertEqual(dictionary.read(), b"preserved\tentry\n")


if __name__ == "__main__":
	unittest.main()
