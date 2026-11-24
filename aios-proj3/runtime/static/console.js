(function () {
  function escapeText(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  }
  let csrfToken = "";
  let selectedJobId = null;
  let cachedCapabilities = { plugins: [], skills: [] };

  const PRESET_HINTS = {
    kernel: "依次验证 AIOS 六篇核心论文机制：A-Mem 卡片盒记忆演化、LSFS 语义文件写入与回滚、LiteCUA 虚拟桌面无障碍树观察、Context 去重与 Cerebrum 工具调用。",
    engineering: "运行受控 Python 工程七阶段修复闭环：原始失败检查 → 缺陷诊断 → 测试设计 → 补丁生成 → 真实隔离测试 → 独立评审 → 哈希核验。",
    dynamic: "验证 Proj3 4.2/4.4：规划节点运行期动态孵化并行子任务（spawn_tasks），同时注入实验分支崩溃，验证故障被精准隔离（isolated_skipped）且不阻塞健康分支。",
    agents: "验证 Proj3 4.3：注册多并发 Agent 控制块（ACB），演示 created → ready → running → paused → terminated 状态转换与在线控制。",
    resources: "验证 Proj3 4.2：多 Agent 竞争 CPU/内存/PIDs 预算，观察 DRF 主导资源份额公平选择与 RR 时间片抢占。",
    ipc: "验证 Proj3 4.7：通过内核级 Mailbox 交换不可变上下文引用（ctx_sha256），自动完成读取 ACL 转授、幂等去重与 ACK 确认。",
    document: "验证 ICLR'25 LSFS：将中英文自然语言指令路由为原子存储系统调用，验证多版本回滚与 lsfs://share 共享链接。",
    litecua: "验证 LiteCUA MCP：在虚拟桌面中观察无障碍树（a11y tree）、点击聚焦搜索框、输入符号并验证快照回滚。",
    llm: "调用当前选中的真实或内置模型执行流式对话系统调用。",
    team: "启动 Planner / Analyst / Tester / Reviewer 四 Agent 协同 DAG 工作流。"
  };

  const TAB_TITLES = {
    workflow: "01 协作与工程修复",
    syscalls: "02 内核队列与系统调用",
    agents: "03 Agent 控制块与生命周期",
    amem: "04 A-Mem 记忆图谱与知识库",
    lsfs: "05 LSFS 终端与 LiteCUA 桌面",
    cerebrum: "06 Cerebrum 应用与能力库"
  };

  function initTheme() {
    const saved = localStorage.getItem("aios_guizang_theme") || "ink-classic";
    document.documentElement.setAttribute("data-theme", saved);
    const switcher = document.getElementById("themeSwitcher");
    if (switcher) {
      switcher.value = saved;
      switcher.addEventListener("change", () => {
        const val = switcher.value;
        document.documentElement.setAttribute("data-theme", val);
        localStorage.setItem("aios_guizang_theme", val);
      });
    }
  }

  function switchTab(tabName) {
    document.querySelectorAll(".tab-pane").forEach((el) => {
      el.style.display = el.id === "pane-" + tabName ? "block" : "none";
    });
    document.querySelectorAll("[data-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-tab") === tabName);
    });
    const label = document.getElementById("activeTabLabel");
    if (label && TAB_TITLES[tabName]) label.textContent = TAB_TITLES[tabName];
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
    box.style.display = "block";
    box.style.borderColor = isErr ? "var(--state-err)" : "var(--accent-primary)";
    box.textContent = msg;
    setTimeout(() => { box.style.display = "none"; }, 6000);
  }

  function renderJobDetail(detail) {
    function sourceText(value) {
      try {
        const files = JSON.parse(value);
        if (files && typeof files === 'object' && !Array.isArray(files)) {
          return Object.entries(files).map(([name, code]) => '# ' + name + '\n' + String(code)).join('\n\n');
        }
      } catch (_) {}
      return value || '';
    }
    document.getElementById("selectedJobTitle").textContent =
      "工作流 [" + detail.preset.toUpperCase() + "] · " + detail.id.slice(0, 10);
    const stBadge = document.getElementById("selectedJobStatus");
    stBadge.textContent = detail.status;
    stBadge.className = "tag " + (detail.status === "succeeded" ? "tag-ok" : detail.status === "failed" ? "tag-err" : "tag-warn");

    const states = detail.states || {};
    const attempts = detail.attempts || {};
    const refs = detail.context_refs || {};
    const rows = Object.keys(states).map((tid) => {
      const st = states[tid];
      const cls = st === "succeeded" ? "tag-ok" : (st === "failed" || st === "isolated_skipped") ? "tag-err" : "tag-warn";
      return `<tr>
        <td class="mono"><strong>${tid}</strong></td>
        <td><span class="tag ${cls}">${st}</span></td>
        <td class="mono">${attempts[tid] || 1}</td>
        <td class="mono">${refs[tid] || "—"}</td>
      </tr>`;
    }).join("");

    document.getElementById("dagTaskTableWrap").innerHTML = rows
      ? `<table class="swiss-table"><thead><tr><th>Task ID</th><th>State</th><th>Attempts</th><th>Context SHA256 Ref</th></tr></thead><tbody>${rows}</tbody></table>`
      : `<p class="hint">尚无任务节点状态</p>`;

    const engBox = document.getElementById("engineeringBox");
    if (detail.engineering) {
      engBox.style.display = "block";
      const eng = detail.engineering;
      document.getElementById("engProjectTitle").textContent = eng.title + " (" + eng.file + ")";
      document.getElementById("engVerdictBadge").textContent = eng.accepted ? "全部测试与哈希核验通过" : "未通过";
      document.getElementById("engOriginalCode").textContent = sourceText(eng.original_source);
      document.getElementById("engPatchedCode").textContent = sourceText(eng.patched_source);
      document.getElementById("engTestSummary").textContent =
        `${eng.test_engine || 'controlled_assertions'} · ${eng.tests_run || '查看记录'} 项测试`;
      const usage = detail.model_budget || {};
      document.getElementById('engTokenSummary').textContent =
        `模型 ${usage.calls || 0} 次；供应商已报告输入 ${usage.actual_input_tokens || 0} / 输出 ${usage.actual_output_tokens || 0} token` +
        `（覆盖 ${usage.usage_reported_calls || 0} 次调用）。输出上限预留 ${usage.output_tokens || 0}，不是实际计费 token。`;
      const download = document.getElementById("engDownload");
      const downloadUrl = eng.artifact && eng.artifact.download_url;
      download.hidden = !downloadUrl;
      if (downloadUrl && /^\/api\/jobs\/[a-f0-9]{32}\/artifact$/.test(downloadUrl)) {
        download.href = downloadUrl;
        download.download = 'verified-order-project.zip';
      } else {
        download.hidden = true;
        download.removeAttribute('href');
      }
      document.getElementById("engPhaseList").innerHTML = (eng.phases || []).map((p) =>
        `<div class="info-card"><div class="mono" style="font-size:11px;color:var(--ink-muted);">${p.id}</div><h3>${escapeText(p.label)}</h3><span class="tag ${p.status === 'succeeded' ? 'tag-ok' : 'tag-warn'}">${p.status}</span></div>`
      ).join("");
    } else {
      engBox.style.display = "none";
    }

    const evs = detail.events || [];
    document.getElementById("eventCountLabel").textContent = evs.length + " events";
    document.getElementById("workflowEventsPre").textContent =
      JSON.stringify({ results: detail.results, events: evs }, null, 2);
  }

  async function refreshStatus() {
    try {
      const st = await apiGet("/api/status");
      csrfToken = st.csrf || csrfToken;
      document.getElementById("enforcementBadge").textContent = "资源模式: " + st.enforcement;
      document.getElementById("activeModelTag").textContent = st.model || "mock-aios-core";
      document.getElementById("lastSampleTime").textContent = new Date().toLocaleTimeString();

      const kStatus = st.kernel_status || {};
      const processes = kStatus.processes || {};
      const records = [...(processes.active_workers || []).map(w => ({...w, phase: '运行中'})),
        ...(processes.receipts || []).slice(-8).reverse().map(w => ({...w, phase: w.reclaimed ? '已回收' : '未确认'}))];
      document.getElementById('workerEvidence').innerHTML = records.length
        ? '<table class="swiss-table"><thead><tr><th>Agent / PID</th><th>状态与隔离</th><th>资源限额 / 采样</th></tr></thead><tbody>' + records.map(w =>
          `<tr><td>${escapeText(w.agent)}<br>PID ${escapeText(w.pid)}</td><td>${escapeText(w.phase)}<br>${escapeText(w.cgroup || 'process_group（非 cgroup 硬限制）')}</td><td>${escapeText(JSON.stringify(w.limits || {}))}<br>${escapeText(JSON.stringify(w.usage || {}))}</td></tr>`).join('') + '</tbody></table>'
        : '<p class="hint">当前没有 Worker 执行记录。</p>';
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
        if (!selectedJobId) selectedJobId = jobs[0].id;
        jList.innerHTML = jobs.map((j) => `
          <div class="info-card" style="margin-bottom:8px;cursor:pointer;border-left:${j.id === selectedJobId ? '3px solid var(--accent-primary)' : '1px solid var(--border-hairline)'};" onclick="window.selectJob('${j.id}')">
            <div style="display:flex;justify-content:space-between;">
              <strong>${j.preset.toUpperCase()}</strong>
              <span class="tag ${j.status === 'succeeded' ? 'tag-ok' : j.status === 'failed' ? 'tag-err' : 'tag-warn'}">${j.status}</span>
            </div>
            <div class="mono" style="font-size:11px;color:var(--ink-muted);margin-top:4px;">ID: ${j.id.slice(0, 12)} · Policy: ${j.policy}</div>
          </div>
        `).join("");
      }

      if (selectedJobId) {
        const det = await apiGet("/api/jobs/" + selectedJobId);
        renderJobDetail(det);
      }
    } catch (err) {
      document.getElementById("connBadge").textContent = "连接断开";
      document.getElementById("connBadge").className = "tag tag-err";
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

      // 5. Profiles
      if (eco.model_profiles) {
        const profs = eco.model_profiles.profiles || [];
        const sel = eco.model_profiles.selected;
        document.getElementById("profileList").innerHTML = profs.map((p) => `
          <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border-hairline);">
            <div>
              <strong>${escapeText(p.label)}</strong> <span class="mono" style="font-size:11px;color:var(--ink-muted);">(${p.provider} / ${escapeText(p.model)})</span>
            </div>
            <div style="display:flex;gap:6px;align-items:center;">
              <span class="tag ${p.configured ? 'tag-ok' : ''}">${p.id === sel ? '当前使用' : (p.configured ? '已配置' : '未设密钥')}</span>
              ${p.configured && p.id !== sel ? `<button class="btn btn-sm" onclick="window.selectProfile('${p.id}')">切换</button>` : ""}
            </div>
          </div>
        `).join("");
      }
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

  window.selectProfile = async function (profId) {
    try {
      await apiPost("/api/model-profiles/select", { id: profId });
      showNotice("已切换当前模型配置为: " + profId);
      await refreshEcosystem();
      await refreshStatus();
    } catch (err) {
      showNotice(err.message, true);
    }
  };

  document.addEventListener("DOMContentLoaded", () => {
    initTheme();

    document.querySelectorAll("[data-tab]").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.getAttribute("data-tab")));
    });

    const presetSelect = document.getElementById("preset");
    presetSelect.addEventListener("change", () => {
      const val = presetSelect.value;
      document.getElementById("projectBox").style.display = val === "engineering" ? "block" : "none";
      document.getElementById("promptBox").style.display = ["document", "llm", "team"].includes(val) ? "block" : "none";
      document.getElementById("presetHint").textContent = PRESET_HINTS[val] || "";
    });

    document.getElementById("jobForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const payload = {
          preset: presetSelect.value,
          project: document.getElementById("projectSelect").value,
          workers: parseInt(document.getElementById("workers").value, 10),
          policy: document.getElementById("policy").value,
          prompt: document.getElementById("taskPrompt").value || "AIOS formal architecture verification",
          model_budget: {
            calls: parseInt(document.getElementById("modelCallsBudget").value, 10),
            output_tokens: parseInt(document.getElementById("modelTokensBudget").value, 10)
          }
        };
        const out = await apiPost("/api/jobs", payload);
        selectedJobId = out.id;
        showNotice("工作流已提交: " + out.id.slice(0, 10));
        await refreshStatus();
        await refreshEcosystem();
      } catch (err) {
        showNotice(err.message, true);
      }
    });

    document.getElementById("profileForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiPost("/api/model-profiles/save", {
          id: document.getElementById("profId").value.trim(),
          label: document.getElementById("profLabel").value.trim(),
          provider: document.getElementById("profProvider").value,
          model: document.getElementById("profModel").value.trim(),
          key: document.getElementById("profKey").value
        });
        document.getElementById("profKey").value = "";
        showNotice("API 配置已安全存入内存持钥服务");
        await refreshEcosystem();
      } catch (err) {
        showNotice(err.message, true);
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
