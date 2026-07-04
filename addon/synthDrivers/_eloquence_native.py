"""Binary protocol adapter for the native Eloquence host."""

from __future__ import annotations

import struct
import threading
from typing import BinaryIO, Dict, Iterable, Tuple

MAGIC = b"ELQH"
VERSION = 1
HEADER = struct.Struct("<4sHHIII")
MAX_PAYLOAD = 4 * 1024 * 1024
AUTH_KEY_BYTES = 16

HELLO = 0x0001
INITIALIZE = 0x0010
BEGIN_GENERATION = 0x0011
ADD_TEXT = 0x0012
INSERT_INDEX = 0x0013
SYNTHESIZE = 0x0014
STOP = 0x0015
DELETE = 0x0016
SET_PARAM = 0x0020
SET_VOICE_PARAM = 0x0021
COPY_VOICE = 0x0022
HELLO_ACK = 0x8001
RESPONSE = 0x8002
ERROR_RESPONSE = 0x8003
AUDIO = 0x9000
INDEX = 0x9001
DONE = 0x9002
STOPPED = 0x9003


class NativeHostProtocolError(RuntimeError):
	pass


def _bytes(value: bytes) -> bytes:
	return struct.pack("<I", len(value)) + value


def _string(value: str) -> bytes:
	return _bytes(value.encode("utf-8"))


def _frame(kind: int, request_id: int, payload: bytes = b"") -> bytes:
	if len(payload) > MAX_PAYLOAD:
		raise NativeHostProtocolError("native host payload exceeds limit")
	return HEADER.pack(MAGIC, VERSION, kind, request_id, 0, len(payload)) + payload


def _read_exact(reader: BinaryIO, length: int) -> bytes:
	chunks = []
	remaining = length
	while remaining:
		chunk = reader.read(remaining)
		if not chunk:
			raise EOFError("native host closed its output")
		chunks.append(chunk)
		remaining -= len(chunk)
	return b"".join(chunks)


def _read_frame(reader: BinaryIO) -> Tuple[int, int, bytes]:
	header = _read_exact(reader, HEADER.size)
	magic, version, kind, request_id, flags, payload_length = HEADER.unpack(header)
	if magic != MAGIC:
		raise NativeHostProtocolError(f"invalid native host frame magic: {magic!r}")
	if version != VERSION:
		raise NativeHostProtocolError(f"unsupported native host protocol version: {version}")
	if flags:
		raise NativeHostProtocolError(f"unsupported native host frame flags: {flags:#x}")
	if payload_length > MAX_PAYLOAD:
		raise NativeHostProtocolError("native host payload exceeds limit")
	return kind, request_id, _read_exact(reader, payload_length)


class _PayloadReader:
	def __init__(self, payload: bytes):
		self._payload = payload
		self._offset = 0

	def _take(self, length: int) -> bytes:
		end = self._offset + length
		if end > len(self._payload):
			raise NativeHostProtocolError("truncated native host payload")
		value = self._payload[self._offset : end]
		self._offset = end
		return value

	def u8(self) -> int:
		return self._take(1)[0]

	def u32(self) -> int:
		return struct.unpack("<I", self._take(4))[0]

	def i32(self) -> int:
		return struct.unpack("<i", self._take(4))[0]

	def u64(self) -> int:
		return struct.unpack("<Q", self._take(8))[0]

	def bytes(self) -> bytes:
		return self._take(self.u32())

	def string(self) -> str:
		return self.bytes().decode("utf-8")

	def finish(self) -> None:
		if self._offset != len(self._payload):
			raise NativeHostProtocolError("native host payload contains trailing data")


