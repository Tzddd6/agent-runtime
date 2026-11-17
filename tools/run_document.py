"""Process a real UTF-8 document with a dynamically planned multi-agent DAG."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runtime.runtime import run
from agent_runtime.scenarios import tasks_for


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('document', type=Path)
    p.add_argument('--cgroup-root')
    p.add_argument('--output-dir', type=Path, default=Path('runs'))
    args = p.parse_args()
    if args.document.stat().st_size > 128 * 1024:
        p.error('文档不得超过 128 KiB')
    states, events = run(tasks_for(args.document.read_text(encoding='utf-8')),
                        cgroup_root=args.cgroup_root, output_dir=args.output_dir)
    root = args.output_dir.resolve() / events[0]['workflow_id']
    print(json.dumps({'states': states, 'report':str(root/'review-1/report.json')}, ensure_ascii=False))
    return 0 if all(s == 'succeeded' for s in states.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
