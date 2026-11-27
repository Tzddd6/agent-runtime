# AIOS Proj3 · 多智能体操作系统级运行时

面向 openEuler 的常驻内核、四类系统调用队列与资源隔离执行环境。R6.2.1 是发布文档与交付校验修订，运行时 Python 代码保持 R6 的版本。

![系统架构](docs/aios_architecture.svg)

## 已完成

- 常驻 Kernel 与本地/远程 SDK；LLM、工具、记忆、存储四路请求调度。
- Agent 生命周期、DRF 资源准入、Agent/Attempt 双层 cgroup v2 CPU/内存/PIDs 限额。
- AF_UNIX 常驻 Worker、故障隔离与恢复；不盲目重放不确定的写操作或计费请求。
- 上下文内容寻址、压缩、ACL、引用通信与持久存储。
- 白色对话网页与独立控制台：队列、DAG、记忆图谱、资源、模型配置。
- 多文件订单项目修复：原始失败 → 补丁 → 真实测试 → 下载含公开测试的项目。

## 运行

Python 3.11+；完整 PDF/图片附件功能及相关测试需安装可选依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install pymupdf pillow
python -m runtime.web --port 8765
```

打开 http://127.0.0.1:8765/ （对话）或 http://127.0.0.1:8765/console/ （控制台）。服务固定绑定本地地址；远程访问可用 SSH 本地端口转发。在网页“模型与 API”保存并选择配置；供应商密钥仅由独立模型服务进程内存持有，重启后重新输入。不要把密钥写进源码、日志或截图。

```bash
python -B -m unittest discover -s tests -v
python tools/doctor.py
```

Linux 硬隔离测试需要 cgroup v2 委派，普通 Windows/未委派会话的跳过不等于实机验收。交付 ZIP 的 `02_acceptance/FINAL_CHECK.sh` 提供委派验收入口，日志写在包外。

## 工作流预算与保留

工程修复的角色配额按工作流分配，内核全局模型预算单独计量。模型服务的单次请求超时只结束该请求的等待，模型配置继续保留在内存。已发出的上游调用可能仍在结束过程中，运行时会丢弃迟到响应。

工作流检查点按状态变更增量写入 SQLite，每 128 次变更和任务结束时生成快照。旧版 JSON 检查点仍可读取。运行中的检查点需要先核对未确认操作才能恢复。

工作台默认保留最近 20 条未标记保留的任务。可在任务结束后选择「保留记录」，让记录、交付 ZIP、检查点和工作流上下文一并保留。淘汰时仅释放该工作流的上下文租约，共享内容仍由其他任务持有。旧版中无法归属到单一工作流的上下文、独立的会话历史与 LSFS 文件按各自的存储规则保留。

## 可验证的断点恢复

在工作台点击「断点恢复」，会选好 `python-json-pointer v3.0.0` 和离线补丁桩。点击「运行」开始演示。首次验证 Worker 创建并完成隔离归组后，运行时会在放行测试前终止并回收该进程。工作流进入 `interrupted`，点击「从原补丁恢复」继续验证、评审与交付。重复提交恢复请求会被拦截。

恢复时核对输入版本、已完成节点的上下文哈希、原模型响应及补丁哈希。恢复保留相同工作流 ID 与补丁引用，验证节点的尝试次数递增。下载包附带 `execution_evidence.json`，含输入版本、执行步骤、模型响应与工具结果哈希、两次验证 Worker 的回收凭据及验证源码哈希。原始模型响应由工作流上下文保留，完整 ZIP 哈希可在工作流详情中核对。交付写入结果未知或恢复凭据失配时，工作流进入 `needs_reconciliation` 等待核对。

## 离线调度对照

控制台中的「资源压力」和「文本统计」支持同时运行两项，每个工作流使用独立的 Agent 标识和上下文。空闲时选择「单工作流调度对照」，即可查看每次试验的短任务等待、长任务完成时间、主导份额和最终占用，并下载原始 JSON。命令行可复跑同一实验。

```bash
python -m runtime.scheduling_comparison > scheduling-comparison.json
```

每次试验有两个 Worker，两组 Agent 分别在自己的临时目录处理固定输入。6 个长节点与 3 个短节点同时就绪，同批顺序为长节点优先。FIFO 从满足准入条件的候选中选最早就绪的节点，DRF 选当前主导份额最低的 Agent。两种策略使用相同的预算，按 FIFO、DRF、DRF、FIFO 各运行两次。排队时长从提交工作流到节点开始计算，P95 采用最近秩法，汇总表取同策略两次测量的中位数。脚本以输出一致和 CPU、内存、PIDs 预留全部归零为验收条件。资源数字是调度器配额记账，实际 Linux 限流、OOM 和 cgroup 清理由委派验收覆盖。

## 跨工作流调度对照

「资源压力」与「文本统计」可以同时运行，共用四个派发槽位和内核 DRF 资源准入。提交时可指定完成目标。就绪节点按「截止时刻 − 当前时刻 − 下游关键路径估计」的最小裕量优先，未指定截止时间时按关键路径长度排序。等待达到 350 ms 的可准入节点按到达顺序优先派发。已运行的节点继续执行，等待仍取决于槽位和资源可用性。结果中的「派发决策」显示实际等待、估计路径和当时的裕量。

在工作台点击「调度对照」，或从项目目录运行同一实验。

```bash
python -m runtime.workflow_scheduling_benchmark > workflow-scheduling.json
```

六轮采用 FIFO、DRF、关键路径、关键路径、DRF、FIFO 的顺序。每轮 60 个独立工作流按 24 ms 间隔交错到达，包括固定模型桩问答、隔离进程中的 Python 测试，以及规划、统计、验证、汇报组成的混合 DAG。每种策略使用相同的四个槽位和资源准入。短问答、长测试、混合 DAG 的截止目标分别为 250、2500、1350 ms。P95 对每种策略的 120 个工作流使用最近秩法，从计划到达时刻计到工作流完成。长测试最大等待从就绪时刻计到派发。结果 JSON 保留各工作流和节点的时序、产出摘要及资源回收数据。

## 上游样本与浏览器验收

在工作台点击「工程修复」，项目下拉框选择「python-json-pointer v3.0.0」，勾选「固定补丁桩」即可离线复跑。样本保留上游路径编解码函数，并加入本地路径封装和评估测试。两处练习缺陷植入在副本中。交付包包含补丁、通过测试的源码和公开测试、上游版本、许可证及 SHA-256 清单。[固定上游源码](https://github.com/stefankoegl/python-json-pointer/blob/f3643addecf9dc66cb7bd120532a8373f19b1e23/jsonpointer.py)

浏览器验收需要上文的 Python 依赖、Node.js 22+ 和 Playwright。以下命令在项目目录执行，输出目录使用新路径。

```bash
pw_dir="$(mktemp -d)"
npm install --prefix "$pw_dir" --no-save playwright@1.59.1
"$pw_dir/node_modules/.bin/playwright" install chromium --only-shell
NODE_PATH="$pw_dir/node_modules" python tools/run_playwright_e2e_acceptance.py --out-dir ./reports/playwright-current
```

脚本启动一个独立的随机端口工作台。提交、双击、刷新、下载和会话操作均通过页面完成，读取任务接口用于交叉核对。下载的两份 ZIP 会重新核验哈希，并在独立目录里复跑公开测试。报告、截图、原始 JSON 和 ZIP 均写入指定目录。可以通过 `--node` 指定 Node.js 路径，通过 `AIOS_E2E_CHROMIUM` 指定现成的 Chromium 或 Chrome。Playwright 默认使用随版本安装的浏览器。[Playwright 浏览器安装说明](https://playwright.dev/docs/browsers)

## 证据与性能边界

- 随包 R6 NUC 记录：第一阶段 111/111、0 跳过；独立 validate_linux 阶段 110 通过、1 项跳过，并另外验证 CPU 节流、真实 OOM 与退出后无孤儿 cgroup。
- 原始 NUC 源包 SHA-256：`f4cbc22d4b64a348726f9860cdc13f2c6162e0eee91fef33dead263237b8a1c8`。发布修订没有重跑 NUC，使用逐文件比对证明运行时代码未变；不能把新 ZIP 的整体哈希称为旧日志的验收对象。
- 同持久性、固定响应桩、单并发、四工程 ABBA 观测：Runtime 仍比直接编排慢约 6.1%～7.6%；不产生真实计费 token。
- 新增真实调用对照只有少量样本。BFCL/HumanEval 扩量组使用不同并发（6 对 2），不作为同条件速度优势；HumanEval 为 154/164 对 157/164，质量未证明不下降。
- 5 套公开任务子集 + 1 套合成压测为自托管实验，非官方榜单。桌面 LiteCUA/VMController 仍为模拟；工程修复入口主要覆盖内置受控项目，非任意项目修复保证。

完整报告、答辩 PPT、原始 JSON、浏览器下载复测及 NUC 日志在仓库的 [交付资料](../releases/proj3-r6.2.1-20261125/)；独立源码 ZIP 不含该同级目录，请使用完整交付包。架构参考 [AIOS](https://github.com/agiresearch/AIOS)，不是官方兼容性或官方成绩声明。

## 演示视频与许可

演示视频链接、提取码：待团队录制后填写，尚未交付视频。团队贡献与签字也需本人核对。

自研代码采用 [MIT](LICENSE)，第三方组件保留原许可，见 [来源说明](THIRD_PARTY_NOTICES.md)。
