"""Client side helper for communicating with the 32-bit Eloquence host."""

from __future__ import annotations

import itertools
import logging
import os

import queue
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ._eloquence_native import NativeHostConnection

from . import _eloquence_job as _job

import config
import globalVars
import nvwave
from buildVersion import version_year

from . import _eloquence_dictionaries

LOGGER = logging.getLogger(__name__)

HOST_EXECUTABLE = "eloquence_host32.exe"
AUTH_KEY_BYTES = 16
STANDARD_SAMPLE_RATE = 11025
NATIVE_SAMPLE_RATE = 16000
PRESENCE_SAMPLE_RATE = 22050
_sample_rate = STANDARD_SAMPLE_RATE
_presence_contour = False
_current_variant = 0
# Seconds to let the native host finish releasing ECI and exit before forcing
# it to stop.
HOST_EXIT_TIMEOUT = 3.0


# Audio handling -----------------------------------------------------------------
class AudioWorker(threading.Thread):
	_CHANNELS = 1
	_BITS_PER_SAMPLE = 16
	_SAMPLE_RATE = 11025
	# NVDA's WASAPI player accepts a zero-byte feed with an onDone callback.
	# It records the current stream position without adding an audible sample.
	_INDEX_MARKER_AUDIO = b""

	def __init__(
		self,
		player: nvwave.WavePlayer,
		queue: "queue.Queue[Optional[AudioChunk]]",
		client: "EloquenceHostClient",
	):
		super().__init__(daemon=True)
		self._player = player
		self._queue = queue
		self._client = client
		self._running = True
		self._stopping = False
		self._player_lock = threading.RLock()

	def run(self) -> None:
		while self._running:
			try:
				chunk = self._queue.get(timeout=0.1)
			except queue.Empty:
				continue
			if chunk is None:
				break
			data, index, is_final, seq = chunk
			if seq < self._client._sequence:
				self._queue.task_done()
				continue

			# --- New logic (EARCONS patch) ---
			if not data:
				if not self._stopping:
					if index is not None:
						# The host emits indexes as separate zero-length chunks.
						# Queue a zero-byte marker so WavePlayer invokes the callback
						# after preceding audio, while later audio can still be fed.
						self._queue_index_marker(index)
					if is_final:
						self._schedule_idle()
				self._queue.task_done()
				continue
			# ------------------------------------

			on_done = None
			if index is not None:

				def _callback(i=index):
					self._invoke_index_callback(i)

				on_done = _callback

			wrapped_on_done = self._make_on_done(on_done, is_final)

			# Early exit if stopping - avoids unnecessary lock acquisition
			if self._stopping:
				self._queue.task_done()
				continue

			# Feed directly - blocks if buffer is full
			try:
				with self._player_lock:
					if not self._stopping:
						if self._player:
							self._player.feed(data, onDone=wrapped_on_done)
			except FileNotFoundError:
				LOGGER.warning("Sound device not found during feed")
			except Exception:
				LOGGER.exception("WavePlayer feed failed")
			self._queue.task_done()

	def stop(self) -> None:
		self._stopping = True
		self._running = False
		self._queue.put(None)

	def _make_on_done(self, callback, is_final: bool):
		def _on_done() -> None:
			try:
				if callback:
					callback()
			except Exception:
				LOGGER.exception("Index callback failed")
			if is_final:
				self._schedule_idle()

		return _on_done

	def _schedule_idle(self) -> None:
		"""Signal the player that playback is complete."""
		try:
			with self._player_lock:
				if not self._stopping and self._player:
					self._player.idle()
		except Exception:
			LOGGER.exception("WavePlayer idle failed")
		if not self._stopping:
			self._invoke_index_callback(None)

	def _queue_index_marker(self, index: int) -> None:
		"""Queue a zero-byte, non-blocking playback marker for a Speech Index."""
		try:
			with self._player_lock:
				if not self._stopping and self._player:
					self._player.feed(
						self._INDEX_MARKER_AUDIO,
						onDone=lambda: self._invoke_index_callback(index),
					)
		except Exception:
			LOGGER.exception("WavePlayer index marker feed failed")

	def _invoke_index_callback(self, value: Optional[int]) -> None:
		global lastindex
		if value is not None:
			lastindex = value
		if onIndexReached:
			try:
				onIndexReached(value)
			except Exception:
				LOGGER.exception("Index callback failed")


