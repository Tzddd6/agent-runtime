# Agent Runtime v0.8 开发版

本项目对应 OS 功能挑战 proj3，与 ICT 算子竞赛无关。Python 3.9+，仅用标准库。

v0.8 补读 AIOS、DRF、Ray、MemGPT 原文，新增 Agent 主导份额公平准入，以及上下文/依赖结果的授权引用通路。网页支持 DRF、主导份额与引用投递观测。机制出处、赛题 4.1—4.7 对照、实验及尚未覆盖的能力见 [论文落地与赛题覆盖](docs/论文落地与赛题覆盖-v0.8.md)。

v0.7 把 Agent 提升为工作流内独立的管理对象：AgentControlBlock 拥有生命周期、旗下任务/在途PID、邮箱身份、上下文授权、并发槽和聚合资源预算。Linux 使用 Agent 父 cgroup → Attempt 子 cgroup 双层限制；支持暂停准入、恢复、终止，终止会回收执行并撤销权限，不能被动态任务重新启用。网页提供“Agent 生命周期”免费演示与所有新运行的 Agent 管理视图。接口、语义及实测见 [Agent 一等对象实现](docs/Agent一等对象-v0.7.md)。

v0.6 完成执行与控制面的再次审查：统一三种调度策略的决策/监控路径，解析祖先 cgroup、cpuset 及进程限额，持久保存最近运行索引；服务中断只恢复观测，不自动重跑模型。新版网页按协作流程、资源监控、任务拓扑和结果记录分视图，提供历史筛选、任务诊断、源码对照和记录导出。使用与本次验收见 [v0.6 审查与升级说明](docs/架构审查与界面升级-v0.6.md)。

v0.4 新增类似任务管理器的实时资源视图：整机与服务用量、实际/预留曲线、任务与 PID、等待原因、依赖图、Attempt 时间线，以及上下文缓存指标。新增老化资源准入、AIOS 启发的 LRU-2 JSON 缓存和 AutoGen 风格的限轮评审。使用方法、论文出处与实测边界见 docs/运行指南-v0.4.md。

v0.5 在网页增加“Python 项目修复”一键入口，支持金额计算、时间区间、文本规范化三个受控项目。原版失败、分析/测试建议、补丁、真实检查、独立评审与哈希核验均由 Runtime 任务执行。网页串行/并行统一总准入预算 2 核、1 GiB、128 pids（同时受服务上限约束），模型调用失败不自动重试。54 次交错真实实验完成 51 次接受、3 次失败；结果与限制见 [三项目重复实验](docs/网页修复与三项目重复实验-2026-11-11.md)。网页用法见 [v0.5 运行指南](docs/运行指南-v0.5.md)。

独立本机模型服务保管内存中的供应商 Key，网页升级不再要求重填；模型服务或主机重启仍会清除 Key。见 [内存模型服务](docs/内存模型服务与升级交接.md)。

现已增加真实模型驱动的受控 Python 金额项目修复：补丁 AST 限制、不可修改的公开/独立检查、测试 Worker 与独立最终评审。真实调用发现并修复了默认思考耗尽小输出预算造成空正文的问题。六种串并行/调度配置各一次均通过 25 项检查；完整过程、失败消耗、费用估算和样本局限见 docs/真实任务实验报告-2026-11-11.md，复查命令见 docs/真实项目运行指南.md。

## 运行

在本目录执行（Windows 可将 python3 换成 python）：

```bash
python3 -m agent_runtime
python3 -m agent_runtime --tasks examples/dynamic.json --workers 2 --output-dir runs
python3 -m unittest -v
python3 -m agent_runtime --tasks examples/ipc.json
python3 tools/run_document.py README.md
python3 -m agent_runtime.web --host 127.0.0.1 --port 18881
```

动态示例使用固定 JSON 规划，创建并行分析/测试准备任务；测试准备先失败一次，按新 Attempt 重试，最后汇总。它尚未调用真实模型。

## 模块和产物

