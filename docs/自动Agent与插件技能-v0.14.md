# 自动 Agent、插件与 Skill v0.14

日期：2026-11-15。用户态 Runtime 开发版，最终赛题验收以官方规则为准。

## 用户入口与执行关系

聊天页取消固定场景选择。用户输入需求或上传附件，模型返回直接回答或结构化任务图；Runtime 校验角色、工具、依赖和数量，再创建受管理的 Agent 与 Attempt。原有固定修复、评审等入口继续保留在独立 `/console`。

每轮最多12个专长或工具任务。模型任务只生成文本；涉及文件、命令、网页或配置的实际操作都必须经过注册工具。工具图完成后，模型检查真实结果，输出最终答案或下一轮计划。最多3轮，模型额度与任务总数也限制执行。新轮次创建新的任务ID，不重放原付费POST。达到轮数、权限或资源边界时停止，不能将未确认结果标记为业务成功。

模型可按观察调整本工作流的并发和准入策略。它没有改写在线 Runtime 源码、修改宿主认证或取得任意 root 权限。

## 常用插件

| 类别 | 工具 | 实际作用与限制 |
|---|---|---|
| 数学与文本 | math.calculate、text.stats、text.search、text.diff | 有界算术、统计、字面检索、差异；不执行表达式中的函数 |
| 数据 | json.validate、csv.profile | 真实解析与格式检查，格式合法不代表业务正确 |
| Python | python.syntax | AST语法和符号检查，不执行任意源码 |
| 固定工程 | project.inspect、project.test、project.patch_check | 三个已注册项目、AST补丁限制、不可修改的检查用例 |
| 工作目录 | file.list、file.read、file.write、file.copy、file.replace | 项目内文件操作；读默认12000字符、可分页；复制保留原文，替换校验原哈希和唯一片段 |
| 产物 | artifact.write | 生成项目文件，聊天页提供经鉴权和哈希核对的下载链接 |
| 联网 | web.fetch | 公网HTTPS文本读取，证书验证、固定解析出的公网IP、每跳重定向重验；拒绝内网和凭据URL |
| 执行 | command.run | bubblewrap命名空间隔离，清空环境，项目目录可写，主机home不可见，命令网络关闭 |
| 控制 | runtime.observe、runtime.configure | 观察当前资源账本/状态，调整workers或FIFO/资源适配/老化/DRF策略；不能扩张全局资源/模型预算 |

合计20个工具。工具注册与参数示例见 `agent_runtime/capabilities.py`，实现分布在 `tooling.py`、现有工程模块和IPC控制通路。

工具只通过受控注册表调用，不动态导入模型给出的模块或安装外部插件。网页正文和附件都是不可信数据，不能赋予权限。静态网页读取不会执行JavaScript，不能替代完整交互浏览器。

## Skill 库

`agent_runtime/skills/` 内含8份可检查的Markdown技能：Python评审、测试设计、项目分解、技术写作、公网资料、隔离项目开发、数据检查、Runtime运维。路由器可选择已注册Skill，消费者按需读取对应指导，单份最多8KiB。Skill不提供额外系统权限，不能指定任意路径，也不会安装第三方代码。

新增库项需要开发者在注册表声明参数、权限和实现，添加针对性测试后部署。当前没有外部插件市场或任意远端Skill安装器。

## 文件与命令权限

会话工作目录位于数据根的 `workspaces/<conversation_id>/`，跨轮保存。模型不能传入这个宿主路径，Runtime负责设置。命令只能在该目录运行，路径操作拒绝上级路径、隐藏路径和符号链接，并用逐层目录描述符防止父路径替换。文件、复制、替换和命令共享位于沙箱外的项目锁，避免并行改写丢失更新。

允许的命令包括python3、bash、git、常见文本工具、gcc/make。命令通过参数数组执行，再进入bubblewrap；无bubblewrap直接拒绝执行。命令最长20秒、输出最多64KiB、单文件RLIMIT_FSIZE为8MiB；项目512项/32MiB配额在操作前与命令运行时检查。目录总配额属于检查机制，不是内核磁盘硬配额，短时间写入可能在检查间隔内超出。

CPU/内存/pids仍由原Agent与Attempt双层cgroup控制。非零退出和工具拒绝会作为真实观察交给后续Agent，不能以模型描述代替实际成功。一般项目中的测试文件仍可被Agent修改，因此重要验收需保留独立检查；固定三项目使用原有不可修改测试与AST约束。