AudioChunk = Tuple[bytes, Optional[int], bool, int]


# RPC client ---------------------------------------------------------------------
@dataclass
class HostProcess:
	process: subprocess.Popen
	connection: Any


class EloquenceHostClient:
	def __init__(self) -> None:
		self._host: Optional[HostProcess] = None
		# One job covers every replacement host and stays open until NVDA exits.
		self._job: Optional[_job.HostJob] = None
		self._pending: Dict[int, threading.Event] = {}
		self._responses: Dict[int, Dict[str, Any]] = {}
		self._receiver: Optional[threading.Thread] = None
		self._id_counter = itertools.count(1)
		self._audio_queue: "queue.Queue[Optional[AudioChunk]]" = queue.Queue()
		self._player: Optional[nvwave.WavePlayer] = None
		self._audio_worker: Optional[AudioWorker] = None
		self._running = threading.Event()
		self._command_lock = threading.Lock()
		self._stop_lock = threading.RLock()
		self._sequence = 0
		self._current_seq = 0
		self._speaking = False

	# ------------------------------------------------------------------
	def ensure_started(self) -> None:
		if self._host:
			return
		addon_dir = os.path.abspath(os.path.dirname(__file__))
		self._host = self._start_host(addon_dir)
		self._receiver = threading.Thread(target=self._receiver_loop, daemon=True)
		self._receiver.start()

	def _start_host(self, addon_dir: str) -> HostProcess:
		authkey = os.urandom(AUTH_KEY_BYTES)
		cmd = [self._resolve_host_executable(addon_dir)]
		cmd.extend(["--auth-key", authkey.hex()])
		# The key is only an ephemeral handshake nonce for the inherited stdio
		# channel. Do not expose it in NVDA logs or diagnostic bundles.
		LOGGER.info("Launching Eloquence host: %s --auth-key <redacted>", cmd[0])
		proc = subprocess.Popen(
			cmd,
			cwd=addon_dir,
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
			creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
		)
		self._adopt_into_job(proc)
		if proc.stdin is None or proc.stdout is None:
			self._terminate_process(proc)
			raise RuntimeError("native Eloquence host pipes were not created")

		result: "queue.Queue[object]" = queue.Queue(maxsize=1)

		def _handshake() -> None:
			try:
				result.put(NativeHostConnection(proc.stdout, proc.stdin, authkey))
			except BaseException as error:
				result.put(error)

		threading.Thread(target=_handshake, name="EloquenceNativeHandshake", daemon=True).start()
		try:
			connection = result.get(timeout=5.0)
		except queue.Empty as exc:
			self._terminate_process(proc)
			raise RuntimeError("native Eloquence host handshake timed out") from exc
		if isinstance(connection, BaseException):
			self._terminate_process(proc)
			raise RuntimeError(f"native Eloquence host handshake failed: {connection}") from connection
		return HostProcess(process=proc, connection=connection)

	def _adopt_into_job(self, process: subprocess.Popen) -> None:
		"""Cover a new native host with the kill-on-NVDA-exit backstop."""
		if self._job is None:
			self._job = _job.HostJob.create()
		if self._job is None:
			return
		try:
			handle = int(process._handle)
		except Exception:
			LOGGER.warning(
				"Eloquence host exposes no process handle; skipping Job Object assignment",
				exc_info=True,
			)
			return
		self._job.assign(handle)

	@staticmethod
	def _terminate_process(process: subprocess.Popen) -> None:
		try:
			process.terminate()
			process.wait(timeout=2)
		except Exception:
			try:
				process.kill()
			except Exception:
				pass

	def _resolve_host_executable(self, addon_dir: str) -> str:
		exe_path = os.path.join(addon_dir, HOST_EXECUTABLE)
		if os.path.exists(exe_path):
			return exe_path
		raise RuntimeError("Eloquence helper resources missing from add-on package")

	# ------------------------------------------------------------------
	def initialize_audio(self) -> None:
		if self._player:
			return
		sample_rate = get_output_sample_rate()
		if version_year >= 2025:
			device = config.conf["audio"]["outputDevice"]
			player = nvwave.WavePlayer(1, sample_rate, 16, outputDevice=device)
		else:
			device = config.conf["speech"]["outputDevice"]
			nvwave.WavePlayer.MIN_BUFFER_MS = 1500
			player = nvwave.WavePlayer(1, sample_rate, 16, outputDevice=device, buffered=True)
		self._player = player
		self._audio_worker = AudioWorker(player, self._audio_queue, self)
		self._audio_worker.start()

	# ------------------------------------------------------------------
	def close_audio(self) -> None:
		if self._audio_worker:
			self._audio_worker.stop()
			self._audio_worker.join(timeout=1)
			self._audio_worker = None
		if self._player:
			try:
				self._player.close()
			except Exception:
				LOGGER.exception("WavePlayer close failed")
			self._player = None

	# ------------------------------------------------------------------
	def _receiver_loop(self) -> None:
		connection = self._host.connection if self._host else None
		if connection is None:
			return
		while True:
			try:
				message = connection.recv()
			except (EOFError, ConnectionAbortedError, OSError):
				LOGGER.info("Host connection closed")
				for msg_id, event in list(self._pending.items()):
					self._responses[msg_id] = {"error": "connectionClosed"}
					event.set()
				self._pending.clear()
				break
			except Exception:
				LOGGER.exception("Unexpected error in receiver loop")
				for msg_id, event in list(self._pending.items()):
					self._responses[msg_id] = {"error": "receiverException"}
					event.set()
				self._pending.clear()
				break
			msg_type = message.get("type")
			if msg_type == "response":
				msg_id = message["id"]
				# MEMORY LEAK PATCH: Only save if an event is waiting
				event = self._pending.pop(msg_id, None)
				if event:
					self._responses[msg_id] = message
					event.set()
			elif msg_type == "event":
				self._handle_event(message["event"], message.get("payload", {}))
			else:
				LOGGER.warning("Unknown message type %s", msg_type)

	def _handle_event(self, event: str, payload: Dict[str, Any]) -> None:
		if event == "audio":
			data = payload.get("data", b"")
			index = payload.get("index")
			is_final = bool(payload.get("final", False))
			seq = self._current_seq
			self._audio_queue.put((data, index, is_final, seq))
		elif event == "stopped":
			# Don't call player.stop() from this thread to avoid race conditions
			# The stop() method will handle player cleanup properly
			LOGGER.debug("Host reported stopped event")
			self._speaking = False
		else:
			LOGGER.debug("Unhandled host event %s", event)

	# ------------------------------------------------------------------
	def prepare_generation(self) -> None:
		connection = self._host.connection if self._host else None
		if connection is not None:
			connection.prepare_generation()

	def stop(self) -> None:
		if not self._host:
			return
		self._sequence += 1
		# Stop local audio player immediately
		if self._player:
			try:
				self._player.stop()
			except Exception:
				LOGGER.exception("WavePlayer stop failed")
		# Tell the host to stop without blocking
		try:
			self.send_command("stop", wait=False)
		except Exception:
			pass

	# ------------------------------------------------------------------
	def send_command(self, command: str, wait: bool = True, **payload: Any) -> Dict[str, Any]:
		if not self._host:
			raise RuntimeError("Host not started")
		event = None
		with self._command_lock:
			msg_id = next(self._id_counter)
			event = threading.Event() if wait else None
			if wait:
				self._pending[msg_id] = event
			try:
				self._host.connection.send(
					{
						"type": "command",
						"id": msg_id,
						"command": command,
						"payload": payload,
					}
				)
			except (ConnectionResetError, BrokenPipeError, OSError):
				if wait:
					self._pending.pop(msg_id, None)
				raise
			except Exception:
				if wait:
					self._pending.pop(msg_id, None)
				raise

		# Waiting must not hold the write lock: Stop is sent from NVDA's control
		# thread while Synthesize is waiting on the worker thread.
		if not wait:
			return {}
		assert event is not None
		if not event.wait(timeout=5.0):
			self._pending.pop(msg_id, None)
			LOGGER.error("Command %s timed out after 5 seconds", command)
			raise RuntimeError(f"Command {command} timed out")
		response = self._responses.pop(msg_id, {"error": "no response received"})
		if "error" in response:
			raise RuntimeError(response["error"])
		return response.get("payload", {})

	# ------------------------------------------------------------------
	def shutdown(self) -> None:
		if not self._host:
			return
		# Stop audio worker first
		if self._audio_worker:
			self._audio_worker.stop()
			self._audio_worker.join(timeout=1)
			self._audio_worker = None
		if self._player:
			self._player.close()
			self._player = None
		# Ask the host worker to release the ECI runtime and exit. The inherited
		# stdout pipe then closes, which ends the receiver thread with EOFError.
		try:
			self.send_command("delete")
		except Exception:
			LOGGER.exception("Failed to delete host cleanly")
		# The Delete response is sent before the Rust worker finishes dropping its
		# runtime. Wait for process exit before closing either inherited pipe so the
		# host can finish releasing ECI without losing its control channel.
		exited = False
		try:
			self._host.process.wait(timeout=HOST_EXIT_TIMEOUT)
			exited = True
		except Exception:
			LOGGER.warning(
				"Eloquence host did not exit within %ss; terminating",
				HOST_EXIT_TIMEOUT,
			)
		# Wait for receiver thread to finish (it will get EOFError and exit)
		if self._receiver:
			self._receiver.join(timeout=2)
			self._receiver = None
		# Now close the inherited pipes, and terminate only if the host is still up.
		try:
			self._host.connection.close()
		except Exception:
			pass
		if not exited:
			# This is the last-resort path for a native host wedged in ECI. On Windows,
			# terminate() calls TerminateProcess and does not run normal cleanup.
			try:
				self._host.process.terminate()
				self._host.process.wait(timeout=2)
			except Exception:
				LOGGER.exception("Failed to terminate host process")
				try:
					self._host.process.kill()
				except Exception:
					pass
		self._host = None


