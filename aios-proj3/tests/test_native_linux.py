"""Run inside a delegated systemd unit with AIOS_TEST_CGROUP_ROOT set."""
import os
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cerebrum.tool.apis import ToolQuery
from runtime.launch import AIOSKernelRuntime


@unittest.skipUnless(os.environ.get('AIOS_TEST_CGROUP_ROOT'), 'Requires delegated Linux subtree')
class NativeLinuxTest(unittest.TestCase):
    def test_native_worker_throttle_oom_and_zero_orphans(self):
        group = os.environ['AIOS_TEST_CGROUP_ROOT']
        with TemporaryDirectory() as folder:
            with AIOSKernelRuntime(storage_root=str(Path(folder) / 'store'), cgroup_root=group) as kernel:
                query = ToolQuery(tool_calls=[{'name': 'runtime.load', 'parameters': {'seconds': 1, 'memory_mib': 8}}],
                                  resources={'cpu_milli': 200, 'memory_bytes': 64 * 1024**2, 'pids': 4})
                response = kernel.execute_request('native-linux', query)['response']
                self.assertEqual(response.status_code, 200)
                receipt = kernel.process_service.snapshot()['receipts'][-1]
                stat = dict(line.split() for line in receipt['usage']['cpu.stat'].splitlines())
                self.assertGreater(int(stat['nr_throttled']), 0)
                ratio = response.response_message['cpu_seconds'] / response.response_message['wall_seconds']
                self.assertLess(ratio, 0.4)
                oom = ToolQuery(tool_calls=[{'name': 'runtime.load', 'parameters': {'seconds': 1, 'memory_mib': 48}}],
                                resources={'cpu_milli': 250, 'memory_bytes': 24 * 1024**2, 'pids': 4})
                self.assertGreaterEqual(kernel.execute_request('native-linux', oom)['response'].status_code, 400)
                receipt = kernel.process_service.snapshot()['receipts'][-1]
                events = dict(line.split() for line in receipt['usage']['memory.events'].splitlines())
                self.assertGreater(int(events['oom_kill']), 0)
                self.assertEqual(kernel.process_service.snapshot()['active'], 0)
                self.assertEqual(kernel.scheduler.drf_controller.used.memory_bytes, 0)
            self.assertFalse([path for path in Path(group).iterdir() if path.is_dir()])
            self.assertEqual((Path(group) / 'cgroup.procs').read_text().strip(), '')
            print('NATIVE_LINUX_VALIDATION ' + json.dumps({'cpu_core_ratio': round(ratio, 4),
                  'nr_throttled': int(stat['nr_throttled']), 'oom_kill': int(events['oom_kill']),
                  'zero_orphan_groups': True, 'execution': 'native_tool_syscall_worker'}), flush=True)