class NativeHostConnection:
	"""Adapts native protocol frames to the legacy connection message shape."""

	def __init__(self, reader: BinaryIO, writer: BinaryIO, authkey: bytes):
		if len(authkey) != AUTH_KEY_BYTES:
			raise ValueError("native host authentication key must be 16 bytes")
		self._reader = reader
		self._writer = writer
		self._send_lock = threading.Lock()
		self._generation = 0
		self._generation_open = False
		self._active_generation = None
		self._write_frame(HELLO, 1, _bytes(authkey))
		kind, request_id, payload = _read_frame(self._reader)
		if kind != HELLO_ACK or request_id != 1 or payload:
			raise NativeHostProtocolError("native host rejected protocol handshake")

	def send(self, message: Dict) -> None:
		if message.get("type") != "command":
			raise NativeHostProtocolError("native connection accepts command messages only")
		request_id = int(message["id"])
		command = message["command"]
		payload = message.get("payload", {})
		with self._send_lock:
			if command in {"addText", "insertIndex"} and not self._generation_open:
				self._generation += 1
				self._active_generation = self._generation
				self._generation_open = True
				self._write_frame(BEGIN_GENERATION, 0, struct.pack("<Q", self._generation))
			kind, encoded = self._encode_command(command, payload)
			self._write_frame(kind, request_id, encoded)
			if command == "synthesize":
				self._generation_open = False
			elif command == "stop":
				self._generation_open = False
				self._active_generation = None

	def recv(self) -> Dict:
		while True:
			kind, request_id, payload = _read_frame(self._reader)
			if kind == RESPONSE:
				return {
					"type": "response",
					"id": request_id,
					"payload": self._decode_state(payload),
				}
			if kind == ERROR_RESPONSE:
				reader = _PayloadReader(payload)
				message = reader.string()
				reader.finish()
				if request_id:
					return {"type": "response", "id": request_id, "error": message}
				return {"type": "event", "event": "hostError", "payload": {"message": message}}
			event = self._decode_event(kind, payload)
			if event is not None:
				return event

	def close(self) -> None:
		for stream in (self._writer, self._reader):
			try:
				stream.close()
			except Exception:
				pass

	def _write_frame(self, kind: int, request_id: int, payload: bytes = b"") -> None:
		self._writer.write(_frame(kind, request_id, payload))
		self._writer.flush()

	def _encode_command(self, command: str, payload: Dict) -> Tuple[int, bytes]:
		if command == "initialize":
			return INITIALIZE, b"".join(
				(
					_string(payload["eciPath"]),
					_string(payload["dataDirectory"]),
					_string(payload["language"]),
					struct.pack("<i", int(payload["languageId"])),
					struct.pack("<B", bool(payload.get("enableAbbreviationDict", False))),
					struct.pack("<B", bool(payload.get("enablePhrasePrediction", False))),
					struct.pack("<i", int(payload.get("voiceVariant", 0))),
				)
			)
		if command == "addText":
			return ADD_TEXT, _bytes(payload["text"])
		if command == "insertIndex":
			return INSERT_INDEX, struct.pack("<I", int(payload["value"]))
		if command == "synthesize":
			return SYNTHESIZE, b""
		if command == "stop":
			return STOP, b""
		if command == "delete":
			return DELETE, b""
		if command == "setParam":
			return SET_PARAM, struct.pack("<ii", int(payload["paramId"]), int(payload["value"]))
		if command == "setVoiceParam":
			return SET_VOICE_PARAM, struct.pack("<ii", int(payload["paramId"]), int(payload["value"]))
		if command == "copyVoice":
			return COPY_VOICE, struct.pack("<i", int(payload["variant"]))
		raise NativeHostProtocolError(f"unsupported native host command: {command}")

	def _decode_event(self, kind: int, payload: bytes):
		reader = _PayloadReader(payload)
		if kind == AUDIO:
			generation = reader.u64()
			data = reader.bytes()
			reader.finish()
			if generation != self._active_generation:
				return None
			return {"type": "event", "event": "audio", "payload": {"data": data}}
		if kind == INDEX:
			generation = reader.u64()
			index = reader.u32()
			recovered = bool(reader.u8())
			reader.finish()
			if generation != self._active_generation:
				return None
			return {
				"type": "event",
				"event": "audio",
				"payload": {"data": b"", "index": index, "recovered": recovered},
			}
		if kind == DONE:
			generation = reader.u64()
			reader.finish()
			if generation != self._active_generation:
				return None
			self._active_generation = None
			return {"type": "event", "event": "audio", "payload": {"data": b"", "final": True}}
		if kind == STOPPED:
			generation = reader.u64()
			reader.finish()
			if generation == self._active_generation:
				self._active_generation = None
			return {"type": "event", "event": "stopped", "payload": {"generation": generation}}
		raise NativeHostProtocolError(f"unexpected native host message kind: {kind:#x}")

	@staticmethod
	def _decode_state(payload: bytes) -> Dict:
		if not payload:
			return {}
		reader = _PayloadReader(payload)
		params = NativeHostConnection._decode_map(reader)
		voice_params = NativeHostConnection._decode_map(reader)
		reader.finish()
		return {"params": params, "voiceParams": voice_params}

	@staticmethod
	def _decode_map(reader: _PayloadReader) -> Dict[int, int]:
		return {reader.i32(): reader.i32() for _ in range(reader.u32())}


def frames(stream: bytes) -> Iterable[Tuple[int, int, bytes]]:
	"""Test helper that decodes all complete frames from a byte string."""
	import io

	reader = io.BytesIO(stream)
	while reader.tell() < len(stream):
		yield _read_frame(reader)