- runtime.py：依赖校验、动态批次、状态、有限重试、资源准入和事件。
- executor.py：独立 Worker、启动许可、进程组清理和可选 cgroup 后端。
- context.py：工作流内不可变 JSON 对象、哈希去重和 API 读取授权。
- ipc.py：按工作流隔离的本机 HTTP mailbox、Attempt 临时凭据、请求去重、读取/确认和引用传递。
- model_gateway.py：集中模型调用、并发限流、供应商密钥隔离和调用用量记录。
- web.py：浏览器提交、状态、事件、取消和内存中的模型配置。
- project_workflow.py：三个固定项目的七阶段修复 DAG、实际测试与最终语义判定。
- model_broker.py：独立内存凭据服务、全局调用槽与付费提交去重。
- job_store.py：原子写入的有界运行索引与中断标记，仅恢复观测，不恢复执行。
- worker.py：echo/sleep/fail/command 动作。command 接收 argv 字符串数组，不启用 shell，仅允许受信任配置，不能直接执行模型生成命令。
- __main__.py：CLI，保留原有 run/validate 导入兼容。

每次执行创建 runs/<workflow_id>/，包含 events.jsonl、summary.json、contexts/<hash> 和 <task_id>-<attempt>/ 下的 result.json、worker.log。日志即时刷新，但没有崩溃续跑能力。

任务字段参照 examples/dynamic.json。资源向量为 cpu_milli、memory_bytes、pids；默认单任务 1000 millicpu、256 MiB、32 pids；默认总预算为 workers×1000 millicpu、1 GiB、128 pids，可通过 Python run(..., budget=...) 调整。超出总预算立即 unschedulable，前置失败则跳过后继。同一 Agent 的任务串行执行。

--policy fifo 保留队首阻塞基线；--policy resource_fit 选择预算能容纳的任务；--policy aging_fit 在就绪任务等待超过 2 秒时停止旁路填充，让在运行任务自然释放资源后优先启动老任务。它是非抢占准入策略，不会抢占供应商的模型推理。

## Linux 严格资源模式

普通模式事件标记 admission_only，只做预算准入，不能当成 OS CPU/内存限制。

严格模式需要管理员预配置独立委派的 attempts 子树：位于 /sys/fs/cgroup 内、无任务进程、已向下启用 cpu/memory/pids、运行用户有管理子级权限，支持 memory.swap.max 和 cgroup.kill。不要使用其他服务目录或默认以 root 运行所有 Worker。

```bash
python3 -m agent_runtime --tasks examples/dynamic.json --cgroup-root /sys/fs/cgroup/实际委派路径/attempts
AGENTRT_TEST_CGROUP=/sys/fs/cgroup/实际委派路径/attempts python3 -m unittest -v
```

创建 Attempt cgroup 后写入资源上限和 memory.swap.max=0；Worker 阻塞等待 stdin，归组核验后才开始任务；结束后 cgroup.kill、确认空组、记录统计并移除，再释放预算。缺能力拒绝启动，不静默降级。OOM 不按原配置重试。

未配置测试路径或不在 Linux 上时，实际 cgroup OOM 测试会跳过。Windows 通过不能替代 openEuler 验收。

## 实现边界

2026-11-10 实机验收：Windows 常规测试 11 项通过；openEuler 3.11.6 常规测试 11 项通过，随后单独启用的 cgroup OOM 测试通过。临时 systemd 服务配置 User=dev、Delegate=yes，Runtime 运行 UID=1000；CPU 200 millicpu 的一次 2 秒负载实测约 0.209 核，记录到 21 次节流，并检查任务组清理完成。原始摘要见 validation/2026-11-10-openeuler.json。该短时烟雾测试不等同于完整性能基准或所有故障边界验收。

