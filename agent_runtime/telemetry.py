"""Read-only Linux resource sampling. Reservations and measurements stay separate."""
import os
import threading
import time
from pathlib import Path


def number(path):
    try:
        value = path.read_text().strip()
        return None if value == 'max' else int(value)
    except (OSError, ValueError):
        return None


class CgroupSampler:
    def __init__(self):
        self.previous = {}

    def sample(self, root):
        empty = {'source':'unavailable', 'cpu_cores':None, 'cpu_total_seconds':None,
                 'memory_bytes':None, 'memory_peak_bytes':None, 'pids':None, 'throttled_periods':None}
        if root is None:
            return empty
        root = Path(root)
        try:
            values = dict(line.split() for line in (root/'cpu.stat').read_text().splitlines())
            usage, now = int(values['usage_usec']), time.monotonic()
            previous = self.previous.get(str(root))
            cores = previous[2] if previous else None
            if previous and now-previous[0] >= .1 and usage >= previous[1]:
                cores = (usage-previous[1])/1e6/(now-previous[0])
                self.previous[str(root)] = (now,usage,cores)
            elif not previous or usage<previous[1]:
                self.previous[str(root)] = (now,usage,None)
                cores = None
            return {'source':'cgroup_v2', 'cpu_cores':cores, 'cpu_total_seconds':usage/1e6,
                    'memory_bytes':number(root/'memory.current'),
                    'memory_peak_bytes':number(root/'memory.peak'),
                    'pids':number(root/'pids.current'),
                    'throttled_periods':int(values.get('nr_throttled',0))}
        except (OSError, ValueError, KeyError):
            return empty

    def forget(self, root):
        if root is not None:
            self.previous.pop(str(root), None)


class MachineSampler:
    def __init__(self, attempts=None):
        self.service = Path(attempts).parent if attempts else None
        self.cgroups = CgroupSampler()
        self.previous = None
        self.cached, self.sampled = None, 0
        self.lock = threading.Lock()

    def sample(self):
        with self.lock:
            now = time.monotonic()
            if self.cached is not None and now-self.sampled < .5:
                return self.cached
            host = {'source':'unavailable', 'cpu_percent':None, 'logical_cpus':os.cpu_count(),
                    'memory_total_bytes':None, 'memory_used_bytes':None}
            try:
                ticks = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
                total, idle = sum(ticks), ticks[3]+ticks[4]
                if self.previous and total > self.previous[0]:
                    host['cpu_percent'] = max(0,min(100,100*(1-(idle-self.previous[1])/(total-self.previous[0]))))
                self.previous = (total,idle)
                memory = {line.split(':',1)[0]:int(line.split()[1])*1024
                          for line in Path('/proc/meminfo').read_text().splitlines() if len(line.split())>=3}
                host.update(source='procfs', memory_total_bytes=memory['MemTotal'],
                            memory_used_bytes=memory['MemTotal']-memory['MemAvailable'])
            except (OSError, ValueError, KeyError, IndexError):
                pass
            service = self.cgroups.sample(self.service)
            service.update(cpu_limit_cores=None, memory_limit_bytes=None)
            if self.service:
                try:
                    quota, period = (self.service/'cpu.max').read_text().split()
                    if quota != 'max':service['cpu_limit_cores']=int(quota)/int(period)
                    service['memory_limit_bytes']=number(self.service/'memory.max')
                except (OSError,ValueError,ZeroDivisionError):
                    pass
            self.cached = {'host':host,'service':service,'time':time.time()}
            self.sampled = now
            return self.cached
