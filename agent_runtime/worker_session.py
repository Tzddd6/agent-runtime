"""Explicit serial workflow lease over a credential-free preloader.

Every Attempt still has a fresh process and cgroup. Agent payloads are never
loaded in the preloader. A failed launch is not replayed; a later workflow may
start a new preloader after the failed workflow has reclaimed its resources.
"""
import threading
from pathlib import Path


class WorkerSession:
    def __init__(self, root):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.preloader=None;self.lock=threading.Lock();self.closed=False;self.tainted=False
        self.stats={'preloader_starts':0,'recovered_preloaders':0,'leases':0,'launches':0,'startup_seconds':0.}

    def acquire(self, workflow_root):
        if not self.lock.acquire(blocking=False):raise RuntimeError('WorkerSession already leased by another workflow')
        try:
            if self.closed:raise RuntimeError('WorkerSession is closed')
            if not Path(workflow_root).resolve().is_relative_to(self.root):raise ValueError('Workflow outside WorkerSession root')
            self.stats['leases']+=1
        except BaseException:self.lock.release();raise

    def launch(self, directory, payload_path):
        if not self.lock.locked() or self.closed:raise RuntimeError('WorkerSession requires an active lease')
        from .forkserver import ForkServer
        try:
            if self.preloader is not None and self.preloader.process.poll() is not None:
                self.preloader.close();self.preloader=None
                self.stats['recovered_preloaders']+=1
            if self.preloader is None:
                self.preloader=ForkServer(self.root);self.stats['preloader_starts']+=1
                self.stats['startup_seconds']+=self.preloader.startup_seconds
            result=self.preloader.launch(directory,payload_path)
            self.stats['launches']+=1
            return result
        except BaseException:
            self.tainted=True
            raise

    def release(self):
        try:
            if self.tainted and self.preloader is not None:
                self.preloader.close();self.preloader=None
        finally:
            self.tainted=False;self.lock.release()

    def close(self):
        if not self.lock.acquire(blocking=False):raise RuntimeError('Cannot close an active WorkerSession')
        try:
            self.closed=True
            if self.preloader is not None:self.preloader.close();self.preloader=None
        finally:self.lock.release()

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
