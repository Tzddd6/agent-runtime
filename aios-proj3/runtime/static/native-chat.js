(() => {
  let conversation = 'conv_' + crypto.randomUUID().replaceAll('-', '');
  let attachments = [];
  const box = document.getElementById('messagesBox');
  function card(role, text = '') {
    const article = document.createElement('article');
    article.className = 'panel';
    const label = document.createElement('strong');
    label.textContent = role;
    const content = document.createElement('div');
    article.append(label, content);
    box.append(article);
    update(content, text);
    return content;
  }
  function update(node, text) {
    node.replaceChildren();
    if (window.renderMarkdown) window.renderMarkdown(node, text);
    else node.textContent = text;
  }
  async function status() {
    const response = await fetch('/api/chat/status');
    if (!response.ok) throw new Error('无法读取服务状态');
    return response.json();
  }
  async function refreshConversations() {
    const response = await fetch('/api/chat/conversations');
    if (!response.ok) throw new Error('无法读取会话记录');
    const data = await response.json();
    const list = document.getElementById('convList');
    list.replaceChildren();
    for (const entry of data.conversations) {
      const button = document.createElement('button');
      button.className = 'nav-item';
      button.textContent = entry.title;
      button.onclick = async () => {
        try {
          const response = await fetch('/api/chat/conversations/' + encodeURIComponent(entry.id));
          if (!response.ok) throw new Error('会话读取失败');
          const chat = await response.json();
          conversation = chat.id;
          attachments = [];
          box.replaceChildren();
          document.getElementById('currConvLabel').textContent = conversation;
          for (const turn of chat.turns) {
            card('你', turn.prompt);
            card('Agent Runtime · ' + turn.status, turn.answer || '运行未确认，请查看执行记录。');
          }
        } catch (error) { card('错误', error.message); }
      };
      list.append(button);
    }
  }
  document.addEventListener('DOMContentLoaded', async () => {
    const form = document.getElementById('chatForm');
    const button = document.getElementById('sendChatBtn');
    const notice = document.createElement('p');
    notice.setAttribute('role', 'status');
    const upload = document.createElement('input');
    upload.type = 'file'; upload.multiple = true;
    upload.accept = '.png,.jpg,.jpeg,.webp,.bmp,.pdf,.docx,.txt,.md,.py,.csv,.json';
    upload.setAttribute('aria-label', '上传图片、PDF、Word 或文本');
    form.prepend(upload, notice);
    upload.onchange = async () => {
      upload.disabled = true; button.disabled = true;
      try {
        const files = [...upload.files];
        if (files.length > 4 || files.some(file => file.size > 8 * 1024 * 1024)) throw new Error('最多 4 个文件，单文件最多 8 MiB');
        const {csrf} = await status();
        const uploaded = [];
        for (const file of files) {
          const encoded = await new Promise((resolve, reject) => {
            const reader = new FileReader(); reader.onerror = reject;
            reader.onload = () => resolve(reader.result.split(',')[1]); reader.readAsDataURL(file);
          });
          const response = await fetch('/api/attachments', {method:'POST', headers:{'Content-Type':'application/json', 'X-AgentRT-CSRF':csrf},
            body:JSON.stringify({conversation_id:conversation, name:file.name, data:encoded})});
          const result = await response.json();
          if (!response.ok) throw new Error(result.error);
          uploaded.push(result.id);
        }
        attachments = uploaded;
        notice.textContent = '已上传 ' + attachments.length + ' 个附件';
      } catch (error) { attachments = []; notice.textContent = error.message; }
      finally { upload.disabled = false; button.disabled = false; }
    };
    for (const prompt of document.querySelectorAll('#quickPrompts [data-prompt]')) {
      prompt.onclick = () => {
        document.getElementById('chatPrompt').value = prompt.dataset.prompt;
        document.getElementById('chatMode').value = prompt.dataset.mode || 'auto';
        form.requestSubmit();
      };
    }
    document.getElementById('newChatBtn').onclick = () => {
      conversation = 'conv_' + crypto.randomUUID().replaceAll('-', '');
      attachments = []; box.replaceChildren(); notice.textContent = ''; upload.value = '';
    };
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (button.disabled) return;
      const promptInput = document.getElementById('chatPrompt');
      const prompt = promptInput.value.trim();
      if (!prompt) return;
      button.disabled = true; upload.disabled = true;
      card('你', prompt);
      const answer = card('Agent Runtime', '正在执行…');
      try {
        const state = await status();
        const response = await fetch('/api/chat/turns', {method:'POST', headers:{'Content-Type':'application/json', 'X-AgentRT-CSRF':state.csrf},
          body:JSON.stringify({conversation_id:conversation, request_id:'req_' + crypto.randomUUID().replaceAll('-', ''), prompt,
            mode:document.getElementById('chatMode').value, use_memory:document.getElementById('useAmemToggle').checked, use_knowledge:document.getElementById('useKnowledgeToggle').checked,
            attachments, stream:true})});
        if (!response.ok) throw new Error((await response.json()).error);
        const reader = response.body.getReader(), decoder = new TextDecoder();
        let buffer = '', completed = false;
        while (true) {
          const {value, done} = await reader.read();
          buffer += done ? decoder.decode() : decoder.decode(value, {stream:true});
          let end;
          while ((end = buffer.indexOf('\n\n')) >= 0) {
            const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
            const kind = frame.split('\n').find(line => line.startsWith('event: '))?.slice(7);
            const dataLine = frame.split('\n').find(line => line.startsWith('data: '));
            if (!dataLine) continue;
            const data = JSON.parse(dataLine.slice(6));
            if (kind === 'delta') update(answer, data.text || data.delta || '');
            if (kind === 'complete') { update(answer, data.answer); notice.textContent = data.status; completed = true; }
            if (kind === 'failed') throw new Error(data.error);
          }
          if (done) break;
        }
        if (!completed) throw new Error('连接已中断，未收到完成确认。请核对历史记录。');
        promptInput.value = ''; attachments = []; upload.value = '';
        await refreshConversations();
      } catch (error) { notice.textContent = error.message; }
      finally { button.disabled = false; upload.disabled = false; }
    });
    try { const state = await status(); document.getElementById('chatModelBadge').textContent = state.model;
      document.getElementById('sideKbCount').textContent = String(state.knowledge.documents);
      document.getElementById('sideAmemCount').textContent = String(state.amem_count);
      await refreshConversations();
    }
    catch (error) { notice.textContent = error.message; }
  });
})();