- 已实现模型网关、无损 gzip 上下文压缩、内容哈希去重、在线 mailbox IPC、消息去重及取消 API。尚无 lease、心跳或崩溃恢复。动态任务在 Worker 成功返回后批量加入，不是完整在线 spawn_batch RPC。
- 上下文授权在 Runtime API 内检查，不是不同 OS 身份之间的安全沙箱。Worker 仍收到序列化内容，不是零拷贝或 KV Cache 共享。
- 普通 POSIX 进程组不能覆盖主动脱离会话的后代；Windows 对存活父进程使用 taskkill 清树，父进程先退出后不能保证清理脱离后代。严格模式通过 cgroup 管理后代。
- 目前没有日志/磁盘/结果体积的强制配额；UI 仅取日志尾部不代表磁盘已限额。
- 这是教学和开发起点。团队需实际审查和修改核心机制、完成目标系统验证，并记录 AI 辅助与真实贡献。

部署时先在独立目录测试，确认差异后整合入已有 Git 仓库，避免覆盖队友改动。设计依据为 docs/设计方案.md；机器历史实测见 NUC环境记录.md。


## 重放 Linux 委派验收

在重装后的 openEuler NUC 项目目录执行（当前 SSH 用户为 tzd；先 cd 到仓库，Python 路径需存在）：

```bash
sudo systemd-run --unit=agentrt-validation --wait --pipe --collect -p User=tzd -p Delegate=yes -p CPUQuota=200% -p MemoryMax=2G -p TasksMax=256 -p RuntimeMaxSec=240 /usr/bin/python3 "$PWD/tools/validate_linux.py"
```

管理员仅用于创建临时委派服务，测试进程以 tzd 运行。脚本在服务自己的 cgroup 子树配置 control/attempts，执行常规测试、实际 OOM/pids/后代回收/超时重试、文档应用、IPC 与 0.2 核 CPU 限制测试。服务结束后 systemd 回收子树。报告写入 reports/linux-validation-latest.json。历史 agent-rt-evidence 目录不修改。

## v0.3 使用与验证

NUC 的浏览器入口是 https://mc.tzddd.cn/agentrt/ ，使用当前网站的 Basic 认证账号密码，提供动态任务、通信、文档、单模型对话和四 Agent 模型协作五种场景。已部署的后台监听 NUC 回环 18880，要求来自反向代理的临时共享头；单纯 SSH 转发后直接打开不能替代该验证。本机开发可运行上述 18881 命令后打开 http://127.0.0.1:18881/ 。严格模式通过 User=tzd、Delegate=yes 的 systemd 服务添加 --delegated 运行。

配置模型时在网页的“模型配置”里输入密钥，或只在服务环境设置 DEEPSEEK_API_KEY；不要提交密钥到 Git。模型默认标识为 deepseek-flash，可用 DEEPSEEK_MODEL 覆盖。模型响应只作为文本和后续任务输入，不会直接执行。四 Agent 模板使用四次调用，分析和测试阶段可并行。自动测试使用本地模拟供应商，不能替代真实供应商验收。

上下文按原始 JSON 的 SHA256 命名，超过 1 KiB 且压缩有收益时以 gzip 保存；读取时校验哈希。重复对象只写一份，summary.json 给出逻辑写入量、唯一对象数和实际存储量。这是磁盘对象压缩和复用，不代表 KV Cache 复用或消除了 Worker 的输入拷贝。

消息通过工作流独立的回环 HTTP broker 交换不可变对象引用；每个 Attempt 的随机凭据在退出后撤销。邮箱有容量上限，send 的 request_id 在工作流内去重，重复 ID 携带不同内容会被拒绝。receive/ack 是内存中的至少一次交付：未确认可重读，控制进程崩溃不恢复；重复业务执行需要调用方按 request_id 处理。相同 OS 用户下仍不构成恶意代码安全沙箱。

`python3 tools/benchmark.py --repeats 3` 比较 FIFO 和 resource_fit 在特定队首阻塞场景下的响应时间与总耗时，逐次原始数据写入 reports/benchmark.json。该实验使用 sleep Worker，不是通用性能结论。需要严格资源实验时在已委派服务内传入 --cgroup-root。

当前统一映射是 Workflow → Agent → Task → Attempt → Worker PID → cgroup。事件带 workflow_id、agent、task_id、attempt、PID、资源请求和终态；同一 Agent 串行，不同 Agent 按依赖和总预算并发。尚未实现多主机、持久恢复、内核修改或容器级安全隔离。
