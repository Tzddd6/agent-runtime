# Proj3 最终交付 · 2026-11-23

- [完整交付包（源码、报告/PDF、PPT、手册及历史证据）](AIOS-Proj3-最终交付候选包-20261123.zip)
- [答辩 PPT](AIOS-Proj3-答辩PPT.pptx)
- [最终版本绑定 NUC 复验日志](nuc-final-bound-acceptance-20261123.log)
- [可浏览、可开发的正式源码](../../aios-proj3/)

## 验收结果与边界

openEuler 24.03 SP4 上，包外复验日志记录：第一阶段89/89通过、0跳过，10.881秒；原生Worker限流约0.1988核、oom_kill=1、零孤儿cgroup。第二阶段重复回归88通过/1跳过，另行CPU验证约0.2074核，复杂工作流通过。此日志是归档验收证据，不代表每台电脑运行均得到相同耗时。

5套公开任务子集＋1套本地合成压测属于自托管受控实验，不是官方排行榜成绩；尚未证明所有场景都快于直接调用。LiteCUA/VMController为内存态桌面模拟。密钥由纯内存独立进程管理，业务子进程环境变量不继承供应商Key。

## openEuler 完整复现

下载完整包并解压，进入 `AIOS-Proj3-最终交付候选包/`，阅读 `02_acceptance/OPEN_EULER_RUNBOOK.md`。建立虚拟环境、安装PyMuPDF/Pillow后，使用有cgroup v2委派权限的普通用户运行：

```bash
set -o pipefail
AIOS_PYTHON="$HOME/aios-venv/bin/python3" bash 02_acceptance/FINAL_CHECK.sh 2>&1 | tee "/tmp/aios-accept-$(date +%Y%m%d-%H%M%S).log"
```

日志写到包外，避免修改受FILES.sha256保护的历史证据。`SHA256SUMS`校验本目录原始交付文件，源码冻结包SHA256记录在完整包的冻结说明内。

## 来源与版本

项目参考 agiresearch/AIOS 等工作；这是本队的Proj3交付，不是官方AIOS发行。原仓库早期原型与参考文档继续保留。目录 `aios-proj3/` 仅在README顶部增加GitHub阅读导航，其余文件来自已验收冻结源码；权威原始源码以交付ZIP内的冻结包为准。
