"""Run a bounded catalogue operation in an isolated child process."""
import json
from pathlib import Path
import sys


def execute(operation, parameters):
    from aios.tool import engineering
    from aios.tool.mcp_server import safe_eval_math
    if operation == 'web.fetch':
        from aios.tool.public_network import fetch_public
        return fetch_public(parameters['url'])
    if operation == 'runtime.load':
        import time
        seconds, memory = parameters.get('seconds', 1), parameters.get('memory_mib', 0)
        if type(seconds) not in (int, float) or not 0 < seconds <= 3 or type(memory) is not int or not 0 <= memory <= 64:
            raise ValueError('Load exceeds bounded experiment limits')
        block = bytearray(memory * 1024 * 1024)
        for offset in range(0, len(block), 4096):
            block[offset] = 1
        started, cpu = time.monotonic(), time.process_time()
        while time.monotonic() - started < seconds:
            pass
        return {'wall_seconds': time.monotonic() - started, 'cpu_seconds': time.process_time() - cpu}
    if operation == 'text.stats':
        text = parameters.get('text', '')
        return {'chars': len(text), 'words': len(text.split()), 'lines': len(text.splitlines())}
    if operation == 'math.calculate':
        expression = parameters.get('expression', '0')
        if not isinstance(expression, str) or len(expression) > 256:
            raise ValueError('Expression exceeds limit')
        return {'result': safe_eval_math(expression)}
    if operation == 'project.test':
        project = parameters.get('project', 'pricing')
        spec = engineering.manifest(project)
        raw_src = spec.get('files') if spec.get('multi_file') else spec['source']
        return engineering.run_check(raw_src, project)
    if operation == 'project.patch_check':
        raw_src = parameters.get('files') if 'files' in parameters else parameters['source']
        return engineering.run_check(raw_src, parameters.get('project', 'pricing'))
    raise ValueError('Unsupported isolated operation')


def main():
    if sys.stdin.read(1) != 'G':
        return 2
    source = Path(sys.argv[2])
    try:
        with source.open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Payload exceeds limit')
        payload = json.loads(raw)
    finally:
        source.unlink(missing_ok=True)
    try:
        result = execute(payload['operation'], payload['parameters'])
        response = {'task_id': payload['id'], 'result': result}
        Path(sys.argv[1]).write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
        return 0
    except Exception:
        Path(sys.argv[1]).write_text(json.dumps({'task_id': payload['id'], 'error': 'Isolated operation failed'}), encoding='utf-8')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
