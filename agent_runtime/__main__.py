import argparse
import json
from pathlib import Path


from .runtime import run, validate
from .scheduling import AdmissionScheduler


DEMO = [
    {'id': 'plan', 'message': '制定计划'},
    {'id': 'analyze', 'depends_on': ['plan'], 'action': 'sleep', 'seconds': 0.2, 'message': '分析代码'},
    {'id': 'test_plan', 'depends_on': ['plan'], 'action': 'sleep', 'seconds': 0.2, 'message': '准备测试'},
    {'id': 'review', 'depends_on': ['analyze', 'test_plan'], 'message': '汇总结果'},
]


def main():
    parser = argparse.ArgumentParser(description='最小 Agent Runtime 演示')
    parser.add_argument('--tasks', type=Path, help='JSON 任务数组；省略则运行内置示例')
    parser.add_argument('--agents',type=Path,help='Agent 控制块配置 JSON 数组；省略则按任务自动注册')
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--output-dir', type=Path, default=Path('runs'))
    parser.add_argument('--policy', choices=AdmissionScheduler.POLICIES, default='resource_fit')
    parser.add_argument('--cgroup-root', help='已委派且已启用控制器的空 attempts 子树')
    parser.add_argument('--model-budget',type=Path,help='工作流模型预算 JSON：calls / output_tokens')
    args = parser.parse_args()
    try:
        tasks = json.loads(args.tasks.read_text(encoding='utf-8')) if args.tasks else DEMO
        agents=json.loads(args.agents.read_text(encoding='utf-8')) if args.agents else None
        model_budget=json.loads(args.model_budget.read_text(encoding='utf-8')) if args.model_budget else None
        states, _ = run(tasks, args.workers, args.timeout, output_dir=args.output_dir, policy=args.policy, cgroup_root=args.cgroup_root,agents=agents,model_budget=model_budget)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    return 0 if all(s == 'succeeded' for s in states.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
