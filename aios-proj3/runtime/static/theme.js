(() => {
  const key = 'aios_guizang_theme';
  const choices = new Set(['ink-classic', 'swiss-ikb', 'forest-ink', 'midnight-ink']);
  const root = document.documentElement;
  const apply = (value) => {
    const theme = choices.has(value) ? value : 'ink-classic';
    root.dataset.theme = theme;
    const select = document.getElementById('themeSwitcher');
    if (select) select.value = theme;
  };
  try {
    apply(localStorage.getItem(key));
  } catch {
    apply(null);
  }
  document.addEventListener('DOMContentLoaded', () => {
    const select = document.getElementById('themeSwitcher');
    if (!select) return;
    select.value = root.dataset.theme;
    select.addEventListener('change', () => {
      apply(select.value);
      try { localStorage.setItem(key, root.dataset.theme); } catch {}
    });
  });
  window.addEventListener('storage', (event) => {
    if (event.key === key || event.key === null) apply(event.newValue);
  });
})();
