# AIOS 控制台聚焦版 Playwright 浏览器端到端自动验收报告

- **执行时间**：`2026-11-23T16:57:42.782Z`
- **浏览器引擎**：`Chromium Headless (Playwright 1.65.1)`（真实页面点击与表单交互，零 API 绕过）
- **总用例数 / 通过数**：**6 / 6**
- **浏览器 `console.error` / `pageerror` 计数**：`1` / `0`

## 核心必测链路验收结果总览

| # | 用例标识 | 验收场景描述 | 关联工作流 ID | 前后端状态一致性 | 浏览器截图 | 结果 |
|---:|---|---|---|---|---|---|
| 1 | `case_01_multifile_order_service_e2e` | 多文件 order_service 修复完整链路 + 多 Agent 系统级视图 + 产物 ZIP SHA-256 核验 | `ca33aa9ebf88` | UI:`succeeded` == Backend:`succeeded` | `reports/playwright_e2e/01_order_service_multifile_repair.png` | **PASS** |
| 2 | `case_02_double_click_idempotency` | 连续快速双击提交按钮幂等保护（仅创建 1 个有效工作流，无重复调用） | `97f1f0bd19d5` | UI:`succeeded` == Backend:`succeeded` | `reports/playwright_e2e/02_double_click_idempotency.png` | **PASS** |
| 3 | `case_03_single_branch_fault_isolation` | 单分支故障隔离（无依赖 audit_agent 标红失败，主修复分支照常完成并交付产物） | `40429d5aa50d` | UI:`completed_with_failures` == Backend:`ok` | `reports/playwright_e2e/03_isolated_branch_fault.png` | **PASS** |
| 4 | `case_04_reload_recovery_consistency` | 页面刷新恢复与前后端快照强一致（DAG 状态、失败原因与产物下载入口完整恢复） | `40429d5aa50d` | UI:`completed_with_failures` == Backend:`completed_with_failures` | `reports/playwright_e2e/04_reload_state_recovery.png` | **PASS** |
| 5 | `case_05_budget_exhaustion_and_cleanup` | 受控预算耗尽与资源回收（modelCallsBudget=0 明确拦截、无静默重试、活跃 Worker 归零） | `ddf217178261` | UI:`failed` == Backend:`failed` | `reports/playwright_e2e/05_budget_exhaustion_reclamation.png` | **PASS** |
| 6 | `case_06_acb_lifecycle_panel` | 控制台 ACB 控制块面板与生命周期状态轨迹可视化核验 | `N/A` | UI:`ok` == Backend:`ok` | `reports/playwright_e2e/06_acb_lifecycle_and_queues.png` | **PASS** |

## 关键证据核查摘要

1. **多文件 `order_service` 真实修复与下载产物 SHA-256 强一致（Case 1）**：
   - 工作流 ID：`ca33aa9ebf884f5bb1972a368ae76ed1`
   - 控制台实时观测到 5 个独立内核级 Agent 实体：`analyst_agent, tester_agent, patcher_agent, reviewer_agent, audit_agent`
   - 隔离测试 Worker 真实子进程 PID：`[51468, 82940]`
   - 通过测试的工作区源码树 SHA-256：`c11cdf677b939f0e4944107eef4cde69bbed19b5b44f62372117051d28a2edfc`
   - 浏览器点击下载的 ZIP 包 SHA-256：`45630b4c7bcf4c3f673b8fb8810ff3fec3c3ae525ed68d9bc588ff9bbda256da`（与后端交付清单 `45630b4c7bcf4c3f673b8fb8810ff3fec3c3ae525ed68d9bc588ff9bbda256da` 完全一致，解压重算 `src/pricing.py`、`src/shipping.py`、`src/refunds.py` 树哈希核验通过）
2. **连续快速双击幂等保护（Case 2）**：
   - 同一事件循环内连续触发两次提交按钮点击，后端仅创建 `1` 个有效任务，杜绝重复写盘或付费模型重放。
3. **无依赖分支故障非级联隔离（Case 3）**：
   - 注入 `audit_agent` 分支故障后，`static_audit` 节点标红为 `failed` 并展示明确原因 `AuditBranchFault`，主修复链路 `answer` 节点状态为 `succeeded`，整体标记为 `completed_with_failures` 并正常生成可下载修复包。
4. **页面刷新状态恢复与快照一致性（Case 4）**：
   - 刷新浏览器后前端恢复展示的状态 `completed_with_failures` 与后端持久化快照 `completed_with_failures` 完全一致，产物下载按钮保持可用。
5. **受控预算耗尽与零残留回收（Case 5）**：
   - 设置 `modelCallsBudget=0` 后，`patch` 节点被内核配额守卫立即拦截为 `failed`，下游 `verify` 节点自动标记为 `isolated_skipped`，无静默重试；结束后活跃 Worker 进程数=`0`，DRF CPU 占用=`0 mCPU`。
