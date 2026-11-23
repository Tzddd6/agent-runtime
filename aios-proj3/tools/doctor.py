"""Read-only AIOS Kernel & openEuler Linux deployment readiness inspector. Never prints credentials."""
import argparse
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import sqlite3
import sys


def inspect_environment() -> dict:
    linux = sys.platform == "linux"
    controllers: list[str] = []
    unified = False
    distro = "unknown"
    if linux:
        try:
            unified = any(" - cgroup2 " in line for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines())
            controllers = (Path("/sys/fs/cgroup") / "cgroup.controllers").read_text(encoding="utf-8").split()
            distro = next(
                line.split("=", 1)[1].strip('"')
                for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
                if line.startswith("PRETTY_NAME=")
            )
        except (OSError, StopIteration):
            pass

    fts = False
    with sqlite3.connect(":memory:") as db:
        try:
            db.execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
            fts = True
        except sqlite3.Error:
            pass

    return {
        "architecture": "AIOS-Formal-Kernel",
        "python": platform.python_version(),
        "python_supported": sys.version_info >= (3, 11),
        "os": distro if linux else platform.system(),
        "linux": linux,
        "kernel": platform.release(),
        "cgroup_v2_mounted": unified,
        "cgroup_controllers": controllers,
        "sqlite_fts5": fts,
        "bubblewrap_installed": bool(shutil.which("bwrap")),
        "pillow_installed": importlib.util.find_spec("PIL") is not None,
        "pyyaml_installed": importlib.util.find_spec("yaml") is not None,
        "delegation_note": "挂载存在不代表可写委派。正式硬限制仍需 systemd Delegate=yes 和 tools/validate_linux.py 严格验收。",
        "core_ready": sys.version_info >= (3, 11) and fts,
        "strict_prerequisites_present": (
            linux and unified and {"cpu", "memory", "pids"} <= set(controllers)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Output JSON format")
    args = parser.parse_args()
    result = inspect_environment()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0 if result["core_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
