# AIOS 六篇核心论文生态在本项目的融合与落地对照

本项目以 `agiresearch/AIOS` 家族的 **6 篇系列顶会/核心论文** 为理论与架构骨架，同时融合了面向 **2026 全国大学生计算机系统能力大赛（操作系统设计赛 proj3）** 的 Linux `cgroup v2` 双层资源隔离与 DRF 公平调度机制。

---

## 一、六篇论文与代码模块一对一落地矩阵

| 论文名称 | 会议 / 出处 | 核心学术思想 | 本项目对应实现模块 | 已验证的核心机制 |
| :--- | :--- | :--- | :--- | :--- |
| **1. AIOS: LLM Agent Operating System** | **COLM 2025** (主论文) | 将 LLM 嵌入 OS 内核：Syscall 原子拆分、四队列并发调度（FIFO 批处理 / RR 时间片抢占）、生成过程上下文快照切换 | [syscall.py](file:///d:/gemini/aios/aios/syscall/syscall.py)<br>[fifo_scheduler.py](file:///d:/gemini/aios/aios/scheduler/fifo_scheduler.py)<br>[rr_scheduler.py](file:///d:/gemini/aios/aios/scheduler/rr_scheduler.py)<br>[simple_context.py](file:///d:/gemini/aios/aios/context/simple_context.py) | `LLMSyscall / MemorySyscall / StorageSyscall / ToolSyscall` 四路独立消费线程、`batch_interval` 批处理、`time_slice` 流式生成挂起与续片恢复 |
| **2. Cerebrum (AIOS SDK)** | **NAACL 2025** (SDK 平台) | Kernel 与 SDK 解耦、标准化四类 `Query/Response` 协议、支持 Local 与 Remote Kernel 模式及 Agent 打包分发发现平台 | [cerebrum/client.py](file:///d:/gemini/aios/cerebrum/client.py)<br>[cerebrum/hub.py](file:///d:/gemini/aios/cerebrum/hub.py)<br>[runtime/launch.py](file:///d:/gemini/aios/runtime/launch.py) | `CerebrumClient`（本地直连 + `/query` 远程 HTTP RPC）、`AgentHub`（`publish / discover / load / run_app`） |
| **3. From Commands to Prompts (LSFS)** | **ICLR 2025** (语义文件系统) | 用自然语言 Prompt 替代传统 Shell 文件命令；支持语义文件检索 `retrieve`、多版本回滚 `rollback`、安全链接共享 `share` | [lsfs.py](file:///d:/gemini/aios/aios/storage/filesystem/lsfs.py)<br>[intent_router.py](file:///d:/gemini/aios/aios/terminal/intent_router.py) | `IntentRouter` 将中英文自然语言指令路由为 `StorageQuery`；`LSFS` 支持沙箱路径保护、版本栈 `rollback(file_path, n)`、`lsfs://share/<token>` 与语义检索 |
| **4. A-Mem: Agentic Memory for LLM Agents** | **arXiv 2502.12110** (Agent 记忆) | 基于卡片盒笔记法（Zettelkasten）的动态记忆网络：结构化笔记构造、自动双向建链（Link Generation）、邻居记忆动态演化（Memory Evolution）与图关联检索 | [note.py](file:///d:/gemini/aios/aios/memory/note.py)<br>[amem.py](file:///d:/gemini/aios/aios/memory/amem.py)<br>[manager.py](file:///d:/gemini/aios/aios/memory/manager.py) | `MemoryNote` 自动提取 `keywords/tags/context_summary`；新记忆写入时由 `AMemEngine.link_and_evolve` 建立双向 `links` 并演化旧笔记标签与摘要；检索支持 1-hop 关联扩散与 LRU-K 淘汰 |
| **5. LiteCUA: Computer as MCP Server** | **arXiv 2505.18829** (电脑操作 Agent) | 将电脑桌面环境抽象为 **VM Controller + MCP Server**，将 Agent 操作意图映射为无障碍树（a11y tree）感知与受控 GUI 动作 | [desktop_env.py](file:///d:/gemini/aios/aios/tool/virtual_env/desktop_env.py)<br>[mcp_server.py](file:///d:/gemini/aios/aios/tool/mcp_server.py) | 内置 `VMController` 与 5 个标准 LiteCUA MCP 工具：`litecua/observe`（无障碍树感知）、`litecua/click`、`litecua/type`、`litecua/hotkey`、`litecua/snapshot`（环境快照保存与回滚） |
| **6. LLM as OS, Agents as Apps** | **arXiv 2312.03815** (奠基思想论文) | “大模型即操作系统，智能体即应用程序”：每个 Agent App 拥有独立权限清单（Manifest）、OS 资源配额契约与运行时沙箱边界 | [cerebrum/hub.py](file:///d:/gemini/aios/cerebrum/hub.py)<br>[drf_admission.py](file:///d:/gemini/aios/aios/scheduler/drf_admission.py)<br>[cgroup_v2.py](file:///d:/gemini/aios/aios/os_kernel/cgroup_v2.py) | `AgentManifest` 声明 `required_syscalls`、`allowed_tools` 与 `resource_quota`；`ScopedAppClient` 拦截越权系统调用与未授权工具；`AgentControlBlock` 强制执行模型预算与 OS 资源配额 |

---

## 二、复现与验证命令

```bash
# 运行六篇论文生态专项单元测试
python -m unittest tests/test_aios_paper_ecosystem.py -v

# 运行全仓库所有测试 (AIOS 内核 + Proj3 4.1~4.7 + SSE 流式 + 六篇论文生态，共 22 项)
python -m unittest discover -s tests -v
```
