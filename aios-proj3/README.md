# Agent Runtime / AIOS Proj3 — v2.1 开发候选版

面向多智能体的常驻运行时。参考 [agiresearch/AIOS](https://github.com/agiresearch/AIOS) 的分层思路，将模型、工具、记忆与存储请求接入统一调度，并通过 Linux cgroup v2 管理真实 Worker 进程。此项目为参赛实现，不是 AIOS 官方发行版。

本目录是独立开发副本；保留原 v2.0 冻结包，不覆盖其他开发者工作区。当前验收日期：2026-11-24。

## 先体验

需要 Python 3.11+。完整图片/PDF附件功能及对应测试需要额外依赖：

```bash
python -m pip install pymupdf pillow
python -m unittest discover -s tests -v
python -m runtime.web --port 18892
```

浏览器打开 **http://127.0.0.1:18892/** 对话页，或 **http://127.0.0.1:18892/console/** 运行控制台。服务固定监听本机；关闭终端后页面不可用。外网访问需要自行配置认证与反向代理。

工程演示：在控制台选择工程修复与 `order_project`，配置有效模型后运行，查看原始失败、补丁、真实测试、最终评审与下载。未配置模型不会自动伪装成真实模型修复；离线浏览器验收使用显式固定响应夹具。

## 当前能做什么

| 层次 | 实现与入口 |
|---|---|
| 应用/SDK | Local/Remote 查询客户端、对话与独立控制台；`cerebrum/`、`runtime/` |
| 内核调度 | LLM/Memory/Storage/Tool 四类队列、DAG依赖和动态任务、资源准入与Agent生命周期；`aios/scheduler/` |
| 资源与容错 | 独立Worker、Agent/Attempt cgroup、CPU/内存/PIDs限制、超时与回收、检查点；`aios/os_kernel/` |
| 上下文与通信 | 内容寻址复用、压缩与预算视图、ACL、引用持有租约、Mailbox；`aios/context/`、`aios/ipc/` |
| 工程修复 | 多文件订单项目，最多两轮补丁，13项评测方测试，实际测试通过后才能下载；`runtime/engineering_workflow.py` |
| 可观测性 | DAG/角色状态、真实PID/cgroup回执、Token使用与预算、上下文引用、失败阶段 |

订单项目目前是固定受控场景，补丁路径与语法受限，不能视作任意不可信代码的通用安全沙箱。五种角色中包含规则执行角色，不代表五次模型调用。

## 已验证结果

详见 [本轮验收报告](docs/v2.1候选版验收报告-20261124.md) 与 [证据目录](reports/next-acceptance/EVIDENCE.md)。

| 检查 | 结果 |
|---|---|
| 本地回归 | 99项：98通过，1项Linux专用检查跳过 |
| NUC/openEuler回归 | 99/99通过，0跳过；订单测试子进程实际位于cgroup，结束后无残留 |
| 浏览器核心流程 | 6/6通过；两轮修复、下载独立复测、刷新、零预算、异常与有效附件、复杂格式流式显示 |
| 真实同条件对照 | DeepSeek Flash，两对交错运行，Runtime与直接编排均2/2完成，均通过13项测试 |
| 实际Token | 两组各3338（输入2864，输出474），没有证明节省Token |
| 冷启动端到端耗时中位数 | Runtime 5.677秒，直接编排3.034秒；本轮Runtime更慢 |

直接编排基线复用相同底层Worker和测试器，绕过Kernel/Syscall/DAG；双方提示词哈希一致、温度0、相同模型和输出上限。仅两个真实样本对，不能推断普遍优势、官方排名或稳定的成功率。

固定响应的免费框架对照：

```bash
python tools/compare_order_runtime.py --pairs 3 --output reports/local-comparison
```

真实模型模式依赖既有持钥服务和累计费用账本，默认命令不会付费。不要将密钥写入源码、命令参数或报告。Linux硬限制需在启用cpu/memory/pids控制器的委派环境中验证；未委派环境不能据页面宣称硬隔离。

## 文档与演示

- [AI工具使用说明](docs/AI工具使用说明.md)：工具与已知/未知版本、真实交互示例、使用环节。
- [研发过程与问题解决](docs/研发过程与问题解决.md)、[类似项目对比](docs/类似项目对比.md)。
- [赛题评分与证据索引](docs/赛题评分与证据索引.md)、[录屏与现场演示脚本](docs/录屏与现场演示.md)。
- 演示视频百度网盘链接：**待团队实际录制、上传后填写**。提取码：**待填写**。

## 能力边界与许可

桌面控制 `LiteCUA/VMController` 仍含模拟实现；历史“5套公开任务子集+1套合成压测”不是官方榜单；尚未证明所有场景比直接调用更快。上下文文本去重不等于远端模型KV Cache共享，流式时间片也不能视作远端推理服务的硬件抢占。

自研部分按 [MIT](LICENSE) 授权；第三方文件保留各自许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。使用AI辅助的范围与待团队确认事项有明确记录，不把自动测试通过等同于全量人工审计或保证没有缺陷。
