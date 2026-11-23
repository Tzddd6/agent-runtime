document.addEventListener('DOMContentLoaded', () => {
  const preset = document.getElementById('preset');
  const supported = ['dynamic', 'document', 'kernel', 'resources', 'agents', 'ipc', 'litecua', 'llm', 'team', 'engineering'];
  for (const option of preset.options) option.disabled = !supported.includes(option.value);
  preset.value = 'dynamic';
  preset.dispatchEvent(new Event('change'));
  const notice = document.getElementById('globalNotice');
  notice.style.display = 'block';
  notice.textContent = '原生 AIOS 内核工作台 · 全部 10 类场景、A-Mem、LSFS、LiteCUA 桌面、AgentHub、知识库与流式对话均已原生接通。';
  const policy = document.getElementById('policy');
  if (policy) policy.disabled = true;
});
