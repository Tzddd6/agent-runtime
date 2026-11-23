# AIOS Proj3 — 2026-11-23 交付快照

本目录为最终冻结源码的展开版本（不含运行产物）。准确的能力边界、测试计数与部署步骤以 [交付说明](../releases/proj3-20261123/README.md) 为准；下方保留的历史说明可能包含旧计数和本机路径。

```bash
cd aios-proj3
python3 -m venv .venv
source .venv/bin/activate
python -m pip install pymupdf pillow
python -m unittest discover -s tests -v
python -m runtime.web --port 8765
```

对话入口：http://127.0.0.1:8765/ ，监控：http://127.0.0.1:8765/console/ 。Windows 激活命令为 `.venv\Scripts\Activate.ps1`；Linux 硬隔离专项需要委派 cgroup v2，请按交付手册验收。不要把 API Key 提交到 Git。

---

## 冻结源码原始说明（历史文本）

# AIOS: AI Agent Operating System (Proj3 正式内核与六篇论文生态架构)

本仓库以 [agiresearch/AIOS](https://github.com/agiresearch/AIOS) 及其 **6 篇系列顶会/核心论文** 为正式架构骨架，并完整实现了 **2026年全国大学生计算机系统能力大赛（操作系统设计赛—功能挑战赛道）proj3：面向多智能体的操作系统级执行时（Agent Runtime）** 的全部 7 项核心技术要求（4.1～4.7）。

详细论文机制对照见：[docs/AIOS六篇论文生态落地对照.md](file:///d:/gemini/aios/docs/AIOS六篇论文生态落地对照.md)。

---

## 一、AIOS 六篇核心论文在本仓库的落地映射

| 论文 | 会议 / 出处 | 本仓库核心落地模块 |
| :--- | :--- | :--- |
| **1. AIOS: LLM Agent Operating System** | **COLM 2025** | [syscall.py](file:///d:/gemini/aios/aios/syscall/syscall.py)、[fifo_scheduler.py](file:///d:/gemini/aios/aios/scheduler/fifo_scheduler.py)、[rr_scheduler.py](file:///d:/gemini/aios/aios/scheduler/rr_scheduler.py)、[simple_context.py](file:///d:/gemini/aios/aios/context/simple_context.py)：四路独立 Syscall 队列、时间窗批处理、RR 时间片抢占与上下文快照恢复 |
| **2. Cerebrum (AIOS SDK)** | **NAACL 2025** | [cerebrum/client.py](file:///d:/gemini/aios/cerebrum/client.py)、[cerebrum/hub.py](file:///d:/gemini/aios/cerebrum/hub.py)：Kernel/SDK 解耦、`LLMQuery/MemoryQuery/StorageQuery/ToolQuery` 协议、Local/Remote 双模式及 `AgentHub` 发现/分发平台 |
| **3. From Commands to Prompts (LSFS)** | **ICLR 2025** | [lsfs.py](file:///d:/gemini/aios/aios/storage/filesystem/lsfs.py)、[intent_router.py](file:///d:/gemini/aios/aios/terminal/intent_router.py)：`IntentRouter` 将中英文自然语言 Prompt 翻译为 `StorageQuery` 系统调用；支持版本栈回滚 `rollback`、安全链接共享 `share` 与语义检索 `retrieve` |
| **4. A-Mem: Agentic Memory for LLM Agents** | **arXiv 2502.12110** | [note.py](file:///d:/gemini/aios/aios/memory/note.py)、[amem.py](file:///d:/gemini/aios/aios/memory/amem.py)、[manager.py](file:///d:/gemini/aios/aios/memory/manager.py)：Zettelkasten 卡片盒记忆笔记、自动双向建链（`links`）、邻居记忆动态演化（`evolution_count` + 标签/摘要融合）与 1-hop 图关联检索 |
| **5. LiteCUA: Computer as MCP Server** | **arXiv 2505.18829** | [desktop_env.py](file:///d:/gemini/aios/aios/tool/virtual_env/desktop_env.py)、[mcp_server.py](file:///d:/gemini/aios/aios/tool/mcp_server.py)：将桌面环境抽象为 `VMController` + 5 个标准 MCP 工具（`litecua/observe` 无障碍树、`click`、`type`、`hotkey`、`snapshot` 状态回滚） |
| **6. LLM as OS, Agents as Apps** | **arXiv 2312.03815** | [cerebrum/hub.py](file:///d:/gemini/aios/cerebrum/hub.py)、[drf_admission.py](file:///d:/gemini/aios/aios/scheduler/drf_admission.py)：将 Agent 建模为带权限清单 `AgentManifest` 的应用程序，`ScopedAppClient` 拦截越权系统调用与未授权工具，`AgentControlBlock` 绑定 OS 资源配额 |

---

## 二、Proj3 赛题技术要求（4.1～4.7）覆盖矩阵

| 赛题条款 | 对应内核模块 | 核心机制实现 |
| :--- | :--- | :--- |
| **4.1 操作系统基础与兼容性** | [cgroup_v2.py](file:///d:/gemini/aios/aios/os_kernel/cgroup_v2.py)、[doctor.py](file:///d:/gemini/aios/tools/doctor.py)、[validate_linux.py](file:///d:/gemini/aios/tools/validate_linux.py) | 支持 openEuler 等 Linux cgroup v2 双层委派树（`cpu.max`, `memory.max`, `memory.swap.max=0`, `pids.max`, `cgroup.kill`）及实机一键委派验收 |
| **4.2 多Agent任务调度** | [workflow_engine.py](file:///d:/gemini/aios/aios/scheduler/workflow_engine.py)、[drf_admission.py](file:///d:/gemini/aios/aios/scheduler/drf_admission.py) | 支持 DAG 任务依赖与环检测、运行时**动态生成子任务（`spawn_tasks`）**并自动接线、DRF 主导资源份额 + Aging 防饥饿 + RR 时间片调度 |
| **4.3 统一的Agent执行抽象模型** | [drf_admission.py](file:///d:/gemini/aios/aios/scheduler/drf_admission.py) | `AgentControlBlock` 一等公民抽象：`created -> ready -> running -> paused -> terminated` 状态机、`pause/resume/terminate` 控制、OS 资源向量与两级模型预算 |
| **4.4 Agent执行容错** | [workflow_engine.py](file:///d:/gemini/aios/aios/scheduler/workflow_engine.py) | 单体 Agent 异常/超时/契约违约经过有限重试后仅隔离故障下游分支（`isolated_skipped`），健康并行分支不受级联影响；支持原子 Checkpoint 恢复 |
| **4.5 高效的上下文管理** | [context_store.py](file:///d:/gemini/aios/aios/context/context_store.py) | **复用**（SHA256 内容寻址去重 + LRU-2 缓存）、**压缩**（磁盘 gzip 无损压缩 + 保留目标/约束的字节预算视图）、**隔离**（Agent ACL 授权表与显式转授） |
| **4.6 复杂场景验证** | [proj3_full_verification.py](file:///d:/gemini/aios/examples/proj3_full_verification.py) | 6-Agent 软件缺陷诊断、动态静态分析/安全审计、故障注入隔离、补丁合成与校验全链路闭环 |
| **4.7 Agent间通信** | [mailbox.py](file:///d:/gemini/aios/aios/ipc/mailbox.py) | 内核级 Mailbox IPC：临时凭据鉴权、`request_id` 幂等与冲突检测、`receive/ack` 至少一次交付、零拷贝 `context_refs` 引用传递与自动 ACL 转授 |

---

## 三、快速启动与全套验收命令

```bash
# 1. 运行全套 53 项单元与集成测试 (AIOS 内核 + Proj3 4.1~4.7 + SSE 流式 + 六篇论文生态 + 原生控制台)
python -m unittest discover -s tests -v

# 2. 启动统一原生 Web 对话工作台 (/) 与内核控制台 (/console/)
python -m runtime.web --port 18892

# 3. 运行环境与 cgroup v2 就绪检查
python tools/doctor.py --json

# 4. 运行双协议 (Chat Completions SSE & Responses SSE) 四类复杂流式格式核验
python tools/validate_stream_formats.py

# 5. 运行 FIFO vs RR 时间片与上下文压缩基准实验 / 原生 DAG 并发基准实验
python tools/benchmark_aios.py --repeats 3
python tools/benchmark_native_dag.py

# 6. 运行 Proj3 4.1~4.7 端到端多Agent工程与容错验收
python examples/proj3_full_verification.py

# 7. 在 openEuler 实机执行 systemd Delegate=yes 严格 cgroup v2 验收 (本地预检可加 --dry-run)
python tools/validate_linux.py --dry-run
```
