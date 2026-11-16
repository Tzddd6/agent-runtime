"""Read-only deployment facts, never prints environment credentials."""
import argparse
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import sqlite3
import sys


def inspect():
    linux=sys.platform=='linux';controllers=[];unified=False;distro='unknown'
    if linux:
        try:
            unified=any(' - cgroup2 ' in line for line in Path('/proc/self/mountinfo').read_text().splitlines())
            controllers=(Path('/sys/fs/cgroup')/'cgroup.controllers').read_text().split()
            distro=next(line.split('=',1)[1].strip('"') for line in Path('/etc/os-release').read_text().splitlines() if line.startswith('PRETTY_NAME='))
        except (OSError,StopIteration):pass
    fts=False
    with sqlite3.connect(':memory:') as db:
        try:db.execute('CREATE VIRTUAL TABLE probe USING fts5(text)');fts=True
        except sqlite3.Error:pass
    return {'python':platform.python_version(),'python_supported':sys.version_info>=(3,11),'os':distro if linux else platform.system(),
        'linux':linux,'kernel':platform.release(),'cgroup_v2_mounted':unified,'cgroup_controllers':controllers,
        'sqlite_fts5':fts,'bubblewrap_installed':bool(shutil.which('bwrap')),'pillow_installed':importlib.util.find_spec('PIL') is not None,
        'delegation_note':'挂载存在不代表可写委派。正式硬限制仍需 systemd Delegate=yes 和项目严格验收。',
        'core_ready':sys.version_info>=(3,11) and fts,
        'strict_prerequisites_present':linux and unified and {'cpu','memory','pids'}<=set(controllers) and bool(shutil.which('bwrap'))}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--json',action='store_true');a=p.parse_args();result=inspect()
    if a.json:print(json.dumps(result,ensure_ascii=False,indent=2))
    else:
        for key,value in result.items():print(f'{key}: {value}')
    raise SystemExit(0 if result['core_ready'] else 1)
