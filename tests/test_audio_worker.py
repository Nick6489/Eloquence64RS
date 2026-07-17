import importlib.util
import queue
import sys
import threading
import types
import unittest
from unittest.mock import Mock
from pathlib import Path


def _load_client_module():
	config_module = types.ModuleType("config")
	config_module.conf = {}
	nvwave_module = types.ModuleType("nvwave")
	nvwave_module.WavePlayer = object
	build_version_module = types.ModuleType("buildVersion")
	build_version_module.version_year = 2026

	stubs = {
		"config": config_module,
		"nvwave": nvwave_module,
		"buildVersion": build_version_module,
	}
	previous = {name: sys.modules.get(name) for name in stubs}
	sys.modules.update(stubs)
	module_name = "addon.synthDrivers._eloquence_audio_test"
	try:
		path = Path(__file__).parents[1] / "addon" / "synthDrivers" / "_eloquence.py"
		spec = importlib.util.spec_from_file_location(module_name, path)
		module = importlib.util.module_from_spec(spec)
		sys.modules[module_name] = module
		spec.loader.exec_module(module)
		return module
	finally:
		sys.modules.pop(module_name, None)
		for name, old_module in previous.items():
			if old_module is None:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = old_module


class FakePlayer:
	def __init__(self, events):
		self.events = events
		self.marker_callback = None

	def feed(self, data, onDone=None):
		self.events.append(("feed", data))
		if onDone:
			self.marker_callback = onDone


class FakeClient:
	_sequence = 0


class AudioWorkerTests(unittest.TestCase):
	def test_initialize_restores_enhanced_mode_before_opening_audio(self):
		module = _load_client_module()
		module._audio_quality = "enhanced"
		module.config.conf = {}
		client = Mock()
		client.send_command.side_effect = [
			{"params": {}, "voiceParams": {}},
			{},
		]
		module._client = client
		module._ensure_synth_worker = Mock()

		module.initialize()

		self.assertEqual(
			[method[0] for method in client.method_calls],
			[
				"ensure_started",
				"send_command",
				"send_command",
				"initialize_audio",
			],
		)
		self.assertEqual(
			client.send_command.call_args_list[1],
			unittest.mock.call("setAudioQuality", enhanced=True),
		)
		module._ensure_synth_worker.assert_called_once_with()

	def test_enhanced_mode_constructs_22_khz_wave_player(self):
		module = _load_client_module()
		module._audio_quality = "enhanced"
		module.config.conf = {"audio": {"outputDevice": "test-device"}}
		player = Mock()
		module.nvwave.WavePlayer = Mock(return_value=player)
		worker = Mock()
		module.AudioWorker = Mock(return_value=worker)
		client = module.EloquenceHostClient()

		client.initialize_audio()

		module.nvwave.WavePlayer.assert_called_once_with(
			1,
			module.ENHANCED_SAMPLE_RATE,
			16,
			outputDevice="test-device",
		)
		module.AudioWorker.assert_called_once_with(player, client._audio_queue, client)
		worker.start.assert_called_once_with()

	def test_audio_quality_switch_reconfigures_host_and_player(self):
		module = _load_client_module()
		client = Mock()
		client._host = object()
		module._client = client

		module.set_audio_quality("enhanced")

		self.assertEqual(module.get_audio_quality(), "enhanced")
		self.assertEqual(
			client.method_calls,
			[
				unittest.mock.call.stop(),
				unittest.mock.call.send_command("setAudioQuality", enhanced=True),
				unittest.mock.call.close_audio(),
				unittest.mock.call.initialize_audio(),
			],
		)

	def test_invalid_audio_quality_falls_back_to_standard(self):
		module = _load_client_module()
		module._audio_quality = "enhanced"
		client = Mock()
		client._host = None
		module._client = client

		module.set_audio_quality("unknown")

		self.assertEqual(module.get_audio_quality(), "standard")
		client.send_command.assert_not_called()

	def test_empty_index_marker_queues_non_blocking_playback_callback(self):
		module = _load_client_module()
		events = []
		module.onIndexReached = lambda index: events.append(("index", index))
		audio_queue = queue.Queue()
		audio_queue.put((b"audio", None, False, 0))
		audio_queue.put((b"", 42, False, 0))
		audio_queue.put(None)
		player = FakePlayer(events)
		worker = module.AudioWorker(player, audio_queue, FakeClient())

		worker.run()

		self.assertEqual(events, [("feed", b"audio"), ("feed", b"")])
		self.assertIsNotNone(player.marker_callback)
		player.marker_callback()
		self.assertEqual(events[-1], ("index", 42))

	def test_waiting_command_does_not_block_stop_write(self):
		module = _load_client_module()
		client = module.EloquenceHostClient()
		connection = Mock()
		client._host = module.HostProcess(process=Mock(), connection=connection)
		waiting_started = threading.Event()

		def wait_for_response():
			waiting_started.set()
			with self.assertRaises(RuntimeError):
				client.send_command("synthesize")

		thread = threading.Thread(target=wait_for_response)
		thread.start()
		self.assertTrue(waiting_started.wait(timeout=1))
		while len(client._pending) == 0:
			pass
		client.send_command("stop", wait=False)
		self.assertEqual(connection.send.call_count, 2)
		for event in client._pending.values():
			event.set()
		thread.join(timeout=1)
		self.assertFalse(thread.is_alive())

	def test_broken_host_pipe_is_an_error(self):
		module = _load_client_module()
		client = module.EloquenceHostClient()
		connection = Mock()
		connection.send.side_effect = BrokenPipeError("host exited")
		client._host = module.HostProcess(process=Mock(), connection=connection)

		with self.assertRaises(BrokenPipeError):
			client.send_command("initialize")
		self.assertFalse(client._pending)


if __name__ == "__main__":
	unittest.main()
