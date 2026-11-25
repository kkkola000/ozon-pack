(() => {
  const root = document.querySelector('[data-assembly]');
  if (!root) return;
  const csrf = document.querySelector('meta[name=csrf-token]').content;
  const modal = root.querySelector('[data-assembly-modal]');
  const backdrop = root.querySelector('[data-assembly-backdrop]');
  const storageKey = `pack-v2:assembly:pending:${root.dataset.user}`;
  const queue = [];
  const drafts = new Map();
  const omissionDrafts = new Map();
  const queuedValues = new Map();
  const labelQueue = [];
  const labelQueued = new Set();
  const labelAttempts = new Map();
  const downloadedLabels = new Set();
  const labelErrors = new Map();
  let labelBusy = false;
  let manualPrintBusy = false;
  let lastIssue = null;
  let rendering = false;
  let session = JSON.parse(root.dataset.session);
  let busy = false;
  let pending = null;
  let recovery = null;
  let paused = false;
  let layoutDraft = null;
  let draggingUnit = null;
  const find = (name) => root.querySelector(`[data-${name}]`);
  const uuid = () => {
    if (crypto.randomUUID) return crypto.randomUUID();
    // getRandomValues also works when the panel is opened by IP over HTTP.
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  };
  const input = () => session ? root.querySelector('#assembly-code') : root.querySelector('#assembly-home-code');
  const stages = { opening: 'Проверяем отправление', scanning: 'Проверка товаров', blocked: 'Проверка приостановлена',
    verified: 'Товары проверены', preparing: 'Ozon обрабатывает сборку', ready: 'Готов к отгрузке', stale: 'Нужна повторная проверка', expired: 'Время истекло', cancelled: 'Отменено' };
  const make = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  };
  const focus = () => { if (!document.activeElement?.closest('.assembly-gtd') && !input().readOnly && !document.hidden) input().focus(); };
  function notice(message, error = false) {
    const target = session ? find('assembly-message') : find('home-message');
    target.hidden = !message;
    target.textContent = message || '';
    target.className = `notice ${error ? 'error' : ''}`;
  }
  function render() {
    if (draggingUnit) return;
    modal.hidden = backdrop.hidden = !session;
    document.body.classList.toggle('assembly-is-open', Boolean(session));
    root.querySelector('.assembly-home-scanner').hidden = Boolean(session);
    const waiting = busy && !session || session?.status === 'opening';
    root.querySelectorAll('[data-scan-input]').forEach(node => { node.readOnly = paused || waiting || Boolean(layoutDraft) || Boolean(session && session.status !== 'scanning'); });
    root.querySelectorAll('[data-network-retry]').forEach(node => { node.hidden = !paused; });
    if (!session) return;
    find('assembly-account').textContent = `${session.market_title || 'Ozon'} · ${session.account_title}`;
    find('assembly-number').textContent = session.posting_number;
    find('assembly-stage').textContent = stages[session.status] || session.status;
    renderLabels();
    find('assembly-stage').className = `badge ${['verified', 'ready'].includes(session.status) ? 'ok' : session.status === 'opening' ? 'accent' : ''}`;
    const scanner = find('scanner-state');
    scanner.textContent = session.status === 'opening' ? 'Проверяем…' : paused ? 'Нет связи' : session.status === 'scanning' ? 'Сканер готов' : stages[session.status];
    scanner.className = `badge ${['opening', 'preparing'].includes(session.status) && !['rejected', 'uncertain', 'stale'].includes(session.readiness?.status) ? 'accent is-loading' : session.status === 'scanning' && !paused ? 'ok' : 'warn'}`;
    find('assembly-progress').max = Math.max(1, session.need);
    find('assembly-progress').value = session.scanned;
    find('assembly-count').textContent = `${session.scanned} из ${session.need}`;
    find('assembly-footer').textContent = `Склад ${session.warehouse || '—'}`;
    rendering = true;
    try {
      const container = find('assembly-items');
      const editing = document.activeElement?.dataset.gtdUnit || document.activeElement?.dataset.gtdAbsent;
      const editingAbsent = Boolean(document.activeElement?.dataset.gtdAbsent);
      const selection = editing && !editingAbsent ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
      container.replaceChildren();
      const editable = session.layout?.editable && session.status === 'scanning';
      const groups = layoutDraft || session.layout?.groups || [];
      const display = [];
      if (editable && groups.length) {
        groups.forEach((group, index) => {
          const zone = make('section', 'assembly-layout-group'); zone.dataset.layoutGroup = index;
          zone.append(make('h3', 'small', `Отправление ${index + 1}`)); container.append(zone);
          for (const uid of group) {
            const unit = session.layout.units.find(u => u.id === uid);
            const item = session.items.find(i => i.id === unit?.item_id);
            if (item) display.push({ ...item, need: 1, scanned: unit.scanned ? 1 : 0, _unitId: uid, _zone: zone });
          }
        });
      } else display.push(...session.items);
      let highlighted = false;
      for (const item of display) {
        const complete = item.scanned >= item.need;
        const selected = !complete && !highlighted;
        if (selected) highlighted = true;
        const card = make('article', `assembly-item ${complete ? 'complete' : selected ? 'selected' : ''}`);
        card.dataset.itemId = item.id;
        if (item._unitId) {
          card.draggable = !busy && !paused && !queue.length; card.dataset.layoutUnit = item._unitId;
          const grip = make('button', 'assembly-layout-handle', '⠿'); grip.type = 'button'; grip.draggable = card.draggable;
          grip.title = 'Перетащите товар в другое отправление. Клавиатура: Ctrl + стрелка вправо или влево';
          grip.setAttribute('aria-label', `Переместить ${item.name}`); grip.disabled = !card.draggable; card.append(grip);
        }
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
          mark.title = state === 'accepted' ? session.marketplace === 'yandex' ? 'Честный знак принят Яндекс Маркетом' : 'Честный знак принят Ozon' : state === 'rejected' ? 'Честный знак не прошёл проверку' : 'Честный знак не проверен';
          mark.setAttribute('aria-label', mark.title); card.append(mark);
        }
        for (const [index, unit] of (extra?.units || []).entries()) {
          if (item._unitId && unit.id !== item._unitId) continue;
          if (unit.market_cis_message) description.append(make('div', `small ${unit.cis_status === 'rejected' ? 'access-error' : unit.cis_status === 'accepted' ? 'access-ok' : 'muted'}`, unit.market_cis_message));
          if (!unit.gtd_required) continue;
          const form = make('form', 'assembly-gtd'); form.dataset.gtdForm = unit.id;
          const field = make('input'); field.type = 'text'; field.autocomplete = 'off'; field.maxLength = 100;
          field.placeholder = `Указать ГТД${extra.units.length > 1 ? ' · ' + (index + 1) + '/' + extra.units.length : ''}`;
          field.setAttribute('aria-label', field.placeholder); field.dataset.gtdUnit = unit.id;
          field.value = drafts.get(unit.id) ?? unit.gtd;
          const disabled = !unit.scanned || paused || session.status !== 'scanning' || ['prepared', 'checking', 'uncertain'].includes(session.data?.status);
          const omitted = omissionDrafts.get(unit.id) ?? unit.gtd_omitted;
          field.disabled = disabled || omitted;
          const choice = make('label', 'assembly-gtd-choice');
          const checkbox = make('input'); checkbox.type = 'checkbox'; checkbox.dataset.gtdAbsent = unit.id;
          checkbox.checked = Boolean(omitted); checkbox.disabled = disabled;
          choice.append(checkbox, document.createTextNode('Не указывать'));
          form.append(field); if (unit.gtd_omission_allowed !== false) form.append(choice);
          const label = unit.gtd_status === 'accepted' ? session.marketplace === 'yandex' ? 'ГТД принята Яндекс Маркетом' : 'ГТД принята Ozon' : unit.gtd_status === 'rejected' ? 'Площадка не приняла ГТД' : unit.gtd_omitted ? 'Выбор «Не указывать» сохранён' : unit.gtd ? 'ГТД сохранена' : 'Введите ГТД и нажмите Enter';
          form.append(make('span', `small ${unit.gtd_status === 'accepted' ? 'access-ok' : unit.gtd_status === 'rejected' ? 'access-error' : 'muted'}`, label));
          description.append(form);
        }
        const amount = make('div', 'assembly-amount'); amount.append(make('b', '', `${complete ? '✓ ' : ''}${item.scanned} / ${item.need}`), make('span', 'small muted', complete ? 'проверено' : 'шт')); card.append(amount);
        (item._zone || container).append(card);
      }
      if (editable && groups.length < Math.min(session.layout.units.length, 100)) {
        const drop = make('section', 'assembly-layout-new'); drop.dataset.layoutGroup = groups.length;
        drop.append(make('b', '', `Новое отправление ${groups.length + 1}`), make('span', 'muted small', 'Перетащите товар сюда'));
        container.append(drop);
      }
      if (layoutDraft) {
        const save = make('button', 'btn small assembly-layout-save', 'Сохранить'); save.type = 'button';
        save.dataset.saveLayout = ''; save.disabled = busy || paused || queue.length > 0; container.append(save);
      }
      for (const child of session.readiness?.children || []) {
        const link = make('a', 'btn assembly-layout-result', child.posting_number);
        link.href = `/pack?shipment_id=${encodeURIComponent(child.shipment_id)}`; container.append(link);
        const label = (session.labels || []).find(l => l.shipment_id === child.shipment_id && l.status === 'ready');
        if (label) { const pdf = make('a', 'btn small', 'Открыть PDF'); pdf.href = label.url; pdf.target = '_blank'; pdf.rel = 'noopener'; container.append(pdf); }
      }
      if (editing) {
        const field = root.querySelector(`[data-gtd-${editingAbsent ? 'absent' : 'unit'}="${editing}"]`);
        if (field && !field.disabled) { field.focus(); if (selection) field.setSelectionRange(...selection); }
      }
    } finally { rendering = false; }
    const verify = root.querySelector('[data-assembly-action=complete], [data-assembly-action=verify]');
    verify.dataset.assemblyAction = session.shipping_supported === false ? 'verify' : 'complete';
    verify.textContent = session.shipping_supported === false ? 'Проверить товары' : 'Завершить сборку';
    verify.hidden = !['scanning', ...(session.shipping_supported === false ? [] : ['verified'])].includes(session.status);
    verify.disabled = busy || paused || Boolean(layoutDraft) || queue.length > 0 || session.need === 0 || session.scanned !== session.need || session.data?.can_verify === false;
    root.querySelector('[data-assembly-action=retry]').hidden = session.status !== 'blocked';
    root.querySelector('[data-assembly-action=check_data]').hidden = !['uncertain', 'rejected'].includes(session.data?.status);
    root.querySelector('[data-assembly-action=cancel]').disabled = busy || paused || queue.length > 0;
    root.querySelector('[data-assembly-action=check_ready]').hidden = !session.readiness?.can_recheck;
    find('assembly-queue').hidden = !['ready', 'cancelled', ...(session.shipping_supported === false ? ['verified'] : [])].includes(session.status) && !['uncertain', 'rejected', 'stale'].includes(session.readiness?.status);
    if (!paused) {
      if (session.status === 'opening') notice(session.refresh_status === 'retrying' ? 'Площадка временно недоступна. Повторяем проверку…' : 'Получаем актуальный состав и состояние отправления…');
      else if (session.status === 'ready' || session.readiness?.status === 'accepted') notice('Ozon подтвердил готовность отправления к отгрузке.');
      else if (session.readiness?.message) notice(session.readiness.message, ['rejected', 'stale'].includes(session.readiness.status));
      else if (session.status === 'verified') notice(session.shipping_supported === false ? 'Товары и дополнительные данные проверены. Готовность и ярлыки Яндекс Маркета будут подключены следующим этапом.' : 'Товары проверены. Завершите сборку для подтверждения готовности Ozon.');
      else if (lastIssue) notice(lastIssue, true);
      else if (session.message) notice(session.message, session.status === 'stale');
      else if (session.data?.message) notice(session.data.message, ['rejected', 'uncertain', 'blocked'].includes(session.data.status));
      else if (session.data?.status === 'accepted') notice('');
    }
    input().placeholder = session.status === 'scanning' ? session.data?.pending_unit_id ? 'Сканируйте код «Честный знак» этого товара…' : session.scanned === session.need && session.data?.can_verify ? session.shipping_supported === false ? 'Нажмите «Проверить товары»…' : 'Сканируйте стикер отправления или завершите сборку…' : 'Сканируйте товар…' : stages[session.status];
    if (['prepared', 'checking'].includes(session.data?.status)) { scanner.textContent = 'Проверяем данные…'; scanner.className = 'badge accent is-loading'; }
  }
  function labelReceipt(label) { return `pack-v2:label:${root.dataset.user}:${label.key}`; }
  function wasDownloaded(label) {
    if (downloadedLabels.has(label.key)) return true;
    try { return sessionStorage.getItem(labelReceipt(label)) === 'done'; } catch (_) { return false; }
  }
  function renderLabels() {
    const labels = session.labels || [], ready = labels.filter(l => l.status === 'ready');
    const buttons = find('label-buttons'); buttons.replaceChildren();
    if (labels.length === 1 && ready.length) {
      const pdf = make('a', 'btn', 'Открыть PDF'); pdf.href = ready[0].url; pdf.target = '_blank'; pdf.rel = 'noopener'; buttons.append(pdf);
    }
    if (labels.length) {
      const print = make('button', 'btn', labels.length > 1 ? 'Печать стикеров' : 'Печать стикера');
      print.type = 'button'; print.dataset.labelPrintAll = ''; print.disabled = !ready.length || manualPrintBusy; buttons.append(print);
      const download = make('button', 'btn', labels.length > 1 ? 'Скачать стикеры' : 'Скачать стикер');
      download.type = 'button'; download.dataset.labelDownloadAll = ''; download.disabled = !ready.length; buttons.append(download);
    }
    if (labels.some(l => l.status === 'failed')) {
      const retry = make('button', 'btn', 'Повторить загрузку'); retry.type = 'button'; retry.dataset.assemblyAction = 'retry_label';
      retry.disabled = busy || paused; buttons.append(retry);
    }
    const failedDownload = ready.some(l => labelErrors.has(l.key));
    const status = find('label-status'); status.hidden = !labels.length;
    status.textContent = failedDownload ? 'Стикер получен. Не удалось скачать файл — нажмите «Скачать стикер».' : labels.length === 1 ? labels[0].message :
      `Получено стикеров: ${ready.length} из ${labels.length}${labels.some(l => l.status === 'failed') ? '. Повторите загрузку недостающих.' : ''}`;
    for (const label of ready) {
      if (!wasDownloaded(label) && (labelAttempts.get(label.key) || 0) < 3) queueLabel(label, true);
    }
  }
  function queueLabel(label, automatic) {
    if (labelQueued.has(label.key)) return;
    labelQueued.add(label.key); labelQueue.push({ label, automatic }); downloadLabel();
  }
  async function downloadLabel() {
    if (labelBusy || !labelQueue.length) return;
    const { label, automatic } = labelQueue.shift(); labelBusy = true;
    try {
      const current = (session?.labels || []).find(l => l.key === label.key && l.status === 'ready');
      if (!current || automatic && wasDownloaded(label)) return;
      labelAttempts.set(label.key, (labelAttempts.get(label.key) || 0) + 1);
      const response = await fetch(label.url + '?download=true', { signal: AbortSignal.timeout(20000) });
      if (!response.ok || !response.headers.get('content-type')?.startsWith('application/pdf')) throw new Error('Label unavailable');
      const file = await response.blob();
      if (!file.size || file.size > 8388608 || await file.slice(0, 5).text() !== '%PDF-') throw new Error('Invalid label');
      // An old result can arrive after a different assembly has opened.
      if (!(session?.labels || []).some(l => l.key === label.key && l.status === 'ready')) return;
      const url = URL.createObjectURL(file), anchor = document.createElement('a');
      anchor.href = url; anchor.download = `ozon-${label.posting_number.replace(/[^a-zA-Z0-9_-]/g, '')}.pdf`;
      document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      downloadedLabels.add(label.key);
      try { sessionStorage.setItem(labelReceipt(label), 'done'); } catch (_) { /* The manual button remains available. */ }
      labelErrors.delete(label.key);
    } catch (_) { labelErrors.set(label.key, true); }
    finally { labelBusy = false; labelQueued.delete(label.key); downloadLabel(); }
  }
  function apply(next) {
    if (next && session?.id === next.id && next.revision < session.revision) return;
    if (session?.id !== next?.id) { drafts.clear(); omissionDrafts.clear(); queuedValues.clear(); }
    if (session?.id !== next?.id || next?.status !== 'scanning') { layoutDraft = null; draggingUnit = null; }
    session = next;
    render();
    window.PackPrinting?.sync(session, printNotice);
  }
  function printNotice(text, state) {
    let note = root.querySelector('[data-print-status]');
    if (!note) { note = make('p', 'muted small printing-status'); note.dataset.printStatus = ''; note.setAttribute('role','status'); root.querySelector('.assembly-header-actions').append(note); }
    note.textContent = text; note.className = `muted small printing-status ${state || ''}`;
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
    metadata.needsGtd = request.action === 'gtd' && !request.payload.absent;
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
        if (result.ok) { drafts.delete(request.payload.unit_id); omissionDrafts.delete(request.payload.unit_id); }
      }
      if (request.action === 'layout' && result.ok) layoutDraft = null;
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
    if (action === 'gtd') { payload.unit_id = target; payload.value = code.value; if (code.absent) payload.absent = true; }
    else if (action === 'layout') payload.groups = code;
    else if (code !== undefined) payload.code = code;
    return { action, url: `/api/v2/assembly/${session.id}/${action}`, payload };
  }
  function drain() {
    if (busy || paused || !queue.length) return;
    const task = queue.shift();
    const request = task.request || build(task.action, task.code, task.target);
    send(request);
  }
  function moveUnit(uid, target) {
    if (!session?.layout?.editable || session.status !== 'scanning' || busy || paused || queue.length) return;
    const groups = (layoutDraft || session.layout.groups).map(group => [...group]);
    if (!session.layout.units.some(u => u.id === uid) || target < 0 || target > groups.length || target >= 100) return;
    if (groups[target]?.includes(uid)) return;
    const destination = groups[target] || [];
    if (target === groups.length) groups.push(destination);
    groups.forEach(group => { const at = group.indexOf(uid); if (at >= 0) group.splice(at, 1); });
    destination.push(uid); layoutDraft = groups.filter(group => group.length);
    render(); notice('Сохраните распределение товаров');
  }
  root.addEventListener('dragstart', event => {
    const card = event.target.closest('[data-layout-unit]');
    if (!card?.draggable || busy || paused) { event.preventDefault(); return; }
    draggingUnit = card.dataset.layoutUnit; event.dataTransfer.setData('text/plain', draggingUnit); event.dataTransfer.effectAllowed = 'move';
    root.classList.add('assembly-layout-dragging');
  });
  root.addEventListener('dragover', event => { if (draggingUnit && event.target.closest('[data-layout-group]')) event.preventDefault(); });
  root.addEventListener('drop', event => {
    const zone = event.target.closest('[data-layout-group]');
    if (!draggingUnit) return;
    event.preventDefault(); const uid = draggingUnit; draggingUnit = null; root.classList.remove('assembly-layout-dragging');
    if (zone) moveUnit(uid, Number(zone.dataset.layoutGroup)); else render();
  });
  root.addEventListener('dragend', () => { draggingUnit = null; root.classList.remove('assembly-layout-dragging'); render(); });
  root.addEventListener('pointerdown', event => {
    const grip = event.target.closest('.assembly-layout-handle');
    if (!grip || grip.disabled || event.pointerType === 'mouse') return;
    draggingUnit = grip.closest('[data-layout-unit]').dataset.layoutUnit; root.classList.add('assembly-layout-dragging'); grip.setPointerCapture(event.pointerId);
  });
  root.addEventListener('pointerup', event => {
    if (event.pointerType === 'mouse' || !draggingUnit) return;
    const zone = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-layout-group]');
    const uid = draggingUnit; draggingUnit = null; root.classList.remove('assembly-layout-dragging');
    if (zone) moveUnit(uid, Number(zone.dataset.layoutGroup)); else render();
  });
  root.addEventListener('pointercancel', event => {
    // Native mouse drag cancels the pointer stream; rebuilding its source aborts HTML drag-and-drop.
    if (event.pointerType === 'mouse') return;
    draggingUnit = null; root.classList.remove('assembly-layout-dragging'); render();
  });
  root.addEventListener('click', event => {
    const printButton = event.target.closest('[data-label-print-all]');
    if (printButton && !printButton.disabled && !manualPrintBusy) {
      manualPrintBusy = true; printButton.disabled = true;
      const current = session;
      Promise.all((current.labels || []).filter(l => l.status === 'ready').map(label => window.PackPrinting.label(label,current.id,(text,state) => { if (session?.id === current.id) printNotice(text,state); }))).finally(() => { manualPrintBusy = false; if (session) renderLabels(); });
    }
    if (event.target.closest('[data-label-download-all]')) {
      for (const label of session?.labels || []) if (label.status === 'ready') queueLabel(label, false);
    }
    if (event.target.closest('[data-assembly-action=retry_label]') && !busy && !paused && session) { queue.push({ action: 'retry_label' }); drain(); }
    if (event.target.closest('[data-save-layout]') && layoutDraft && !busy && !paused) {
      queue.push({ action: 'layout', code: layoutDraft.map(group => [...group]) }); drain();
    }
  });
  root.addEventListener('keydown', event => {
    const grip = event.target.closest('.assembly-layout-handle');
    if (!grip || !event.ctrlKey || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault(); const card = grip.closest('[data-layout-unit]');
    moveUnit(card.dataset.layoutUnit, Number(card.closest('[data-layout-group]').dataset.layoutGroup) + (event.key === 'ArrowRight' ? 1 : -1));
    root.querySelector(`[data-layout-unit="${card.dataset.layoutUnit}"] .assembly-layout-handle`)?.focus();
  });
  root.querySelectorAll('[data-scan-form]').forEach(form => form.addEventListener('submit', event => {
    event.preventDefault();
    const field = form.querySelector('input');
    const code = field.value; field.value = '';
    if (!code || field.readOnly) return;
    if (recovery?.needsCode) {
      const request = { ...recovery, payload: { ...recovery.payload, code } };
      paused = false; queue.unshift({ request }); drain(); return;
    }
    if (hasUnsavedGtd()) { notice('Укажите ГТД или выберите «Не указывать»', true); return; }
    queue.push({ action: session ? 'scan' : 'open', code }); drain();
  }));
  root.addEventListener('input', event => {
    if (event.target.dataset.gtdUnit) drafts.set(event.target.dataset.gtdUnit, event.target.value);
  });
  const gtdUnit = id => Object.values(session?.data?.items || {}).flatMap(item => item.units).find(unit => unit.id === id);
  function hasUnsavedGtd() {
    return [...new Set([...drafts.keys(), ...omissionDrafts.keys()])].some(id => {
      const saved = gtdUnit(id); if (!saved) return false;
      const absent = omissionDrafts.get(id) ?? saved.gtd_omitted;
      return absent !== saved.gtd_omitted || !absent && (drafts.get(id) ?? saved.gtd).trim() !== saved.gtd;
    });
  }
  function saveGtd(field) {
    const unitId = field.dataset.gtdUnit, saved = gtdUnit(unitId);
    const absent = omissionDrafts.get(unitId) ?? saved?.gtd_omitted ?? false;
    const value = absent ? '' : field.value.trim();
    const choice = JSON.stringify({ value, absent });
    if (rendering || !field.isConnected || !saved || !absent && (field.disabled || !value) || queuedValues.get(unitId) === choice) return;
    if (!recovery?.needsGtd && saved.gtd_omitted === absent && (absent || saved.gtd === value) && saved.gtd_status !== 'rejected') {
      drafts.delete(unitId); omissionDrafts.delete(unitId); return;
    }
    if (recovery?.needsGtd) {
      if (recovery.payload.unit_id !== unitId || absent) { notice('Сначала повторите ГТД последнего запроса', true); return; }
      paused = false; queue.unshift({ request: { ...recovery, payload: { ...recovery.payload, value } } });
    } else if (paused) return;
    else queue.push({ action: 'gtd', target: unitId, code: { value, absent } });
    queuedValues.set(unitId, choice); drain();
  }
  root.addEventListener('submit', event => {
    if (!event.target.dataset.gtdForm) return;
    event.preventDefault(); const field = event.target.querySelector('[data-gtd-unit]'); saveGtd(field);
  });
  root.addEventListener('change', event => {
    if (event.target.dataset.gtdAbsent) {
      const id = event.target.dataset.gtdAbsent;
      omissionDrafts.set(id, event.target.checked);
      const field = event.target.closest('form').querySelector('[data-gtd-unit]');
      field.disabled = event.target.checked;
      saveGtd(field);
    } else if (event.target.dataset.gtdUnit && drafts.has(event.target.dataset.gtdUnit)) saveGtd(event.target);
  });
  root.addEventListener('focusout', event => {
    if (event.relatedTarget?.dataset.gtdAbsent === event.target.dataset.gtdUnit) return;
    if (event.target.dataset.gtdUnit && drafts.has(event.target.dataset.gtdUnit)) saveGtd(event.target);
  });
  root.querySelectorAll('[data-clear-code]').forEach(button => button.addEventListener('click', () => { input().value = ''; focus(); }));
  root.querySelectorAll('[data-open-shipment]').forEach(button => button.addEventListener('click', () => { if (!busy && !paused) { queue.push({ action: 'open', target: button.dataset.openShipment }); drain(); } }));
  root.querySelectorAll('[data-assembly-action]').forEach(button => button.addEventListener('click', () => { if (!busy && !paused && session) { if (button.dataset.assemblyAction === 'complete' && hasUnsavedGtd()) { notice('Укажите ГТД или выберите «Не указывать»', true); return; } queue.push({ action: button.dataset.assemblyAction }); drain(); } }));
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
    if (!busy && !paused && session && !document.hidden && (['opening', 'scanning', 'blocked', 'preparing'].includes(session.status) || ['queued', 'checking'].includes(session.readiness?.status) || (session.labels || []).some(l => ['queued', 'processing'].includes(l.status)))) {
      try {
        const response = await fetch(`/api/v2/assembly/${session.id}`);
        if (response.ok) {
          const next = (await response.json()).session; apply(next);
          if (session.status === 'opening' && ['succeeded', 'failed', 'stale'].includes(session.refresh_status)) { queue.push({ action: 'activate' }); drain(); }
        }
      } catch (_) { /* Read failures never advance the product counter. */ }
    }
    setTimeout(poll, ['opening', 'preparing'].includes(session?.status) || ['prepared', 'checking'].includes(session?.data?.status) || (session?.labels || []).some(l => ['queued', 'processing'].includes(l.status)) ? 1000 : 5000);
  }
  setInterval(async () => {
    if (!session || busy || paused || document.hidden || !['opening', 'scanning', 'blocked', 'preparing'].includes(session.status)) return;
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
  window.PackPrinting?.sync(session, printNotice);
  recover().then(() => {
    if (!recovery && root.dataset.open && !session) { queue.push({ action: 'open', target: root.dataset.open }); drain(); }
    focus(); poll();
  });
})();