`runtime.configure` 的修改只作用于当前工作流，workers不能超过初始并发上限。已运行任务不会因降低并发而被杀死；新准入遵守新设置。全局资源与模型额度始终保持原上限。更换策略会重建该工作流的就绪决策状态，不承诺跨切换保留历史公平性。

## 深度思考与图片

深度思考使用 DeepSeek 原生 `thinking.type=enabled`、`reasoning_effort=high`，不是让模型在正文里模拟思考。普通请求仍关闭。勾选时要求单次4096 token额度，推理与答案共享供应商计数，模型服务仍有原有绝对截止时间，复杂请求可能超时或耗尽额度，失败不自动付费重试。只保留最终正文和使用量，不记录或展示原始 `reasoning_content`。

兼容现有内存密钥broker：网关把经过校验的选项编码成内部系统消息标记，单次供应商子进程在发送前移除标记并转换为真实API参数。图片同样转换为官方多模态content数组。现有broker只做内存持钥与请求去重，不需要读取或导出密钥，也不重启。升级现有旧broker子进程代码前需备份、比对与原子替换；新安装使用本版本客户端即可。

来源：[DeepSeek思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)、[视觉接口](https://api-docs.deepseek.com/guides/vision/)。单独实测已确认123个推理token，以及合成红色方块的真实识别。

## 附件与 SQLite

支持PNG/JPEG及UTF-8 txt/md/py/json/csv/log。每次最多4个文件、其中2张图片。服务端接收上限8MiB，文本不超过1MiB；PNG/JPEG校验后限制1600万像素，缩至1280边长并去除元数据，标准化JPEG不超过256KiB。入口反向代理可能有更小的上传上限。SVG、可执行文件、私钥和其他未注册格式拒绝接收。

图片只交给初始视觉路由，后续Agent获得图片观察文本，不反复传整张图。文本附件保存到本项目上传目录，模型先得到最多4000字符摘要，可按需用文件工具读取。附件按会话ID绑定，随机ID、SHA-256完整性校验和私有文件权限保护存储；不是加密存储。

SQLite位于 `metadata/runtime.sqlite3`，WAL模式，参数化SQL，保存附件元数据、数值运行观测和工具审计。供应商密钥、原始思考和工具正文不进入数据库。附件目录与数据库均不在命令沙箱内。当前上限100附件/128MiB、10000条观测及审计；会话正文仍在原JSON存储，未迁移为SQL。

网页POST保留代理鉴权、CSRF/Origin检查，服务最多8个处理线程，图片解码串行。产物下载始终鉴权、限制项目路径并可校验哈希。对话支持100轮、最近16组/96KiB历史；完成会话通过revision增量轮询，列表缓存根据mtime与大小失效，减少反复解析大历史。

## 安装与运行

openEuler安装发行版签名包 `bubblewrap python3-pillow`，验证非root用户可用user namespace。网页服务保留 `Delegate=yes`、`NoNewPrivileges=yes` 及既有CPU/内存/pids约束。

```bash
python3 -m unittest -q
python3 -m agent_runtime.web --host 127.0.0.1 --port 18880 --delegated --data-dir /path/to/private-data
```

原生思考与视觉兼容代码通过验证后，服务环境启用 `AGENTRT_NATIVE_THINKING=1 AGENTRT_VISION=1`。保留独立broker与私有代理配置，不把供应商Key放进Worker、命令参数、文档或Git。

## 与 AIOS 和赛题的关系

借鉴 [AIOS v5](https://arxiv.org/html/2403.16971v5) 的应用与资源服务分层，将模型、工具、上下文与访问控制集中到Runtime。自由路由是应用规划，OS侧仍由Agent控制块、调度、cgroup、命名空间与授权执行。不能因加入工具就宣称完整复现AIOS、已实现云模型KV Cache抢占恢复或所有赛题评分点。原有 [赛题与论文机制对照](论文落地与赛题覆盖-v0.8.md) 仍适用。

实测数据、失败样本和剩余限制见本次 `validation/agent-2026-11-15/` 与性能报告。当前仍是团队共享登录、共享Linux内核和单工作流服务，不是多租户强安全平台；没有控制器崩溃自动续跑、任意宿主root配置、分布式执行或无限迭代。
