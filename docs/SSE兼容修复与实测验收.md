# SSE 兼容修复与新一轮验证（2026-11-17）

已修复的确定缺陷：Responses 与 Chat Completions 原来逐行解 JSON，无法处理标准 SSE 的多行 data 事件。本版共用有大小上限的 UTF-8 SSE 解析器，支持 BOM、LF/CRLF/CR、注释、分割的中文编码；必须收到完整事件和供应商终态才认可完成。公式、代码和表格正文不做替换。官方协议说明：https://html.spec.whatwg.org/multipage/server-sent-events.html#event-stream-interpretation 。

HTTP 400 不再丢失全部诊断信息：只记录白名单数值类别（error_kind、error_param），不保存错误正文。标准顶层 error 事件也能正确分类，且 MODEL_UPSTREAM_FAILED 穿过网关保持一致。未知错误仍为类别0，不猜测业务原因。过去的 HTTP 400 和约29.7秒中断未保留原始业务原因，不能倒推为本次 SSE 缺陷。

真实同模型对照：固定中转返回的 gpt-6-luna 标识、同一持钥服务、相同请求哈希及预算，两次交错顺序，共每组6题/8次调用。直接调用6/6、150.417秒、输入736/输出330 token；Runtime6/6、110.408秒、输入736/输出339 token。两组均无 usage 缺失。本次观察到耗时少26.6%，但 token 多0.84%；上游等待显著波动且样本量小，不能声称框架因果提速或节省 token。去除 broker 等待后的本地每次耗时约直接0.0686秒、Runtime0.1229秒，框架仍有额外开销。底层模型身份未独立核实。

真实流式探针覆盖混合格式、LaTeX 公式、Python/JSON代码块、40行表格，4/4通过，累计流式正文与最终正文逐字一致。本轮没有复现先前HTTP400和间歇性中断，不代表它们永不再现。旧失败记录保留在 validation/transport-live-2026-11-17。

免费验收包括有委派 cgroup 的最终完整回归398项（397通过、1跳过），包含真实实验浏览器验收；跨协议专项22项通过；本地Markdown DOM回归10项及断网控制回归1项通过。唯一跳过项为NUC缺失Node的控制测试，已在本机Node环境补测通过。官方AIOS固定访问矩阵通过26项，本地边界8项通过；这是适配合同测试，不是完整AIOS能力排名，实际生产隔离另由主链路回归验证。

本轮20次付费调用（16次对照+4次格式），不自动重放失败；标称预留合计0.0054887美元，实际中转账单尚未核验，不能承诺按次计费的最终金额。测试不读取供应商密钥，密钥只由现有RAM服务持有。阶段日志启用标记测试结束删除。

复现：在委派非root Linux服务内运行完整 unittest；固定配置后运行 tools/benchmark_provider_paired.py --live --aizex --profile <profile-id> --limit-usd 0.007 --output <新目录>；真实格式验证运行 tools/validate_stream_formats.py --live --output <新目录>，内部标称上限0.003美元、最多4次。费用应先按实际供应商规则核对。

官方 τ-bench airline 50条历史轨迹分别在 Runtime/直接链路回放，50/50请求、对话与奖励一致，保留原39/50成功，不是新模型成绩。无外网的网络命名空间内运行，供应商请求0。回放耗时Runtime66.274秒、直接38.835秒，框架仍有额外开销；顺序分组且不是统计显著性实验。官方新排名未确认。
