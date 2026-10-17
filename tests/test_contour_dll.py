"""Exercise real DLL audio through the add-on's playback worker."""
import queue
import struct
import unittest
from pathlib import Path

from test_audio_worker import FakeClient, FakePlayer, _load_client_module


DLL = Path(__file__).parents[1] / "addon/synthDrivers/eloquence_contours.dll"


@unittest.skipUnless(DLL.exists(), "build_contours.cmd is required")
class ContourPlaybackTests(unittest.TestCase):
	def test_all_modes_preserve_audio_and_indexes_across_synthesis_boundaries(self):
		pcm = struct.pack("<1024h", *((i * 7919 % 65536) - 32768 for i in range(1024)))
		for rate in (11025, 16000):
			for mode in ((0, 1, 2, 3) if rate == 11025 else (0, 1, 2)):
				with self.subTest(rate=rate, mode=mode):
					module = _load_client_module()
					module._presence_contour = mode
					client = FakeClient()
					client._contour = module.Contour(DLL, rate, mode)
					with module.Contour(DLL, rate, mode) as reference:
						expected = reference.process(pcm)
						if mode != 3:
							reference.reset()
						expected += reference.process(pcm)
						events = []
						module.onIndexReached = lambda index: events.append(("index", index))
						audio_queue = queue.Queue()
						for _ in range(2):
							for part in (pcm[:274], pcm[274:]):
								audio_queue.put((part, None, False, 0))
							audio_queue.put((b"", 42, False, 0))
							audio_queue.put((b"", None, True, 0))
						audio_queue.put(None)
						module.AudioWorker(FakePlayer(events), audio_queue, client).run()
						fed = [event[1] for event in events if event[0] == "feed"]
						self.assertEqual(b"".join(fed), expected)
						self.assertEqual([event for event in events if event[0] == "index"],
							[("index", 42), ("index", None)] * 2)
						with self.assertRaises(RuntimeError):
							client._contour.reset()  # Worker owns and closes the handle.

	def test_cancel_resets_filter_history_before_next_generation(self):
		for rate in (11025, 16000):
			for mode in ((1, 2, 3) if rate == 11025 else (1, 2)):
				with self.subTest(rate=rate, mode=mode):
					module = _load_client_module()
					module._presence_contour = mode
					client = FakeClient()
					client._contour = module.Contour(DLL, rate, mode)
					pcm = struct.pack("<64h", *range(64))
					with module.Contour(DLL, rate, mode) as reference:
						if mode != 3:
							reference.process(pcm)
							reference.reset()
						expected = reference.process(pcm)
					q = queue.Queue()
					q.put((pcm, None, False, 0))
					original_get = q.get
					def get(**kwargs):
						if q.empty():
							client._sequence = 1
							q.put((pcm, 99, False, 0))  # Stale PCM must not enter the filter.
							q.put((pcm, None, False, 1))
							q.put((b"", None, True, 1))
							q.put(None)
						return original_get(**kwargs)
					q.get = get
					events = []
					module.AudioWorker(FakePlayer(events), q, client).run()
					self.assertEqual([event for event in events if event[0] == "feed"], [("feed", expected)])
