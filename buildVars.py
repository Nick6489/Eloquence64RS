# Build customizations
# Change this file instead of SConstruct or manifest files, whenever possible.

import subprocess


def _get_version():
	"""Derive addon version from git tags.

	- On exact tag: returns tag name (e.g. "v19.0-RS")
	- Between tags: returns describe output (e.g. "v19.0-RS-2-gabcdef")
	- No git / no tags: returns "dev"
	"""
	try:
		result = subprocess.run(
			["git", "describe", "--tags"],
			capture_output=True,
			text=True,
		)
		if result.returncode == 0:
			return result.stdout.strip()
	except FileNotFoundError:
		pass
	return "dev"


addon_info = {
	"addon_name": "Eloquence",
	"addon_summary": "Eloquence64RS Synthesizer",
	"addon_description": "Community-maintained Eloquence synthesizer for 64-bit NVDA with a Rust host",
	"addon_version": _get_version(),
	"addon_author": "Nick Giannak III and contributors",
	"addon_url": "https://github.com/Nick6489/Eloquence64RS",
	"addon_lastTestedNVDAVersion": "2026.1",
}
