"""独立进程、启动许可、进程组清理。cgroup 严格模式需要 Linux 委派子树。"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class Executor:
    def __init__(self, cgroup_root=None):
        self.root = Path(cgroup_root).resolve() if cgroup_root else None
        if self.root:
            if sys.platform != 'linux' or not str(self.root).startswith('/sys/fs/cgroup/'):
                raise RuntimeError('严格模式需要 /sys/fs/cgroup 下的 Linux 委派子树')
            controls = (self.root / 'cgroup.subtree_control').read_text().split()
            if not {'cpu', 'memory', 'pids'} <= set(controls):
                raise RuntimeError('委派子树尚未向子级启用 cpu/memory/pids 控制器')

    def launch(self, payload, directory, limits):
        directory.mkdir(parents=True)
        group = self.root / directory.name if self.root else None
        proc = None
        output = (directory / 'worker.log').open('w+', encoding='utf-8')
        try:
            if group:
                group.mkdir()
                for name, value in {'memory.max': limits['memory_bytes'], 'memory.swap.max': 0,
                                    'pids.max': limits['pids'],
                                    'cpu.max': f"{limits['cpu_milli'] * 100} 100000"}.items():
                    (group / name).write_text(str(value))
                if not (group / 'cgroup.kill').exists():
                    raise RuntimeError('严格模式要求 cgroup.kill')
            # Worker 只运行受信任的启动代码，在 stdin 收到许可前不执行任务。
            payload = {**payload, '_workspace': str(directory)}
            proc = subprocess.Popen([sys.executable, '-X', 'utf8', '-m', 'agent_runtime.worker',
                                     str(directory / 'result.json')],
                                    stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                                    text=True, encoding='utf-8', start_new_session=(os.name == 'posix'))
            if group:
                (group / 'cgroup.procs').write_text(str(proc.pid))
                if str(proc.pid) not in (group / 'cgroup.procs').read_text().split():
                    raise RuntimeError('Worker cgroup 归组核验失败')
            proc.stdin.write(json.dumps(payload, ensure_ascii=False))
            proc.stdin.close()
            return {'proc': proc, 'output': output, 'group': group, 'directory': directory,
                    'started': time.monotonic()}
        except BaseException:
            if proc:
                proc.kill()
                proc.wait()
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            output.close()
            if group and group.exists():
                group.rmdir()
            raise

    def cleanup(self, handle):
        proc, group = handle['proc'], handle['group']
        try:
            if group:
                (group / 'cgroup.kill').write_text('1')
            elif os.name == 'posix':
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif proc.poll() is None:
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            usage = {}
            if group:
                end = time.monotonic() + 5
                while 'populated 1' in (group / 'cgroup.events').read_text():
                    if time.monotonic() >= end:
                        raise RuntimeError('CLEANUP_FAILED: cgroup 仍有存活成员')
                    time.sleep(0.02)
                for name in ('memory.peak', 'memory.events', 'cpu.stat'):
                    if (group / name).exists():
                        usage[name] = (group / name).read_text()
                group.rmdir()
            handle['output'].seek(0)
            # 仅把尾部诊断回传；日志完整保存在 Attempt 目录。
            output = handle['output'].read()[-65536:]
            return proc.returncode, output, usage
        finally:
            handle['output'].close()