_client = EloquenceHostClient()
synth_queue = queue.Queue()
params: Dict[int, int] = {}
voice_params: Dict[int, int] = {}
lastindex: Optional[int] = None
onIndexReached = None
_synth_worker: Optional[threading.Thread] = None
_synth_worker_lock = threading.Lock()
_synth_worker_stop = threading.Event()


# Public API ---------------------------------------------------------------------
hsz = 1
pitch = 2
fluctuation = 3
rgh = 4
bth = 5
rate = 6
vlm = 7
PARAM_MAX = {
	rate: 250,
	pitch: 100,
	vlm: 100,
	hsz: 100,
	fluctuation: 100,
	rgh: 100,
	bth: 100,
}
eciPath = os.path.abspath(os.path.join(os.path.dirname(__file__), "eloquence", "eci.dll"))
langs = {
	"esm": (131073, "Latin American Spanish"),
	"esp": (131072, "Castilian Spanish"),
	"ptb": (458752, "Brazilian Portuguese"),
	"frc": (196609, "French Canadian"),
	"fra": (196608, "French"),
	"fin": (589824, "Finnish"),
	"deu": (262144, "German"),
	"ita": (327680, "Italian"),
	"enu": (65536, "American English"),
	"eng": (65537, "British English"),
	"chs": (393216, "Mandarin Chinese"),  # 0x00060000
	"jpn": (524288, "Japanese"),  # 0x00080000
	"kor": (655360, "Korean"),
}  # 0x000A0000


