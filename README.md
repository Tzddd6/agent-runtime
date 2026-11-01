# Agent Runtime 最小原型

Python 3.9+，只使用标准库。在本目录执行：

```bash
python -m agent_runtime
python -m unittest -v
```

输出是逐行 JSON 事件。示例先执行 plan，再并行执行 analyze 和 test_plan，最后执行 review。可用 `python -m agent_runtime --tasks tasks.json --workers 2 --timeout 10` 加载自定义任务数组，字段参照 `agent_runtime/__main__.py` 中的 `DEMO`。

当前功能：静态依赖校验、有限并发、独立 Worker 进程、超时终止、失败分支隔离和事件输出。模拟动作包括 echo、sleep、fail。

当前限制：尚未接入真实大模型、动态任务、上下文管理、IPC 服务、cgroups、进程树清理、可靠持久化或重试；超时只终止 Worker 本身。它是跨平台教学原型，不能作为最终比赛作品或安全沙箱。

下一步在 NUC 的 openEuler 环境运行并验证，再按照项目总体框架依次实现动态任务、进程树管理与 cgroups、上下文和真实模型适配。CPU/内存资源控制需确认目标环境的 cgroup v2 委派权限。
