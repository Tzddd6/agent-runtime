"""Credential-free single-threaded preloader, fresh child for every Attempt.

Only a private inherited Unix socketpair is exposed. No Agent payload is read
in the preloader. Worker execution remains behind the normal admission pipe.
"""
import array
import ctypes
import json
import os
from pathlib import Path
import selectors
import select
import signal
import socket
import subprocess
import sys
import time
import threading


def parent_death(expected):
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(1,signal.SIGKILL,0,0,0)!=0:raise OSError(ctypes.get_errno(),'PR_SET_PDEATHSIG failed')
    if os.getppid()!=expected:os._exit(125)


class ForkProcess:
    def __init__(self,pid,permit,status):
        self.pid=pid;self.stdin=os.fdopen(permit,'w',encoding='utf-8');self.status=status
        os.set_inheritable(permit,False);os.set_inheritable(status,False)
        try:self.pidfd=os.pidfd_open(pid,0)
        except (AttributeError,OSError):self.pidfd=None
        os.set_blocking(status,False);self.returncode=None;self._raw=b''
        self._lock=threading.RLock()

    def poll(self):
        with self._lock:return self._poll()

    def _poll(self):
        if self.returncode is not None:return self.returncode
        try:data=os.read(self.status,128)
        except BlockingIOError:return None
        self._raw+=data
        if b'\n' in self._raw:
            self.returncode=int(self._raw.split(b'\n')[0]);os.close(self.status);self.status=None
        elif not data:
            # A dead launcher cannot certify success. Executor still reclaims
            # the Attempt cgroup/process group before releasing any resource.
            if self.pidfd is not None and not select.select([self.pidfd],[],[],0)[0]:return None
            if self.pidfd is None:
                try:
                    if Path(f'/proc/{self.pid}/stat').read_text().split(') ',1)[1].split()[0]!='Z':return None
                except FileNotFoundError:pass
            self.returncode=125;os.close(self.status);self.status=None
        if self.returncode is not None and self.pidfd is not None:os.close(self.pidfd);self.pidfd=None
        return self.returncode

    def wait(self,timeout=None):
        end=time.monotonic()+timeout if timeout is not None else float('inf')
        selector=selectors.DefaultSelector()
        try:
            while self.poll() is None:
                remaining=end-time.monotonic()
                if remaining<=0:raise subprocess.TimeoutExpired('fork-worker',timeout)
                if not selector.get_map():selector.register(self.status,selectors.EVENT_READ)
                selector.select(min(remaining,.1))
            return self.returncode
        finally:selector.close()

    def kill(self):
        if self.poll() is None:
            try:os.kill(self.pid,signal.SIGKILL)
            except ProcessLookupError:pass


