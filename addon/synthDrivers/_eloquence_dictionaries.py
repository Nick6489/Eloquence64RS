"""Dictionary profile storage and updates for the Eloquence driver."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from collections import OrderedDict


BUILTIN_PROFILE = "builtin"
ALTERNATIVE_PROFILE = "alternative"
COMMUNITY_PROFILE = "community"
LEGACY_PROFILE = "legacy"
PROFILE_DIRECTORY_NAME = "dictionaries"

PROVIDERS = OrderedDict(
	(
		(
			ALTERNATIVE_PROFILE,
			"https://github.com/mohamed00/AltIBMTTSDictionaries/archive/refs/heads/master.zip",
		),
		(
			COMMUNITY_PROFILE,
			"https://github.com/eigencrow/IBMTTSDictionaries/archive/refs/heads/master.zip",
		),
	)
)

_DICTIONARY_FILENAME = re.compile(r"^(?:[a-z]{3})?(?:main|root|abbr)\.dic$", re.IGNORECASE)


def profiles_directory(data_directory: str) -> str:
	return os.path.join(data_directory, PROFILE_DIRECTORY_NAME)


def profile_directory(data_directory: str, profile: str) -> str | None:
	if profile == BUILTIN_PROFILE:
		return None
	if profile not in {*PROVIDERS, LEGACY_PROFILE}:
		raise ValueError(f"unknown Eloquence dictionary profile: {profile}")
	return os.path.join(profiles_directory(data_directory), profile)


def dictionary_files(directory: str | None) -> list[str]:
	if not directory or not os.path.isdir(directory):
		return []
	return sorted(
		(
			name
			for name in os.listdir(directory)
			if name.lower().endswith(".dic") and os.path.isfile(os.path.join(directory, name))
		),
		key=str.lower,
	)


def migrate_legacy_files(data_directory: str) -> bool:
	"""Move old root-level dictionary files into the isolated legacy profile."""
	legacy_files = dictionary_files(data_directory)
	if not legacy_files:
		return bool(dictionary_files(profile_directory(data_directory, LEGACY_PROFILE)))

	legacy_directory = profile_directory(data_directory, LEGACY_PROFILE)
	assert legacy_directory is not None
	os.makedirs(legacy_directory, exist_ok=True)
	for filename in legacy_files:
		source = os.path.join(data_directory, filename)
		target = os.path.join(legacy_directory, filename)
		if os.path.exists(target):
			# The root-level file is the active pre-profile copy, so preserve it.
			os.replace(source, target)
		else:
			shutil.move(source, target)
	return True


def resolve_profile(configuration, data_directory: str, *, migrate: bool = True) -> str:
	"""Resolve old and new configuration without changing existing pronunciation."""
	configured = configuration.get("dictionary_profile")
	if configured in {BUILTIN_PROFILE, *PROVIDERS, LEGACY_PROFILE}:
		return configured

	legacy_available = False
	if migrate:
		try:
			legacy_available = migrate_legacy_files(data_directory)
		except OSError:
			# Secure/read-only configurations can still use the old root layout.
			legacy_available = bool(dictionary_files(data_directory))
	else:
		legacy_available = bool(dictionary_files(profile_directory(data_directory, LEGACY_PROFILE)))
		legacy_available = legacy_available or bool(dictionary_files(data_directory))
	return LEGACY_PROFILE if legacy_available else BUILTIN_PROFILE


def active_directory(data_directory: str, profile: str) -> str | None:
	"""Return the installed profile directory, or None for built-in behavior."""
	if profile == BUILTIN_PROFILE:
		return None
	profile_path = profile_directory(data_directory, profile)
	if dictionary_files(profile_path):
		return profile_path
	# A read-only legacy installation may not have been migrated yet.
	if profile == LEGACY_PROFILE and dictionary_files(data_directory):
		return data_directory
	return None


def update_profile(data_directory: str, profile: str) -> dict[str, int]:
	"""Download and atomically replace one provider profile."""
	try:
		url = PROVIDERS[profile]
	except KeyError as error:
		raise ValueError("only downloaded dictionary profiles can be updated") from error

	profile_root = profiles_directory(data_directory)
	os.makedirs(profile_root, exist_ok=True)
	archive_fd, archive_path = tempfile.mkstemp(prefix="eloquence-dictionaries-", suffix=".zip")
	os.close(archive_fd)
	staging_directory = tempfile.mkdtemp(prefix=f".{profile}-", dir=profile_root)
	try:
		_download(url, archive_path)
		selected = _read_dictionary_snapshot(archive_path)
		entry_count = 0
		for filename, contents in selected.items():
			_validate_dictionary(filename, contents)
			with open(os.path.join(staging_directory, filename.lower()), "wb") as dictionary:
				dictionary.write(contents)
			entry_count += _entry_count(contents)

		target_directory = profile_directory(data_directory, profile)
		assert target_directory is not None
		_replace_directory(staging_directory, target_directory)
		staging_directory = ""
		return {"files": len(selected), "entries": entry_count}
	finally:
		try:
			os.remove(archive_path)
		except FileNotFoundError:
			pass
		if staging_directory:
			shutil.rmtree(staging_directory, ignore_errors=True)


def _download(url: str, destination: str) -> None:
	with urllib.request.urlopen(url, timeout=30) as response, open(destination, "wb") as archive:
		while chunk := response.read(64 * 1024):
			archive.write(chunk)


def _read_dictionary_snapshot(archive_path: str) -> dict[str, bytes]:
	selected: dict[str, tuple[tuple[int, str], bytes]] = {}
	with zipfile.ZipFile(archive_path, "r") as archive:
		for member in archive.infolist():
			if member.is_dir():
				continue
			parts = member.filename.replace("\\", "/").split("/")
			filename = parts[-1]
			if not _DICTIONARY_FILENAME.fullmatch(filename):
				continue
			# Prefer repository-root dictionaries over version-specific copies.
			rank = (len(parts), member.filename.lower())
			key = filename.lower()
			if key not in selected or rank < selected[key][0]:
				selected[key] = (rank, archive.read(member))
	if not selected:
		raise ValueError("download contained no supported Eloquence dictionary files")
	return {filename: selected[filename][1] for filename in sorted(selected)}


def _validate_dictionary(filename: str, contents: bytes) -> None:
	if not contents:
		raise ValueError(f"downloaded dictionary is empty: {filename}")
	if b"\0" in contents:
		raise ValueError(f"downloaded dictionary contains invalid null bytes: {filename}")
	# ECI uses the active Windows ANSI code page for non-Asian dictionary files.
	contents.decode("cp1252")


def _entry_count(contents: bytes) -> int:
	return sum(
		1 for line in contents.splitlines() if line.strip() and not line.lstrip().startswith((b"#", b";"))
	)


def _replace_directory(staging_directory: str, target_directory: str) -> None:
	backup_directory = target_directory + ".previous"
	if os.path.exists(backup_directory):
		shutil.rmtree(backup_directory)
	if os.path.exists(target_directory):
		os.replace(target_directory, backup_directory)
	try:
		os.replace(staging_directory, target_directory)
	except Exception:
		if os.path.exists(backup_directory):
			os.replace(backup_directory, target_directory)
		raise
	if os.path.exists(backup_directory):
		shutil.rmtree(backup_directory)
