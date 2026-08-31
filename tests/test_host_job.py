import importlib.util
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch
from pathlib import Path


requires_windows = unittest.skipUnless(sys.platform == "win32", "Job Objects are Windows only")
JOB_MODULE = Path(__file__).parents[1] / "addon" / "synthDrivers" / "_eloquence_job.py"


def _load_job_module():
	spec = importlib.util.spec_from_file_location("eloquence_job_test", JOB_MODULE)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


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
	module_name = "addon.synthDrivers._eloquence_job_client_test"
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


class HostJobTests(unittest.TestCase):
	"""Exercise the non-cooperative native-host cleanup backstop."""

	@requires_windows
	def test_closing_the_job_kills_an_assigned_process(self):
		job = _load_job_module().HostJob.create()
		self.assertIsNotNone(job, "Windows refused to create a Job Object")
		process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
		try:
			self.assertTrue(job.assign(int(process._handle)))
			self.assertIsNone(process.poll(), "helper exited before the job was closed")
			job.close()
			process.wait(timeout=10)
			self.assertIsNotNone(process.poll())
		finally:
			if process.poll() is None:
				process.kill()
				process.wait(timeout=10)

	@requires_windows
	def test_assign_after_close_is_refused(self):
		job = _load_job_module().HostJob.create()
		self.assertIsNotNone(job)
		job.close()
		self.assertFalse(job.assign(0))

	@requires_windows
	def test_close_is_idempotent(self):
		job = _load_job_module().HostJob.create()
		self.assertIsNotNone(job)
		job.close()
		job.close()

	def test_client_assigns_the_spawned_process_handle(self):
		client_module = _load_client_module()
		client = client_module.EloquenceHostClient()
		client._job = Mock()

		client._adopt_into_job(types.SimpleNamespace(_handle=12345))

		client._job.assign.assert_called_once_with(12345)

	def test_missing_job_does_not_block_client_startup(self):
		client_module = _load_client_module()
		client = client_module.EloquenceHostClient()
		with patch.object(client_module._job.HostJob, "create", return_value=None):
			client._adopt_into_job(object())
		self.assertIsNone(client._job)


if __name__ == "__main__":
	unittest.main()
