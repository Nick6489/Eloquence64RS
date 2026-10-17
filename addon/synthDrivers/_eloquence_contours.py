"""In-process ctypes adapter for OpenEVV or any mono PCM16 synthesizer."""
import ctypes
import threading
from pathlib import Path


class Contour:
    """One stream. Select the DLL matching the *calling process* architecture.

    mode: 0 Raw, 1 Classic Presence/native Smooth, 2 Presence,
    3 Presence treble (11025 Hz only).
    Recreate at a rate/mode transition; reset on cancellation or a new stream.
    """

    def __init__(self, dll_path, sample_rate=16000, mode=0):
        self._lock = threading.RLock()
        self._handle = None
        self._dll = ctypes.CDLL(str(Path(dll_path).resolve()))
        signatures = {
            "abi_version": ([], ctypes.c_uint32),
            "create": ([ctypes.c_uint32, ctypes.c_uint32], ctypes.c_void_p),
            "destroy": ([ctypes.c_void_p], None),
            "reset": ([ctypes.c_void_p], ctypes.c_int32),
            "output_rate": ([ctypes.c_void_p], ctypes.c_uint32),
            "output_count": ([ctypes.c_void_p, ctypes.c_size_t], ctypes.c_size_t),
            "process": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t,
                         ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t], ctypes.c_int32),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self._dll, "ec_contour_" + name)
            fn.argtypes, fn.restype = args, result
        if self._dll.ec_contour_abi_version() != 1:
            raise RuntimeError("Unsupported contour ABI")
        if (sample_rate not in (11025, 16000) or mode not in (0, 1, 2, 3)
                or (mode == 3 and sample_rate != 11025)):
            raise ValueError("Unsupported contour rate or mode")
        self._handle = self._dll.ec_contour_create(sample_rate, mode)
        if not self._handle:
            raise RuntimeError("Could not create contour")
        self.output_rate = self._dll.ec_contour_output_rate(self._handle)

    def _require_open(self):
        if not self._handle:
            raise RuntimeError("Contour is closed")

    def process(self, pcm):
        """Return PCM bytes; empty index/final events remain empty."""
        with self._lock:
            self._require_open()
            if len(pcm) % 2:
                raise ValueError("PCM16 data must contain complete samples")
            count = len(pcm) // 2
            capacity = self._dll.ec_contour_output_count(self._handle, count)
            if capacity == ctypes.c_size_t(-1).value:
                raise ValueError("PCM buffer too large")
            source = (ctypes.c_int16 * count).from_buffer_copy(pcm)
            output = (ctypes.c_int16 * capacity)()
            result = self._dll.ec_contour_process(self._handle, source, count, output, capacity)
            if result:
                raise RuntimeError(f"Contour processing failed: {result}")
            return bytes(output)

    def reset(self):
        with self._lock:
            self._require_open()
            if self._dll.ec_contour_reset(self._handle):
                raise RuntimeError("Could not reset contour")

    def close(self):
        with self._lock:
            if self._handle:
                self._dll.ec_contour_destroy(self._handle)
                self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
