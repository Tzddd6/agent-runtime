"""Start an isolated console, run browser acceptance, and verify its downloads."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from aios.tool.engineering import manifest, verify_delivery_zip_bytes
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole, create_server


def verify_download(out_dir, row):
    spec = manifest(row['project'], include_reference=False)
    raw = (out_dir / row['zip']).read_bytes()
    verified = verify_delivery_zip_bytes(raw, row['source_sha256'], multi_file=True)
    if set(verified['files']) != set(spec['editable_files']):
        raise ValueError('Unexpected delivered source files')
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        public = bundle.read('tests/test_public.py')
        if public != spec['public_tests']['tests/test_public.py'].encode():
            raise ValueError('Delivered public tests differ from the evaluator')
        if row.get('execution_evidence'):
            if json.loads(bundle.read('execution_evidence.json')) != row['execution_evidence']:
                raise ValueError('Downloaded recovery evidence differs from the workflow record')
        if 'tests/test_hidden.py' in bundle.namelist():
            raise ValueError('Hidden tests are present in the delivered ZIP')
        if spec.get('provenance'):
            if json.loads(bundle.read('UPSTREAM.json')) != spec['provenance']:
                raise ValueError('Upstream provenance mismatch')
            if not bundle.read('LICENSE.upstream.txt'):
                raise ValueError('Missing upstream license')

    with TemporaryDirectory(prefix='aios-download-check-') as folder:
        root = Path(folder)
        for name, content in verified['files'].items():
            (root / name).write_bytes(content.encode())
        (root / 'test_public.py').write_bytes(public)
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'test_public'],
                                cwd=root, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError(result.stderr)
    return {'source_tree_sha256': verified['source_tree_sha256'], 'public_retest': result.stderr.strip()}


def run_browser(args, out_dir, root):
    with AIOSKernelRuntime(storage_root=str(root / 'storage')) as kernel:
        console = NativeConsole(kernel, root / 'jobs')
        try:
            with create_server(console, port=0) as server:
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    return subprocess.run(
                        [args.node, str(PROJECT_ROOT / 'tools' / 'playwright_e2e_runner.js')],
                        cwd=PROJECT_ROOT, timeout=180,
                        env={**os.environ, 'AIOS_BASE_URL': f'http://127.0.0.1:{server.server_port}',
                             'AIOS_E2E_OUT_DIR': str(out_dir)},
                    ).returncode
                finally:
                    server.shutdown()
                    thread.join()
        finally:
            console.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', default='node', help='Node.js executable')
    parser.add_argument('--out-dir', type=Path, required=True, help='New output directory for screenshots, reports and ZIPs')
    args = parser.parse_args()
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.iterdir()):
        parser.error('--out-dir must be empty')

    with TemporaryDirectory(prefix='aios-pw-e2e-') as tmp:
        try:
            code = run_browser(args, out_dir, Path(tmp))
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f'Browser acceptance could not finish: {exc}', file=sys.stderr)
            return 1

    report_path = out_dir / 'playwright_e2e_report.json'
    if not report_path.is_file():
        return code or 1
    report = json.loads(report_path.read_text(encoding='utf-8'))
    for row in report['cases']:
        if row['passed'] and row.get('zip'):
            try:
                row['zip_verification'] = verify_download(out_dir, row)
            except (OSError, ValueError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
                row.update(passed=False, error=str(exc))

    report['total_cases'] = len(report['cases'])
    report['passed_cases'] = sum(row['passed'] for row in report['cases'])
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = ['# AIOS 浏览器自动验收', '', report['browser'], '',
             '| 场景 | 工作流 | 截图 | 结果 |', '| --- | --- | --- | --- |']
    for row in report['cases']:
        result = '通过' if row['passed'] else '失败'
        lines.append(f"| {row['title']} | {row.get('job_id', '—')} | [查看]({row['screenshot']}) | {result} |")
    lines += ['', f"浏览器错误 {len(report['console_errors'])} 条，页面异常 {len(report['page_errors'])} 条。",
              '', '[原始报告](playwright_e2e_report.json)']
    (out_dir / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f"Playwright {report['passed_cases']}/{report['total_cases']} · {report_path}")
    return code or int(report['passed_cases'] != report['total_cases'])


if __name__ == '__main__':
    raise SystemExit(main())
