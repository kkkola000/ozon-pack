(() => {
  const root = document.querySelector('[data-assembly]');
  if (!root) return;
  const csrf = document.querySelector('meta[name=csrf-token]').content;
  const modal = root.querySelector('[data-assembly-modal]');
  const backdrop = root.querySelector('[data-assembly-backdrop]');
  const storageKey = `pack-v2:assembly:pending:${root.dataset.user}`;
  const queue = [];
  const drafts = new Map();
  const queuedValues = new Map();
  let lastIssue = null;
  let rendering = false;
  let session = JSON.parse(root.dataset.session);
  let busy = false;
  let pending = null;
  let recovery = null;
  let paused = false;
  const find = (name) => root.querySelector(`[data-${name}]`);
  const uuid = () => crypto.randomUUID();
  const input = () => session ? root.querySelector('#assembly-code') : root.querySelector('#assembly-home-code');
  const stages = { opening: 'Проверяем отправление', scanning: 'Проверка товаров', blocked: 'Проверка приостановлена',
    verified: 'Товары проверены', stale: 'Нужна повторная проверка', expired: 'Время истекло', cancelled: 'Отменено' };
  const make = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  };
  const focus = () => { if (!document.activeElement?.dataset.gtdUnit && !input().readOnly && !document.hidden) input().focus(); };
  function notice(message, error = false) {
    const target = session ? find('assembly-message') : find('home-message');
    target.hidden = !message;
    target.textContent = message || '';
    target.className = `notice ${error ? 'error' : ''}`;
  }
  function render() {
    modal.hidden = backdrop.hidden = !session;
    document.body.classList.toggle('assembly-is-open', Boolean(session));
    root.querySelector('.assembly-home-scanner').hidden = Boolean(session);
    const waiting = busy && !session || session?.status === 'opening';
    root.querySelectorAll('[data-scan-input]').forEach(node => { node.readOnly = paused || waiting || Boolean(session && session.status !== 'scanning'); });
    root.querySelectorAll('[data-network-retry]').forEach(node => { node.hidden = !paused; });
    if (!session) return;
    find('assembly-account').textContent = `Ozon · ${session.account_title}`;
    find('assembly-number').textContent = session.posting_number;
    find('assembly-stage').textContent = stages[session.status] || session.status;
    find('assembly-stage').className = `badge ${session.status === 'verified' ? 'ok' : session.status === 'opening' ? 'accent' : ''}`;
    const scanner = find('scanner-state');
    scanner.textContent = session.status === 'opening' ? 'Проверяем…' : paused ? 'Нет связи' : session.status === 'scanning' ? 'Сканер готов' : stages[session.status];
    scanner.className = `badge ${session.status === 'opening' ? 'accent is-loading' : session.status === 'scanning' && !paused ? 'ok' : 'warn'}`;
    find('assembly-progress').max = Math.max(1, session.need);
    find('assembly-progress').value = session.scanned;
    find('assembly-count').textContent = `${session.scanned} из ${session.need}`;
    find('assembly-footer').textContent = `Склад ${session.warehouse || '—'}`;
    rendering = true;
    try {
      const container = find('assembly-items');
      const editing = document.activeElement?.dataset.gtdUnit;
      const selection = editing ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
      container.replaceChildren();
      let highlighted = false;
      for (const item of session.items) {
        const complete = item.scanned >= item.need;
        const selected = !complete && !highlighted;
        if (selected) highlighted = true;
        const card = make('article', `assembly-item ${complete ? 'complete' : selected ? 'selected' : ''}`);
        card.dataset.itemId = item.id;
        const info = make('div', 'product-name');
        if (item.image_url?.startsWith('https://')) {
          const img = make('img'); img.src = item.image_url; img.width = img.height = 64; img.alt = ''; img.referrerPolicy = 'no-referrer'; info.append(img);
        }
        const description = make('div', 'assembly-description');
        description.append(make('b', '', item.name));
        const sku = item.references.filter(pair => pair[0] === 'sku').map(pair => pair[1]).join(', ');
        description.append(make('div', 'muted small', `Артикул ${item.article || '—'}${sku ? ' · SKU ' + sku : ''}`));
        description.append(make('div', 'muted small code-value', `ШК ${item.codes.join(', ') || '—'}`));
        if (selected && session.status === 'scanning') description.append(make('span', 'badge accent', 'Сканируйте этот товар'));
        info.append(description); card.append(info);
        const extra = session.data?.items?.[item.id];
        if (extra ? extra.cis : item.marking_hint) {
          const state = extra?.mark_status || 'pending';
          const mark = make('span', `assembly-mark ${state}`, 'М');
          mark.title = state === 'accepted' ? 'Честный знак принят Ozon' : state === 'rejected' ? 'Честный знак не прошёл проверку' : 'Честный знак не проверен';
          mark.setAttribute('aria-label', mark.title); card.append(mark);
        }
        for (const [index, unit] of (extra?.units || []).entries()) {
          if (!unit.gtd_required) continue;
          const form = make('form', 'assembly-gtd'); form.dataset.gtdForm = unit.id;
          const field = make('input'); field.type = 'text'; field.autocomplete = 'off'; field.maxLength = 100;
          field.placeholder = `Указать ГТД${extra.units.length > 1 ? ' · ' + (index + 1) + '/' + extra.units.length : ''}`;
          field.setAttribute('aria-label', field.placeholder); field.dataset.gtdUnit = unit.id;
          field.value = drafts.get(unit.id) ?? unit.gtd;
          field.disabled = !unit.scanned || paused || session.status !== 'scanning' || ['prepared', 'checking', 'uncertain'].includes(session.data?.status);
          form.append(field);
          const label = unit.gtd_status === 'accepted' ? 'ГТД принята Ozon' : unit.gtd_status === 'rejected' ? 'Ozon не принял ГТД' : unit.gtd ? 'ГТД сохранена' : 'Введите ГТД и нажмите Enter';
          form.append(make('span', `small ${unit.gtd_status === 'accepted' ? 'access-ok' : unit.gtd_status === 'rejected' ? 'access-error' : 'muted'}`, label));
          description.append(form);
        }
        const amount = make('div', 'assembly-amount'); amount.append(make('b', '', `${complete ? '✓ ' : ''}${item.scanned} / ${item.need}`), make('span', 'small muted', complete ? 'проверено' : 'шт')); card.append(amount);
        container.append(card);
      }
      if (editing) {
        const field = root.querySelector(`[data-gtd-unit="${editing}"]`);
        if (field && !field.disabled) { field.focus(); field.setSelectionRange(...selection); }
      }
    } finally { rendering = false; }
    const verify = root.querySelector('[data-assembly-action=verify]');
    verify.hidden = session.status !== 'scanning';
    verify.disabled = busy || paused || queue.length > 0 || session.need === 0 || session.scanned !== session.need || session.data?.can_verify === false;
    root.querySelector('[data-assembly-action=retry]').hidden = session.status !== 'blocked';
    root.querySelector('[data-assembly-action=check_data]').hidden = !['uncertain', 'rejected'].includes(session.data?.status);
    root.querySelector('[data-assembly-action=cancel]').disabled = busy || paused || queue.length > 0;
    find('assembly-queue').hidden = !['verified', 'cancelled'].includes(session.status);
    if (!paused) {
      if (session.status === 'opening') notice(session.refresh_status === 'retrying' ? 'Ozon временно недоступен. Повторяем проверку…' : 'Получаем актуальный состав и состояние отправления…');
      else if (session.status === 'verified') notice('Товары проверены. Готовность отправления на площадке проверяется отдельно.');
      else if (lastIssue) notice(lastIssue, true);
      else if (session.message) notice(session.message, session.status === 'stale');
      else if (session.data?.message) notice(session.data.message, ['rejected', 'uncertain', 'blocked'].includes(session.data.status));
      else if (session.data?.status === 'accepted') notice('');
    }
    input().placeholder = session.status === 'scanning' ? session.data?.pending_unit_id ? 'Сканируйте код «Честный знак» этого товара…' : 'Сканируйте товар…' : stages[session.status];
    if (['prepared', 'checking'].includes(session.data?.status)) { scanner.textContent = 'Проверяем данные…'; scanner.className = 'badge accent is-loading'; }
  }
  function apply(next) {
    if (next && session?.id === next.id && next.revision < session.revision) return;
    session = next;
    render();
  }
  async function decode(response) {
    const data = await response.json();
    if ([401, 403].includes(response.status)) { paused = true; notice(data.detail || 'Войдите в панель заново', true); render(); window.location.assign('/login?next=%2Fpack'); }
    return data;
  }
  function persist(request) {
    const metadata = { ...request, payload: { ...request.payload } };
    metadata.needsCode = Object.hasOwn(metadata.payload, 'code');
    delete metadata.payload.code;
    metadata.needsGtd = request.action === 'gtd';
    delete metadata.payload.value;
    try { sessionStorage.setItem(storageKey, JSON.stringify(metadata)); } catch (_) { /* Retry still uses the in-memory command. */ }
  }
  async function send(request) {
    busy = true; pending = request; persist(request); render();
    try {
      const response = await fetch(request.url, { method: 'POST', signal: AbortSignal.timeout(20000), headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(request.payload) });
      const result = await decode(response);
      sessionStorage.removeItem(storageKey); pending = recovery = null;
      lastIssue = result.ok === false && !result.pending ? result.message || result.detail : null;
      if (request.action === 'gtd') {
        queuedValues.delete(request.payload.unit_id);
        if (result.ok) drafts.delete(request.payload.unit_id);
      }
      if (result.session) apply(result.session);
      notice(result.message || result.detail || '', result.ok === false && !result.pending);
      if (!response.ok && !result.session) {
        const current = await fetch('/api/v2/assembly/current');
        if (current.ok) apply((await current.json()).session);
      }
    } catch (_) {
      paused = true;
      notice('Связь прервана. Повторите запрос — скан не будет засчитан дважды.', true);
    } finally { busy = false; render(); if (!paused) { focus(); drain(); } }
  }
  function build(action, code, target) {
    const payload = { command_id: uuid() };
    if (action === 'open') {
      if (root.dataset.account) payload.account_id = root.dataset.account;
      if (target) payload.shipment_id = target; else payload.code = code;
      return { action, url: '/api/v2/assembly/open', payload };
    }
    Object.assign(payload, { lease_token: session.lease_token, revision: session.revision });
    if (action === 'gtd') { payload.unit_id = target; payload.value = code; }
    else if (code !== undefined) payload.code = code;
    return { action, url: `/api/v2/assembly/${session.id}/${action}`, payload };
  }
  function drain() {
    if (busy || paused || !queue.length) return;
    const task = queue.shift();
    const request = task.request || build(task.action, task.code, task.target);
    send(request);
  }
  root.querySelectorAll('[data-scan-form]').forEach(form => form.addEventListener('submit', event => {
    event.preventDefault();
    const field = form.querySelector('input');
    const code = field.value; field.value = '';
    if (!code || field.readOnly) return;
    if (recovery?.needsCode) {
      const request = { ...recovery, payload: { ...recovery.payload, code } };
      paused = false; queue.unshift({ request }); drain(); return;
    }
    queue.push({ action: session ? 'scan' : 'open', code }); drain();
  }));
  root.addEventListener('input', event => {
    if (event.target.dataset.gtdUnit) drafts.set(event.target.dataset.gtdUnit, event.target.value);
  });
  function saveGtd(field) {
    const unitId = field.dataset.gtdUnit, value = field.value;
    if (rendering || !field.isConnected || field.disabled || !value || queuedValues.get(unitId) === value) return;
    if (recovery?.needsGtd) {
      if (recovery.payload.unit_id !== unitId) { notice('Сначала повторите ГТД последнего запроса', true); return; }
      paused = false; queue.unshift({ request: { ...recovery, payload: { ...recovery.payload, value } } });
    } else if (paused) return;
    else queue.push({ action: 'gtd', target: unitId, code: value });
    queuedValues.set(unitId, value); drain();
  }
  root.addEventListener('submit', event => {
    if (!event.target.dataset.gtdForm) return;
    event.preventDefault(); const field = event.target.querySelector('input'); field.blur(); saveGtd(field); if (!input().readOnly) input().focus();
  });
  root.addEventListener('change', event => {
    if (event.target.dataset.gtdUnit && drafts.has(event.target.dataset.gtdUnit)) saveGtd(event.target);
  });
  root.addEventListener('focusout', event => {
    if (event.target.dataset.gtdUnit && drafts.has(event.target.dataset.gtdUnit)) saveGtd(event.target);
  });
  root.querySelectorAll('[data-clear-code]').forEach(button => button.addEventListener('click', () => { input().value = ''; focus(); }));
  root.querySelectorAll('[data-open-shipment]').forEach(button => button.addEventListener('click', () => { if (!busy && !paused) { queue.push({ action: 'open', target: button.dataset.openShipment }); drain(); } }));
  root.querySelectorAll('[data-assembly-action]').forEach(button => button.addEventListener('click', () => { if (!busy && !paused && session) { queue.push({ action: button.dataset.assemblyAction }); drain(); } }));
  root.querySelectorAll('[data-network-retry]').forEach(button => button.addEventListener('click', () => {
    if (busy) return;
    paused = false;
    if (pending) send(pending);
    else if (recovery) recover().then(() => { render(); if (recovery?.needsCode && !paused) notice('Повторите последний скан'); focus(); });
  }));
  async function recover() {
    try { recovery = JSON.parse(sessionStorage.getItem(storageKey)); } catch (_) { sessionStorage.removeItem(storageKey); }
    if (!recovery) return;
    try {
      const response = await fetch(`/api/v2/assembly/commands/${recovery.payload.command_id}`);
      if (response.ok) { const result = await response.json(); if (result.session) apply(result.session); sessionStorage.removeItem(storageKey); recovery = null; }
      else if (response.status === 404 && !recovery.needsCode && !recovery.needsGtd) { await send(recovery); }
      else if (response.status === 404) { notice(recovery.needsGtd ? 'Повторно введите ГТД для последнего запроса' : 'Повторите последний незавершённый скан'); }
      else { paused = true; notice('Войдите в панель заново', true); }
    } catch (_) { paused = true; notice('Не удалось проверить предыдущий запрос. Повторите попытку.', true); }
  }
  async function poll() {
    if (!busy && !paused && session && !document.hidden && ['opening', 'scanning', 'blocked'].includes(session.status)) {
      try {
        const response = await fetch(`/api/v2/assembly/${session.id}`);
        if (response.ok) {
          const next = (await response.json()).session; apply(next);
          if (session.status === 'opening' && ['succeeded', 'failed', 'stale'].includes(session.refresh_status)) { queue.push({ action: 'activate' }); drain(); }
        }
      } catch (_) { /* Read failures never advance the product counter. */ }
    }
    setTimeout(poll, session?.status === 'opening' || ['prepared', 'checking'].includes(session?.data?.status) ? 1000 : 5000);
  }
  setInterval(async () => {
    if (!session || busy || paused || document.hidden || !['opening', 'scanning', 'blocked'].includes(session.status)) return;
    try {
      const response = await fetch(`/api/v2/assembly/${session.id}/heartbeat`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify({ lease_token: session.lease_token }) });
      const result = await decode(response); if (result.session) apply(result.session);
    } catch (_) { /* The server lease remains authoritative. */ }
  }, 20000);
  root.addEventListener('keydown', event => {
    if (event.target.matches('[data-scan-input]') && !event.target.readOnly && (event.key === '\x1d' || event.key === 'GroupSeparator' || event.ctrlKey && event.code === 'BracketRight')) {
      event.preventDefault(); event.target.setRangeText('\x1d', event.target.selectionStart, event.target.selectionEnd, 'end'); return;
    }
    if (!session) return;
    if (event.key === 'Escape') input().focus();
    if (event.key === 'Tab') {
      const nodes = [...modal.querySelectorAll('input, button, a[href]')].filter(node => !node.disabled && node.getClientRects().length);
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  render();
  recover().then(() => {
    if (!recovery && root.dataset.open && !session) { queue.push({ action: 'open', target: root.dataset.open }); drain(); }
    focus(); poll();
  });
})();
