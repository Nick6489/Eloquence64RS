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
	global_vars_module = types.ModuleType("globalVars")
	global_vars_module.appArgs = types.SimpleNamespace(secure=False)

	stubs = {
		"config": config_module,
		"nvwave": nvwave_module,
		"buildVersion": build_version_module,
		"globalVars": global_vars_module,
	}
	repo = Path(__file__).parents[1]
	packages = {
		"addon": repo / "addon",
		"addon.synthDrivers": repo / "addon" / "synthDrivers",
	}
	previous = {name: sys.modules.get(name) for name in (*stubs, *packages)}
	sys.modules.update(stubs)
	for name, path in packages.items():
		if name not in sys.modules or not hasattr(sys.modules[name], "__path__"):
			package = types.ModuleType(name)
			package.__path__ = [str(path)]
			package.__package__ = name
			sys.modules[name] = package
	module_name = "addon.synthDrivers._eloquence_audio_test"
	try:
		path = repo / "addon" / "synthDrivers" / "_eloquence.py"
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
	def test_permanent_voice_parameter_waits_for_host_readback(self):
		module = _load_client_module()
		client = Mock()
		client.send_command.return_value = {"voiceParams": {module.rate: 173}}
		module._client = client
		module.voice_params.clear()

		module.setVParam(module.rate, 180)

		client.send_command.assert_called_once_with(
			"setVoiceParam",
			paramId=module.rate,
			value=180,
			temporary=False,
		)
		self.assertEqual(module.voice_params[module.rate], 173)

	def test_temporary_voice_parameter_waits_without_changing_base(self):
		module = _load_client_module()
		client = Mock()
		client.send_command.return_value = {"voiceParams": {module.rate: 200}}
		module._client = client
		module.voice_params[module.rate] = 150

		module.setVParam(module.rate, 200, temporary=True)

		client.send_command.assert_called_once_with(
			"setVoiceParam",
			paramId=module.rate,
			value=200,
			temporary=True,
		)
		self.assertEqual(module.voice_params[module.rate], 150)

	def test_initialize_restores_presence_and_rate_before_opening_audio(self):
		module = _load_client_module()
		module._presence_contour = True
		module.config.conf = {}
		client = Mock()
		client.send_command.side_effect = [
			{"params": {}, "voiceParams": {}},
			{},
		]
		module._client = client
		module._ensure_synth_worker = Mock()

		module.initialize(sample_rate=16000)

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
			unittest.mock.call("setPresenceContour", enabled=True),
		)
		self.assertEqual(client.send_command.call_args_list[0].kwargs["sampleRate"], 16000)
		self.assertEqual(client.send_command.call_args_list[0].kwargs["dictionaryDirectory"], "")
		module._ensure_synth_worker.assert_called_once_with()

	def test_native_mode_constructs_16_khz_wave_player(self):
		module = _load_client_module()
		module._sample_rate = module.NATIVE_SAMPLE_RATE
		module.config.conf = {"audio": {"outputDevice": "test-device"}}
		player = Mock()
		module.nvwave.WavePlayer = Mock(return_value=player)
		worker = Mock()
		module.AudioWorker = Mock(return_value=worker)
		client = module.EloquenceHostClient()

		client.initialize_audio()

		module.nvwave.WavePlayer.assert_called_once_with(
			1,
			module.NATIVE_SAMPLE_RATE,
			16,
			outputDevice="test-device",
		)
		module.AudioWorker.assert_called_once_with(player, client._audio_queue, client)
		worker.start.assert_called_once_with()

	def test_classic_presence_switch_reconfigures_player_for_22_khz(self):
		module = _load_client_module()
		module._sample_rate = module.STANDARD_SAMPLE_RATE
		client = Mock()
		client._host = object()
		module._client = client

		module.set_presence_contour(True)

		self.assertTrue(module.get_presence_contour())
		self.assertEqual(
			client.method_calls,
			[
				unittest.mock.call.stop(),
				unittest.mock.call.send_command("setPresenceContour", enabled=True),
				unittest.mock.call.close_audio(),
				unittest.mock.call.initialize_audio(),
			],
		)
		self.assertEqual(module.get_output_sample_rate(), module.PRESENCE_SAMPLE_RATE)

	def test_native_presence_switch_keeps_16_khz_player(self):
		module = _load_client_module()
		module._sample_rate = module.NATIVE_SAMPLE_RATE
		client = Mock()
		client._host = object()
		module._client = client

		module.set_presence_contour(True)

		self.assertEqual(
			client.method_calls,
			[
				unittest.mock.call.stop(),
				unittest.mock.call.send_command("setPresenceContour", enabled=True),
			],
		)
		self.assertEqual(module.get_output_sample_rate(), module.NATIVE_SAMPLE_RATE)

	def test_classic_presence_constructs_22_khz_wave_player(self):
		module = _load_client_module()
		module._sample_rate = module.STANDARD_SAMPLE_RATE
		module._presence_contour = True
		module.config.conf = {"audio": {"outputDevice": "test-device"}}
		module.nvwave.WavePlayer = Mock()
		module.AudioWorker = Mock()
		client = module.EloquenceHostClient()

		client.initialize_audio()

		self.assertEqual(module.nvwave.WavePlayer.call_args.args[:3], (1, 22050, 16))

	def test_invalid_sample_rate_is_rejected(self):
		module = _load_client_module()
		module._sample_rate = module.NATIVE_SAMPLE_RATE
		client = Mock()
		client._host = None
		module._client = client

		with self.assertRaises(ValueError):
			module.set_sample_rate("unknown")

		self.assertEqual(module.get_sample_rate(), module.NATIVE_SAMPLE_RATE)
		client.send_command.assert_not_called()

	def test_sample_rate_restart_restores_live_engine_state(self):
		module = _load_client_module()
		client = Mock()
		client._host = object()
		module._client = client
		module._sample_rate = module.STANDARD_SAMPLE_RATE
		module._current_variant = 4
		module.onIndexReached = Mock()
		module.params.clear()
		module.params[9] = 262144
		module.voice_params.clear()
		module.voice_params.update({module.pitch: 63, module.rate: 171})
		module.initialize = Mock()
		module.set_voice = Mock()
		module.setVariant = Mock()
		module.setVParam = Mock()

		module.set_sample_rate(module.NATIVE_SAMPLE_RATE)

		self.assertEqual(client.method_calls, [unittest.mock.call.stop(), unittest.mock.call.shutdown()])
		module.initialize.assert_called_once_with(module.onIndexReached, sample_rate=16000)
		module.set_voice.assert_called_once_with(262144)
		module.setVariant.assert_called_once_with(4)
		self.assertEqual(
			module.setVParam.call_args_list,
			[unittest.mock.call(module.pitch, 63), unittest.mock.call(module.rate, 171)],
		)

	def test_builtin_dictionary_switch_stops_speech_and_sends_empty_directory(self):
		module = _load_client_module()
		client = Mock()
		client._host = object()
		module._client = client

		module.set_dictionary_directory(None, reload=True)

		self.assertEqual(
			client.method_calls,
			[
				unittest.mock.call.stop(),
				unittest.mock.call.send_command(
					"setDictionaryDirectory",
					directory="",
					reload=True,
				),
			],
		)

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

	def test_cancel_during_prosody_reset_does_not_send_remaining_speech_commands(self):
		# The worker checks the Speech Generation only before the first command.
		# Cancel during the blocking prosody reset must drop the rest of the
		# item so leftover addText/synthesize cannot start after Stop.
		module = _load_client_module()
		client = module.EloquenceHostClient()
		client._host = module.HostProcess(process=Mock(), connection=Mock())
		module._client = client
		module.voice_params.update(
			{
				module.rate: 50,
				module.pitch: 50,
				module.vlm: 50,
			}
		)
		recorded = []
		first_voice_param = threading.Event()
		resume = threading.Event()

		def send_command(command, wait=True, **payload):
			recorded.append(command)
			if command == "setVoiceParam" and not first_voice_param.is_set():
				first_voice_param.set()
				self.assertTrue(resume.wait(timeout=2))
			return {"voiceParams": {}}

		client.send_command = send_command
		item = [
			(module.cmdProsody, (module.rate, 1, 0)),
			(module.cmdProsody, (module.pitch, 1, 0)),
			(module.cmdProsody, (module.vlm, 1, 0)),
			(module.speak, (b"hello",)),
			(module.index, (0xFFFF,)),
			(module.synth, ()),
		]
		try:
			module.synth_queue.put((item, client._sequence))
			module._ensure_synth_worker()
			self.assertTrue(first_voice_param.wait(timeout=2))
			module.stop()
			resume.set()
			module.synth_queue.join()
			self.assertIn("stop", recorded)
			self.assertNotIn("addText", recorded)
			self.assertNotIn("insertIndex", recorded)
			self.assertNotIn("synthesize", recorded)
		finally:
			resume.set()
			module._stop_synth_worker()

	def test_new_utterance_after_cancel_still_sends_speech_commands(self):
		module = _load_client_module()
		client = module.EloquenceHostClient()
		client._host = module.HostProcess(process=Mock(), connection=Mock())
		module._client = client
		module.voice_params.update(
			{
				module.rate: 50,
				module.pitch: 50,
				module.vlm: 50,
			}
		)
		recorded = []
		first_voice_param = threading.Event()
		resume = threading.Event()

		def send_command(command, wait=True, **payload):
			recorded.append(command)
			if command == "setVoiceParam" and not first_voice_param.is_set():
				first_voice_param.set()
				self.assertTrue(resume.wait(timeout=2))
			return {"voiceParams": {}}

		client.send_command = send_command
		cancelled_item = [
			(module.cmdProsody, (module.rate, 1, 0)),
			(module.speak, (b"old",)),
			(module.synth, ()),
		]
		next_item = [
			(module.cmdProsody, (module.rate, 1, 0)),
			(module.speak, (b"new",)),
			(module.synth, ()),
		]
		try:
			module.synth_queue.put((cancelled_item, client._sequence))
			module._ensure_synth_worker()
			self.assertTrue(first_voice_param.wait(timeout=2))
			module.stop()
			module.synth_queue.put((next_item, client._sequence))
			resume.set()
			module.synth_queue.join()
			self.assertIn("stop", recorded)
			self.assertEqual(recorded.count("addText"), 1)
			self.assertEqual(recorded.count("synthesize"), 1)
		finally:
			resume.set()
			module._stop_synth_worker()

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
