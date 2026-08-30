# Build customizations
# Change this file instead of SConstruct or manifest files, whenever possible.

import hashlib
import os
import subprocess
from pathlib import Path


_DEVELOPMENT_VERSION = "v19.5-RS-dev"


def _run_git(*args):
	return subprocess.run(
		["git", *args],
		capture_output=True,
		check=False,
	)


def _get_development_source_hash():
	"""Hash Git state plus packaged files, including new untracked assets."""
	try:
		head = _run_git("rev-parse", "HEAD")
		diff = _run_git(
			"diff",
			"--no-ext-diff",
			"--binary",
			"HEAD",
			"--",
			".",
			":(exclude)addon/manifest.ini",
		)
		if head.returncode == 0 and diff.returncode == 0:
			digest = hashlib.sha256(
				head.stdout.rstrip() + b"\0" + diff.stdout + b"\0" + _addon_tree_fingerprint()
			).hexdigest()
			return digest[:8]
	except FileNotFoundError:
		pass
	return "unknown"


def _addon_tree_fingerprint(root=Path("addon")):
	"""Fingerprint exactly the files that can enter the add-on archive."""
	digest = hashlib.sha256()
	if not root.is_dir():
		return digest.digest()
	for path in sorted((path for path in root.rglob("*") if path.is_file()), key=lambda path: path.as_posix()):
		relative = path.relative_to(root).as_posix()
		if relative == "manifest.ini" or "__pycache__" in path.parts:
			continue
		digest.update(relative.encode("utf-8"))
		digest.update(b"\0")
		digest.update(path.read_bytes())
		digest.update(b"\0")
	return digest.digest()


def _get_version():
	"""Use release tags exactly; otherwise identify the 19.5 development source.

	The source hash includes tracked changes and all packaged file content, so
	iterative test packages cannot accidentally reuse a previous identity.
	"""
	override = os.environ.get("ELOQUENCE_BUILD_VERSION", "").strip()
	if override:
		return override
	try:
		result = _run_git("describe", "--tags", "--exact-match")
		if result.returncode == 0:
			return result.stdout.decode().strip()
	except FileNotFoundError:
		pass
	return f"{_DEVELOPMENT_VERSION}-{_get_development_source_hash()}"


addon_info = {
	"addon_name": "Eloquence",
	"addon_summary": "Eloquence64RS Synthesizer",
	"addon_description": "Community-maintained Eloquence synthesizer for 64-bit NVDA with a Rust host",
	"addon_version": _get_version(),
	"addon_author": "Nick Giannak III and contributors",
	"addon_url": "https://github.com/Nick6489/Eloquence64RS",
	"addon_lastTestedNVDAVersion": "2026.1",
}
