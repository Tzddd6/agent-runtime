/** Browser acceptance. All writes go through the UI; API reads corroborate the rendered state. */
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { chromium, expect } = require('playwright/test');

const baseUrl = process.env.AIOS_BASE_URL;
const outDir = process.env.AIOS_E2E_OUT_DIR;
const terminal = ['succeeded', 'completed_with_failures', 'failed', 'interrupted', 'needs_reconciliation'];
const sha256 = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

async function main() {
  if (!baseUrl || !outDir) throw new Error('Use tools/run_playwright_e2e_acceptance.py to start the suite');
  const browser = await chromium.launch({
    executablePath: process.env.AIOS_E2E_CHROMIUM || undefined,
    headless: true,
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, acceptDownloads: true });
  const page = await context.newPage();
  const summary = {
    timestamp: new Date().toISOString(),
    browser: `Chromium ${browser.version()} / Playwright ${require('playwright/package.json').version}`,
    console_errors: [], page_errors: [], cases: [],
  };
  page.on('console', msg => {
    if (msg.type() === 'error') summary.console_errors.push(msg.text());
  });
  page.on('pageerror', err => summary.page_errors.push(err.message));

  async function get(route) {
    const response = await context.request.get(baseUrl + route);
    assert.ok(response.ok(), `GET ${route} returned ${response.status()}`);
    return response.json();
  }

  async function submit(status, action = () => page.locator('#submitJobBtn').click(), timeout = 20000) {
    const response = page.waitForResponse(res =>
      new URL(res.url()).pathname === '/api/jobs' && res.request().method() === 'POST'
    );
    await action();
    const accepted = await response;
    assert.equal(accepted.status(), 202, await accepted.text());
    const { id } = await accepted.json();
    await expect(page.locator('#selectedJobTitle')).toContainText(id.slice(0, 10), { timeout: 15000 });
    await expect(page.locator('#selectedJobStatus')).toHaveAttribute('data-status', new RegExp(`^(${terminal.join('|')})$`), { timeout });
    const job = await get('/api/jobs/' + id);
    assert.equal(await page.locator('#selectedJobStatus').getAttribute('data-status'), status, JSON.stringify(job.states));
    assert.equal(job.status, status);
    return job;
  }

  async function configureEngineering(project, { audit = true, fault = false, calls = 32, interrupt = false } = {}) {
    await page.locator('#preset').selectOption('engineering');
    await page.locator('#projectSelect').selectOption(project);
    await page.locator('#fixedModelChk').check();
    await page.locator('#interruptVerificationChk').setChecked(interrupt);
    const options = page.locator('#jobOptions');
    const toggle = options.locator('summary');
    if (!(await options.evaluate(element => element.open))) await toggle.click();
    await page.locator('#includeAuditBranchChk').setChecked(audit);
    await page.locator('#simulateAuditFailureChk').setChecked(fault);
    await page.locator('#modelCallsBudget').fill(String(calls));
    await toggle.click();
  }

  async function download(job) {
    await expect(page.locator('#engVerdictBadge')).toContainText('通过');
    const [event] = await Promise.all([
      page.waitForEvent('download'),
      page.locator('#engDownloadZipBtn').click(),
    ]);
    const filename = `${job.engineering.project_id}-${job.id.slice(0, 10)}.zip`;
    const destination = path.join(outDir, filename);
    await event.saveAs(destination);
    const digest = sha256(fs.readFileSync(destination));
    assert.equal(digest, job.engineering.artifact.zip_sha256);
    assert.equal(job.engineering.artifact.readback_verified, true);
    return { zip: filename, zip_sha256: digest, source_sha256: job.engineering.source_sha256,
      project: job.engineering.project_id, job_id: job.id, backend_status: job.status };
  }

  async function run(id, title, operation) {
    const row = { id, title, passed: false, screenshot: id + '.png' };
    summary.cases.push(row);
    try {
      Object.assign(row, await operation());
      row.passed = true;
    } catch (error) {
      row.error = error.stack || String(error);
    } finally {
      await page.screenshot({ path: path.join(outDir, row.screenshot), fullPage: true });
    }
  }

  let isolated;
  try {
    await page.goto(baseUrl + '/console/');
    await run('01_order_service', '内置多文件修复、Agent 记录与浏览器下载', async () => {
      await configureEngineering('order_service');
      const job = await submit('succeeded');
      for (const role of ['analyst', 'patcher', 'tester', 'reviewer', 'audit']) {
        assert.ok(job.agent_telemetry[role + '_agent']);
      }
      assert.ok(job.agent_telemetry.tester_agent.worker_pids.length > 0);
      assert.ok(job.context_refs.inspect && job.context_refs.plan);
      assert.equal(job.results.answer.public.passed, 3);
      assert.equal(job.results.answer.heldout.passed, 2);
      await page.locator('#pinJobBtn').click();
      await expect(page.locator('#pinJobBtn')).toHaveText('取消保留');
      assert.equal((await get('/api/jobs/' + job.id)).pinned, true);
      return { ...await download(job), worker_pids: job.agent_telemetry.tester_agent.worker_pids,
        public_passed: 3, heldout_passed: 2 };
    });

    await run('02_double_click', '快速双击仅生成一个任务', async () => {
      const before = (await get('/api/jobs')).jobs.length;
      await page.locator('#preset').selectOption('document');
      await page.locator('#taskPrompt').fill('Browser idempotency acceptance');
      await page.locator('#workflowDeadline').fill('5000');
      let submissions = 0;
      const count = request => {
        if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/jobs') submissions++;
      };
      page.on('request', count);
      try {
        const job = await submit('succeeded', () => page.locator('#submitJobBtn').evaluate(button => {
          button.click();
          button.click();
        }));
        assert.equal(submissions, 1);
        assert.equal(job.deadline_ms, 5000);
        assert.equal(job.deadline_met, true);
        await expect(page.locator('#dagTaskTableWrap')).toContainText('派发决策');
        assert.equal((await get('/api/jobs')).jobs.length, before + 1);
        return { job_id: job.id, backend_status: job.status, submissions };
      } finally {
        page.off('request', count);
      }
    });

    await run('03_audit_fault', '审计分支故障隔离', async () => {
      await page.locator('#workflowDeadline').fill('49');
      await configureEngineering('order_service', { fault: true });
      await expect(page.locator('#workflowDeadline')).toBeDisabled();
      isolated = await submit('completed_with_failures');
      assert.equal(isolated.states.static_audit, 'failed');
      assert.equal(isolated.states.answer, 'succeeded');
      await expect(page.locator('#dagTaskTableWrap')).toContainText('AuditBranchFault');
      await expect(page.locator('#engDownloadZipBtn')).toBeVisible();
      return { job_id: isolated.id, backend_status: isolated.status };
    });

    await run('04_reload', '刷新后恢复原任务状态和下载入口', async () => {
      assert.ok(isolated);
      await page.reload();
      await expect(page.locator('#selectedJobTitle')).toContainText(isolated.id.slice(0, 10));
      await expect(page.locator('#selectedJobStatus')).toHaveAttribute('data-status', isolated.status);
      await expect(page.locator('#engDownloadZipBtn')).toBeVisible();
      return { job_id: isolated.id, backend_status: isolated.status };
    });

    await run('05_budget', '预算耗尽后隔离下游并归还资源', async () => {
      await configureEngineering('order_service', { audit: false, calls: 0 });
      const job = await submit('failed');
      const snapshot = await get('/core/status');
      assert.equal(job.states.patch, 'failed');
      assert.equal(job.states.verify, 'isolated_skipped');
      assert.equal(job.model_budget.calls, 0);
      assert.equal(snapshot.processes.active, 0);
      assert.deepEqual(snapshot.drf_resources.used, { cpu_milli: 0, memory_bytes: 0, pids: 0 });
      await expect(page.locator('#dagTaskTableWrap')).toContainText('model budget exceeded');
      return { job_id: job.id, backend_status: job.status, used_after: snapshot.drf_resources.used };
    });

    await run('06_acb', 'Agent 面板显示修复角色', async () => {
      await page.locator('#kernelNav > summary').click();
      await page.locator('#topTabs [data-tab="agents"]').click();
      for (const role of ['analyst', 'patcher', 'tester', 'reviewer']) {
        await expect(page.locator('#agentCardsGrid')).toContainText(role + '_agent');
      }
      await page.locator('#topTabs [data-tab="workflow"]').click();
    });

    await run('07_upstream_sample', '上游样本复现失败、修复并下载', async () => {
      await configureEngineering('json_pointer', { audit: false });
      const job = await submit('succeeded');
      assert.equal(job.results.baseline.public.failures, 2);
      assert.equal(job.results.answer.public.passed, 3);
      assert.equal(job.results.answer.heldout.passed, 3);
      assert.equal(job.results.answer.tests_intact, true);
      assert.equal(job.engineering.provenance.release, 'v3.0.0');
      assert.equal(job.engineering.model_mode, 'fixed_patch');
      await expect(page.locator('#engRunDetails')).toContainText('固定补丁桩');
      await expect(page.locator('#engProjectTitle')).toContainText('python-json-pointer');
      return { ...await download(job), provenance: job.engineering.provenance,
        baseline_public_failures: 2, public_passed: 3, heldout_passed: 3 };
    });

    await run('09_verified_recovery', '沿用原补丁恢复被终止的验证 Worker', async () => {
      await configureEngineering('json_pointer', { audit: false, interrupt: true });
      const stopped = await submit('interrupted');
      assert.equal(stopped.states.patch, 'succeeded');
      assert.equal(stopped.states.verify, 'interrupted');
      assert.equal(stopped.model_budget.calls, 1);
      const button = page.locator('#resumeJobBtn');
      await expect(button).toBeVisible();
      const [response] = await Promise.all([
        page.waitForResponse(res => new URL(res.url()).pathname === `/api/jobs/${stopped.id}/resume`
                            && res.request().method() === 'POST'),
        button.click(),
      ]);
      assert.equal(response.status(), 202);
      await expect(page.locator('#selectedJobStatus')).toHaveAttribute('data-status', 'succeeded', { timeout: 20000 });
      await expect(page.locator('#engRecoveryDetails')).toContainText('新增模型调用 0 次');
      const job = await get('/api/jobs/' + stopped.id);
      const proof = job.engineering.execution_evidence;
      assert.equal(job.context_refs.patch, stopped.context_refs.patch);
      assert.equal(job.attempts.patch, 1);
      assert.equal(job.attempts.verify, 2);
      assert.equal(proof.model_calls_during_resume, 0);
      assert.equal(proof.verified_source_sha256, proof.model.patch.candidate_sha256);
      return { ...await download(job), execution_evidence: proof };
    });

    await run('10_cross_workflow_scheduling', '跨工作流调度三策略对照与逐轮结果', async () => {
      await page.locator('#preset').selectOption('workflow_scheduling');
      const job = await submit('succeeded', undefined, 100000);
      const result = job.workflow_scheduling;
      assert.equal(result.verified, true);
      assert.equal(result.trials.length, 6);
      for (const summary of Object.values(result.summaries)) assert.equal(summary.sample_size, 120);
      assert.ok(result.trials.every(trial => trial.peak_active <= result.slots && trial.resources_released));
      const panel = page.locator('#schedulingResult');
      await expect(panel).toContainText('截止达成率');
      await expect(panel).toContainText('关键路径 + 老化');
      await panel.locator('summary').first().click();
      await expect(panel.locator('details').first()).toHaveAttribute('open', '');
      return { job_id: job.id, backend_status: job.status, summaries: result.summaries };
    });

    await run('08_attachments', '附件上传、边界提示与 LSFS 流式会话完成', async () => {
      await page.goto(baseUrl + '/');
      const form = page.locator('#chatForm');
      const upload = form.locator('input[type=file]');
      const feedback = form.getByRole('status');
      const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGM0CKlgYGBgYmBgYGBgAAALsgEAK4hfYgAAAABJRU5ErkJggg==', 'base64');
      await upload.setInputFiles({ name: 'image.png', mimeType: 'image/png', buffer: png });
      await expect(feedback).toHaveText('已上传 1 个附件');
      await upload.setInputFiles({ name: 'empty.txt', mimeType: 'text/plain', buffer: Buffer.alloc(0) });
      await expect(feedback).toHaveText('附件不能为空');
      await upload.setInputFiles({ name: 'large.txt', mimeType: 'text/plain', buffer: Buffer.alloc(8 * 1024 * 1024 + 1) });
      await expect(feedback).toContainText('单文件最多 8 MiB');
      await page.locator('#chatMode').selectOption('lsfs');
      await page.locator('#chatPrompt').fill("write 'browser acceptance' to e2e.txt");
      await page.locator('#sendChatBtn').click();
      await expect(page.locator('#messagesBox')).toContainText('mapped_syscall');
      await expect(page.locator('#messagesBox')).toContainText('文件操作 · 本地文件工具');
    });
    await run('11_theme_and_navigation', '四套主题换色、刷新保留与核心场景入口', async () => {
      await page.goto(baseUrl + '/');
      const themes = ['ink-classic', 'swiss-ikb', 'forest-ink', 'midnight-ink'];
      const colors = [];
      for (const theme of themes) {
        await page.locator('#themeSwitcher').selectOption(theme);
        await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
        colors.push(await page.locator('body').evaluate(body => getComputedStyle(body).backgroundColor));
      }
      assert.equal(new Set(colors).size, themes.length);
      await page.reload();
      await expect(page.locator('#themeSwitcher')).toHaveValue('midnight-ink');
      await page.getByRole('link', { name: /断点恢复/ }).click();
      await expect(page.locator('#themeSwitcher')).toHaveValue('midnight-ink');
      assert.equal(await page.locator('body').evaluate(body => getComputedStyle(body).backgroundColor), colors[3]);
      await expect(page.locator('#projectSelect')).toHaveValue('json_pointer');
      await expect(page.locator('#fixedModelChk')).toBeChecked();
      await expect(page.locator('#interruptVerificationChk')).toBeChecked();
      await expect(page.locator('#jobOptions')).not.toHaveAttribute('open', '');
      await page.getByRole('button', { name: /^调度对照/ }).click();
      await expect(page.locator('#preset')).toHaveValue('workflow_scheduling');
      await page.locator('#themeSwitcher').selectOption('forest-ink');
      assert.equal(await page.locator('body').evaluate(body => getComputedStyle(body).backgroundColor), colors[2]);
      await page.getByRole('link', { name: '对话', exact: true }).click();
      await expect(page.locator('#themeSwitcher')).toHaveValue('forest-ink');
      await page.getByRole('link', { name: '模型设置', exact: true }).click();
      await expect(page.locator('#apiProfilesPanel')).toBeVisible();
      await expect(page.locator('#jobForm')).toBeHidden();
      return { themes, background_colors: colors };
    });

    await run('12_mobile_layout', '窄屏的核心入口、历史和输入区', async () => {
      await page.setViewportSize({ width: 390, height: 844 });
      const noOverflow = () => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth);
      await page.goto(baseUrl + '/');
      await expect(page.locator('#chatPrompt')).toBeVisible();
      await expect(page.locator('#chatHistory')).not.toHaveAttribute('open', '');
      assert.ok(await noOverflow());
      await page.screenshot({ path: path.join(outDir, '12_mobile_chat.png'), fullPage: true });
      await page.getByRole('link', { name: /调度对照/ }).click();
      await expect(page.locator('#preset')).toHaveValue('workflow_scheduling');
      await expect(page.locator('#submitJobBtn')).toBeVisible();
      assert.ok(await noOverflow());
    });

    await run('13_model_settings_and_chat_modes', '对话模式、默认槽位和启用模型', async () => {
      await page.setViewportSize({ width: 1440, height: 960 });
      await page.goto(baseUrl + '/');
      await expect(page.locator('#chatMode')).toHaveValue('llm');
      await page.locator('#chatPrompt').fill('Which model would answer this?');
      await page.locator('#sendChatBtn').click();
      await expect(page.locator('#chatForm').getByRole('status')).toContainText('启用模型');
      await page.getByRole('link', { name: '模型设置', exact: true }).first().click();
      await expect(page.locator('#activeModelConnection')).toContainText('内置默认槽位未配置密钥');
      await page.locator('#profileEditorLabel').click();
      await expect(page.locator('#profModel')).toHaveValue('deepseek-flash');
      await expect(page.locator('#profKey')).toBeVisible();
      await expect(page.locator('#profId')).toBeHidden();
      await page.locator('#profModel').fill('deepseek_chat');
      await expect(page.locator('#profileModelHint')).toContainText('deepseek-flash');
      await page.locator('#profModel').fill('deepseek-flash');
      const key = 'synthetic-browser-profile-value';
      await page.locator('#profKey').fill(key);
      await page.getByRole('button', { name: '保存并启用' }).click();
      await expect(page.locator('#activeModelSummary')).toHaveText('DeepSeek · deepseek-flash');
      await expect(page.locator('#profKey')).toHaveValue('');
      const profiles = await get('/api/model-profiles');
      assert.match(profiles.selected, /^model_[a-z0-9-]{13}$/);
      assert.ok(!JSON.stringify(profiles).includes(key));
      await page.getByRole('link', { name: '对话', exact: true }).click();
      await expect(page.locator('#chatModelBadge')).toContainText('deepseek-flash · 连接未验证');
      await page.locator('#chatMode').selectOption('lsfs');
      await expect(page.locator('#chatModelBadge')).toHaveText('本地文件工具');
      await page.locator('#chatMode').selectOption('llm');
      await expect(page.locator('#chatModelBadge')).toContainText('deepseek-flash');
    });
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(outDir, 'playwright_e2e_report.json'), JSON.stringify(summary, null, 2));
  }
  if (summary.cases.some(row => !row.passed) || summary.page_errors.length || summary.console_errors.length) {
    process.exitCode = 1;
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
