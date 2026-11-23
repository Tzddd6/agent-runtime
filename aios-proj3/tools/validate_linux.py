"""
openEuler / Linux cgroup v2 Delegated Subtree Validation Suite for AIOS Formal Kernel (Proj3 4.1).

Run on openEuler / Linux inside a temporary delegated systemd unit:
  sudo systemd-run --unit=aios-validation --wait --pipe --collect \
    -p User=tzd -p Delegate=yes -p CPUQuota=200% -p MemoryMax=2G -p TasksMax=256 -p RuntimeMaxSec=240 \
    /usr/bin/python3 "$PWD/tools/validate_linux.py"
"""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def run_delegated_validation(dry_run: bool = False) -> dict:
    if not dry_run and (sys.platform != "linux" or getattr(os, "geteuid", lambda: 1)() == 0):
        raise SystemExit("需要在 Linux (如 openEuler) 上通过 User=普通用户、Delegate=yes 的临时 systemd 服务运行 (或传 --dry-run 做跨平台预检)")

    os.chdir(PROJECT_ROOT)
    print("PLATFORM", sys.version, flush=True)

    # 1. Run full unit and Proj3 acceptance test suite
    ret = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(ret.stdout, flush=True)
    if ret.returncode != 0:
        raise SystemExit(ret.returncode)

    match = re.search(r"Ran (\d+) tests?", ret.stdout)
    regular_tests = int(match.group(1)) if match else 0
    skipped_match = re.search(r"OK \(skipped=(\d+)\)", ret.stdout)
    regular_skipped = int(skipped_match.group(1)) if skipped_match else 0

    if dry_run or sys.platform != "linux":
        report = {
            "mode": "dry_run_precheck",
            "os": platform.system(),
            "kernel": platform.release(),
            "python": platform.python_version(),
            "regular_discovered": regular_tests,
            "regular_passed": regular_tests - regular_skipped,
            "regular_skipped": regular_skipped,
            "status": "PRECHECK_PASSED",
        }
        return report

    # 2. Configure delegated cgroup v2 subtree: <root>/control and <root>/attempts
    relative = Path("/proc/self/cgroup").read_text(encoding="utf-8").strip().split("0::", 1)[1].splitlines()[0]
    root = Path("/sys/fs/cgroup") / relative.lstrip("/")
    print("DELEGATED_ROOT", root, flush=True)

    control = root / "control"
    control.mkdir(exist_ok=True)
    (control / "cgroup.procs").write_text(str(os.getpid()), encoding="utf-8")
    (root / "cgroup.subtree_control").write_text("+cpu +memory +pids", encoding="utf-8")

    attempts = root / "attempts"
    attempts.mkdir(exist_ok=True)
    (attempts / "cgroup.subtree_control").write_text("+cpu +memory +pids", encoding="utf-8")

    # 3. Verify AIOS CgroupV2Manager real slice creation, 0.2 core CPU quota throttling & zero-orphan teardown
    from aios.os_kernel.cgroup_v2 import CgroupV2Manager

    cg = CgroupV2Manager(cgroup_root=str(attempts), strict=True)
    slice_info = cg.create_syscall_slice(
        agent_name="cpu_throttle_agent",
        syscall_id=1,
        cpu_milli=200,
        memory_bytes=128 * 1024 * 1024,
        pids_max=32,
    )
    slice_path = Path(slice_info["path"])

    start = time.monotonic()
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; sys.stdin.read(1); end=time.monotonic()+2.0\nwhile time.monotonic()<end: pass",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    (slice_path / "cgroup.procs").write_text(str(worker.pid), encoding="utf-8")
    assert worker.stdin is not None
    worker.stdin.write("1")
    worker.stdin.flush()
    worker.wait(timeout=15)
    elapsed = time.monotonic() - start

    cpu_stat_raw = (slice_path / "cpu.stat").read_text(encoding="utf-8")
    usage = dict(line.split() for line in cpu_stat_raw.splitlines() if len(line.split()) == 2)
    ratio = int(usage.get("usage_usec", "0")) / 1e6 / max(elapsed, 0.001)

    cg.destroy_syscall_slice(slice_info)
    try:
        slice_path.parent.rmdir()
    except OSError:
        pass

    assert worker.returncode == 0, f"Worker exited with {worker.returncode}"
    assert 0.0 < ratio < 0.40, f"Expected ~0.2 core ratio, got {ratio}"
    assert int(usage.get("nr_throttled", "0")) > 0, f"Expected CPU throttling > 0, got {usage}"
    assert not [p for p in attempts.iterdir() if p.is_dir()], "Orphan cgroup directories left in attempts"

    # 4. Run full Proj3 end-to-end verification under delegated Linux environment
    e2e = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "examples" / "proj3_full_verification.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if e2e.returncode != 0:
        print(e2e.stdout, flush=True)
        raise SystemExit(e2e.returncode)

    report = {
        "enforcement": "cgroup_v2",
        "runtime_user": os.getuid(),
        "platform": os.uname().release,
        "python": sys.version,
        "wall_seconds": round(elapsed, 4),
        "cpu_core_ratio": round(ratio, 4),
        "cpu_stat": usage,
        "regular_discovered": regular_tests,
        "regular_passed": regular_tests - regular_skipped,
        "regular_skipped": regular_skipped,
        "proj3_e2e": "passed",
        "zero_orphan_cgroups": True,
    }
    reports_dir = PROJECT_ROOT / "reports"
    reports_dir.mkdir(exist_ok=True)
    (reports_dir / "linux-validation-latest.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print("LINUX_VALIDATION", json.dumps(report, ensure_ascii=False), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Run cross-platform test & precheck without Linux root cgroup mount")
    args = parser.parse_args()
    res = run_delegated_validation(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
