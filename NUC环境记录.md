# NUC 与 openEuler 环境记录

## 2026-11-11 重装后的当前环境（已实机核验）

裸机 openEuler 24.03 LTS-SP4，主机名 NNserver；内核 6.6.0-159.4.20.174.oe2403sp4.x86_64，Python 3.11.6。登录账号 tzd（UID 1001），仓库 /home/tzd/agent-runtime，SSH 入口 mc.tzddd.cn:2222。cgroup2fs 已启用。验证进程通过 User=tzd、Delegate=yes 的独立 systemd 服务运行，限额 CPUQuota=200%、MemoryMax=2G、TasksMax=256，不修改全局 swap。

普通测试和单独启用的四项真实 cgroup 测试全部通过；CPU 200 millicpu 实测约 0.204 核，并发生 22 次节流。原始报告见 validation/2026-11-11-openeuler-v03.json，最新日志和实验数据保存在 NUC 仓库 reports/。/home/tzd/agent-rt-evidence/ 中的既有证据保持原样。

本项目控制台 agentrt-console.service 以 tzd 运行，代理回源只走回环 18880。ECS 的 agentrt-nuc-forward.service 使用独立受限公钥转发该单一端口。NUC 的 /etc/ssh/sshd_config.d/90-agentrt-console.conf 仅为 tzd 的回环连接允许到 127.0.0.1:18880 的本地转发；其他用户仍禁止 TCP 转发。域名入口复用现有网页登录认证，凭据不纳入仓库。

## 以下为旧环境历史记录，不能作为当前配置

记录日期：2026-11-01。以下机器状态和实验数值由用户提供，尚未由本工作区独立连接验证。更换镜像、内核或启动参数后需重新检查。

- Ubuntu 宿主机上运行 openEuler 虚拟机；用户报告虚拟机 4 vCPU、约 7.3 GiB 内存、47G 文件系统，Python 3.11.6，Git 2.54.0，gcc 12.3.1。
- 项目路径 `/home/dev/projects/agent-runtime`。用户报告示例运行正确，3 项测试通过。
- 此实例原先使用 cgroup v1，用户报告添加 `systemd.unified_cgroup_hierarchy=1 cgroup_no_v1=all` 并重启后启用 v2。不能据此推断所有 openEuler 版本或镜像的默认模式；变更启动参数应先根据目标系统文档及现有引导配置评估。
- 检查命令：`stat -fc %T /sys/fs/cgroup`、`cat /proc/self/cgroup` 和 `cat /sys/fs/cgroup/cgroup.controllers`。分别检查挂载模式、进程所在层级、可用控制器；还应确认目标子树的控制器启用情况和写入权限。
- 用户报告：原有约 7.9 GB swap。设置 `memory.max=200M` 而允许 swap 时，400 MB 分配未触发 OOM；设置 `memory.swap.max=0` 后，400 MB 测试触发 OOM，100 MB 测试通过。建议在受控实验的任务 cgroup 内显式设置并记录 swap 策略；是否关闭全局 swap 另行按运维需求决定。
- 用户报告 `cpu.max=20000 100000` 的测试中，CPU 时间与墙钟时间比约 20.4%，同时观察到节流计数。该数值是一次测试结果，不代表所有负载下的固定精度。
- 当前 `systemd-run --user --scope` 报 user bus 连接失败，用户报告使用具备权限的 root 操作完成 cgroup 试验；普通用户委派及 user-scope 路线尚未验证。

后续 Runtime 的资源管理需要实际实现并验证：控制器委派、工作进程启动时的归组顺序、异常清理和权限边界；避免将整个处理不可信任务的运行时和 Worker 默认作为 root 运行。当前 Python 原型尚未实现 cgroups 或进程树管理。

参考：[Linux cgroups v2 官方文档](https://docs.kernel.org/admin-guide/cgroup-v2.html)。
