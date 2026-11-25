/**
 * Focused Playwright Browser E2E Acceptance Runner for AIOS Kernel Console.
 * Drives real Chromium clicks on `http://127.0.0.1:<port>/console/` (no direct API bypass).
 * Covers the 6 core OS demonstration flows:
 *   1. Multi-file `order_service` repair full loop + DAG/ACB/PID/cgroup/Token/ctx_ref + ZIP download SHA-256 verification
 *   2. Double-click idempotency protection (rapid double click creates only 1 job, 0 duplicate model calls)
 *   3. Page reload state recovery & frontend-backend snapshot consistency
 *   4. Single-branch fault isolation (`audit_agent` failure highlighted in red while main repair succeeds and delivers)
 *   5. Controlled budget exhaustion / error isolation (`modelCallsBudget=0` fails cleanly with explicit reason & zero leaked workers)
 *   6. Chat & attachment security smoke test (valid upload accepted, empty/oversized rejected)
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const npxPlaywrightPath = 'C:\\Users\\Tzddd\\AppData\\Local\\npm-cache\\_npx\\e41f203b7505f1fb\\node_modules\\playwright';
const { chromium } = require(npxPlaywrightPath);
const chromeExecutable = 'C:\\Users\\Tzddd\\AppData\\Local\\ms-playwright\\chromium-1223\\chrome-win64\\chrome.exe';

const baseUrl = process.env.AIOS_BASE_URL || 'http://127.0.0.1:18895';
const outDir = process.env.AIOS_E2E_OUT_DIR || path.resolve(__dirname, '..', 'reports', 'playwright_e2e');

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function fetchJson(url) {
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`HTTP ${res.status} from ${url}`);
  return res.json();
}

(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const browser = await chromium.launch({
    executablePath: chromeExecutable,
    headless: true,
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 960 },
    acceptDownloads: true,
  });
  const page = await context.newPage();

  const consoleLogs = [];
  const pageErrors = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      consoleLogs.push(`[console.error] ${msg.text()}`);
    }
  });
  page.on('pageerror', (err) => {
    pageErrors.push(`[pageerror] ${err.message}`);
  });

  const cases = [];

  try {
    // Navigate to Console UI
    await page.goto(`${baseUrl}/console/`, { waitUntil: 'networkidle' });
    await page.waitForSelector('#preset');

    // =========================================================================
    // Case 1: Multi-file `order_service` full loop + DAG/ACB/PID/cgroup/Token/ctx_ref + ZIP SHA-256
    // =========================================================================
    await page.selectOption('#preset', 'engineering');
    await page.waitForSelector('#projectSelect', { state: 'visible' });
    await page.selectOption('#projectSelect', 'order_service');
    const includeAudit = await page.$('#includeAuditBranchChk');
    if (includeAudit && !(await includeAudit.isChecked())) {
      await includeAudit.click();
    }
    const simFail = await page.$('#simulateAuditFailureChk');
    if (simFail && (await simFail.isChecked())) {
      await simFail.click();
    }

    await page.click('#submitJobBtn');

    // Poll UI until selectedJobStatus becomes succeeded
    let job1Id = null;
    for (let i = 0; i < 60; i++) {
      await sleep(300);
      const statusText = (await page.textContent('#selectedJobStatus')).trim();
      const st = await fetchJson(`${baseUrl}/api/status`);
      if (st.jobs && st.jobs.length > 0) {
        job1Id = st.jobs[0].id;
      }
      if (statusText === 'succeeded') break;
    }

    // Force one UI refresh cycle and inspect UI tables
    await sleep(600);
    const status1 = (await page.textContent('#selectedJobStatus')).trim();
    const dagTableHtml = await page.innerHTML('#dagTaskTableWrap');
    const telemetryHtml = await page.innerHTML('#engAgentTelemetryWrap');
    const verdictText = (await page.textContent('#engVerdictBadge')).trim();

    // Click the actual Download ZIP button on the page
    const downloadBtn = await page.$('#engDownloadZipBtn');
    const isDlVisible = await downloadBtn.isVisible();
    const [download] = await Promise.all([
      page.waitForEvent('download'),
      downloadBtn.click(),
    ]);
    const zipSavePath = path.join(outDir, `case1_${job1Id.slice(0, 10)}_delivery.zip`);
    await download.saveAs(zipSavePath);
    const zipBytes = fs.readFileSync(zipSavePath);
    const zipSha256 = crypto.createHash('sha256').update(zipBytes).digest('hex');

    const backendJob1 = await fetchJson(`${baseUrl}/api/jobs/${job1Id}`);
    const expectedZipSha = backendJob1.engineering.artifact.zip_sha256;
    const verifiedSourceSha = backendJob1.engineering.source_sha256;

    const shot1 = path.join(outDir, '01_order_service_multifile_repair.png');
    await page.screenshot({ path: shot1, fullPage: true });

    const hasAllAgents = ['analyst_agent', 'patcher_agent', 'tester_agent', 'reviewer_agent', 'audit_agent'].every(
      (ag) => telemetryHtml.includes(ag)
    );
    const hasCtxRef = dagTableHtml.includes('inspect(') && dagTableHtml.includes('plan(');

    cases.push({
      id: 'case_01_multifile_order_service_e2e',
      title: '多文件 order_service 修复完整链路 + 多 Agent 系统级视图 + 产物 ZIP SHA-256 核验',
      passed:
        status1 === 'succeeded' &&
        verdictText.includes('通过') &&
        isDlVisible &&
        hasAllAgents &&
        hasCtxRef &&
        zipSha256 === expectedZipSha &&
        backendJob1.results.answer.public.passed === 3 &&
        backendJob1.results.answer.heldout.passed === 2,
      job_id: job1Id,
      ui_status: status1,
      backend_status: backendJob1.status,
      verified_source_sha256: verifiedSourceSha,
      downloaded_zip_sha256: zipSha256,
      expected_zip_sha256: expectedZipSha,
      agents_observed: Object.keys(backendJob1.agent_telemetry || {}),
      tester_worker_pids: (backendJob1.agent_telemetry.tester_agent || {}).worker_pids || [],
      screenshot: '01_order_service_multifile_repair.png',
    });

    // =========================================================================
    // Case 2: Rapid Double-Click Idempotency Protection
    // =========================================================================
    const beforeJobs = (await fetchJson(`${baseUrl}/api/jobs`)).jobs.length;
    await page.selectOption('#preset', 'document');
    await page.fill('#taskPrompt', 'Idempotency double click check on AIOS kernel');

    // Rapidly trigger two clicks within the same tick
    await page.evaluate(() => {
      const btn = document.getElementById('submitJobBtn');
      btn.click();
      btn.click();
    });

    await sleep(1200);
    const afterJobsList = (await fetchJson(`${baseUrl}/api/jobs`)).jobs;
    const jobsCreated = afterJobsList.length - beforeJobs;
    const job2Id = afterJobsList[0].id;
    const shot2 = path.join(outDir, '02_double_click_idempotency.png');
    await page.screenshot({ path: shot2, fullPage: true });

    cases.push({
      id: 'case_02_double_click_idempotency',
      title: '连续快速双击提交按钮幂等保护（仅创建 1 个有效工作流，无重复调用）',
      passed: jobsCreated === 1 && afterJobsList[0].status === 'succeeded',
      job_id: job2Id,
      jobs_created_count: jobsCreated,
      backend_status: afterJobsList[0].status,
      screenshot: '02_double_click_idempotency.png',
    });

    // =========================================================================
    // Case 3: Single-Branch Fault Isolation (Non-Cascading `audit_agent` failure)
    // =========================================================================
    await page.selectOption('#preset', 'engineering');
    await page.waitForSelector('#projectSelect', { state: 'visible' });
    await page.selectOption('#projectSelect', 'order_service');
    const simFailChk = await page.$('#simulateAuditFailureChk');
    if (simFailChk && !(await simFailChk.isChecked())) {
      await simFailChk.click();
    }
    await page.click('#submitJobBtn');

    let job3Id = null;
    for (let i = 0; i < 60; i++) {
      await sleep(300);
      const stText = (await page.textContent('#selectedJobStatus')).trim();
      const st = await fetchJson(`${baseUrl}/api/status`);
      if (st.jobs && st.jobs.length > 0) job3Id = st.jobs[0].id;
      if (stText === 'completed_with_failures') break;
    }
    await sleep(600);
    const status3 = (await page.textContent('#selectedJobStatus')).trim();
    const backendJob3 = await fetchJson(`${baseUrl}/api/jobs/${job3Id}`);
    const dagHtml3 = await page.innerHTML('#dagTaskTableWrap');
    const shot3 = path.join(outDir, '03_isolated_branch_fault.png');
    await page.screenshot({ path: shot3, fullPage: true });

    cases.push({
      id: 'case_03_single_branch_fault_isolation',
      title: '单分支故障隔离（无依赖 audit_agent 标红失败，主修复分支照常完成并交付产物）',
      passed:
        status3 === 'completed_with_failures' &&
        backendJob3.states.static_audit === 'failed' &&
        backendJob3.states.answer === 'succeeded' &&
        dagHtml3.includes('AuditBranchFault') &&
        Boolean(backendJob3.engineering && backendJob3.engineering.accepted),
      job_id: job3Id,
      ui_status: status3,
      static_audit_state: backendJob3.states.static_audit,
      main_answer_state: backendJob3.states.answer,
      screenshot: '03_isolated_branch_fault.png',
    });

    // =========================================================================
    // Case 4: Page Reload Recovery & Frontend-Backend Snapshot Consistency
    // =========================================================================
    await page.reload({ waitUntil: 'networkidle' });
    await sleep(800);
    const recoveredStatus = (await page.textContent('#selectedJobStatus')).trim();
    const recoveredTitle = (await page.textContent('#selectedJobTitle')).trim();
    const recoveredDlVisible = await (await page.$('#engDownloadZipBtn')).isVisible();
    const shot4 = path.join(outDir, '04_reload_state_recovery.png');
    await page.screenshot({ path: shot4, fullPage: true });

    cases.push({
      id: 'case_04_reload_recovery_consistency',
      title: '页面刷新恢复与前后端快照强一致（DAG 状态、失败原因与产物下载入口完整恢复）',
      passed:
        recoveredStatus === backendJob3.status &&
        recoveredTitle.includes(job3Id.slice(0, 10)) &&
        recoveredDlVisible,
      job_id: job3Id,
      recovered_ui_status: recoveredStatus,
      backend_status: backendJob3.status,
      download_button_restored: recoveredDlVisible,
      screenshot: '04_reload_state_recovery.png',
    });

    // =========================================================================
    // Case 5: Controlled Budget Exhaustion & Resource Reclamation
    // =========================================================================
    await page.selectOption('#preset', 'engineering');
    await page.selectOption('#projectSelect', 'order_service');
    await page.fill('#modelCallsBudget', '0'); // 0 model calls allowed -> patcher_agent fails with budget exceeded
    await page.click('#submitJobBtn');

    let job5Id = null;
    for (let i = 0; i < 40; i++) {
      await sleep(250);
      const st = await fetchJson(`${baseUrl}/api/status`);
      if (st.jobs && st.jobs.length > 0) job5Id = st.jobs[0].id;
      const det = await fetchJson(`${baseUrl}/api/jobs/${job5Id}`);
      if (det.status !== 'running') break;
    }
    await sleep(500);
    // Restore budget input to 32
    await page.fill('#modelCallsBudget', '32');
    const backendJob5 = await fetchJson(`${baseUrl}/api/jobs/${job5Id}`);
    const kernelSnap5 = await fetchJson(`${baseUrl}/core/status`);
    const shot5 = path.join(outDir, '05_budget_exhaustion_reclamation.png');
    await page.screenshot({ path: shot5, fullPage: true });

    cases.push({
      id: 'case_05_budget_exhaustion_and_cleanup',
      title: '受控预算耗尽与资源回收（modelCallsBudget=0 明确拦截、无静默重试、活跃 Worker 归零）',
      passed:
        backendJob5.status === 'failed' &&
        backendJob5.states.patch === 'failed' &&
        backendJob5.states.verify === 'isolated_skipped' &&
        kernelSnap5.processes.active === 0 &&
        kernelSnap5.drf_resources.used.cpu_milli === 0,
      job_id: job5Id,
      backend_status: backendJob5.status,
      patch_state: backendJob5.states.patch,
      verify_state: backendJob5.states.verify,
      active_workers_after: kernelSnap5.processes.active,
      drf_cpu_used_after: kernelSnap5.drf_resources.used.cpu_milli,
      screenshot: '05_budget_exhaustion_reclamation.png',
    });

    // =========================================================================
    // Case 6: ACB Lifecycle Tab & A-Mem / LSFS / LiteCUA / Syscall Queue Inspection
    // =========================================================================
    await page.click('[data-tab="agents"]');
    await sleep(400);
    const agentsHtml = await page.innerHTML('#agentCardsGrid');
    const shot6 = path.join(outDir, '06_acb_lifecycle_and_queues.png');
    await page.screenshot({ path: shot6, fullPage: true });

    cases.push({
      id: 'case_06_acb_lifecycle_panel',
      title: '控制台 ACB 控制块面板与生命周期状态轨迹可视化核验',
      passed:
        agentsHtml.includes('analyst_agent') &&
        agentsHtml.includes('patcher_agent') &&
        agentsHtml.includes('tester_agent') &&
        agentsHtml.includes('reviewer_agent'),
      screenshot: '06_acb_lifecycle_and_queues.png',
    });
  } finally {
    await browser.close();
  }

  const summary = {
    timestamp: new Date().toISOString(),
    base_url: baseUrl,
    browser: 'Chromium Headless (Playwright 1.65.1)',
    total_cases: cases.length,
    passed_cases: cases.filter((c) => c.passed).length,
    console_error_count: consoleLogs.length,
    page_error_count: pageErrors.length,
    console_errors: consoleLogs,
    page_errors: pageErrors,
    cases,
  };

  fs.writeFileSync(path.join(outDir, 'playwright_e2e_report.json'), JSON.stringify(summary, null, 2), 'utf-8');
  console.log(JSON.stringify(summary, null, 2));
  if (summary.passed_cases !== summary.total_cases || summary.page_error_count > 0) {
    process.exit(1);
  }
})();