def initialize(indexCallback=None, sample_rate=None):
	global onIndexReached, _sample_rate, _current_variant
	eci_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "eloquence", "eci.dll"))
	try:
		onIndexReached = indexCallback
		if sample_rate is not None:
			_sample_rate = _normalize_sample_rate(sample_rate)
		voice_conf = config.conf.get("speech", {}).get("eci", {})
		language = voice_conf.get("voice", "enu")
		data_directory = os.path.dirname(eci_path)
		dictionary_profile = _eloquence_dictionaries.resolve_profile(
			config.conf.get("eloquence", {}),
			data_directory,
			migrate=not globalVars.appArgs.secure,
		)
		dictionary_directory = _eloquence_dictionaries.active_directory(
			data_directory,
			dictionary_profile,
		)
		_client.ensure_started()
		_current_variant = int(voice_conf.get("variant", _current_variant) or 0)
		payload = {
			"eciPath": eci_path,
			"dataDirectory": data_directory,
			"dictionaryDirectory": dictionary_directory or "",
			"language": language,
			"languageId": langs.get(language, langs["enu"])[0],
			"enableAbbreviationDict": config.conf.get("speech", {}).get("eci", {}).get("ABRDICT", False),
			"enablePhrasePrediction": config.conf.get("speech", {})
			.get("eci", {})
			.get("phrasePrediction", False),
			"voiceVariant": _current_variant,
			"sampleRate": _sample_rate,
		}
		response = _client.send_command("initialize", **payload)
		_client.send_command("setPresenceContour", enabled=_presence_contour)
		_client.initialize_audio()
		_ensure_synth_worker()
	except Exception:
		LOGGER.exception("Eloquence native host initialization failed")
		try:
			_client.shutdown()
		except Exception:
			LOGGER.exception("Eloquence native host cleanup failed")
		_stop_synth_worker()
		raise
	params.update(response.get("params", {}))
	voice_params.update(response.get("voiceParams", {}))


