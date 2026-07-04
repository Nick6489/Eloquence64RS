import importlib.util
import io
import struct
import unittest
from pathlib import Path


def load_module():
	path = Path(__file__).parents[1] / "addon" / "synthDrivers" / "_eloquence_native.py"
	spec = importlib.util.spec_from_file_location("eloquence_native_protocol_test", path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class NativeProtocolTests(unittest.TestCase):
	def setUp(self):
		self.module = load_module()
		self.key = bytes(range(16))
		hello_ack = self.module._frame(self.module.HELLO_ACK, 1)
		self.reader = io.BytesIO(hello_ack)
		self.writer = io.BytesIO()
		self.connection = self.module.NativeHostConnection(self.reader, self.writer, self.key)

	def test_add_text_opens_generation_before_engine_bytes(self):
		self.connection.send(
			{"type": "command", "id": 7, "command": "addText", "payload": {"text": b"hello"}}
		)
		decoded = list(self.module.frames(self.writer.getvalue()))
		self.assertEqual([frame[0] for frame in decoded], [self.module.HELLO, self.module.BEGIN_GENERATION, self.module.ADD_TEXT])
		self.assertEqual(decoded[-1][2], struct.pack("<I", 5) + b"hello")

	def test_initialize_encodes_language_and_configuration(self):
		self.connection.send(
			{
				"type": "command",
				"id": 2,
				"command": "initialize",
				"payload": {
					"eciPath": r"C:\Eloquence\ECI.DLL",
					"dataDirectory": r"C:\Eloquence",
					"language": "enu",
					"languageId": 65536,
					"enableAbbreviationDict": True,
					"enablePhrasePrediction": False,
					"voiceVariant": 3,
				},
			}
		)
		kind, request_id, payload = list(self.module.frames(self.writer.getvalue()))[-1]
		self.assertEqual((kind, request_id), (self.module.INITIALIZE, 2))
		reader = self.module._PayloadReader(payload)
		self.assertEqual(reader.string(), r"C:\Eloquence\ECI.DLL")
		self.assertEqual(reader.string(), r"C:\Eloquence")
		self.assertEqual(reader.string(), "enu")
		self.assertEqual(reader.i32(), 65536)
		self.assertEqual((reader.u8(), reader.u8(), reader.i32()), (1, 0, 3))
		reader.finish()

	def test_response_state_preserves_integer_parameter_keys(self):
		payload = b"".join(
			(
				struct.pack("<Iii", 1, 9, 65536),
				struct.pack("<Iiiii", 2, 2, 65, 6, 180),
			)
		)
		self.reader = io.BytesIO(self.module._frame(self.module.RESPONSE, 4, payload))
		self.connection._reader = self.reader
		self.assertEqual(
			self.connection.recv(),
			{
				"type": "response",
				"id": 4,
				"payload": {"params": {9: 65536}, "voiceParams": {2: 65, 6: 180}},
			},
		)

	def test_index_and_done_map_to_legacy_audio_markers(self):
		self.connection._active_generation = 5
		index_payload = struct.pack("<QIB", 5, 42, 1)
		done_payload = struct.pack("<Q", 5)
		self.connection._reader = io.BytesIO(
			self.module._frame(self.module.INDEX, 0, index_payload)
			+ self.module._frame(self.module.DONE, 0, done_payload)
		)
		self.assertEqual(self.connection.recv()["payload"]["index"], 42)
		self.assertTrue(self.connection.recv()["payload"]["final"])


if __name__ == "__main__":
	unittest.main()
