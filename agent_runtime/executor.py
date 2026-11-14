"""独立进程、启动许可、进程组清理。cgroup 严格模式需要 Linux 委派子树。"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


class Executor:
    def __init__(self, cgroup_root=None):
        self.root = Path(cgroup_root).resolve() if cgroup_root else None
        self.agent_roots={}
        if self.root:
            if sys.platform != 'linux' or not str(self.root).startswith('/sys/fs/cgroup/'):
                raise RuntimeError('严格模式需要 /sys/fs/cgroup 下的 Linux 委派子树')
            controls = (self.root / 'cgroup.subtree_control').read_text().split()
            if not {'cpu', 'memory', 'pids'} <= set(controls):
                raise RuntimeError('委派子树尚未向子级启用 cpu/memory/pids 控制器')

    def launch(self, payload, directory, limits):
        started = time.monotonic()
        directory.mkdir(parents=True)
        payload_path = directory / '.payload.json'
        parent=self.agent_roots.get(payload.get('agent',payload['id']),self.root)
        group = parent / f'{directory.parent.name}-{directory.name}' if parent else None
        proc = handle = None
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
            # A private bounded file replaces the large blocking stdin payload.
            total = 0
            descriptor = os.open(str(payload_path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'wb') as target:
                for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(
                        {**payload,'_workspace':str(directory)}):
                    raw=chunk.encode('utf-8')
                    total+=len(raw)
                    if total>16*1024**2:raise ValueError('Worker payload 超过 16 MiB')
                    target.write(raw)
            environment = dict(os.environ)
            environment.pop('DEEPSEEK_API_KEY', None)
            environment.pop('AGENTRT_PROXY_TOKEN', None)
            proc = subprocess.Popen([sys.executable, '-X', 'utf8', '-m', 'agent_runtime.worker',
                                     str(directory / 'result.json'),str(payload_path)],
                                    stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                                    text=True, encoding='utf-8', env=environment,
                                    start_new_session=(os.name == 'posix'))
            handle={'proc':proc,'output':output,'group':group,'directory':directory,
                    'payload_path':payload_path,'started':started,'expired':threading.Event(),
                    '_cleanup_lock':threading.RLock()}
            if group:
                (group / 'cgroup.procs').write_text(str(proc.pid))
                if str(proc.pid) not in (group / 'cgroup.procs').read_text().split():
                    raise RuntimeError('Worker cgroup 归组核验失败')
            def expire():
                with handle['_cleanup_lock']:
                    if not handle.get('_reclaimed') and proc.poll() is None:
                        handle['expired'].set()
                        self._signal(handle)
            timer=threading.Timer(max(0,started+payload.get('timeout',10)-time.monotonic()),expire)
            timer.daemon=True
            handle['_timer']=timer
            timer.start()
            try:proc.stdin.write('G')
            except BrokenPipeError:
                if not handle['expired'].is_set():raise
            finally:
                try:proc.stdin.close()
                except BrokenPipeError:
                    if not handle['expired'].is_set():raise
            return handle
        except BaseException:
            if handle is not None:self.cleanup(handle)
            else:
                if proc:
                    proc.kill();proc.wait(timeout=5)
                output.close()
                if group and group.exists():group.rmdir()
                payload_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _signal(handle):
        proc,group=handle['proc'],handle['group']
        errors=[]
        if group and group.exists():
            try:(group/'cgroup.kill').write_text('1')
            except OSError as exc:errors.append(str(exc))
        elif os.name=='posix':
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            except OSError as exc:errors.append(str(exc))
        elif proc.poll() is None:
            try:
                subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
            except (OSError,subprocess.TimeoutExpired) as exc:errors.append(str(exc))
        if proc.poll() is None:
            try:proc.kill()
            except ProcessLookupError:pass
            except OSError as exc:errors.append(str(exc))
        return errors

    def cleanup(self, handle):
        lock=handle.setdefault('_cleanup_lock',threading.RLock())
        with lock:
            if '_cleanup_result' in handle:return handle['_cleanup_result']
            proc,group=handle['proc'],handle['group']
            timer=handle.get('_timer')
            if timer:timer.cancel()
            diagnostic=[]
            usage=handle.setdefault('_usage',{})
            try:
                if not handle.get('_reclaimed'):
                    diagnostic.extend(self._signal(handle))
                    proc.wait(timeout=5)
                    if group and group.exists():
                        end=time.monotonic()+5
                        while 'populated 1' in (group/'cgroup.events').read_text():
                            if time.monotonic()>=end:raise RuntimeError('cgroup 仍有存活成员')
                            time.sleep(.02)
                        for name in ('memory.peak','memory.events','cpu.stat','pids.events','pids.peak'):
                            try:usage[name]=(group/name).read_text()
                            except OSError as exc:diagnostic.append('统计不可读: '+str(exc))
                        group.rmdir()
                    handle['_reclaimed']=True
            except (OSError,subprocess.TimeoutExpired,RuntimeError) as exc:
                raise RuntimeError('CLEANUP_FAILED: '+str(exc)) from exc
            finally:
                if proc.stdin and not proc.stdin.closed:
                    try:proc.stdin.close()
                    except OSError:pass
                try:handle['output'].close()
                except OSError as exc:diagnostic.append('日志关闭失败: '+str(exc))
            payload_path=handle.get('payload_path')
            if payload_path:
                try:payload_path.unlink(missing_ok=True)
                except OSError as exc:diagnostic.append('临时payload清理失败: '+str(exc))
            try:
                with (handle['directory']/'worker.log').open('rb') as log:
                    log.seek(0,2);log.seek(max(0,log.tell()-65536))
                    output=log.read().decode('utf-8',errors='replace')
            except OSError as exc:
                output=''
                diagnostic.append('日志不可读（进程已回收）: '+str(exc))
            if diagnostic:output+='\n'+'\n'.join(diagnostic)
            handle['_cleanup_result']=(proc.returncode,output,dict(usage))
            return handle['_cleanup_result']
