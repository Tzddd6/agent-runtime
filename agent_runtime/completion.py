"""Exit notification only. pidfds never authorize resource release or termination."""
import os
import selectors
import sys
import time


class CompletionWaiter:
    def __init__(self):
        self.selector=selectors.DefaultSelector()
        self.files={}
        self.stats={'pidfds_registered':0,'fallback_registrations':0,'waits':0,'exit_wakeups':0}

    def add(self,key,proc):
        fd=None
        try:
            if sys.platform!='linux' or not hasattr(os,'pidfd_open'):raise OSError('pidfd unavailable')
            fd=os.pidfd_open(proc.pid,0)
            self.selector.register(fd,selectors.EVENT_READ,key);self.files[key]=fd
            self.stats['pidfds_registered']+=1
        except (OSError,ValueError,KeyError):
            if fd is not None:os.close(fd)
            self.stats['fallback_registrations']+=1

    def remove(self,key):
        fd=self.files.pop(key,None)
        if fd is None:return
        try:self.selector.unregister(fd)
        except (KeyError,OSError,ValueError):pass
        try:os.close(fd)
        except OSError:pass

    def wait(self,timeout=.02):
        self.stats['waits']+=1
        if not self.files:time.sleep(timeout);return
        try:
            if self.selector.select(timeout):self.stats['exit_wakeups']+=1
        except (OSError,ValueError):time.sleep(timeout)

    def close(self):
        for key in list(self.files):self.remove(key)
        self.selector.close()