def speak(text_bytes):
	try:
		_client.send_command("addText", text=text_bytes, wait=False)
	except Exception:
		LOGGER.exception("Failed to send text to synthesizer")


def index(idx):
	try:
		_client.send_command("insertIndex", value=int(idx), wait=False)
	except Exception:
		LOGGER.exception("Failed to insert index")


def cmdProsody(pr, multiplier, offset=0):
	"""
	Apply a prosody change using the current base value from voice_params.

	Called at synthesis time so voice_params[pr] reflects the latest base.
	Computes: value = base * multiplier + offset
	For caps pitch: NVDA sends multiplier=1, offset=30 (or similar).
	For revert: NVDA sends multiplier=1, offset=0.
	Uses temporary=True so voice_params is never corrupted.
	"""
	base = getVParam(pr)
	value = int(base * multiplier + offset)
	# Clamp to valid ECI parameter range.
	value = max(0, min(value, PARAM_MAX.get(pr, 100)))
	setVParam(pr, value, temporary=True)


def synth():
	try:
		_client.send_command("synthesize")
	except Exception:
		LOGGER.exception("Failed to start synthesis")


def stop():
	_client.stop()


def pause(switch):
	if _client._player:
		_client._player.pause(switch)


def close_audio():
	_client.close_audio()


def get_presence_contour():
	return _presence_contour


def get_output_sample_rate():
	if _sample_rate == STANDARD_SAMPLE_RATE and _presence_contour:
		return PRESENCE_SAMPLE_RATE
	return _sample_rate


def set_presence_contour(enabled):
	"""Enable the rate-independent acoustic contour without changing PCM format."""
	global _presence_contour
	enabled = bool(enabled)
	if enabled == _presence_contour:
		return
	if _client._host:
		_client.stop()
		_client.send_command("setPresenceContour", enabled=enabled)
		if _sample_rate == STANDARD_SAMPLE_RATE:
			_client.close_audio()
	_presence_contour = enabled
	if _client._host and _sample_rate == STANDARD_SAMPLE_RATE:
		_client.initialize_audio()


def get_sample_rate():
	return _sample_rate


def _normalize_sample_rate(value):
	rate = int(value)
	if rate not in (STANDARD_SAMPLE_RATE, NATIVE_SAMPLE_RATE):
		raise ValueError(f"unsupported Eloquence sample rate: {value}")
	return rate


def set_sample_rate(value):
	"""Restart ECI into its selected native rate while preserving synth state."""
	global _sample_rate
	rate = _normalize_sample_rate(value)
	if rate == _sample_rate:
		return
	was_running = bool(_client._host)
	saved_params = dict(params)
	saved_voice_params = dict(voice_params)
	saved_variant = _current_variant
	callback = onIndexReached
	if was_running:
		_client.stop()
		_client.shutdown()
	_sample_rate = rate
	if not was_running:
		return
	initialize(callback, sample_rate=rate)
	# Restore the live engine state; this prevents a profile-rate change from
	# silently resetting the user's language, variant, or prosody settings.
	if 9 in saved_params:
		set_voice(saved_params[9])
	setVariant(saved_variant)
	for parameter, setting in saved_voice_params.items():
		setVParam(parameter, setting)


