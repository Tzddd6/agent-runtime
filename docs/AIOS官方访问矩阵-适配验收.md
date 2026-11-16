# AIOS 官方访问矩阵：适配验收记录

本次检查的是框架的上下文授权规则，不是模型能力。线上 v0.19.2 没有切换；新增组件处于实验验收阶段，没有接入线上请求链。

## 固定来源与测试范围

- 官方文件：[test_context_injector_access_matrix.py](https://github.com/agiresearch/AIOS/blob/292d98f78c835499df18cdb6de6325bba1895862/tests/modules/memory/test_context_injector_access_matrix.py)。
- 固定提交：`292d98f78c835499df18cdb6de6325bba1895862`。
- 原始文件 SHA256：`2a01240304a7b5460b2fc7508c310a20446afde9e707547c2a87893cd0a20658`。
- 26 个原测试方法，原文件字节、断言均未修改，没有 skip、xfail 或删除用例。运行器在导入外部文件前强制校验 SHA256。
- 检查同用户/跨用户、同 Agent/其他 Agent、private/shared、命名空间精确匹配、缺失元数据、去重、相关性与预算前的隔离。

## 实测结果

在 NUC 专用实验目录通过本项目 `command.run` 执行。子进程处于 bubblewrap 用户、挂载、PID、网络隔离环境，不挂载用户主目录；关闭网络，外部命令时间限制 15 秒。没有读取或传递 API 密钥，没有模型请求，没有重启线上服务。

| 检查 | 实际结果 | 含义 |
|---|---|---|
| 原文件直接运行 | 导入失败：没有 `cerebrum` | 不算运行了 26 个测试，更不算能力得分 |
| 原文件＋Runtime 显式接口适配 | 26/26，0 跳过，0 错误 | 原断言可检查新组件的授权与选择行为 |
| 自有存储 ACL/UTF-8 预算边界 | 8/8 | 补充原文件未覆盖的边界 |
| 原测试＋自有测试合并 | 34/34 | 同一进程完成，模型请求 0 次 |
| 故意关闭用户分区检查 | 原测试 11 项失败 | 能检测这类跨用户授权错误 |
| 故意关闭 Agent 私有检查 | 原测试 3 项失败 | 能检测这类 Agent 越权 |
| 故意关闭预算检查 | 原测试仍 26/26 | 原套件不能单独证明预算上限有效 |
| 关闭预算＋补充测试 | 34 项中 2 项失败 | 补充测试能抓住零预算及精确边界错误 |

未把故障注入产生的预期失败算成产品故障，也没有把这些重复运行累加成能力总分。正常 26 项断言执行约 0.009 秒，仅供复现定位；不包含进程启动，不能作为相对直接调用的性能优势。

## 适配层到底做了什么

`tools/aios_access_adapter.py` 仅在独立测试子进程内提供 AIOS/Cerebrum 名称下的 DTO、管理器外壳与 `ContextInjector` 桥接接口。这些不是官方包实现，也不在正常业务进程中注册这些名称。

真正的授权、排序、去重、预算与对象读写由以下代码执行：

1. 新增 `agent_runtime/memory_access.py`：可信用户身份精确匹配，授权先于排名/去重/预算；私有记忆限拥有者，共享限同一用户；拒绝损坏元数据、非有限分数。
2. 现有 `agent_runtime/context.py`：内容寻址、完整性校验、读者 ACL、显式 grant 与实际对象读取。用户与 Agent 采用二元组编码，避免拼接身份碰撞。
3. 预算用 UTF-8 字节保守计数，包含生成的记忆行前缀和换行；不是供应商 token 计数器。适配入口把上游 `max_memory_tokens` 数值作为更保守的字节预算。格式包装、真正 tokenizer、写入屏障不在本次等价范围内。

适配器每次请求使用临时 ContextStore，验证结束清理。因此没有测持久化恢复、跨请求缓存复用或写入并发，也没有声称这些功能已经通过。

## 如何复现

保留外部文件在参考目录，不把未知外部测试直接放进线上服务。推荐重复使用 NUC 的网络隔离实验工作区：

```bash
# 在 /home/tzd/agentrt-aios-access-final-20261116/workspace 下，
# 通过 Runtime command.run 的 bubblewrap 子进程执行以下 argv 对应命令：
python3 -B tools/run_aios_access_matrix.py \
  --test official_access_matrix.py --report adapter.json

python3 -B tools/run_aios_access_matrix.py \
  --test official_access_matrix.py --report combined.json --include-local

# 预期返回非零，原测试应出现 11 项失败：
python3 -B tools/run_aios_access_matrix.py \
  --test official_access_matrix.py --report drop-user-check.json \
  --mutation drop-user-check

# 预期返回非零，补充测试应出现 2 项预算失败：
python3 -B tools/run_aios_access_matrix.py \
  --test official_access_matrix.py --report budget-boundary-mutation.json \
  --mutation drop-budget --include-local
```

证据：`validation/aios-access-matrix/summary.json`。报告包含被测实现文件 SHA256、原测试 SHA256、实际失败方法列表与隔离模式。NUC 原始命令输出保存在 `/home/tzd/agentrt-aios-access-final-20261116/*-raw.json`；原有部署与验收记录没有覆盖。

## 不能由这次结果推出的结论

- 不能写成“原生 AIOS 26/26”：原生路径仍缺 Cerebrum；本次成功路径替换了接口层。
- 不能写成“线上 Agent 完整通过官方安全验收”：新组件还未连接网页身份、会话、知识库与模型上下文链路。
- 本模块要求协调器提供可信身份和可信存储元数据。若将模型生成的 `user_id` 或 `sharing_policy` 直接当作授权依据，即使这些单元测试通过仍可能越权。
- 上游套件显式关闭写入屏障，也使用返回所有记录的假 provider，不能证明实际数据库过滤、写后读顺序、宕机恢复或并发隔离。
- 不覆盖调度、资源限制、KV Cache、浏览器、文件上传或项目修复；这不是完整 AIOS/Agent benchmark 分数。

后续上线门槛是把身份从可信服务会话传入，再对网页实际调用链做跨用户、跨 Agent、撤权与持久化并发测试。应另建固定依赖的完整 AIOS 环境运行官方实现，才能增加“官方实现原生基线”，不应把适配层成绩替代它。
