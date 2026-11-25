(() => {
  const archive = document.querySelector('[data-sticker-archive]');
  if (!archive) return;
  const csrf = document.querySelector('meta[name=csrf-token]').content;
  const dialog = document.querySelector('[data-marking-dialog]');
  const rows = dialog.querySelector('[data-marking-rows]');
  const error = dialog.querySelector('[data-marking-error]');
  let labelId = null, busy = false, requestVersion = 0;
  const make = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const checks = { accepted:['Принят','ok'], rejected:['Отклонён','err'], pending:['Ожидает проверки',''], unknown:['Результат не подтверждён','warn'] };
  const showError = text => { error.hidden = !text; error.textContent = text; };
  function draw(data) {
    rows.replaceChildren();
    document.getElementById('marking-title').textContent = `Честный знак · ${data.posting_number}`;
    dialog.querySelector('[data-marking-release-help]').hidden = !data.can_manage;
    for (const mark of data.marks) {
      const row = make('tr'); row.dataset.markId = mark.id;
      const product = make('td'); product.append(make('b','',mark.product.name || 'Товар'), make('div','muted small',mark.product.article || ''));
      const identity = make('td'); identity.append(make('div','code-value',mark.gtin), make('div','code-value',mark.serial));
      const full = make('details','stickers-full-code'); full.append(make('summary','small','Полный код'),make('code','',mark.code)); identity.append(full);
      const binding = make('td');
      const title = mark.binding === 'unbound' ? mark.release_reason === 'shipment_split' ? 'Перенесён' : mark.release_reason === 'replacement_confirmed' ? 'Заменён' : 'Отвязан' : mark.binding === 'retired' ? 'Ожидает сверки' : 'Привязан';
      binding.append(make('span',`badge ${mark.binding === 'bound' ? 'accent' : ''}`,title));
      const check = make('td'), status = checks[mark.check] || checks.unknown;
      check.append(make('span',`badge ${status[1]}`,status[0]));
      const actions = make('td','stickers-actions');
      if (mark.can_release) {
        const button = make('button','btn small','Отвязать'); button.type = 'button'; button.disabled = busy;
        button.addEventListener('click', () => release(mark)); actions.append(button);
      }
      row.append(product,identity,binding,check,actions); rows.append(row);
    }
    if (!data.marks.length) { const row = make('tr'), cell = make('td','muted','Коды не найдены'); cell.colSpan = 5; row.append(cell); rows.append(row); }
    const archiveRow = archive.querySelector(`[data-sticker-row="${labelId}"]`);
    const note = archiveRow?.querySelector('[data-marking-bound]');
    if (note) note.textContent = data.marks.some(m => m.binding !== 'unbound') ? 'Есть привязанные коды' : 'Коды отвязаны';
  }
  async function open(id) {
    labelId = id; const version = ++requestVersion;
    showError(''); rows.replaceChildren();
    document.getElementById('marking-title').textContent = 'Честный знак';
    dialog.querySelector('[data-marking-release-help]').hidden = true;
    if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch(`/api/v2/stickers/${id}/marking`, { signal:AbortSignal.timeout(20000) });
      const data = await response.json();
      if (version !== requestVersion || !dialog.open) return;
      if (!response.ok) { showError(data.detail || 'Не удалось получить коды'); return; }
      draw(data);
    } catch (_) { if (version === requestVersion && dialog.open) showError('Нет связи. Откройте список заново.'); }
  }
  async function release(mark) {
    if (busy || !labelId) return;
    if (!window.confirm(`Отвязать Честный знак ${mark.gtin}, серия ${mark.serial} от этого отправления? Данные на площадке останутся.`)) return;
    const id = labelId, version = requestVersion;
    busy = true; showError(''); rows.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const response = await fetch(`/api/v2/stickers/${id}/marking/${mark.id}/unbind`, {
        method:'POST', signal:AbortSignal.timeout(20000), headers:{'Content-Type':'application/json','X-CSRF-Token':csrf}, body:JSON.stringify({revision:mark.revision}),
      });
      const data = await response.json();
      if (version !== requestVersion || !dialog.open) return;
      if (!response.ok) showError(data.detail || 'Не удалось отвязать код');
      else { busy = false; draw(data); }
    } catch (_) { if (version === requestVersion && dialog.open) showError('Ответ не получен. Откройте список заново, чтобы проверить привязку.'); }
    finally { busy = false; rows.querySelectorAll('button').forEach(button => { button.disabled = false; }); }
  }
  archive.addEventListener('click', event => {
    const button = event.target.closest('[data-sticker-marking]');
    if (button && !busy) open(button.dataset.stickerMarking);
  });
  dialog.querySelector('[data-marking-close]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { labelId = null; requestVersion++; });
  archive.querySelectorAll('input[type=date]').forEach(input => input.addEventListener('change', () => { input.parentElement.dataset.dateEmpty = input.value ? 'false' : 'true'; }));
  archive.addEventListener('click', async event => {
    const more = event.target.closest('[data-stickers-more]');
    if (!more) return;
    event.preventDefault(); if (more.getAttribute('aria-disabled') === 'true') return;
    more.setAttribute('aria-disabled','true'); more.textContent = 'Загружаем…';
    const notice = archive.querySelector('[data-stickers-notice]'); notice.hidden = true;
    try {
      const response = await fetch(more.href,{signal:AbortSignal.timeout(20000)});
      if (!response.ok || !response.headers.get('content-type')?.startsWith('text/html')) throw new Error();
      const page = new DOMParser().parseFromString(await response.text(),'text/html');
      const added = page.querySelectorAll('[data-sticker-row]');
      if (!page.querySelector('[data-sticker-archive]')) throw new Error();
      const body = archive.querySelector('[data-stickers-rows]');
      const known = new Set([...body.querySelectorAll('[data-sticker-row]')].map(row => row.dataset.stickerRow));
      for (const row of added) if (!known.has(row.dataset.stickerRow)) body.append(document.importNode(row,true));
      const next = page.querySelector('[data-stickers-more]');
      if (next) more.href = next.href; else more.remove();
      archive.querySelector('[data-stickers-count]').textContent = `Показано: ${body.querySelectorAll('[data-sticker-row]').length}`;
    } catch (_) { notice.hidden = false; notice.textContent = 'Не удалось загрузить список. Повторите попытку или нажмите «Найти» заново.'; }
    finally { more.removeAttribute('aria-disabled'); more.textContent = 'Ещё'; }
  });
})();
