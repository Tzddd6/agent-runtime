# τ-bench 自定义 Runtime 评测协议

官方来源：sierra-research/tau2-bench，`v1.0.1` 固定提交 `fc0055dc4e0a316c3f83133267fbd6faaa770992`。源码归档安装在独立 Python 3.12 环境。官方任务、领域工具、策略、状态更新与 ALL 判分器保持字节一致；运行前核验 1155 个相关文件 SHA256。历史模型答案轨迹不装入评测目录。

本轮正式范围：航空 base 全部 50 题，每题一次，Runtime 与直接调用各一组。前两题预验收是独立调试记录，不混入完整域结果。只报告 pass^1，不推算 pass^2/3/4，不把单域说成完整四域。官方推荐四次以上重复，本轮预算与调用额度有限，必须注明单次采样的局限。

## 保持什么，改变什么

- 保持官方 Agent 和模拟用户的提示词、状态/消息转换、工具 schemas、工具执行、官方终止条件与判分程序。原生 function call、tool_call_id 和 tool 结果不转成额外提示词。
- 工具仍由官方环境在协调进程中执行。Runtime 管理 Agent 每次模型推理的独立 Attempt、模型网关、授权、上下文引用和资源限制。不能把该实验描述为“所有官方工具各有独立 Runtime Attempt”。
- Agent 和模拟用户均通过同一受保护服务调用 `deepseek-flash`，temperature=0、最大输出 2048 token。模拟用户替代官方默认 GPT-4.1，因此提交声明 `custom`，不能冒充默认模拟用户成绩。
- 对照组保持上述模型/工具/消息协议、seed、输出额度和任务不变，仅省去 Agent 推理的 Runtime Attempt。模拟用户使用相同调用路径。模型采样与远端延迟仍可能波动，不能将所有结果差异归因于框架。
- 不提供任务的预期动作、奖励或隐藏答案给 Agent。每个任务从官方初始环境重建，无跨任务记忆。不加入额外模型评审、幻觉重试或不确定请求重放。
- 航空任务使用官方 DB/COMMUNICATE 规则。零售 112 题和银行知识 1 题要求官方 GPT-4.1 NL 判分；没有该模型凭据时拒绝启动这些完整域，不擅自改用 DeepSeek 判分。

## 预算与计费

总预算 10 元，由所有预验收、正式运行和对照组共享一个私有、原子写入并加文件锁的账本，不因重开进程重置。

2026-11-17 [官方人民币单价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)：Flash 高峰每百万 token，缓存未命中输入 2 元、命中输入 0.04 元、输出 8 元；空闲时段减半。预算全部按高峰价计算。实际账单以提供商为准，不把保守计价写成精确扣款。

请求前按模型 1M 上下文上限（保守取 1,048,576 token）与本次输出额度预留最坏费用，获得实际 usage 后结算；若请求失败且计费未知，预留保留。预算不足时拒绝下一次调用，而不是先扣费后检查。这里只限制本次 benchmark，不控制用户在其他网页的独立请求。

独立 cgroup 限制协调程序与 Worker 为 2 CPU、2 GiB，交换禁用。保存 CPU 时间、进程峰值 RSS 与 Runtime Attempt usage；远端模型推理资源及独立持钥服务不纳入本地资源比较。没有租用新服务器。

## 运行与发布

```bash
# 在安装官方依赖的隔离环境运行。没有 --run 只生成计划，不发模型请求。
python tools/benchmark_tau.py --official-root /path/to/tau2-v101 \
  --domain airline --arm runtime --output reports/airline-runtime

# 付费运行需显式指定同一个私有共享账本；不要将密钥写入命令行：
python tools/benchmark_tau.py --official-root /path/to/tau2-v101 \
  --domain airline --arm runtime --trials 1 --output reports/airline-runtime \
  --cny-limit 10 --max-calls 3000 --budget-ledger /private/tau-budget.json --run
```

由私有启动器注入 localhost 代理控制配置，供应商密钥继续只在原有 RAM 持钥服务及受信单次子进程中使用。原生工具协议扩展不重启持钥进程，不导出密钥。

公开材料只选取审核过的源码、配置说明、official results.json、脱敏用量汇总及协议。不要公开 Runtime Attempt 的 payload、控制 token、私有环境文件或整份 reports 目录。用户的私人联系邮箱不写进公开文件；公开联系人使用 GitHub 账号，是否被维护者接受以审核结果为准。

完成全部题目不代表已上榜。需要官方格式和轨迹校验通过、提交审核并被维护者接收，才可能出现公开名次。任何中断、预算停止或未完成任务都标为未完成，不能上传成完整成绩。
