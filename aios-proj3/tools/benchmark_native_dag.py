"""Deterministic comparison of DAG coordination, with no model requests."""
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
import time

from runtime.launch import AIOSKernelRuntime


def run():
    rows = []
    with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
        def step(task, upstream, attempt):
            time.sleep(0.06)
            return {'value': 7}

        tasks = [{'id': f'branch-{i}', 'handler': step} for i in range(4)]
        tasks.append({'id': 'join', 'depends_on': [task['id'] for task in tasks],
                      'handler': lambda task, upstream, attempt: {'sum': sum(item['value'] for item in upstream.values())}})
        for repeat in range(5):
            for workers in ((1, 4) if repeat % 2 == 0 else (4, 1)):
                start = time.perf_counter()
                result = kernel.workflow_engine.run_workflow(f'benchmark-{repeat}-{workers}', tasks, max_workers=workers)
                elapsed = time.perf_counter() - start
                assert result['results']['join'] == {'sum': 28}
                assert all(value == 'succeeded' for value in result['states'].values())
                assert kernel.scheduler.drf_controller.used.cpu_milli == 0
                rows.append({'repeat': repeat, 'workers': workers, 'seconds': round(elapsed, 6), 'quality_passed': True})
    serial = median(row['seconds'] for row in rows if row['workers'] == 1)
    parallel = median(row['seconds'] for row in rows if row['workers'] == 4)
    report = {'scope': 'synthetic waiting tasks; identical handlers, resources and expected output',
              'provider_calls': 0, 'rows': rows, 'serial_median_seconds': serial,
              'parallel_median_seconds': parallel, 'speedup': round(serial / parallel, 3),
              'quality_passed': 10, 'quality_total': 10}
    target = Path('reports/native-dag-concurrency.json')
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    run()
