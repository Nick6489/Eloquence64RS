import importlib.util
import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


def load_module():
	path = ROOT / "addon" / "synthDrivers" / "_eloquence_native.py"
	spec = importlib.util.spec_from_file_location("eloquence_native_process_test", path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class NativeClientProcessTests(unittest.TestCase):
	def test_legacy_client_messages_drive_real_native_host(self):
		host = ROOT / "native_host" / "target" / "i686-pc-windows-msvc" / "debug" / "eloquence_host32.exe"
		eci = Path(os.environ.get("ELOQUENCE_ECI_PATH", ROOT / "addon" / "synthDrivers" / "eloquence" / "ECI.DLL"))
		if not host.is_file() or not eci.is_file():
			self.skipTest("local i686 native host or proprietary ECI.DLL is unavailable")

		module = load_module()
		key = bytes(range(module.AUTH_KEY_BYTES))
		process = subprocess.Popen(
			[host, "--auth-key", key.hex()],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
		)
		self.assertIsNotNone(process.stdin)
		self.assertIsNotNone(process.stdout)
		connection = module.NativeHostConnection(process.stdout, process.stdin, key)
		try:
			connection.send(
				{
					"type": "command",
					"id": 2,
					"command": "initialize",
					"payload": {
						"eciPath": str(eci),
						"dataDirectory": str(eci.parent),
						"language": "enu",
						"languageId": 65536,
						"enableAbbreviationDict": True,
						"enablePhrasePrediction": True,
						"voiceVariant": 0,
					},
				}
			)
			response = connection.recv()
			self.assertEqual(response["id"], 2)
			self.assertIn(6, response["payload"]["voiceParams"])

			commands = [
				(3, "addText", {"text": b"Python native client integration test."}),
				(4, "insertIndex", {"value": 42}),
				(5, "insertIndex", {"value": 0xFFFF}),
				(6, "synthesize", {}),
			]
			for request_id, command, payload in commands:
				connection.send(
					{
						"type": "command",
						"id": request_id,
						"command": command,
						"payload": payload,
					}
				)

			saw_audio = False
			saw_index = False
			saw_done = False
			saw_synthesize_response = False
			while not saw_done or not saw_synthesize_response:
				message = connection.recv()
				if message["type"] == "response":
					saw_synthesize_response |= message["id"] == 6
				elif message["event"] == "audio":
					payload = message["payload"]
					saw_audio |= bool(payload.get("data"))
					saw_index |= payload.get("index") == 42
					saw_done |= bool(payload.get("final"))
			self.assertTrue(saw_audio)
			self.assertTrue(saw_index)

			connection.send({"type": "command", "id": 7, "command": "delete", "payload": {}})
			while connection.recv().get("id") != 7:
				pass
		finally:
			connection.close()
			if process.stderr:
				process.stderr.close()
			try:
				process.wait(timeout=2)
			except subprocess.TimeoutExpired:
				process.kill()
				process.wait(timeout=2)
		self.assertEqual(process.returncode, 0)


if __name__ == "__main__":
	unittest.main()
