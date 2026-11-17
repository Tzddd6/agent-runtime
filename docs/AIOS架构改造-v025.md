# AIOS 架构改造 v0.25

参考仓库为 [agiresearch/AIOS](https://github.com/agiresearch/AIOS)，固定提交
`292d98f78c835499df18cdb6de6325bba1895862`。本实现借鉴系统调用、分类队列、管理器与客户端分离的结构，代码独立编写。此提交的 LICENSE 仅含空白，没有据此复制上游源码或引入其代码依赖。

## 实际运行结构

```text
浏览器 / CLI / 类型化 SDK
        KernelCoordinator
     分类就绪队列与 Syscall 元数据
        AdmissionScheduler
    Agent 和工作流的统一资源准入
        Executor / cgroup v2
          新 Worker
 LLM / Tool / Memory / Storage / Context Manager
      原有模型网关、授权 IPC 与工作区工具
```

所有现有 `run()` 工作流都会经过该链路，网页、模型对话、动态子任务、工程修复也一样。没有把真实工作留在旧链路、只给测试增加适配器。为节省协调成本，分类队列共享一个协调器，没有给每个调用新增一套调度线程。

## 与 AIOS 的对应

| AIOS 参考结构 | 本项目实际实现 |
| --- | --- |
| `aios/syscall/` | `kernel/syscall.py` 的调用身份、状态、时间和服务类型 |
| `aios/scheduler/base.py` 与 FIFO/RR | `kernel/coordinator.py` 分类准入队列，复用资源适配、老化和 DRF |
| LLM / memory / storage / tool manager | `kernel/managers.py`，真实 Worker 按服务分派 |
| Cerebrum 请求接口 | `sdk.py` 的 LLMQuery、ToolQuery、MemoryQuery、StorageQuery |
| 记忆写入与读取顺序 | SDK 批次接受顺序编译为依赖，失败写入阻止后续读取/模型执行 |
| Kernel HTTP 与监控 | 现有认证网页服务，新增内核演示和真实调用表 |

该 SDK 是本项目的接口，不是 Cerebrum 协议的兼容替代品。

## 调度与调用状态

服务包括 `llm`、`tool`、`memory`、`storage`、`context`、`process`、`runtime`。分类由真实 action 和注册工具推导，忽略载荷中伪造的 service。调用 ID 绑定工作流、任务和 Attempt。

`kernel_queue_policy='fifo'` 保持原准入顺序；`round_robin` 在分类就绪队列间轮转。全局 FIFO 仍保留队首语义，老化/DRF 的排空与预算检查仍由 AdmissionScheduler 负责。这里没有抢占供应商 GPU 推理，也没有把一次未完成模型生成切片重发。

`events.jsonl` 记录 syscall 身份；`summary.json` 与监控快照包含分类队列、调用状态、真实 PID、等待/执行耗时。页面只展示最近 100 条，完整事件仍在运行目录。元数据不包含提示词、正文、临时凭据或 API Key。

## 类型化 SDK

```python
from agent_runtime.sdk import KernelClient, Request, ToolQuery, StorageQuery

client = KernelClient('runs/sdk', user_identity='trusted-user')
result = client.batch([
    Request('calculate', 'analyst', ToolQuery('math.calculate', {'expression': '(12+3)*2'})),
    Request('save', 'writer', StorageQuery('write', {'path': 'note.txt', 'content': 'example'})),
    Request('read', 'reader', StorageQuery('read', {'path': 'note.txt'}), depends_on=('save',)),
])
```

模型通过受信任宿主传入 `model_client`，记忆通过 `memory_repository` 传入。密钥不放在请求中。SDK 将不同可信用户映射到不同工作区目录，并拒绝错误用户的记忆库。`user_identity` 来自宿主认证，不能相信模型或浏览器自行声明身份。同一 Unix 用户仍不是恶意代码强隔离边界。

同一批次中，后接受的模型请求/记忆读取会依赖此前接受的记忆写入或删除。依赖只传递顺序，不传递其他 Agent 的私有内容；原记忆 ACL 仍会检查。写失败则后续请求跳过，预算不会消耗。显式依赖与该顺序冲突时，在调用模型前拒绝有环任务图。

## 网页与运行

在独立运行控制台选择“AIOS 架构 · 内核服务免费验收”。该场景执行文件写入/读取、记忆读取、授权上下文检索、确定性计算及本地汇总，不调用供应商模型，也不写长期记忆。在资源、任务或日志页查看“内核服务与系统调用”。

```bash
python3 examples/kernel-sdk.py
python3 -m agent_runtime --tasks examples/dynamic.json --kernel-queues round_robin
python3 -m unittest test_kernel test_kernel_web -v
```

严格资源与故障验收必须在已委派的 Linux cgroup v2 中执行。持久恢复沿用原检查点语义：保留确认结果与预算，不重放不确定模型、命令或写入；旧资源未回收则停止新准入。恢复时从已确认事件重建调用记录。

## 本次范围与保留边界

完成的是实际执行链的内核服务划分、类型化客户端、分类准入、调用观测和批次记忆顺序。保留 cgroup、访问授权、预算、工程判分与模型持钥服务。没有迁入 AIOS 的整个依赖树、Agent 商店、任意插件自动安装、Redis 文件回滚或云 GPU KV Cache 管理。本次不把之前尚未完成验收的 clone3 候选混入此版本。

这是架构改造，不是新的模型能力分数。是否更快必须固定回复、工具和资源预算重新对照，不能仅凭结构相似或测试通过推断性能优势。
