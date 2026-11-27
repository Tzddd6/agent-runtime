(function () {
  function escapeText(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  }
  let csrfToken = "";
  let selectedJobId = null;
  let noticeTimer;
  let historyKey;
  let modelProfiles = { profiles: [], selected: null };
  const providerUrls = {
    deepseek: "https://api.deepseek.com/v1", openai: "https://api.openai.com/v1",
    aizex: "https://ca.memofun.net/v1", ollama: "http://127.0.0.1:11434/v1", vllm: "http://127.0.0.1:8000/v1"
  };
  const oldDeepseekNames = new Set(["deepseek_chat", "deepseek-chat", "deepseek_reasoner", "deepseek-reasoner"]);
  let cachedCapabilities = { plugins: [], skills: [] };

  const PRESET_HINTS = {
    engineering: "运行隔离测试并核验交付包。",
    workflow_scheduling: "同一批负载比较三种策略。使用离线模型桩。",
    scheduling: "比较同一工作流内的 FIFO 与 DRF。",
    resources: "运行 CPU 与内存负载。",
    document: "统计字符、词数与行数。"
  };

  const STATUS_LABELS = {
    running: "运行中", succeeded: "完成", failed: "失败", completed_with_failures: "有分支失败",
    interrupted: "已中断", needs_reconciliation: "待核对"
  };

  function sceneLabel(value) {
    return [...document.getElementById("preset").options].find(option => option.value === value)?.textContent
      || { lsfs: "文件操作", auto: "对话" }[value] || value;
  }

  function switchTab(tabName) {
    document.querySelectorAll(".tab-pane").forEach((el) => {
      el.style.display = el.id === "pane-" + tabName ? "block" : "none";
    });
    document.querySelectorAll("[data-tab]").forEach((btn) => {
      const selected = btn.dataset.tab === tabName;
      btn.classList.toggle("active", selected);
      btn.setAttribute("aria-pressed", String(selected));
    });
    const kernelNav = document.getElementById("kernelNav");
    kernelNav.classList.toggle("is-current", !["workflow", "models"].includes(tabName));
    kernelNav.open = false;
  }

  async function apiGet(url) {
    const res = await fetch(url, { headers: { "Accept": "application/json" } });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  }

  async function apiPost(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AgentRT-CSRF": csrfToken
      },
      body: JSON.stringify(body || {})
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    return data;
  }

  function showNotice(msg, isErr = false) {
    const box = document.getElementById("globalNotice");
    if (!box) return;
    clearTimeout(noticeTimer);
    box.hidden = false;
    box.dataset.error = String(isErr);
    box.textContent = msg;
    noticeTimer = setTimeout(() => { box.hidden = true; }, 6000);
  }

  const ms = (value) => Number.isFinite(value) ? value.toFixed(1) + " ms" : "—";
  const workflowKinds = { short: "短问答", long: "长测试", mixed: "混合 DAG" };
  const policyNames = { fifo: "FIFO", drf: "DRF", critical_path: "关键路径 + 老化" };

  function renderWorkflowComparison(box, result, jobId) {
    const rows = Object.entries(result.summaries).map(([policy, row]) => {
      const ratio = (100 * row.deadline_met / row.sample_size).toFixed(1);
      return `<tr><td>${policyNames[policy]}</td><td>${ms(row.workflow_p95_ms)}</td>
        <td>${ratio}% <small>(${Number(row.deadline_met)}/${Number(row.sample_size)})</small></td>
        <td>${ms(row.max_long_wait_ms)}</td></tr>`;
    }).join("");
    const trials = result.trials.map((trial, index) => {
      const jobs = trial.workflows.map((job) => {
        const nodes = job.nodes.map((node) => `${escapeText(node.id)} ${ms(node.ready_wait_ms)}` +
          (node.reason === "aging" ? "（老化）" : "")).join(" · ");
        return `<tr><td>${escapeText(job.id)} · ${workflowKinds[job.kind]}</td><td>${ms(job.planned_ms)}</td>
          <td>${ms(job.latency_ms)}</td><td>${job.deadline_met ? "达成" : "未达成"}</td><td>${nodes}</td></tr>`;
      }).join("");
      return `<details style="margin-top:8px;"><summary>第 ${index + 1} 轮 · ${policyNames[trial.policy]}
        · 峰值 ${Number(trial.peak_active)} 个 Worker · ${trial.resources_released ? "资源已归还" : "仍有占用"}</summary>
        <table class="swiss-table"><thead><tr><th>工作流</th><th>计划到达</th><th>完成耗时</th>
        <th>截止目标</th><th>节点排队时长</th></tr></thead><tbody>${jobs}</tbody></table></details>`;
    }).join("");
    box.innerHTML = `<p class="hint">每种策略 ${Number(result.jobs_per_trial) * 2} 个任务 · ${Number(result.slots)} 个共享 Worker ·
      ${result.same_outputs ? "产出一致" : "产出核验失败"} · <a href="/api/jobs/${encodeURIComponent(jobId)}" download>下载 JSON</a></p>
      <table class="swiss-table"><thead><tr><th>策略</th><th>完成 P95</th><th>截止达成率</th>
        <th>长测试最大等待</th></tr></thead><tbody>${rows}</tbody></table>${trials}`;
  }

  function renderScheduling(detail) {
    const box = document.getElementById("schedulingResult");
    const crossWorkflow = detail.preset === "workflow_scheduling";
    const result = crossWorkflow ? detail.workflow_scheduling : detail.scheduling;
    box.hidden = !crossWorkflow && detail.preset !== "scheduling";
    if (box.hidden) {
      box.replaceChildren();
      delete box.dataset.rendered;
      return;
    }
    const key = detail.id + ":" + detail.status;
    if (box.dataset.rendered === key) return;
    box.dataset.rendered = key;
    if (!result) {
      box.textContent = detail.error || (detail.status === "running" ? "正在运行调度试验…" : "暂无对照结果");
      return;
    }
    if (crossWorkflow) {
      renderWorkflowComparison(box, result, detail.id);
      return;
    }
    const policyRows = Object.entries(result.summaries).map(([policy, row]) => `<tr>
      <td>${escapeText(policy.toUpperCase())}</td><td>${ms(row.short_first_ms)}</td>
      <td>${ms(row.short_p95_ms)}</td><td>${ms(row.duration_ms)}</td>
    </tr>`).join("");
    const trials = result.trials.map((trial, index) => {
      const events = trial.timeline.map((event) => `<tr>
        <td>${escapeText(event.id)}</td><td>${ms(event.wait_ms)}</td><td>${ms(event.finish_ms)}</td>
        <td>${(100 * Number(event.dominant_share)).toFixed(1)}%</td>
      </tr>`).join("");
      const used = trial.used_after;
      return `<details style="margin-top:8px;">
        <summary>第 ${index + 1} 次 · ${escapeText(trial.policy.toUpperCase())}
          · 长任务完成 ${Number(trial.long_completed)} / ${Number(result.workload.long.count)}
          · 短任务完成 ${Number(trial.short_completed)} / ${Number(result.workload.short.count)}
          · ${trial.resources_released ? "配额已归还" : "仍有占用"}</summary>
        <p class="hint">结束占用 ${Number(used.cpu_milli)} mCPU · ${Number(used.memory_bytes)} B · ${Number(used.pids)} PIDs</p>
        <table class="swiss-table"><thead><tr><th>节点</th><th>开始 / 排队时间</th><th>完成时刻</th><th>开始时主导份额</th></tr></thead>
        <tbody>${events}</tbody></table>
      </details>`;
    }).join("");
    box.innerHTML = `<p class="hint">每种策略运行两次。表中取两次指标的中位数；展开试验可查看同批入队后的各节点时间。
      ${result.same_outputs ? "四次输出一致。" : "输出核验失败。"} <a href="/api/jobs/${encodeURIComponent(detail.id)}" download>下载原始 JSON</a></p>
      <table class="swiss-table"><thead><tr><th>策略</th><th>首个短任务等待</th><th>短任务 P95 等待</th><th>总耗时</th></tr></thead>
      <tbody>${policyRows}</tbody></table>${trials}`;
  }

  function renderJobDetail(detail) {
    document.getElementById("selectedJobTitle").textContent =
      sceneLabel(detail.preset) + " · " + detail.id.slice(0, 10);
    const stBadge = document.getElementById("selectedJobStatus");
    stBadge.dataset.status = detail.status;
    stBadge.textContent = STATUS_LABELS[detail.status] || detail.status;
    stBadge.className = "tag " + (detail.status === "succeeded" ? "tag-ok" : detail.status === "failed" ? "tag-err" : "tag-warn");

    const resume = document.getElementById("resumeJobBtn");
    resume.style.display = detail.status === "interrupted" && detail.recovery_available ? "" : "none";
    resume.dataset.jobId = detail.id;
    const pin = document.getElementById("pinJobBtn");
    pin.style.display = detail.status === "running" ? "none" : "";
    pin.textContent = detail.pinned ? "取消保留" : "保留记录";
    pin.setAttribute("aria-pressed", String(Boolean(detail.pinned)));
    pin.dataset.jobId = detail.id;
    const error = document.getElementById("selectedJobError");
    const isScheduling = ["scheduling", "workflow_scheduling"].includes(detail.preset);
    error.textContent = isScheduling ? "" : (detail.error || "");
    error.hidden = !error.textContent;
    const jobMeta = document.getElementById("selectedJobMeta");
    jobMeta.hidden = isScheduling;
    jobMeta.textContent = detail.policy === "critical_path"
      ? `关键路径调度${detail.deadline_ms == null ? "" :
          ` · 目标 ${ms(detail.deadline_ms)}${detail.deadline_met == null ? "" : detail.deadline_met ? " · 已达成" : " · 未达成"}`}`
      : "";
    jobMeta.hidden = !jobMeta.textContent;
    document.getElementById("dagTaskTableWrap").hidden = isScheduling;
    document.getElementById("workflowDetails").hidden = isScheduling || !detail.events?.length;
    renderScheduling(detail);

    const states = detail.states || {};
    const summary = document.getElementById("resultSummary");
    const metrics = [];
    const stateValues = Object.values(states);
    if (stateValues.length) {
      metrics.push(["节点完成", `${stateValues.filter(value => value === "succeeded").length}/${stateValues.length}`]);
    }
    if (detail.engineering?.accepted) metrics.push(["源码校验", "通过"]);
    if (detail.model_budget?.calls != null) {
      const resumed = detail.engineering?.execution_evidence?.resumed;
      metrics.push([resumed ? "恢复新增模型调用" : "模型调用",
        resumed ? detail.engineering.execution_evidence.model_calls_during_resume : detail.model_budget.calls]);
    }
    summary.hidden = isScheduling || !metrics.length;
    summary.innerHTML = metrics.map(([label, value]) =>
      `<div><span>${label}</span> <strong>${escapeText(value)}</strong></div>`).join("");
    const attempts = detail.attempts || {};
    const refs = detail.context_refs || {};
    const graphNodes = detail.graph || [];
    const nodeMap = {};
    graphNodes.forEach((n) => { nodeMap[n.id] = n; });
    const resultsMap = detail.results || {};
    const decisions = Object.fromEntries((detail.events || [])
      .filter(event => event.dispatch).map(event => [event.task_id, event.dispatch]));
    const hasDecisions = Object.keys(decisions).length > 0;

    const rows = Object.keys(states).map((tid) => {
      const st = states[tid];
      const cls = st === "succeeded" ? "tag-ok" : (st === "failed" || st === "isolated_skipped") ? "tag-err" : "tag-warn";
      const nodeInfo = nodeMap[tid] || {};
      const agName = nodeInfo.agent || tid;
      const dispatch = decisions[tid];
      const reason = dispatch?.reason === "aging" ? "等待老化" : "关键路径";
      const decisionText = dispatch ? `${reason} · 等待 ${ms(dispatch.ready_wait_ms)} · 路径 ${ms(dispatch.path_ms)}` +
        (dispatch.slack_ms == null ? "" : ` · 裕量 ${ms(dispatch.slack_ms)}`) : "—";
      const deps = (nodeInfo.depends_on || []).map((d) => `${d}(${refs[d] ? refs[d].slice(0, 8) : "—"})`).join(" → ") || "root";
      const errText = (resultsMap[tid] && resultsMap[tid].error) ? `<div class="mono" style="color:var(--state-err);font-size:11px;margin-top:2px;">${escapeText(resultsMap[tid].error)}</div>` : "";
      return `<tr>
        <td class="mono"><strong>${escapeText(tid)}</strong>${errText}</td>
        <td class="mono">${escapeText(agName)}</td>
        <td><span class="tag ${cls}">${escapeText(st)}</span></td>
        <td class="mono">${attempts[tid] || 1}</td>
        <td class="mono" style="font-size:11px;">${escapeText(deps)}</td>
        <td class="mono" style="font-size:11px;">${refs[tid] ? escapeText(refs[tid].slice(0, 16)) + "…" : "—"}</td>
        ${hasDecisions ? `<td class="mono">${escapeText(decisionText)}</td>` : ""}
      </tr>`;
    }).join("");

    document.getElementById("dagTaskTableWrap").innerHTML = rows
      ? `<table class="swiss-table"><thead><tr><th>Task ID</th><th>Agent ID</th><th>State</th><th>Attempts</th><th>Upstream ctx_ref Lineage</th><th>Output ctx_ref (SHA-256)</th>${hasDecisions ? "<th>派发决策</th>" : ""}</tr></thead><tbody>${rows}</tbody></table>`
      : `<p class="hint">尚无任务节点状态</p>`;

    const engBox = document.getElementById("engineeringBox");
    if (detail.engineering) {
      engBox.style.display = "block";
      const eng = detail.engineering;
      document.getElementById("engProjectTitle").textContent = eng.title + " (" + eng.file + ")";
      const runDetails = document.getElementById("engRunDetails");
      const modelLabel = { fixed_patch: "固定补丁桩", configured_model: "已配置模型" }[eng.model_mode];
      const upstream = eng.provenance;
      runDetails.textContent = [
        modelLabel,
        upstream && `上游 ${upstream.release} · ${upstream.revision.slice(0, 12)} · ${upstream.license}`
      ].filter(Boolean).join(" · ");
      runDetails.hidden = !runDetails.textContent;
      const recovery = document.getElementById("engRecoveryDetails");
      const evidence = eng.execution_evidence;
      recovery.hidden = !evidence?.resumed;
      if (evidence?.resumed) {
        recovery.textContent = `沿用原补丁 · 新增模型调用 ${evidence.model_calls_during_resume} 次`;
        recovery.title = eng.source_sha256 || "";
      }
      document.getElementById("engVerdictBadge").textContent = eng.accepted ? "全部测试与哈希核验通过" : "未通过";
      document.getElementById("engOriginalCode").textContent = eng.original_source || "";
      document.getElementById("engPatchedCode").textContent = (eng.unified_diff ? (eng.unified_diff + "\n\n" + (eng.patched_source || "")) : (eng.patched_source || ""));

      const dlBtn = document.getElementById("engDownloadZipBtn");
      if (dlBtn) {
        if (eng.artifact && eng.artifact.zip_download_url) {
          dlBtn.style.display = "inline-block";
          dlBtn.href = eng.artifact.zip_download_url;
        } else {
          dlBtn.style.display = "none";
        }
      }

      const telemetry = detail.agent_telemetry || {};
      const telAgents = Object.values(telemetry);
      const telWrap = document.getElementById("engAgentTelemetryWrap");
      if (telWrap) {
        telWrap.innerHTML = telAgents.length
          ? `<h3>执行资源</h3>
             <table class="swiss-table"><thead><tr><th>Agent ID</th><th>ACB State</th><th>Worker PID</th><th>OS Isolation / cgroup Path</th><th>Quota (CPU / Mem)</th><th>Model Calls</th><th>Tokens (In / Out)</th></tr></thead><tbody>` +
            telAgents.map((ag) => `<tr>
              <td class="mono"><strong>${escapeText(ag.agent_id)}</strong></td>
              <td><span class="tag tag-ok">${escapeText(ag.state)}</span></td>
              <td class="mono">${ag.last_pid ? escapeText(ag.last_pid) : "in-kernel"}</td>
              <td class="mono" style="font-size:11px;">${escapeText(ag.cgroup_path || ag.enforcement)}</td>
              <td class="mono">${ag.cpu_milli_limit}m / ${((ag.memory_bytes_limit || 0) / (1024 * 1024)).toFixed(0)}MiB</td>
              <td class="mono">${ag.model_calls_used || 0}</td>
              <td class="mono">${ag.input_tokens_used || 0} / ${ag.output_tokens_used || 0}</td>
            </tr>`).join("") + `</tbody></table>`
          : "";
      }

      document.getElementById("engPhaseList").innerHTML = (eng.phases || []).map((p) => {
        const stCls = p.status === "succeeded" ? "tag-ok" : (p.status === "failed" || p.status === "isolated_skipped") ? "tag-err" : "tag-warn";
        return `<div class="info-card" style="border-left:${p.status === 'failed' ? '3px solid var(--state-err)' : '1px solid var(--border-hairline)'};">
          <div class="mono" style="font-size:11px;color:var(--ink-muted);">${escapeText(p.id)} · ${escapeText(p.agent || "")}</div>
          <h3>${escapeText(p.label)}</h3>
          <span class="tag ${stCls}">${escapeText(p.status)}</span>
          ${p.ctx_ref ? `<div class="mono" style="font-size:10.5px;margin-top:4px;color:var(--ink-secondary);">ctx_ref: ${escapeText(p.ctx_ref.slice(0, 12))}…</div>` : ""}
          ${p.error ? `<div class="mono" style="font-size:10.5px;margin-top:4px;color:var(--state-err);">${escapeText(p.error)}</div>` : ""}
        </div>`;
      }).join("");
    } else {
      engBox.style.display = "none";
    }

    const evs = detail.events || [];
    document.getElementById("eventCountLabel").textContent = evs.length + " 条事件";
    document.getElementById("workflowEventsPre").textContent =
      JSON.stringify({ results: detail.results, events: evs }, null, 2);
  }

  async function refreshStatus() {
    try {
      const st = await apiGet("/api/status");
      csrfToken = st.csrf || csrfToken;
      const connection = document.getElementById("connBadge");
      connection.textContent = "在线";
      connection.dataset.state = "online";
      document.getElementById("enforcementBadge").textContent = "资源模式: " + st.enforcement;
      document.getElementById("lastSampleTime").textContent = new Date().toLocaleTimeString();

      const kStatus = st.kernel_status || {};
      const drf = kStatus.drf_resources || {};
      const cap = drf.capacity || { cpu_milli: 4000 };
      const used = drf.used || { cpu_milli: 0 };
      document.getElementById("metricCpu").textContent = `${used.cpu_milli || 0} / ${cap.cpu_milli || 4000} mCPU`;
      document.getElementById("metricCpuSub").textContent = `逻辑核心: ${(st.machine && st.machine.host && st.machine.host.logical_cpus) || 4} 核`;

      const memLim = ((kStatus.components && kStatus.components.memory_limit_bytes) || 104857600) / (1024 * 1024);
      document.getElementById("metricMem").textContent = `${memLim.toFixed(1)} MiB`;

      const cStats = kStatus.context_store || {};
      const ratio = ((cStats.storage_saving_ratio || 0) * 100).toFixed(1);
      document.getElementById("metricCtx").textContent = `${ratio}%`;
      document.getElementById("metricCtxSub").textContent =
        `唯一对象: ${cStats.unique_objects || 0} · 去重命中: ${cStats.dedup_hits || 0}`;

      document.getElementById("metricSyscalls").textContent = String(kStatus.total_syscalls || 0);

      // Render ContextStore cards in Tab 02
      document.getElementById("contextStoreGrid").innerHTML = `
        <div class="info-card"><div class="mono">LOGICAL WRITTEN</div><h3>${cStats.logical_bytes_written || 0} B</h3><p class="hint">原始 JSON 累计逻辑写入字节</p></div>
        <div class="info-card"><div class="mono">PHYSICAL STORED</div><h3>${cStats.physical_bytes_stored || 0} B</h3><p class="hint">SHA256 去重与 Gzip 后物理落盘</p></div>
        <div class="info-card"><div class="mono">DEDUP & CACHE HITS</div><h3>${cStats.dedup_hits || 0} / ${cStats.cache_hits || 0}</h3><p class="hint">内容寻址复用与 LRU-2 命中</p></div>
      `;

      // Render Syscall table
      const syscalls = st.syscalls || [];
      const tbody = document.getElementById("syscallTableBody");
      if (syscalls.length) {
        tbody.innerHTML = syscalls.slice().reverse().slice(0, 40).map((sc) => `
          <tr>
            <td class="mono">#${sc.syscall_id}</td>
            <td class="mono">${sc.pid}</td>
            <td><strong>${sc.agent_name}</strong></td>
            <td><span class="tag">${sc.target}</span></td>
            <td><span class="tag ${sc.status === 'done' ? 'tag-ok' : 'tag-warn'}">${sc.status}</span></td>
            <td class="mono">${(sc.waiting_time || 0).toFixed(4)}</td>
            <td class="mono">${(sc.turnaround_time || 0).toFixed(4)}</td>
          </tr>
        `).join("");
      }

      // Render Agent Control Blocks in Tab 03
      const agentsMap = drf.agents || {};
      const agentNames = Object.keys(agentsMap);
      document.getElementById("agentTotalBadge").textContent = agentNames.length + " Agents";
      document.getElementById("agentCardsGrid").innerHTML = agentNames.length
        ? agentNames.map((name) => {
            const a = agentsMap[name];
            const stCls = a.state === "terminated" ? "tag-err" : a.state === "paused" ? "tag-warn" : "tag-ok";
            return `<div class="info-card">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                <strong class="mono">${name}</strong>
                <span class="tag ${stCls}">${a.state}</span>
              </div>
              <div class="mono" style="font-size:11.5px;color:var(--ink-secondary);margin-bottom:8px;">
                Syscalls: ${a.total_syscalls} · Model Calls: ${a.model_calls_used} · DRF Share: ${(a.dominant_share * 100).toFixed(1)}%
              </div>
              <div class="mono" style="font-size:11px;color:var(--ink-muted);margin-bottom:10px;">
                轨迹: ${(a.state_history || []).join(" → ")}
              </div>
              <div style="display:flex;gap:6px;">
                <button class="btn btn-sm" onclick="window.controlKernelAgent('${name}', 'pause')">暂停准入</button>
                <button class="btn btn-sm" onclick="window.controlKernelAgent('${name}', 'resume')">恢复</button>
                <button class="btn btn-sm" style="color:var(--state-err);" onclick="window.controlKernelAgent('${name}', 'terminate')">终止回收</button>
              </div>
            </div>`;
          }).join("")
        : `<p class="hint">尚无已注册的 Agent 控制块</p>`;

      // Render Job History
      const jobs = st.jobs || [];
      document.getElementById("jobTotalTag").textContent = jobs.length + " 条";
      const jList = document.getElementById("jobHistoryList");
      if (jobs.length) {
        if (!jobs.some(job => job.id === selectedJobId)) selectedJobId = jobs[0].id;
      } else {
        selectedJobId = null;
      }

      const nextHistoryKey = JSON.stringify([selectedJobId, jobs]);
      if (historyKey !== nextHistoryKey) {
        historyKey = nextHistoryKey;
        jList.innerHTML = jobs.length ? jobs.map(j => {
          const stateClass = j.status === "succeeded" ? "tag-ok" : j.status === "failed" ? "tag-err" : "tag-warn";
          return `<button type="button" class="history-item${j.id === selectedJobId ? " active" : ""}"
            aria-pressed="${j.id === selectedJobId}" data-job-id="${escapeText(j.id)}">
            <span><strong>${escapeText(sceneLabel(j.preset))}</strong><small>${escapeText(j.id.slice(0, 8))}</small></span>
            <span class="tag ${stateClass}">${escapeText(STATUS_LABELS[j.status] || j.status)}</span>
          </button>`;
        }).join("") : '<p class="hint">暂无运行记录</p>';
      }
      if (selectedJobId) {
        const requestedJobId = selectedJobId;
        const det = await apiGet("/api/jobs/" + requestedJobId);
        if (selectedJobId === requestedJobId) renderJobDetail(det);
      }
    } catch (err) {
      const connection = document.getElementById("connBadge");
      connection.textContent = "连接断开";
      connection.dataset.state = "offline";
    }
  }

  async function refreshEcosystem() {
    try {
      const eco = await apiGet("/api/aios/ecosystem");
      // 1. A-Mem notes
      const notes = eco.amem_notes || [];
      document.getElementById("amemCountBadge").textContent = notes.length + " 笔记";
      document.getElementById("amemNotesGrid").innerHTML = notes.length
        ? notes.map((n) => `
          <div class="info-card">
            <div style="display:flex;justify-content:space-between;">
              <span class="mono" style="font-size:11px;color:var(--accent-primary);">${escapeText(n.id)}</span>
              <span class="tag">演化 #${n.evolution_count || 0}</span>
            </div>
            <p style="margin:8px 0;font-weight:500;">${escapeText(n.content)}</p>
            <div class="mono" style="font-size:11px;color:var(--ink-muted);">
              标签: ${escapeText((n.tags || []).join(", ") || "—")}<br>
              双向链接: ${escapeText((n.links || []).join(", ") || "无")}
            </div>
          </div>
        `).join("")
        : `<p class="hint">尚无 A-Mem 卡片盒记忆笔记</p>`;

      // 2. LiteCUA VM state
      const vm = eco.litecua_state || {};
      document.getElementById("vmWindowBadge").textContent =
        `${vm.active_window || "Desktop"} · Cursor(${(vm.cursor && vm.cursor.x) || 0}, ${(vm.cursor && vm.cursor.y) || 0})`;
      document.getElementById("litecuaTreeGrid").innerHTML = (vm.a11y_tree || []).map((el) => `
        <div class="info-card" style="border-left:${el.focused ? '3px solid var(--accent-primary)' : '1px solid var(--border-hairline)'};">
          <div style="display:flex;justify-content:space-between;">
            <strong class="mono">${el.element_id}</strong>
            <span class="tag ${el.focused ? 'tag-ok' : ''}">${el.role}${el.focused ? ' · FOCUSED' : ''}</span>
          </div>
          <div style="margin:6px 0;font-size:12.5px;">${el.label}</div>
          <div class="mono" style="font-size:11.5px;color:var(--ink-secondary);">Value: "${el.value || ""}" · BBox: [${(el.bbox || []).join(",")}]</div>
        </div>
      `).join("");

      // 3. Cerebrum AgentHub Apps
      const apps = eco.agent_hub_apps || [];
      document.getElementById("hubAppCountBadge").textContent = apps.length + " Apps";
      document.getElementById("agentHubGrid").innerHTML = apps.map((app) => `
        <div class="info-card">
          <div style="display:flex;justify-content:space-between;">
            <strong class="mono">${app.app_id}</strong>
            <span class="tag">v${app.version}</span>
          </div>
          <h3 style="margin-top:6px;">${app.name}</h3>
          <p class="hint">${app.description}</p>
          <div class="mono" style="font-size:11px;margin-top:8px;color:var(--ink-secondary);">
            许可 Syscalls: ${(app.required_syscalls || []).join(", ")}<br>
            配额: ${app.resource_quota.cpu_milli}mCPU / ${(app.resource_quota.memory_bytes / (1024*1024)).toFixed(0)}MiB
          </div>
        </div>
      `).join("");

      // 4. Capabilities (36 Tools + 17 Skills)
      if (eco.capabilities) {
        cachedCapabilities = eco.capabilities;
        renderCapabilities("");
      }

      if (eco.model_profiles) renderModelProfiles(eco.model_profiles);
    } catch (e) {}
  }

  async function refreshKnowledge(query = "") {
    try {
      const kb = await apiGet("/api/knowledge");
      const docs = kb.documents || [];
      document.getElementById("kbCountBadge").textContent = `${docs.length} 篇文档 (${kb.chunks || 0} 分块)`;
      if (query.trim()) {
        const res = await apiGet("/api/knowledge/search?q=" + encodeURIComponent(query));
        const sources = res.sources || [];
        document.getElementById("kbListWrap").innerHTML = sources.length
          ? sources.map((s) => `<div class="info-card" style="margin-bottom:8px;"><span class="tag tag-ok">[${s.citation}]</span> <strong>${escapeText(s.title)}</strong> (行 ${s.line_start}-${s.line_end})<p class="hint" style="margin-top:4px;">${escapeText(s.body)}</p></div>`).join("")
          : `<p class="hint">无匹配知识分块</p>`;
      } else {
        document.getElementById("kbListWrap").innerHTML = docs.map((d) =>
          `<div class="info-card" style="margin-bottom:8px;"><strong>${escapeText(d.title)}</strong> <span class="mono" style="font-size:11px;color:var(--ink-muted);">rev.${d.revision} · ${d.size}B · SHA:${d.sha256.slice(0,10)}</span></div>`
        ).join("");
      }
    } catch (e) {}
  }

  function renderCapabilities(filterText) {
    const q = (filterText || "").toLowerCase().trim();
    const plugins = (cachedCapabilities.plugins || []).filter((p) => !q || (p.id + " " + p.description).toLowerCase().includes(q));
    const skills = (cachedCapabilities.skills || []).filter((s) => !q || (s.id + " " + s.description).toLowerCase().includes(q));
    const cards = [
      ...plugins.map((p) => `<div class="info-card"><div style="display:flex;justify-content:space-between;"><strong class="mono">${p.id}</strong><span class="tag">TOOL</span></div><p class="hint">${escapeText(p.description)}</p></div>`),
      ...skills.map((s) => `<div class="info-card"><div style="display:flex;justify-content:space-between;"><strong class="mono">${s.id}</strong><span class="tag tag-ok">SKILL</span></div><p class="hint">${escapeText(s.description)}</p></div>`)
    ];
    document.getElementById("capabilitiesGrid").innerHTML = cards.join("");
  }

  window.selectJob = async function (jobId) {
    selectedJobId = jobId;
    await refreshStatus();
  };

  window.controlKernelAgent = async function (agentName, op) {
    try {
      await apiPost(`/api/aios/agents/${encodeURIComponent(agentName)}/${encodeURIComponent(op)}`, {});
      showNotice(`已对 Agent '${agentName}' 执行生命周期操作: ${op}`);
      await refreshStatus();
    } catch (err) {
      showNotice(err.message, true);
    }
  };

  function renderModelProfiles(info) {
    modelProfiles = info;
    const active = info.profiles.find(profile => profile.id === info.selected && profile.configured);
    document.getElementById("activeModelTag").textContent = active ? "已启用" : "未启用";
    document.getElementById("activeModelSummary").textContent = active
      ? `${active.label} · ${active.model}` : "当前没有可用的模型配置";
    const connection = active?.connection;
    const route = active?.transport === "proxy" ? "服务端代理" : active?.transport === "direct" ? "服务端直连" : "";
    document.getElementById("activeModelConnection").textContent = active
      ? [route, connection ? connection.message : "连接未验证",
         connection?.checked_at ? new Date(connection.checked_at * 1000).toLocaleTimeString() : ""].filter(Boolean).join(" · ")
      : "内置默认槽位未配置密钥。添加模型后保存并启用。";
    document.getElementById("testModelConnection").disabled = !active;
    document.getElementById("profileList").innerHTML = info.profiles.map(profile => {
      const isActive = profile.id === active?.id;
      const suggestion = profile.suggested_model || (profile.provider === "deepseek" && oldDeepseekNames.has(profile.model)
        ? "deepseek-flash" : "");
      const action = (name, label) => `<button class="btn btn-sm" type="button" data-profile-action="${name}"
        data-profile-id="${escapeText(profile.id)}">${label}</button>`;
      return `<div class="model-profile">
        <div><strong>${escapeText(profile.legacy ? "内置默认槽位" : profile.label)}</strong>
          <span class="tag ${isActive ? "tag-ok" : ""}">${isActive ? "当前对话" : profile.configured ? "已保存" : "未启用"}</span>
          <div class="hint">${escapeText(profile.model)}${suggestion ? " · 建议改用 " + escapeText(suggestion) : ""}</div>
          ${profile.connection && !isActive ? `<div class="hint">${escapeText(profile.connection.message)}</div>` : ""}
        </div>
        <div class="model-actions">
          ${profile.configured && !isActive ? action("select", "启用") : ""}
          ${profile.configured && profile.base_url ? action("test", "测试") : ""}
          ${!profile.legacy ? action("edit", "编辑") : ""}
        </div>
      </div>`;
    }).join("");
  }

  function editProfile(profile) {
    const form = document.getElementById("profileForm");
    form.reset();
    const id = document.getElementById("profId");
    const key = document.getElementById("profKey");
    id.readOnly = Boolean(profile);
    document.getElementById("profProvider").disabled = Boolean(profile);
    key.required = !profile;
    key.placeholder = profile ? "留空沿用现有密钥" : "";
    document.getElementById("profileEditorLabel").textContent = profile ? "编辑模型" : "添加模型";
    document.getElementById("newProfileBtn").hidden = !profile;
    if (profile) {
      for (const [element, value] of Object.entries({
        profId: profile.id, profProvider: profile.provider, profLabel: profile.label,
        profModel: profile.model, profBaseUrl: profile.base_url || ""
      })) document.getElementById(element).value = value;
    }
    updateProfileHint();
    document.getElementById("profileEditor").open = true;
    document.getElementById(profile ? "profModel" : "profProvider").focus();
  }

  function updateProfileHint() {
    const provider = document.getElementById("profProvider").value;
    const model = document.getElementById("profModel").value.trim();
    document.getElementById("profBaseUrl").placeholder = providerUrls[provider] || "";
    document.getElementById("profileModelHint").textContent = provider === "deepseek" && oldDeepseekNames.has(model)
      ? "旧名称或拼写有误，DeepSeek 官方推荐 deepseek-flash。" : "";
  }

  async function testProfile(id, button) {
    const result = document.getElementById("modelConnectionResult");
    button.disabled = true;
    result.textContent = "正在读取供应商模型列表…";
    try {
      const check = await apiPost("/api/model-profiles/test", id ? { id } : {});
      result.textContent = [check.model, check.message,
                           check.suggested_model && "建议改用 " + check.suggested_model].filter(Boolean).join(" · ");
      renderModelProfiles(await apiGet("/api/model-profiles"));
    } catch (error) {
      result.textContent = error.message;
    } finally {
      if (button.id !== "testModelConnection") button.disabled = false;
      else button.disabled = !modelProfiles.profiles.some(p => p.id === modelProfiles.selected && p.configured);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    const params = new URLSearchParams(window.location.search);
    const view = params.get("view") || location.hash.replace(/^#pane-/, "");
    if (view && document.getElementById("pane-" + view)) switchTab(view);

    document.querySelectorAll("[data-tab]").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.getAttribute("data-tab")));
    });

    const presetSelect = document.getElementById("preset");
    presetSelect.addEventListener("change", () => {
      const val = presetSelect.value;
      const comparison = ["scheduling", "workflow_scheduling"].includes(val);
      const shared = ["document", "resources"].includes(val);
      document.getElementById("workersBox").hidden = comparison;
      const model = ["engineering", "llm", "team"].includes(val);
      document.getElementById("jobOptions").hidden = comparison;
      document.getElementById("modelOptions").hidden = !model;
      document.getElementById("auditOptions").hidden = val !== "engineering";
      for (const id of ["modelCallsBudget", "modelTokensBudget"]) document.getElementById(id).disabled = !model;
      document.getElementById("policy").value = shared ? "critical_path" : "drf";
      document.getElementById("deadlineBox").style.display = shared ? "" : "none";
      document.getElementById("workflowDeadline").disabled = !shared;
      document.getElementById("projectBox").style.display = val === "engineering" ? "block" : "none";
      document.getElementById("promptBox").style.display = ["document", "llm", "team"].includes(val) ? "block" : "none";
      const isRecovery = val === "engineering" && document.getElementById("interruptVerificationChk").checked;
      document.getElementById("presetHint").textContent = isRecovery
        ? "验证中断后，从原补丁继续。" : PRESET_HINTS[val] || "";
      for (const tile of document.querySelectorAll("[data-run]")) {
        tile.setAttribute("aria-pressed", String(tile.dataset.run === (isRecovery ? "recovery" : val)));
      }
    });

    function chooseScenario(scenario, recovery = false) {
      presetSelect.value = scenario;
      document.getElementById("interruptVerificationChk").checked = recovery;
      if (recovery) {
        document.getElementById("projectSelect").value = "json_pointer";
        document.getElementById("fixedModelChk").checked = true;
      }
      presetSelect.dispatchEvent(new Event("change"));
    }

    for (const tile of document.querySelectorAll("[data-run]")) {
      tile.addEventListener("click", () => {
        const recovery = tile.dataset.run === "recovery";
        chooseScenario(recovery ? "engineering" : tile.dataset.run, recovery);
      });
    }
    document.getElementById("interruptVerificationChk").addEventListener("change",
      () => presetSelect.dispatchEvent(new Event("change")));
    const requestedPreset = params.get("preset");
    const initial = Array.from(presetSelect.options).some(option => option.value === requestedPreset)
      ? requestedPreset : "engineering";
    chooseScenario(initial, initial === "engineering" && params.get("demo") === "recovery");

    document.getElementById("jobHistoryList").addEventListener("click", event => {
      const selection = event.target.closest("button[data-job-id]");
      if (selection) window.selectJob(selection.dataset.jobId);
    });

    let isSubmittingJob = false;
    document.getElementById("jobForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (isSubmittingJob) return;
      isSubmittingJob = true;
      const submitBtn = document.getElementById("submitJobBtn");
      if (submitBtn) submitBtn.disabled = true;
      try {
        const incAudit = document.getElementById("includeAuditBranchChk");
        const simFail = document.getElementById("simulateAuditFailureChk");
        const payload = {
          preset: presetSelect.value,
          project: document.getElementById("projectSelect").value,
          deterministic_stub: presetSelect.value === "engineering" && document.getElementById("fixedModelChk").checked,
          interrupt_verification: presetSelect.value === "engineering" && document.getElementById("interruptVerificationChk").checked,
          workers: parseInt(document.getElementById("workers").value, 10),
          policy: document.getElementById("policy").value,
          prompt: document.getElementById("taskPrompt").value || "AIOS formal architecture verification",
          include_audit_branch: incAudit ? incAudit.checked : false,
          simulate_audit_failure: simFail ? simFail.checked : false,
          model_budget: {
            calls: parseInt(document.getElementById("modelCallsBudget").value, 10),
            output_tokens: parseInt(document.getElementById("modelTokensBudget").value, 10)
          }
        };
        const target = document.getElementById("workflowDeadline");
        if (["document", "resources"].includes(payload.preset) && target.value !== "") {
          payload.deadline_ms = target.valueAsNumber;
        }
        const out = await apiPost("/api/jobs", payload);
        selectedJobId = out.id;
        showNotice("任务已提交");
        await refreshStatus();
        await refreshEcosystem();
      } catch (err) {
        showNotice(err.message, true);
      } finally {
        isSubmittingJob = false;
        if (submitBtn) submitBtn.disabled = false;
      }
    });

    const resumeJob = document.getElementById("resumeJobBtn");
    resumeJob.addEventListener("click", async () => {
      resumeJob.disabled = true;
      try {
        await apiPost("/api/jobs/" + encodeURIComponent(resumeJob.dataset.jobId) + "/resume", {});
        await refreshStatus();
      } catch (error) {
        showNotice(error.message, true);
      } finally {
        resumeJob.disabled = false;
      }
    });

    const pinJob = document.getElementById("pinJobBtn");
    pinJob.addEventListener("click", async () => {
      pinJob.disabled = true;
      try {
        const pinned = pinJob.getAttribute("aria-pressed") !== "true";
        await apiPost("/api/jobs/" + encodeURIComponent(pinJob.dataset.jobId) + "/retention", { pinned });
        showNotice(pinned ? "已保留任务记录和交付产物" : "已取消保留");
        await refreshStatus();
      } catch (error) {
        showNotice(error.message, true);
      } finally {
        pinJob.disabled = false;
      }
    });

    const testConnection = document.getElementById("testModelConnection");
    testConnection.addEventListener("click", () => testProfile(null, testConnection));
    document.getElementById("profileList").addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-profile-action]");
      if (!button) return;
      const profile = modelProfiles.profiles.find(item => item.id === button.dataset.profileId);
      if (!profile) return;
      if (button.dataset.profileAction === "edit") return editProfile(profile);
      if (button.dataset.profileAction === "test") return testProfile(profile.id, button);
      try {
        await apiPost("/api/model-profiles/select", { id: profile.id });
        document.getElementById("modelConnectionResult").textContent = "";
        renderModelProfiles(await apiGet("/api/model-profiles"));
      } catch (error) {
        showNotice(error.message, true);
      }
    });
    document.getElementById("newProfileBtn").addEventListener("click", () => editProfile(null));
    document.getElementById("profModel").addEventListener("input", updateProfileHint);
    document.getElementById("profProvider").addEventListener("change", () => {
      document.getElementById("profBaseUrl").value = "";
      document.getElementById("profModel").value =
        document.getElementById("profProvider").value === "deepseek" ? "deepseek-flash" : "";
      updateProfileHint();
    });
    updateProfileHint();

    document.getElementById("profileForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = event.currentTarget.querySelector('button[type="submit"]');
      if (submit.disabled) return;
      const provider = document.getElementById("profProvider");
      const key = document.getElementById("profKey");
      const payload = {
        id: document.getElementById("profId").value.trim() || "model_" + crypto.randomUUID().slice(0, 13),
        label: document.getElementById("profLabel").value.trim() || provider.selectedOptions[0].textContent,
        provider: provider.value,
        model: document.getElementById("profModel").value.trim()
      };
      const baseUrl = document.getElementById("profBaseUrl").value.trim();
      if (baseUrl) payload.base_url = baseUrl;
      if (key.value) payload.key = key.value;
      submit.disabled = true;
      try {
        if (modelProfiles.profiles.some(profile => profile.base_url)) {
          await apiPost("/api/model-profiles/save-and-select", payload);
        } else {
          await apiPost("/api/model-profiles/save", payload);
          await apiPost("/api/model-profiles/select", { id: payload.id });
        }
        document.getElementById("modelConnectionResult").textContent = "";
        renderModelProfiles(await apiGet("/api/model-profiles"));
        document.getElementById("profileEditor").open = false;
        showNotice("模型已启用");
      } catch (error) {
        showNotice(error.message, true);
      } finally {
        submit.disabled = false;
        key.value = "";
      }
    });

    document.getElementById("amemForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const content = document.getElementById("amemContent").value.trim();
        await apiPost("/api/memory/save", { content, policy: "shared" });
        document.getElementById("amemContent").value = "";
        showNotice("A-Mem 记忆卡片已写入并完成双向建链演化");
        await refreshEcosystem();
      } catch (err) {
        showNotice(err.message, true);
      }
    });

    document.getElementById("kbForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiPost("/api/knowledge/save", {
          title: document.getElementById("kbTitle").value.trim(),
          body: document.getElementById("kbBody").value.trim()
        });
        document.getElementById("kbTitle").value = "";
        document.getElementById("kbBody").value = "";
        showNotice("知识库文档已保存并建立 SQLite FTS5 双语索引");
        await refreshKnowledge();
      } catch (err) {
        showNotice(err.message, true);
      }
    });

    document.getElementById("kbSearchInput").addEventListener("input", (e) => {
      refreshKnowledge(e.target.value);
    });

    document.getElementById("lsfsPromptForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const prompt = document.getElementById("lsfsPromptInput").value.trim();
        const res = await apiPost("/api/aios/intent", { prompt });
        document.getElementById("lsfsTerminalOutput").textContent = JSON.stringify(res, null, 2);
      } catch (err) {
        showNotice(err.message, true);
      }
    });

    document.querySelectorAll("[data-lsfs]").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.getElementById("lsfsPromptInput").value = btn.getAttribute("data-lsfs");
        document.getElementById("lsfsPromptForm").requestSubmit();
      });
    });

    document.querySelectorAll("[data-cua]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const act = btn.getAttribute("data-cua");
        try {
          await apiPost("/api/aios/litecua", { action: act });
          await refreshEcosystem();
        } catch (err) {
          showNotice(err.message, true);
        }
      });
    });

    document.getElementById("capSearchInput").addEventListener("input", (e) => {
      renderCapabilities(e.target.value);
    });

    refreshStatus();
    refreshEcosystem();
    refreshKnowledge();
    setInterval(refreshStatus, 2500);
  });
})();
