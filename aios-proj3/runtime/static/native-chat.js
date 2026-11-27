(() => {
  let conversation = 'conv_' + crypto.randomUUID().replaceAll('-', '');
  let attachments = [];
  const box = document.getElementById('messagesBox');
  const modeControl = document.getElementById('chatMode');
  const modeLabels = {
    llm: '直接问答', auto: '工具助手', team: '协作分析', engineering: '工程修复', lsfs: '文件操作'
  };
  const modeHints = {
    llm: '由当前模型直接回答。',
    auto: '模型可调用工具，再根据结果回答。',
    team: '由多个 Agent 分析后汇总。',
    engineering: '执行补丁生成与测试验收。',
    lsfs: '由本地文件工具执行，无需模型。'
  };
  let lastStatus;

  function updateModelBadge() {
    const mode = modeControl.value;
    document.getElementById('chatModeHint').textContent = modeHints[mode] || '';
    const badge = document.getElementById('chatModelBadge');
    if (mode === 'lsfs') {
      badge.textContent = '本地文件工具';
      return;
    }
    if (!lastStatus) return;
    const profile = lastStatus.model_profile;
    if (!lastStatus.model_configured) {
      badge.textContent = '模型未启用';
      return;
    }
    const check = profile?.connection;
    const diagnostic = check?.ok ? '模型列表已验证' : check ? '连接有异常' : '连接未验证';
    badge.textContent = [profile?.profile_label, lastStatus.model, diagnostic].filter(Boolean).join(' · ');
  }

  function renderReply(answer, turn, fallbackRoute = '') {
    const article = answer.parentElement;
    article.querySelector('strong').textContent = turn.status === 'failed' ? '请求失败' : 'AIOS Runtime';
    const route = turn.model_route;
    const routeLabel = route?.kind === 'local' ? '本地文件工具' : route?.kind === 'stub' ? '固定模型桩'
      : [route?.profile_label, route?.model].filter(Boolean).join(' · ') || fallbackRoute;
    article.querySelector('.chat-message-meta').textContent =
      [modeLabels[turn.mode], routeLabel].filter(Boolean).join(' · ');
    update(answer, turn.answer || '运行未确认，请查看执行记录。');
    if (turn.status === 'failed' && turn.mode !== 'lsfs') {
      const link = document.createElement('a');
      link.href = '/console/?view=models';
      link.className = 'chat-model-link';
      link.textContent = '打开模型设置与连接测试';
      answer.append(link);
    }
  }
  function card(role, text = '') {
    const article = document.createElement('article');
    article.className = role === '你' ? 'panel chat-message--user' : 'panel';
    document.getElementById('chatIntro').hidden = true;
    const label = document.createElement('strong');
    label.textContent = role;
    const content = document.createElement('div');
    const meta = document.createElement('span');
    meta.className = 'chat-message-meta';
    article.append(label, meta, content);
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
    lastStatus = await response.json();
    updateModelBadge();
    return lastStatus;
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
      button.type = 'button';
      button.dataset.conversation = entry.id;
      button.classList.toggle('active', entry.id === conversation);
      button.textContent = entry.title;
      button.onclick = async () => {
        try {
          const response = await fetch('/api/chat/conversations/' + encodeURIComponent(entry.id));
          if (!response.ok) throw new Error('会话读取失败');
          const chat = await response.json();
          conversation = chat.id;
          attachments = [];
          box.replaceChildren();
          document.getElementById('currConvLabel').textContent = entry.title;
          document.getElementById('chatAttachment').value = '';
          document.getElementById('chatFeedback').textContent = '';
          document.getElementById('chatIntro').hidden = chat.turns.length > 0;
          for (const item of list.children) item.classList.toggle('active', item === button);
          for (const turn of chat.turns) {
            card('你', turn.prompt);
            renderReply(card('AIOS Runtime'), turn);
          }
        } catch (error) { card('错误', error.message); }
      };
      list.append(button);
    }
  }
  document.addEventListener('DOMContentLoaded', async () => {
    const form = document.getElementById('chatForm');
    const button = document.getElementById('sendChatBtn');
    const notice = document.getElementById('chatFeedback');
    const upload = document.getElementById('chatAttachment');
    if (window.matchMedia('(max-width: 850px)').matches) document.getElementById('chatHistory').open = false;
    modeControl.addEventListener('change', updateModelBadge);
    updateModelBadge();
    window.addEventListener('pageshow', (event) => {
      if (event.persisted) status().catch(error => { notice.textContent = error.message; });
    });
    upload.onchange = async () => {
      upload.disabled = true; button.disabled = true;
      try {
        const files = [...upload.files];
        if (files.length > 4 || files.some(file => file.size > 8 * 1024 * 1024)) throw new Error('最多 4 个文件，单文件最多 8 MiB');
        if (files.some(file => file.size === 0)) throw new Error('附件不能为空');
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
    document.getElementById('newChatBtn').onclick = () => {
      conversation = 'conv_' + crypto.randomUUID().replaceAll('-', '');
      attachments = []; box.replaceChildren(); notice.textContent = ''; upload.value = '';
      document.getElementById('currConvLabel').textContent = '新对话';
      document.getElementById('chatIntro').hidden = false;
      for (const item of document.getElementById('convList').children) item.classList.remove('active');
      document.getElementById('chatPrompt').focus();
    };
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (button.disabled) return;
      const promptInput = document.getElementById('chatPrompt');
      const prompt = promptInput.value.trim();
      if (!prompt) return;
      button.disabled = true; upload.disabled = true;
      const mode = modeControl.value;
      const conversationId = conversation;
      const queuedAttachments = [...attachments];
      let answer;
      try {
        const state = await status();
        if (mode !== 'lsfs' && !state.model_configured) {
          notice.textContent = '请先在右上角的模型设置启用模型。';
          return;
        }
        notice.textContent = '';
        card('你', prompt);
        answer = card('AIOS Runtime', '正在执行…');
        answer.parentElement.querySelector('.chat-message-meta').textContent =
          [modeLabels[mode], mode === 'lsfs' ? '本地文件工具' : state.model].filter(Boolean).join(' · ');
        const response = await fetch('/api/chat/turns', {method:'POST', headers:{'Content-Type':'application/json', 'X-AgentRT-CSRF':state.csrf},
          body:JSON.stringify({conversation_id:conversationId, request_id:'req_' + crypto.randomUUID().replaceAll('-', ''), prompt,
            mode, use_memory:document.getElementById('useAmemToggle').checked, use_knowledge:document.getElementById('useKnowledgeToggle').checked,
            attachments:queuedAttachments, stream:true})});
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
            if (kind === 'complete') {
              renderReply(answer, data, mode === 'lsfs' ? '本地文件工具' : state.model);
              notice.textContent = '';
              completed = true;
              if (data.status === 'succeeded') {
                promptInput.value = ''; attachments = []; upload.value = '';
              }
            }
            if (kind === 'failed') throw new Error(data.error);
          }
          if (done) break;
        }
        if (!completed) throw new Error('连接已中断，未收到完成确认。请核对历史记录。');
        await refreshConversations();
      } catch (error) {
        if (answer) renderReply(answer, {status:'failed', answer:error.message, mode});
        else notice.textContent = error.message;
      }
      finally { button.disabled = false; upload.disabled = false; }
    });
    try {
      await status();
      await refreshConversations();
    }
    catch (error) { notice.textContent = error.message; }
  });
})();