def set_dictionary_directory(directory, *, reload=False):
	"""Immediately activate an isolated dictionary profile, or ECI's built-in behavior."""
	if not _client._host:
		return
	_client.stop()
	_client.send_command(
		"setDictionaryDirectory",
		directory=os.path.abspath(directory) if directory else "",
		reload=bool(reload),
	)


def terminate():
	_client.shutdown()
	_stop_synth_worker()


def set_voice(vl):
	try:
		voice_id = int(vl)
		# Save the user-configured voice params before the language change.
		# The host re-reads all voice params from the DLL after eciSetParam(9),
		# but the DLL may still hold temporary prosody values (e.g. elevated
		# pitch for a capital letter).  If we blindly accept those re-read
		# values, the temporary pitch becomes the new permanent base and the
		# pitch never reverts -- the "stuck pitch on language change" bug.
		saved_vparams = dict(voice_params)
		response = _client.send_command("setParam", paramId=9, value=voice_id)
		params.update(response.get("params", {}))
		# Do NOT update voice_params from the response.  Instead, restore the
		# user's base values and push them to the DLL so the new language uses
		# the correct settings, not stuck temporary ones.
		for pr, val in saved_vparams.items():
			voice_params[pr] = val
			try:
				_client.send_command(
					"setVoiceParam",
					paramId=int(pr),
					value=int(val),
					temporary=False,
				)
			except Exception:
				pass
		LOGGER.debug("Voice changed to ID %d", voice_id)
	except Exception:
		LOGGER.exception("Failed to set voice")


def getVParam(pr):
	val = voice_params.get(pr, 0)
	return val


def setVParam(pr, vl, temporary=False):
	try:
		response = _client.send_command(
			"setVoiceParam",
			paramId=int(pr),
			value=int(vl),
			temporary=bool(temporary),
		)
		if not temporary:
			# The native host reads the value back from ECI in its response.  Keep
			# our shadow state authoritative instead of claiming success as soon as
			# an asynchronous command has merely been written to the pipe.  This is
			# especially important while NVDA reconstructs the driver for an audio
			# device profile change: initSettings must not return until that profile's
			# voice parameters have actually reached the replacement engine.
			applied = response.get("voiceParams", {}).get(pr, vl)
			voice_params[pr] = applied
			if applied != vl:
				LOGGER.warning(
					"Eloquence host read back voice parameter %s as %s after setting %s",
					pr,
					applied,
					vl,
				)
	except Exception:
		LOGGER.exception("Failed to set voice parameter")


def setVariant(v):
	global _current_variant
	try:
		_current_variant = int(v)
		response = _client.send_command("copyVoice", variant=_current_variant)
		voice_params.update(response.get("voiceParams", {}))
	except Exception:
		LOGGER.exception("Failed to set variant")


def process():
	_ensure_synth_worker()


def _synth_worker_loop() -> None:
	while True:
		try:
			item = synth_queue.get(timeout=0.1)
		except queue.Empty:
			if _synth_worker_stop.is_set():
				break
			continue
		if item is None:
			synth_queue.task_done()
			break
		lst, seq = item
		if seq < _client._sequence:
			synth_queue.task_done()
			continue
		_client._current_seq = seq
		_client.prepare_generation()
		try:
			for func, args in lst:
				if seq < _client._sequence:
					break
				try:
					func(*args)
				except Exception:
					LOGGER.exception("Synthesis command failed")
		finally:
			synth_queue.task_done()


def _ensure_synth_worker() -> None:
	global _synth_worker
	with _synth_worker_lock:
		if _synth_worker and _synth_worker.is_alive():
			return
		_synth_worker_stop.clear()
		_synth_worker = threading.Thread(target=_synth_worker_loop, name="EloquenceSynthWorker", daemon=True)
		_synth_worker.start()


def _stop_synth_worker() -> None:
	global _synth_worker
	with _synth_worker_lock:
		if not _synth_worker:
			return
		_synth_worker_stop.set()
		synth_queue.put(None)
		_synth_worker.join(timeout=1)
		if _synth_worker.is_alive():
			LOGGER.warning("Synthesis worker failed to terminate cleanly")
		_synth_worker = None
		_synth_worker_stop.clear()


def eciCheck() -> bool:
	eci_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "eloquence", "eci.dll"))
	return os.path.exists(eci_path)
