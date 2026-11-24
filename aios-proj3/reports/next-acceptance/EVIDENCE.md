# 验收证据索引

全部日志来自本独立候选版的实际运行；没有把固定模型结果写成真实云端结果。

| 文件 | 内容 |
|---|---|
| local-regression.log | 本地99项：1项Linux跳过 |
| nuc-regression.log | NUC99/99、0跳过 |
| nuc-order-cgroup.json / nuc-completed.json | 新订单测试进程的真实PID/cgroup与回收；实机源码摘要 |
| NUC_SOURCE_BINDING.json | 当时实机测试快照各文件摘要与本版逐项核对 |
| browser-results.json | 6条浏览器核心检查，无页面JS异常 |
| order-completed.png / chat-formats.png | 控制台工程结果和聊天复杂格式截图 |
| download-unittest.log | 浏览器下载ZIP独立运行13项测试 |
| real-comparison.json | DeepSeek Flash两对交错、真实usage、耗时与资源回执 |
| fixed-comparison.json | 固定响应3对免费框架对照 |
| derived-metrics.json | 从真实记录推导的模型等待之外耗时及计算口径 |
| prior-failures/ | 前两轮真实对照失败/中间结果，含公开合成订单项目模型补丁 |
| prior-browser-callable-failure.log | 修复前可调用对象JSON序列化错误记录 |

这些证据含公开合成项目、临时目录和实验PID；不含供应商密钥、实际客户资料或真实业务代码。历史失败记录没有被成功结果覆盖。
