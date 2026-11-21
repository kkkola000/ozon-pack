document.querySelectorAll('.dropdown').forEach((menu) => {
  let timer;
  let openedByHover = false;
  menu.addEventListener('pointerenter', (event) => {
    if (event.pointerType === 'touch') return;
    clearTimeout(timer);
    if (!menu.open) {
      menu.open = true;
      openedByHover = true;
    }
  });
  menu.querySelector('summary').addEventListener('click', (event) => {
    // A first mouse click after hover confirms the open menu instead of closing it.
    if (event.detail && openedByHover && menu.open) event.preventDefault();
    openedByHover = false;
  });
  menu.addEventListener('pointerleave', (event) => {
    if (event.pointerType === 'touch') return;
    timer = setTimeout(() => { if (!menu.contains(document.activeElement)) menu.open = false; }, 150);
  });
  menu.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { menu.open = false; menu.querySelector('summary').focus(); }
  });
  menu.addEventListener('focusout', () => {
    setTimeout(() => { if (!menu.contains(document.activeElement)) menu.open = false; }, 0);
  });
  document.addEventListener('click', (event) => { if (!menu.contains(event.target)) menu.open = false; });
});

document.querySelectorAll('form[data-pending]').forEach((form) => {
  form.addEventListener('submit', (event) => {
    if (form.dataset.busy === 'true') { event.preventDefault(); return; }
    const button = event.submitter || form.querySelector('button[type=submit]');
    if (!button) return;
    form.dataset.busy = 'true';
    form.setAttribute('aria-busy', 'true');
    button.dataset.originalText = button.textContent;
    button.disabled = true;
    button.classList.add('is-pending');
    button.textContent = button.dataset.pendingText || 'Сохраняем…';
  });
});

window.addEventListener('pageshow', () => {
  document.querySelectorAll('form[data-busy=true]').forEach((form) => {
    form.dataset.busy = 'false';
    form.removeAttribute('aria-busy');
    const button = form.querySelector('button.is-pending');
    if (button) {
      button.disabled = false;
      button.classList.remove('is-pending');
      button.textContent = button.dataset.originalText;
    }
  });
});

const catalogStatus = document.querySelector('[data-catalog-status]');
if (catalogStatus) {
  const labels = JSON.parse(catalogStatus.dataset.syncLabels);
  const cards = [...catalogStatus.querySelectorAll('[data-account-id]')];
  const formatter = new Intl.DateTimeFormat('ru-RU', { timeZone: 'Europe/Moscow', dateStyle: 'short', timeStyle: 'short' });
  const active = () => cards.some(card => ['queued', 'running', 'retrying'].includes(card.dataset.runStatus));
  const poll = async () => {
    let delay = 30000;
    if (!document.hidden) {
      try {
        const response = await fetch('/api/v2/catalog/sync', { headers: { Accept: 'application/json' } });
        if ([401, 403].includes(response.status)) return;
        if (!response.ok) throw new Error('status unavailable');
        const statuses = await response.json();
        for (const card of cards) {
          const row = statuses.find(item => item.account_id === card.dataset.accountId);
          if (!row || !labels[row.status]) continue;
          const changed = card.dataset.runId !== row.id || card.dataset.runStatus !== row.status;
          if (changed && row.status === 'succeeded') document.querySelector('#catalog-refresh').hidden = false;
          card.dataset.runId = row.id;
          card.dataset.runStatus = row.status;
          const badge = card.querySelector('[data-sync-badge]');
          badge.textContent = labels[row.status][0];
          badge.className = `badge ${labels[row.status][1]}`;
          card.querySelector('[data-sync-progress]').textContent = `Карточек: ${row.items} · Страниц: ${row.pages}`;
          card.querySelector('[data-sync-time]').textContent = 'Последняя полная загрузка: ' + (row.last_success_at ? formatter.format(new Date(row.last_success_at)) : '—');
          const error = card.querySelector('[data-sync-error]');
          error.textContent = row.error || '';
          error.hidden = !row.error;
          error.className = row.status === 'failed' ? 'small access-error' : 'small muted';
        }
        delay = active() ? 5000 : 30000;
      } catch (_) { /* Keep the saved list usable when the status endpoint is temporarily unavailable. */ }
    }
    window.setTimeout(poll, delay);
  };
  window.setTimeout(poll, active() ? 1500 : 30000);
}
