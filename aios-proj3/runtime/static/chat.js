(function () {
  let csrfToken = "";
  let currentConvId = "conv_" + Math.random().toString(36).slice(2, 10);

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

  function appendMessageCard(role, title, bodyText, metaText, citations) {
    const box = document.getElementById("messagesBox");
    const card = document.createElement("article");
    card.className = "panel";
    card.style.marginBottom = "0";
    if (role === "user") {
      card.style.borderLeft = "3px solid var(--border-strong)";
    } else {
      card.style.borderLeft = "3px solid var(--accent-primary)";
    }

    const citeHtml = (citations && citations.length)
      ? `<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border-hairline);font-size:12px;">
           <strong>知识库命中引用：</strong>
           ${citations.map((c) => `<span class="tag tag-ok" style="margin-right:6px;">[${c.citation}] ${c.title} (L${c.line_start}-${c.line_end})</span>`).join("")}
         </div>`
      : "";

    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;margin-bottom:6px;">
        <span class="kicker" style="margin:0;">${role === "user" ? "USER REQUEST" : "AIOS KERNEL RESPONSE"} · ${title}</span>
        <span class="mono" style="font-size:11px;color:var(--ink-muted);">${metaText || ""}</span>
      </div>
      <div style="white-space:pre-wrap;font-size:13.5px;">${bodyText}</div>
      ${citeHtml}
    `;
    box.appendChild(card);
  }

  async function loadChatStatus() {
    try {
      const st = await apiGet("/api/chat/status");
      csrfToken = st.csrf || csrfToken;
      document.getElementById("chatModelBadge").textContent = st.model || "mock-aios-core";
      document.getElementById("sideKbCount").textContent = String((st.knowledge && st.knowledge.documents) || 0);
      document.getElementById("sideAmemCount").textContent = String(st.amem_count || 0);

      const convs = await apiGet("/api/chat/conversations");
      const list = convs.conversations || [];
      if (list.length) {
        document.getElementById("convList").innerHTML = list.map((c) => `
          <button class="nav-item ${c.id === currentConvId ? 'active' : ''}" onclick="window.openConversation('${c.id}')">
            <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${c.title || c.id}</span>
            <span class="mono" style="font-size:11px;">${c.turns || 0}</span>
          </button>
        `).join("");
      }
    } catch (e) {}
  }

  window.openConversation = async function (convId) {
    currentConvId = convId;
    document.getElementById("currConvLabel").textContent = convId;
    try {
      const detail = await apiGet("/api/chat/conversations/" + encodeURIComponent(convId));
      const box = document.getElementById("messagesBox");
      box.innerHTML = "";
      (detail.turns || []).forEach((t) => {
        appendMessageCard("user", t.mode.toUpperCase(), t.prompt, new Date(t.created * 1000).toLocaleTimeString());
        appendMessageCard(
          "assistant",
          "Job " + (t.job_id || "").slice(0, 8),
          t.answer || "执行完成",
          `Status: ${t.status} · Syscall #${t.syscall_id || "—"}`,
          t.citations || []
        );
      });
      await loadChatStatus();
    } catch (e) {}
  };

  document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    loadChatStatus();

    document.getElementById("newChatBtn").addEventListener("click", () => {
      currentConvId = "conv_" + Math.random().toString(36).slice(2, 10);
      document.getElementById("currConvLabel").textContent = currentConvId;
      document.getElementById("messagesBox").innerHTML = "";
    });

    document.querySelectorAll("#quickPrompts .info-card").forEach((card) => {
      card.addEventListener("click", () => {
        document.getElementById("chatMode").value = card.getAttribute("data-mode") || "auto";
        document.getElementById("chatPrompt").value = card.getAttribute("data-prompt") || "";
        document.getElementById("chatForm").requestSubmit();
      });
    });

    document.getElementById("chatForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const promptInput = document.getElementById("chatPrompt");
      const prompt = promptInput.value.trim();
      if (!prompt) return;
      const mode = document.getElementById("chatMode").value;
      const useKnowledge = document.getElementById("useKnowledgeToggle").checked;
      promptInput.value = "";

      appendMessageCard("user", mode.toUpperCase(), prompt, new Date().toLocaleTimeString());

      try {
        const res = await apiPost("/api/chat/turns", {
          conversation_id: currentConvId,
          request_id: "req_" + Math.random().toString(36).slice(2, 10),
          prompt,
          mode,
          use_knowledge: useKnowledge
        });
        appendMessageCard(
          "assistant",
          `Job ${(res.job_id || "").slice(0, 8)}`,
          res.answer || "执行完成",
          `Status: ${res.status} · Syscall #${res.syscall_id || "—"}`,
          res.citations || []
        );
        await loadChatStatus();
      } catch (err) {
        appendMessageCard("assistant", "ERROR", err.message, "请求被拒绝");
      }
    });
  });
})();