class ForkServer:
    def __init__(self,root):
        if sys.platform!='linux' or not hasattr(socket,'SCM_RIGHTS'):raise RuntimeError('forkserver requires Linux SCM_RIGHTS')
        start=time.monotonic();self.socket,child=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        self.socket.settimeout(10)
        # No provider key, proxy token, shell configuration or user HOME enters
        # the independent preloader. It is not forked from the web controller.
        env={'PATH':os.defpath,'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'}
        self.process=subprocess.Popen([sys.executable,'-X','utf8','-m','agent_runtime.forkserver',str(child.fileno()),str(Path(root).resolve()),str(os.getpid())],
            pass_fds=(child.fileno(),),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            cwd=Path(__file__).resolve().parents[1],env=env,start_new_session=True)
        child.close()
        try:
            if self.socket.recv(1024)!=b'READY':raise RuntimeError('Preloader did not become ready')
        except BaseException:self.close();raise
        self.startup_seconds=time.monotonic()-start

    def launch(self,directory,payload_path):
        request=json.dumps({'directory':str(directory.resolve()),'payload':str(payload_path.resolve())}).encode()
        self.socket.send(request)
        raw,ancillary,flags,_=self.socket.recvmsg(4096,socket.CMSG_SPACE(8))
        descriptors=[]
        for level,kind,data in ancillary:
            if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
                found=array.array('i');found.frombytes(data[:len(data)//found.itemsize*found.itemsize]);descriptors.extend(found)
        try:
            reply=json.loads(raw)
            if flags&(socket.MSG_TRUNC|socket.MSG_CTRUNC) or len(descriptors)!=2 or reply.get('error'):raise RuntimeError('Preloader launch not acknowledged')
            return ForkProcess(reply['pid'],*descriptors)
        except BaseException:
            for fd in descriptors:os.close(fd)
            raise

    def close(self):
        connection=getattr(self,'socket',None)
        if connection is not None:connection.close();self.socket=None
        proc=getattr(self,'process',None)
        if proc is not None:
            try:proc.wait(timeout=3)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=3)


def serve(fd,root,parent):
    parent_death(parent)
    # Import only stateless modules, before any Agent data or credentials exist.
    from . import worker,capabilities,repair_loop,routing,workspace_tools,tooling
    root=Path(root).resolve();channel=socket.socket(fileno=fd);children={}
    wake_r,wake_w=os.pipe2(os.O_NONBLOCK|os.O_CLOEXEC)
    signal.signal(signal.SIGCHLD,lambda *_:None);signal.set_wakeup_fd(wake_w)
    channel.send(b'READY')
    def reap():
        while True:
            try:pid,status=os.waitpid(-1,os.WNOHANG)
            except ChildProcessError:return
            if not pid:return
            pipe=children.pop(pid,None)
            if pipe is not None:
                try:os.write(pipe,(str(os.waitstatus_to_exitcode(status))+'\n').encode())
                except BrokenPipeError:pass
                finally:os.close(pipe)
    try:
        while True:
            reap()
            ready,_,_=select.select([channel,wake_r],[],[],.2)
            if wake_r in ready:
                try:os.read(wake_r,4096)
                except BlockingIOError:pass
                reap()
            if channel not in ready:continue
            raw=channel.recv(16384)
            if not raw:break
            permit_r=permit_w=status_r=status_w=log=None
            try:
                value=json.loads(raw);directory=Path(value['directory']);payload=Path(value['payload'])
                if not directory.is_absolute() or not directory.resolve().is_relative_to(root) or directory.is_symlink() or payload!=directory/'.payload.json':raise ValueError('invalid launch paths')
                permit_r,permit_w=os.pipe();status_r,status_w=os.pipe()
                log=os.open(directory/'worker.log',os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW)
                server_pid=os.getpid();pid=os.fork()
                if pid==0:
                    try:
                        parent_death(server_pid);os.setsid()
                        signal.set_wakeup_fd(-1);signal.signal(signal.SIGCHLD,signal.SIG_DFL)
                        os.dup2(permit_r,0);os.dup2(log,1);os.dup2(log,2)
                        # Close listener and every other Attempt's descriptors.
                        for name in os.listdir('/proc/self/fd'):
                            number=int(name)
                            if number>2:
                                try:os.close(number)
                                except OSError:pass
                        sys.stdin=open(0,'r',encoding='utf-8',closefd=False)
                        sys.stdout=open(1,'w',encoding='utf-8',closefd=False,buffering=1)
                        sys.stderr=open(2,'w',encoding='utf-8',closefd=False,buffering=1)
                        sys.argv=['agent_runtime.worker',str(directory/'result.json'),str(payload)]
                        code=worker.main();sys.stdout.flush();sys.stderr.flush();os._exit(code)
                    except BaseException:
                        import traceback
                        traceback.print_exc();sys.stdout.flush();sys.stderr.flush();os._exit(1)
                children[pid]=status_w;status_w=None
                channel.sendmsg([json.dumps({'pid':pid}).encode()],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[permit_w,status_r]))])
            except (ValueError,KeyError,TypeError,OSError):
                try:channel.send(b'{"error":"launch rejected"}')
                except OSError:break
            finally:
                for number in (permit_r,permit_w,status_r,status_w,log):
                    if number is not None:
                        try:os.close(number)
                        except OSError:pass
    finally:
        for pid in children:
            try:os.killpg(pid,signal.SIGKILL)
            except ProcessLookupError:
                try:os.kill(pid,signal.SIGKILL)
                except ProcessLookupError:pass
        end=time.monotonic()+2
        while children and time.monotonic()<end:reap();time.sleep(.005)
        for pipe in children.values():os.close(pipe)
        signal.set_wakeup_fd(-1);os.close(wake_r);os.close(wake_w)
        channel.close()


if __name__=='__main__':serve(int(sys.argv[1]),sys.argv[2],int(sys.argv[3]))
