"""Run an unchanged pinned upstream suite through explicit Runtime bridge.

Usage: python tools/run_aios_access_matrix.py --test /path/to/official.py
Network/model calls are neither needed nor made. Use a sandbox for external code.
"""
import argparse
import hashlib
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aios_access_adapter import install
from agent_runtime.memory_access import MemoryContext

EXPECTED_SHA = '2a01240304a7b5460b2fc7508c310a20446afde9e707547c2a87893cd0a20658'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--include-local', action='store_true', help='Also run Runtime budget/ACL boundary regressions')
    parser.add_argument('--mutation', choices=['none', 'drop-user-check', 'drop-agent-check', 'drop-budget'], default='none')
    args = parser.parse_args()
    digest = hashlib.sha256(args.test.read_bytes()).hexdigest()
    if digest != EXPECTED_SHA:
        raise SystemExit('Upstream test SHA256 mismatch; refusing modified assertions')
    install()
    original = MemoryContext.allowed
    if args.mutation == 'drop-user-check':
        MemoryContext.allowed = staticmethod(lambda m,u,a: original(m,m.get('user_id') if isinstance(m,dict) else u,a))
    elif args.mutation == 'drop-agent-check':
        MemoryContext.allowed = staticmethod(lambda m,u,a: original(m,u,m.get('owner_agent') if isinstance(m,dict) else a))
    elif args.mutation == 'drop-budget':
        select = MemoryContext.select
        def no_budget(self, rows, **kwargs):
            kwargs['max_bytes'] = 1000000
            return select(self, rows, **kwargs)
        MemoryContext.select = no_budget
    spec = importlib.util.spec_from_file_location('official_access_matrix', args.test)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    start = time.perf_counter()
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    if args.include_local:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName('test_memory_access'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'mode': 'unchanged_upstream_assertions_with_runtime_adapter',
              'upstream_commit': '292d98f78c835499df18cdb6de6325bba1895862',
              'test_sha256': digest, 'mutation': args.mutation,
              'tests': result.testsRun, 'failures': len(result.failures),
              'errors': len(result.errors), 'skipped': len(result.skipped),
              'seconds': time.perf_counter()-start, 'supplier_calls': 0,
              'native_aios': False, 'production_integrated': False,
              'include_local': args.include_local,
              'implementation_sha256': {name: hashlib.sha256(
                  (Path(__file__).resolve().parents[1]/name).read_bytes()).hexdigest()
                  for name in ('agent_runtime/context.py', 'agent_runtime/memory_access.py',
                               'tools/aios_access_adapter.py', 'test_memory_access.py')},
              'failed_tests': [test.id() for test,_ in result.failures+result.errors]}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
