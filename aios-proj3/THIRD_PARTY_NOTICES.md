# 开源与第三方来源说明

2026-11-24，项目负责人在本会话明确选择MIT。根LICENSE适用于团队有权许可的自研代码和新增修改，不替换第三方许可、不对未知来源重新授权。历史冻结ZIP保持原样；此声明不声称历史每个归档文件已经完成来源核对。

| 来源 | 使用性质/范围 | 许可与处理 |
|---|---|---|
| agiresearch/AIOS，参考快照292d98f78c835499df18cdb6de6325bba1895862 | 分层内核、四类请求、SDK与管理器的架构参考；模块名相似不能直接证明代码来源 | 本地快照LICENSE仅2字节，无法从该文件确认明确许可；不把“公开GitHub仓库”当成任意复制授权。若实际借用具体源码，需队伍逐文件核对授权和归属 |
| Cerebrum、LSFS、A-Mem、LiteCUA论文 | 思路参考与本项目受限实现 | 论文引用不是对源码的许可；不宣称完整复现或官方兼容 |
| python-json-pointer v3.0.0 | 工程修复样本复用上游编解码函数，固定在 f3643addecf9dc66cb7bd120532a8373f19b1e23 | BSD-3-Clause，样本目录及交付 ZIP 包含上游版本和许可证 |
| KaTeX 0.19.1 | runtime/static/vendor/katex下JS/CSS/字体 | 保留目录内MIT LICENSE及原版权说明；根MIT不覆盖其归属 |
| Python、PyMuPDF、Pillow、Poppler/pdftotext | 运行环境或可选文档解析依赖，未把其包文件纳入自研源码 | 各自适用原许可证；部署/再分发前核对所用版本许可，不能由根MIT概括 |
| dsh-guizang-design | 界面风格参考 | 参考入口https://github.com/op7418/dsh-guizang-design；若实际复制素材/代码，需记录具体文件和许可 |
| BFCL、HumanEval、MBPP、AgentBench、tau2-bench | 任务/评测参考，当前代码含适配与实验 | 基准源码/数据各有原许可；自写适配不等于官方结果，不重新许可上游数据 |

来源审核责任：团队应逐模块确认设计参考、局部借用或第三方直接分发，补齐实际作者和文件映射。本文件不虚构授权审查完成记录。核心模块头保留原docstring，并链接到本说明；vendor不做批量重写。
