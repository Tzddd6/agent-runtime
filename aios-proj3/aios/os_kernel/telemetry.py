"""Read-only Linux / Host resource sampling for AIOS Kernel Console."""
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, Optional


def _number(path: Path) -> Optional[int]:
    try:
        val = path.read_text(encoding="utf-8").strip()
        return None if val == "max" else int(val)
    except (OSError, ValueError):
        return None


class CgroupSampler:
    def __init__(self):
        self.previous: Dict[str, tuple[float, int, Optional[float]]] = {}

    def sample(self, root: Optional[str | Path]) -> Dict[str, Any]:
        empty = {
            "source": "unavailable",
            "cpu_cores": None,
            "cpu_total_seconds": None,
            "memory_bytes": None,
            "memory_peak_bytes": None,
            "pids": None,
            "throttled_periods": None,
        }
        if root is None:
            return empty
        p = Path(root)
        try:
            values = dict(line.split() for line in (p / "cpu.stat").read_text(encoding="utf-8").splitlines())
            usage, now = int(values["usage_usec"]), time.monotonic()
            prev = self.previous.get(str(p))
            cores = prev[2] if prev else None
            if prev and now - prev[0] >= 0.1 and usage >= prev[1]:
                cores = (usage - prev[1]) / 1e6 / (now - prev[0])
                self.previous[str(p)] = (now, usage, cores)
            elif not prev or usage < prev[1]:
                self.previous[str(p)] = (now, usage, None)
                cores = None
            return {
                "source": "cgroup_v2",
                "cpu_cores": cores,
                "cpu_total_seconds": usage / 1e6,
                "memory_bytes": _number(p / "memory.current"),
                "memory_peak_bytes": _number(p / "memory.peak"),
                "pids": _number(p / "pids.current"),
                "throttled_periods": int(values.get("nr_throttled", 0)),
            }
        except (OSError, ValueError, KeyError):
            return empty


class MachineSampler:
    def __init__(self, attempts: Optional[str | Path] = None):
        self.service = Path(attempts).parent if attempts else None
        self.cgroups = CgroupSampler()
        self.previous = None
        self.cached: Optional[Dict[str, Any]] = None
        self.sampled = 0.0
        self.lock = threading.Lock()

    def sample(self) -> Dict[str, Any]:
        with self.lock:
            now = time.monotonic()
            if self.cached is not None and now - self.sampled < 0.5:
                return self.cached
            host: Dict[str, Any] = {
                "source": "unavailable",
                "cpu_percent": None,
                "logical_cpus": os.cpu_count() or 4,
                "memory_total_bytes": None,
                "memory_used_bytes": None,
            }
            try:
                ticks = [int(v) for v in Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:9]]
                total, idle = sum(ticks), ticks[3] + ticks[4]
                if self.previous and total > self.previous[0]:
                    host["cpu_percent"] = max(
                        0.0, min(100.0, 100.0 * (1.0 - (idle - self.previous[1]) / (total - self.previous[0])))
                    )
                self.previous = (total, idle)
                mem = {
                    line.split(":", 1)[0]: int(line.split()[1]) * 1024
                    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
                    if len(line.split()) >= 3
                }
                host.update(
                    source="procfs",
                    memory_total_bytes=mem["MemTotal"],
                    memory_used_bytes=mem["MemTotal"] - mem["MemAvailable"],
                )
            except (OSError, ValueError, KeyError, IndexError):
                pass
            service = self.cgroups.sample(self.service)
            service.update(cpu_limit_cores=None, memory_limit_bytes=None, pids_limit=None)
            self.cached = {"host": host, "service": service, "time": time.time()}
            self.sampled = now
            return self.cached
