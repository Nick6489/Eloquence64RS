"""Windows Job Object backstop for the native Eloquence host.

The Rust host normally observes NVDA disappearing when its inherited stdin
pipe reaches EOF. It can then release ECI and exit without intervention.
However, a host whose worker is permanently blocked inside ECI cannot finish
joining that worker, so it could outlive a crashed or force-killed NVDA process.

A Job Object configured with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` closes
that gap without host cooperation. NVDA holds the only job handle; Windows
closes it when NVDA exits for any reason and terminates any host still inside.

This is strictly a last-resort backstop. Normal synth termination continues to
use the protocol's Delete command and waits for clean process exit. Every Job
Object failure is logged and ignored because missing hardening must never stop
Eloquence from speaking.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Optional

LOGGER = logging.getLogger(__name__)

# winnt.h: terminate all processes when the last job handle closes.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
# JOBOBJECTINFOCLASS.JobObjectExtendedLimitInformation.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

# ULONG_PTR and SIZE_T are pointer-sized on both supported architectures.
_ULONG_PTR = ctypes.c_size_t


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
	_fields_ = [
		("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
		("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
		("LimitFlags", wintypes.DWORD),
		("MinimumWorkingSetSize", _ULONG_PTR),
		("MaximumWorkingSetSize", _ULONG_PTR),
		("ActiveProcessLimit", wintypes.DWORD),
		("Affinity", _ULONG_PTR),
		("PriorityClass", wintypes.DWORD),
		("SchedulingClass", wintypes.DWORD),
	]


class _IO_COUNTERS(ctypes.Structure):
	_fields_ = [
		("ReadOperationCount", ctypes.c_ulonglong),
		("WriteOperationCount", ctypes.c_ulonglong),
		("OtherOperationCount", ctypes.c_ulonglong),
		("ReadTransferCount", ctypes.c_ulonglong),
		("WriteTransferCount", ctypes.c_ulonglong),
		("OtherTransferCount", ctypes.c_ulonglong),
	]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
	_fields_ = [
		("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
		("IoInfo", _IO_COUNTERS),
		("ProcessMemoryLimit", _ULONG_PTR),
		("JobMemoryLimit", _ULONG_PTR),
		("PeakProcessMemoryUsed", _ULONG_PTR),
		("PeakJobMemoryUsed", _ULONG_PTR),
	]


def _kernel32() -> ctypes.WinDLL:
	kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
	kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
	kernel32.CreateJobObjectW.restype = wintypes.HANDLE
	kernel32.SetInformationJobObject.argtypes = [
		wintypes.HANDLE,
		ctypes.c_int,
		wintypes.LPVOID,
		wintypes.DWORD,
	]
	kernel32.SetInformationJobObject.restype = wintypes.BOOL
	kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
	kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
	kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
	kernel32.CloseHandle.restype = wintypes.BOOL
	return kernel32


class HostJob:
	"""A kill-on-close Job Object containing each native host we launch."""

	def __init__(self, handle: int, kernel32: ctypes.WinDLL):
		self._handle: Optional[int] = handle
		self._kernel32 = kernel32
		self._lock = threading.Lock()

	@classmethod
	def create(cls) -> Optional["HostJob"]:
		"""Return a kill-on-close job, or None if Windows refuses it."""
		try:
			kernel32 = _kernel32()
			handle = kernel32.CreateJobObjectW(None, None)
			if not handle:
				raise ctypes.WinError(ctypes.get_last_error())
			info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
			info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
			if not kernel32.SetInformationJobObject(
				handle,
				_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
				ctypes.byref(info),
				ctypes.sizeof(info),
			):
				error = ctypes.WinError(ctypes.get_last_error())
				kernel32.CloseHandle(handle)
				raise error
		except Exception:
			LOGGER.warning(
				"Could not create the Eloquence host Job Object; a wedged host may "
				"outlive NVDA after an unclean exit",
				exc_info=True,
			)
			return None
		return cls(handle, kernel32)

	def assign(self, process_handle: int) -> bool:
		"""Assign a spawned process to the job, returning whether it is covered."""
		with self._lock:
			if self._handle is None:
				return False
			try:
				if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
					raise ctypes.WinError(ctypes.get_last_error())
			except Exception:
				LOGGER.warning(
					"Could not assign the Eloquence host to its Job Object; it will "
					"not be killed automatically if NVDA exits uncleanly",
					exc_info=True,
				)
				return False
		return True

	def close(self) -> None:
		"""Close the job and terminate members; used only to verify behavior."""
		with self._lock:
			handle, self._handle = self._handle, None
		if handle is not None:
			self._kernel32.CloseHandle(handle)
