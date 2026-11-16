# 2026-11-16 直接调用对照原始证据

被测产品提交：`f6a7cf4393987ef225543712f7df26756d624348`。全部内容只含受控示例项目与合成资料，不含生产会话或密钥。

- `primary-protocol.json` / `dataset.json`：真实调用前冻结的方案与输入。
- `primary-results.json`：21对、42个主实验执行样本，失败与慢样本全部保留。
- `supplement-protocol.json` / `supplement-results.json`：看到主结果后另行声明的默认额度与相同检索片段对照，12个样本。
- `summary.json` / `samples.csv`：可复算汇总和54个样本的指标。
- `diagnostics.json`：主实验工程任务的观测记录，绝对项目路径脱敏为REPO。
- `cleanup-verification.json`：独立验证实验cgroup已释放，生产模型broker进程未更换。
- `frozen/`：实测时脚本原文，其SHA256与方案一致。原始脚本的残留计数字段误数虚拟控制文件，保留不改；维护版脚本已修正。归档用于核对来源，重现实验时应放回tools目录并使用新的输出目录。

离线复算，不消耗模型额度：

```bash
python3 tools/summarize_direct_comparison.py \
  validation/direct-2026-11-16/primary-results.json \
  validation/direct-2026-11-16/supplement-results.json /tmp/direct-summary.json
python3 tools/test_direct_compare.py
```

不能使用原输出目录重新执行付费实验；脚本会拒绝重放。新实验需重新prepare、保留新的声明与数据，使用已配置代理凭据的委派服务。不要把密钥写进脚本或命令行。

解读和适用边界见 `docs/直接调用对照验收-2026-11-16.md`。主实验工程额度小于网页默认，补充默认组单独列出，不合并成功率。知识组的token收益比较的是全量资料输入；相同检索片段的直调基线另列。54个样本都没有剔除；78个模型请求都有实际usage且finish_reason=stop。
