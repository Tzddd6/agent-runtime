# Agent 一等对象：v0.7 实现与验收

Agent 是工作流内独立注册、可寻址、可控制、拥有资源预算的 Runtime 对象。Task 必须属于一个 Agent，Attempt 使用其预算执行。本实现建立用户态 Runtime 的 Agent 管理边界，未修改 Linux 内核；Agent 身份和执行状态暂不跨工作流共享。

## 控制块与状态

`agents.py` 提供 AgentControlBlock、AgentRegistry、RuntimeControl。控制块保存 id、role、state/mode、CPU/内存/pids 预算、预留账本、并发上限、旗下任务、活跃任务PID、上下文引用、邮箱身份、聚合用量和已记录的模型调用/token用量。缺少测量时不伪造为0。

注册后进入 ready；有执行进入 running，没有当前待办则 idle，工作流结束进入 completed/failed。暂停命令将准入关闭：有在途任务时是 pausing，待其结束为 paused；resume 重新开放准入。这里不使用 cgroup.freeze 暂停云端模型请求，也不声称暂停立即停止在途计算。

terminate 进入 terminating，回收该 Agent 的全部在途 Attempt，取消待执行任务，撤销邮箱凭据与上下文授权，再进入 terminated。已完成的任务结果保留。以后动态生成的同名 Agent 任务不能使它复活。其他 Agent 的独立任务继续执行；依赖被取消任务的后继仍按 DAG 规则跳过。

网页命令写入容量64的控制队列，由单协调器应用，返回queued不等于已完成；页面显示applied/rejected及实际状态。历史运行不能控制，也不会自动恢复执行。父Agent及子Attempt资源只能在确认回收后释放；清理失败仍进入原有recovery_required隔离流程。

## 两层资源

```text
工作流总预算
  ├─ Agent alpha cgroup：聚合 cpu.max / memory.max / memory.swap.max=0 / pids.max
  │    ├─ Task a1 / Attempt 1 cgroup → Worker及工具后代
  │    └─ Task a2 / Attempt 1 cgroup → Worker及工具后代
  └─ Agent beta cgroup → 其旗下Attempt
```

准入同时检查全局预算、Agent预算、Agent并发槽和全局Worker槽。单任务超过所属Agent预算会直接unschedulable，不启动模型。未显式配置时按任务身份自动注册，默认预算继承工作流、同一Agent默认并发1，保留旧行为。CPU预算是速率上限，内存是硬上限，不是最低性能保证或预分配。

Agent父cgroup在多个Task/Attempt之间持续存在，记录真实聚合CPU时间和内存峰值，工作流结束后确认空组并删除。上层服务/祖先cgroup仍可能有更紧约束。非Linux严格模式仅做准入账本；cgroup不提供文件和网络安全沙箱。

## 模型、消息与权限

每个模型Attempt取得独立临时网关凭据和取消事件。清理Attempt会撤销凭据；终止一个Agent只取消它的请求，不关闭整个工作流的模型网关。真实DeepSeekClient和BrokerClient按请求复制取消上下文，仍共享全局调用槽。供应商Key继续仅在独立模型服务内存中，不传给Agent。撤销本地请求不保证供应商立即停止计算或计费，丢失响应的调用用量可能未知。

终止会撤销Agent全部邮箱凭据并拒绝对其新投递，移除其上下文读取授权。已被其他Agent授权读取的结果不追溯删除。身份在工作流中稳定，不等于独立Unix用户隔离。

## 使用

网页：https://mc.tzddd.cn/agentrt/ → 选择“Agent 生命周期” → 启动 → “Agent 管理”。Alpha有2个并发槽、Beta有1个，二者各有500m CPU、192MiB内存、32pids预算；演示不调用模型。暂停Alpha后等待其在途任务结束，可见Beta继续；恢复Alpha后可终止其新任务。此时工作流总状态为cancelled是主动终止的结果，应分别检查两个Agent的终态。

CLI配置示例（普通模式只做准入，严格模式另传管理员已委派的--cgroup-root）：

```bash
python3 -m agent_runtime --tasks examples/agent_tasks.json --agents examples/agent_specs.json --workers 3
```

Python：`run(tasks, agents=[{"id":"alpha","max_concurrency":2,"budget":{...}}], control=control)`；控制对象为 `from agent_runtime.agents import RuntimeControl`。在执行线程运行期间，调用 `control.request("alpha","pause"/"resume"/"terminate")`，通过观察回调或 `control.history()`查看确认。Agent注册尚未完成或工作流已结束时拒绝控制。

HTTP：`POST /api/jobs/<job>/agents/<agent>/<pause|resume|terminate>`，正文`{}`，沿用代理认证、CSRF和Origin检查。任务事件仍在events.jsonl；Agent状态转移单独写agents.jsonl，并在summary.json和网页快照中保存Agent汇总。

## 验收

常规145项：137通过、8项按环境跳过；随后在NUC单独运行7项严格验收，全部通过、无残留cgroup。新增严格用例让同一Agent的两个Worker同时运行，在Agent父cgroup中测量聚合CPU/内存并确认父子层级回收；既有CPU/OOM/pids/后代清理/超时用例仍通过。

离线用例覆盖暂停排空与恢复、独立Agent继续执行、终止回收及禁止动态复活、Agent预算与并发槽分离、上下文和邮箱撤权、网页认证及历史控制拒绝、单请求模型撤销不影响其他请求，以及Agent终态日志写入异常时仍关闭模型网关。模型相关用例使用本机模拟供应商，不产生费用。

网页实机记录：Alpha两个初始任务完成后保持paused，Beta继续；resume后Alpha第三项运行，terminate将它取消；最终Alpha terminated、Beta completed，三条指令均applied，所有Agent预留归零，无残留cgroup、无模型调用。见 [online.json](../validation/agents-2026-11-14/online.json) 和 [regression.json](../validation/agents-2026-11-14/regression.json)。

当前代码目录 `/home/tzd/agent-runtime-v07-20261114`，网页和产物地址沿用原部署；模型服务进程未重启、Key仍配置。Agent快照恢复仅供历史查看，不会重建进程或续跑任务。本轮不声称已实现跨工作流常驻Agent、Agent优先级公平调度、持久邮箱恢复或供应商费用硬配额。
