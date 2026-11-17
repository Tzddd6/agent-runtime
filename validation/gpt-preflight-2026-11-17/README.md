# GPT 接入预验收：网络阻塞，未产生真实对照成绩

2026-11-17，通过已有 RAM 持钥服务绑定 gpt-4.1-mini，不读取或导出 API Key，不改变网页的当前 DeepSeek 选择。用户配置原模型字段为泛称，已仅更正为具体模型 ID，沿用原有内存密钥。

一次真实调用（最多128输出token）在约30.017秒后超时；立即停止，无自动重试，未取得 usage，远端计费未知。另一次无密钥检查显示NUC DNS正常、官方HTTPS连接超时；当前电脑能校验证书并取得无认证401。尝试临时SSH回环转发被现有策略拒绝，未改SSH安全设置、未建立通道、未生成网络配置。

不能据此评价GPT能力，不能报告Runtime/direct真实速度、token或完成质量差异，也不能假定费用为零。

## 已完成的免费准备

`tools/benchmark_provider_paired.py` 默认离线，无供应商请求。预验收包含结构化回答、退款表达式修复、固定工具查询两轮协议。两轮交错顺序，每臂6个任务、8次调用，共16次固定夹具调用；两臂请求摘要一致，独立判分通过。夹具的token和时间不能冒充真实API成绩。

真实运行必须显式 `--live --profile <id>`，仅允许已配置的官方OpenAI gpt-4.1-mini，固定256输出token、2轮、最多16调用；调用前将保守费用预留写盘并fsync，美元上限0.05，失败保留预留、不自动重放，不覆盖旧账本。价格来源 https://developers.openai.com/api/docs/models/gpt-4.1-mini ，输入0.40美元/百万token、输出1.60美元/百万token，不按缓存折扣估算。费用仅为目录价估算，实际扣费以供应商为准，先前超时请求须单独核对。

`direct` 和 `runtime` 共用同一RAM broker、模型版本和网络路径，只比较Runtime相对共同安全传输层的开销；不是裸HTTP对比。工具消息统一规范化，两个阶段的输入一致。修复表达式只使用受限AST解释器，对45组边界输入验证，不执行任意模型代码；它是微型修复契约，并非完整项目修复验收。

## 网络恢复后的复现

为NUC配置可达OpenAI官方接口的受信出口后，在现有授权限额服务内注入私有控制token和代理地址（不要把供应商密钥写进命令行）：

```bash
python tools/benchmark_provider_paired.py --output reports/gpt-new-run --live --profile <profile-id>
```

输出路径必须不存在；不要重跑或覆盖失败的付费运行。免费脚本测试：

```bash
python -m unittest -q test_model_network test_provider_benchmark
python tools/benchmark_provider_paired.py --output reports/gpt-offline-new
```

可选 `model_network.py` 只接受持有人私有目录中的0600普通文件，固定回环18889，最长一小时有效；仅影响官方OpenAI TLS传输，不改变API原点、证书校验、模型或重试。当前未启用，也未部署出站代理。跨供应商、任意代理地址、符号链接、公开权限和过期配置均被拒绝。
