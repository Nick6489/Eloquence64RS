import tempfile
import unittest
import zipfile
from pathlib import Path

from site_scons.site_tools.NVDATool.addon import createAddonBundleFromPath


_REPO_ROOT = Path(__file__).resolve().parents[1]


class AddonPackagingTests(unittest.TestCase):
	def test_nvda_addon_bundle_includes_script_conversion_data(self):
		with tempfile.TemporaryDirectory() as root:
			addon_path = Path(root) / "Eloquence-test.nvda-addon"
			createAddonBundleFromPath(_REPO_ROOT / "addon", addon_path, excludePatterns=())

			with zipfile.ZipFile(addon_path) as addon:
				bundled_files = set(addon.namelist())

		for file_name in ("TSCharacters.txt", "TSPhrases.txt", "LICENSE", "PROVENANCE.md"):
			with self.subTest(file_name=file_name):
				self.assertIn(f"synthDrivers/t2s_data/{file_name}", bundled_files)

		self.assertEqual(
			[name for name in bundled_files if name.endswith("eloquence_host32.exe")],
			["synthDrivers/eloquence_host32.exe"],
		)
		for retired_file in (
			"eloquence_host32_native.exe",
			"host_eloquence32.py",
			"_eloquence_ipc.py",
		):
			with self.subTest(retired_file=retired_file):
				self.assertFalse(any(name.endswith(retired_file) for name in bundled_files))


if __name__ == "__main__":
	unittest.main()
