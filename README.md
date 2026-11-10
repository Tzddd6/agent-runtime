# Agent Runtime：面向多智能体的操作系统级执行时

本仓库是 **2026 年全国大学生计算机系统能力大赛—操作系统设计赛—中西部与华南区域赛—功能挑战赛道** 赛题
「面向多智能体的操作系统级执行时（Multi-Agent-Oriented Operating System-Level Agent Runtime）」的参赛项目。
赛题难度 A，维护单位武汉大学。完整赛题见 [docs/赛题要求.md](docs/赛题要求.md)。

## 目标

现有多 Agent 系统大多停留在应用层框架（Workflow/Graph 调度），缺乏操作系统级的统一执行时，导致：
调度不统一、上下文（Context/KV Cache）冗余拷贝、模型/工具/系统资源缺乏统一抽象、执行过程缺乏系统级可观测性与控制。

本项目把 Agent 提升为“操作系统一等公民”，在国内开源 OS（计划 openEuler）上实现对 Agent 任务的统一建模、调度、资源管理与上下文优化。

## 赛题要求与当前进度

| 赛题要求 | 内容 | 当前状态 |
| --- | --- | --- |
| 4.1 OS 基础与兼容性 | 基于 openEuler / openKylin / OpenHarmony 至少一个开发，鼓励多发行版测试 | 待在 NUC 的 openEuler 上验证 |
| 4.2 多 Agent 任务调度 | 任务依赖、动态生成任务、资源感知调度 | 已有静态依赖校验与有限并发；动态任务、资源感知调度待做 |
| 4.3 统一执行抽象 | Agent 生命周期、状态转换、与 OS 资源的关系 | 已有独立 Worker 进程与事件输出；完整状态机与资源映射待设计 |
| 4.4 执行容错 | 隔离单个 Agent 故障，避免级联失败 | 已有超时终止与失败分支隔离；进程树清理、重试、持久化待做 |
| 4.5 上下文管理 | 上下文复用、压缩、隔离，降低执行开销 | 待做 |
| 4.6 复杂场景验证 | 构建真实多 Agent 应用验证可用性与稳定性 | 待做 |
| 4.7 Agent 间通信 | 高效通信机制 | 待做（计划 IPC 服务） |

## 评分权重

| 维度 | 权重 |
| --- | ---: |
| 系统设计与机制创新 | 30% |
| 功能完整性与实现深度 | 25% |
| 性能优化效果 | 20% |
| 工程实现质量 | 15% |
| 实验与分析质量 | 10% |

## 快速开始（当前最小原型）

Python 3.9+，只使用标准库。在本目录执行：

```bash
python -m agent_runtime
python -m unittest -v
```

输出是逐行 JSON 事件。示例先执行 plan，再并行执行 analyze 和 test_plan，最后执行 review。
可用 `python -m agent_runtime --tasks tasks.json --workers 2 --timeout 10` 加载自定义任务数组，字段参照 `agent_runtime/__main__.py` 中的 `DEMO`。

当前功能：静态依赖校验、有限并发、独立 Worker 进程、超时终止、失败分支隔离和事件输出。模拟动作包括 echo、sleep、fail。

当前限制：尚未接入真实大模型、动态任务、上下文管理、IPC 服务、cgroups、进程树清理、可靠持久化或重试；超时只终止 Worker 本身。它是跨平台教学原型，不能作为最终比赛作品或安全沙箱。

## 路线图

1. 在 NUC 的 openEuler 环境运行并验证当前原型。
2. 动态任务生成与资源感知调度。
3. 进程树管理与 cgroups v2 资源控制（需确认目标环境的 cgroup v2 委派权限）。
4. Agent 生命周期状态机与故障隔离、重试、持久化。
5. 上下文复用、压缩、隔离机制。
6. Agent 间通信（IPC 服务）。
7. 真实大模型适配、复杂多 Agent 场景与性能实验。

## 参考资料

赛题列出的参考方向，PDF 见 [docs/references](docs/references)：

- AIOS: LLM Agent Operating System
- 多 Agent 框架：LangGraph / AutoGen / CrewAI
- Linux 调度器（CFS / EEVDF）与 cgroup v2 容器资源管理


## v0.2 初版（2026-11-10）

新增模块化 Runtime、动态 DAG、资源准入、有限重试、上下文对象、严格 cgroup 后端。运行方式及验证边界见 [运行指南](docs/运行指南-v0.2.md)。
