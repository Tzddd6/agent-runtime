# 多 API 配置与执行优化

模型配置入口在运行控制台的“模型与 API”。最多保留八项配置（包含原有 DeepSeek 服务），支持同一供应商的多把密钥与不同模型。当前支持 DeepSeek 官方 API 和 OpenAI 官方 Chat Completions API；模型名称填写自己的 API 项目可访问的名称，不接收任意自定义地址。

添加配置后，密钥输入框清空，页面只显示名称、供应商、模型和是否已保存。保存和选择配置都不执行模型测试，不自动切换或在失败时尝试另一供应商。手动“设为当前”后，新工作流固定绑定配置 ID 与版本；配置发生变化时，旧版本在下一次付费前拒绝继续，而不是默默换模型。运行期间，所有用户共享的配置修改均被阻止。

密钥只保存在独立持钥服务的内存中，不返回浏览器、不写入配置文件、SQLite、工作流载荷或 Git。模型服务重启后需要重新填写。新多 API 服务位于固定回环端口 18887，原有 DeepSeek 持钥服务继续在 18886；添加 OpenAI 不要求读取、迁移或重启原有持钥进程。原有服务故障不会触发自动向 GPT 发出请求。

OpenAI 请求使用官方 `https://api.openai.com/v1/chat/completions`、`max_completion_tokens` 与 `store: false`；不会发送 DeepSeek 的 `thinking` 参数。文本、标准化图片和 SSE 文本分块沿用同一条 Runtime 链路；具体模型必须支持所用参数和输入类型。供应商错误与不完整流式输出不会被当成成功，也不会自动重放不确定的付费请求。`store: false` 不代表供应商绝不保留数据，数据处理以供应商政策为准。

参考：[OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[流式事件](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)。

## 启动示例

私有环境文件仅保存本机控制 token 和回环地址，不保存供应商密钥。保留旧持钥服务；用相同控制 token 启动新服务，并让新版网页通过 `AGENTRT_MODEL_BROKER_URL=http://127.0.0.1:18887` 连接它：

```bash
python -m agent_runtime.model_broker --port 18887
```

不要把密钥写进命令行或示例文档。实际部署应继续使用受认证的 HTTPS 入口、非 root systemd 服务和资源限制。配置接口仅主工作区可用，并检查代理身份、CSRF、全局工作流忙碌与恢复状态。配置审计只记录操作种类，不保存密钥、请求体或私人标签。

## 进程复用与质量验证

`WorkerSession` 显式管理一组串行工作流共享的无凭据预加载器，每个 Attempt 仍创建全新的 Worker 和 cgroup；预加载器不读取 Agent 载荷。会话拒绝并发租用，路径限定在调用方声明的根目录。发生启动失败时不重放该 Attempt；资源回收后，后续新工作流才可重建预加载器。OOM、超时及预加载器在模型请求中死亡均有独立测试。

```python
from agent_runtime.runtime import run
from agent_runtime.worker_session import WorkerSession

with WorkerSession(private_run_directory) as session:
    states, events = run(tasks, output_dir=private_run_directory,
                        worker_session=session, model_client=bound_client,
                        cgroup_root=delegated_attempts)
```

纯模型任务没有共享上下文时，不再建立空 IPC 服务或读取空上下文视图。非空上下文仍使用原来的授权与预算检查；本地 RPC 不经过系统代理或跳转。

实际 τ-bench 可通过 `--reuse-workers` 开关在每题内部复用预加载器；官方任务、消息和工具不变。原来的人民币评测预算只能用于已绑定的 DeepSeek Flash，遇到其他供应商或模型会在调用前拒绝，不能套用 DeepSeek 单价统计 GPT 费用。

固定模型回放只能证明框架没有改变请求、回答与判分，并测量本地开销；它不代表新的模型能力成绩。原始真实运行的 39/50 与直接调用 41/50 保留不变。官方草稿 PR 为 [#763](https://github.com/sierra-research/tau2-bench/pull/763)，尚未成为公开排名。
