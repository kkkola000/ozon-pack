/* Раздел «Товары»: каталог всех кабинетов, наборы и сопоставление.

   Товар здесь — пара (кабинет, SKU): одинаковый артикул в двух кабинетах это
   две разные карточки, пока их не сопоставили. Поэтому ключ везде составной,
   «account:sku», и ссылки в разметке несут обе части.

   Состав набора редактируется целиком и сохраняется одной кнопкой: так на
   экране видно ровно то, что уедет в базу, и не бывает набора, у которого
   половина состава от прошлой версии. */
function cardKey(value) {
  const [account, ...rest] = String(value || '').split(':');
  return { account_id: Number(account), sku: rest.join(':') };
}
/* Подсказка по каталогу. Запрос уходит не на каждую букву: склад большой,
   а искать по двум символам всё равно бессмысленно.

   Мышь по списку не должна уводить фокус из поля. Иначе так: человек тянет
   полосу прокрутки, поле теряет фокус, и список прячется прямо под курсором —
   пролистать до нужного товара невозможно. Поэтому на mousedown внутри
   списка отменяем действие по умолчанию: фокус остаётся, клик работает. */
function suggest(field, box, onPick) {
  let timer = null;
  let found = [];

  function show(html) {
    box.innerHTML = html;
    box.hidden = false;
  }

  function render() {
    /* Названия у вариантов одного товара совпадают до буквы — различает их
       артикул. Поэтому он не в общей серой строке, а отдельно и заметно. */
    show(found.map((item) => `
      <div class="pick" data-key="${item.account_id}:${escapeHtml(item.sku)}">
        <b class="mono">${escapeHtml(item.offer_id || '—')}</b>
        ${escapeHtml(item.name || 'Без названия')}
        <div class="muted small">${escapeHtml(item.shop || '')}
          ${item.barcodes.length ? ' · ' + escapeHtml(item.barcodes.join(', ')) : ''}</div>
      </div>`).join(''));
  }

  async function search() {
    const query = field.value.trim();
    if (query.length < 2) { box.hidden = true; return; }
    try {
      const data = await api(`/api/products/search?q=${encodeURIComponent(query)}&limit=50`,
                             undefined, 'GET');
      found = data.items;
      if (!found.length) {
        show('<div class="muted small" style="padding:10px">Ничего не нашлось</div>');
        return;
      }
      render();
    } catch (error) {
      found = [];
      show(`<div class="small" style="padding:10px;color:var(--err)">${escapeHtml(error.message)}</div>`);
    }
  }

  field.addEventListener('input', () => {
    clearTimeout(timer);
    if (field.value.trim().length < 2) { box.hidden = true; return; }
    timer = setTimeout(search, 250);
  });

  /* Вернулись в поле с прежним запросом — показываем, что уже нашли. */
  field.addEventListener('focus', () => { if (found.length) render(); });
  field.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { box.hidden = true; field.blur(); }
  });

  box.addEventListener('mousedown', (event) => event.preventDefault());
  box.addEventListener('click', (event) => {
    const row = event.target.closest('.pick');
    if (!row) return;
    onPick(found.find((item) => `${item.account_id}:${item.sku}` === row.dataset.key));
    box.hidden = true;
    found = [];
    field.value = '';
  });

  /* Ушли из поля по-настоящему (Tab, клик в стороне) — список не нужен.
     Клик по списку сюда не доходит: фокус не терялся. */
  field.addEventListener('blur', () => { box.hidden = true; });
}

const editor = document.getElementById('set-editor');

if (editor) {
  const productField = document.getElementById('set-product');
  const productFound = document.getElementById('set-product-found');
  const productChosen = document.getElementById('set-product-chosen');
  const titleField = document.getElementById('set-title');
  const partsBody = document.querySelector('#set-parts tbody');
  const partSearch = document.getElementById('part-search');
  const partFound = document.getElementById('part-found');
  const barcodeField = document.getElementById('part-barcode');
  const partTitleField = document.getElementById('part-title');
  const message = document.getElementById('set-msg');

  let card = null;          // выбранный товар-набор: {account_id, sku}
  let parts = [];

  function renderChosen(item) {
    productChosen.innerHTML = item
      ? `<b>${escapeHtml(item.name || 'Без названия')}</b>
         <span class="muted mono">· ${escapeHtml(item.offer_id || '—')}</span>
         <span class="muted">· ${escapeHtml(item.shop || '')}</span>`
      : '<span class="muted">товар не выбран</span>';
  }

  function renderParts() {
    if (!parts.length) {
      partsBody.innerHTML = '<tr><td colspan="4" class="muted small">'
        + 'Пока пусто. Добавьте части — из каталога или штрихкодом.</td></tr>';
      return;
    }
    partsBody.innerHTML = parts.map((part, index) => `
      <tr>
        <td>${escapeHtml(part.title || part.barcode || part.sku || '—')}
          ${part.sku ? `<div class="muted small mono">SKU ${escapeHtml(part.sku)}</div>`
                     : '<div class="muted small">нет в каталоге</div>'}</td>
        <td class="mono small">${escapeHtml(part.barcode || '—')}</td>
        <td><input type="number" min="1" value="${part.quantity}" data-qty="${index}"
                   style="width:80px"></td>
        <td><button class="btn small danger" data-drop="${index}">Убрать</button></td>
      </tr>`).join('');
  }

  partsBody.addEventListener('input', (event) => {
    const field = event.target.closest('[data-qty]');
    if (!field) return;
    const value = parseInt(field.value, 10);
    parts[Number(field.dataset.qty)].quantity = Number.isFinite(value) && value > 0 ? value : 1;
  });

  partsBody.addEventListener('click', (event) => {
    const button = event.target.closest('[data-drop]');
    if (!button) return;
    parts.splice(Number(button.dataset.drop), 1);
    renderParts();
  });

  suggest(productField, productFound, (item) => {
    card = { account_id: item.account_id, sku: item.sku };
    renderChosen(item);
    if (!titleField.value) titleField.value = item.name || '';
  });

  /* Часть можно взять из каталога любого кабинета. Но SKU у кабинетов свои, и
     чужой в составе не совпал бы ни с чем при сборке: часть из другого кабинета
     кладём штрихкодом — по нему панель найдёт товар там, где он нужен. */
  suggest(partSearch, partFound, (item) => {
    const foreign = !card || item.account_id !== card.account_id;
    if (foreign && !item.barcodes.length) {
      toast('У товара из другого кабинета нет штрихкода — добавить его частью нельзя', 'error', 8000);
      return;
    }
    addPart({
      sku: foreign ? '' : item.sku,
      barcode: item.barcodes[0] || '',
      title: item.name,
      quantity: 1,
    });
  });

  function addPart(part) {
    /* Та же часть второй раз — это «нужно две штуки», а не второй ряд:
       иначе в составе будут две строки про одно и то же. */
    const same = parts.find((p) => (p.sku && p.sku === part.sku)
      || (!p.sku && !part.sku && p.barcode === part.barcode));
    if (same) same.quantity += part.quantity;
    else parts.push(part);
    renderParts();
  }

  document.getElementById('part-add-barcode').onclick = () => {
    const barcode = barcodeField.value.trim();
    if (!barcode) { toast('Введите штрихкод', 'error'); return; }
    /* Название не обязательно, но без него сборщик увидит на экране голый код
       и не поймёт, что именно класть в коробку. */
    addPart({ sku: '', barcode, title: partTitleField.value.trim(), quantity: 1 });
    barcodeField.value = '';
    partTitleField.value = '';
  };

  function open(key) {
    editor.hidden = false;
    message.textContent = '';
    card = key ? cardKey(key) : null;
    parts = [];
    titleField.value = '';
    renderChosen(null);
    renderParts();
    editor.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    if (card) load(card);
  }

  async function load(chosen) {
    try {
      const data = await api(
        `/api/products/${chosen.account_id}/${encodeURIComponent(chosen.sku)}`, undefined, 'GET');
      renderChosen(data.product);
      titleField.value = (data.set && data.set.title) || data.product.name || '';
      parts = ((data.set && data.set.parts) || []).map((part) => ({
        sku: part.part_sku || '',
        barcode: part.barcode || '',
        title: part.title || '',
        quantity: part.quantity,
      }));
      renderParts();
    } catch (error) {
      message.innerHTML = `<span style="color:var(--err)">${escapeHtml(error.message)}</span>`;
    }
  }

  document.getElementById('set-new').onclick = () => open('');
  document.getElementById('set-cancel').onclick = () => { editor.hidden = true; };

  /* Из каталога сюда приходят ссылкой с готовым товаром: редактор живёт только
     на этой вкладке, и заставлять человека искать товар второй раз незачем. */
  const wanted = new URLSearchParams(window.location.search).get('edit');
  if (wanted) open(wanted);

  document.addEventListener('click', (event) => {
    const edit = event.target.closest('[data-edit-set]');
    if (edit) { event.preventDefault(); open(edit.dataset.editSet); }
  });

  document.getElementById('set-save').onclick = async (event) => {
    if (!card) { toast('Выберите товар-набор', 'error'); return; }
    if (!parts.length) { toast('Добавьте хотя бы одну часть', 'error'); return; }
    event.target.disabled = true;
    try {
      const data = await api('/api/products/sets', {
        account_id: card.account_id, sku: card.sku, title: titleField.value.trim(), parts,
      });
      toast(data.message, 'ok');
      setTimeout(() => { window.location.href = '/products?tab=sets'; }, 700);
    } catch (error) {
      message.innerHTML = `<span style="color:var(--err)">${escapeHtml(error.message)}</span>`;
      toast(error.message, 'error', 10000);
      event.target.disabled = false;
    }
  };
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-del-set]');
  if (!button) return;
  event.preventDefault();
  if (!confirm('Убрать набор? Товар останется — он будет собираться по своему штрихкоду.')) return;
  button.disabled = true;
  try {
    const chosen = cardKey(button.dataset.delSet);
    const data = await api(
      `/api/products/sets/${chosen.account_id}/${encodeURIComponent(chosen.sku)}`, undefined, 'DELETE');
    toast(data.message, 'ok');
    setTimeout(() => window.location.reload(), 700);
  } catch (error) {
    toast(error.message, 'error', 10000);
    button.disabled = false;
  }
});

/* Обновление каталогов. Обход идёт в фоне — тысячи карточек за один запрос
   браузера не успеть, — поэтому кнопка спрашивает о ходе, пока он не кончится.
   Кабинетов с каталогом может быть несколько, и обходятся они разом. */
const refreshButton = document.getElementById('catalog-refresh');

if (refreshButton) {
  const state = document.getElementById('catalog-state');
  let timer = null;

  function show(data) {
    const jobs = Object.values(data.jobs || {});
    if (data.running) {
      refreshButton.disabled = true;
      refreshButton.textContent = 'Обновляем…';
      const busy = jobs.filter((job) => job.running);
      const done = busy.reduce((sum, job) => sum + (job.done || 0), 0);
      const total = busy.reduce((sum, job) => sum + (job.total || 0), 0);
      state.textContent = total
        ? `Карточки: ${done} из ${total}… (кабинетов ${busy.length})`
        : 'Спрашиваем список товаров у площадок…';
      return true;
    }
    refreshButton.disabled = false;
    refreshButton.textContent = 'Обновить каталоги';
    if (data.errors && data.errors.length) {
      state.innerHTML = `<span style="color:var(--err)">Каталог не обновился: `
        + `${escapeHtml(data.errors[0] || 'причина неизвестна')}</span>`;
      return false;
    }
    const live = jobs.reduce((sum, job) => sum + (job.live || 0), 0);
    const skipped = jobs.reduce((sum, job) => sum + (job.archived_skipped || 0), 0);
    if (jobs.some((job) => job.status === 'ok')) {
      state.textContent = `Каталог обновлён: ${live} товаров, архив пропущен — ${skipped}.`;
      /* Список на странице теперь старый — показываем свежий. */
      setTimeout(() => window.location.reload(), 900);
    }
    return false;
  }

  async function poll() {
    try {
      const data = await api('/api/products/catalog/status', undefined, 'GET');
      if (!show(data)) clearInterval(timer);
    } catch (error) {
      clearInterval(timer);
      refreshButton.disabled = false;
      refreshButton.textContent = 'Обновить каталоги';
      state.innerHTML = `<span style="color:var(--err)">${escapeHtml(error.message)}</span>`;
    }
  }

  function watch() {
    clearInterval(timer);
    timer = setInterval(poll, 1500);
  }

  /* Страницу могли открыть, пока обход уже идёт, — тогда сразу следим. */
  if (state.dataset.running) { refreshButton.disabled = true; watch(); }

  refreshButton.onclick = async () => {
    refreshButton.disabled = true;
    state.textContent = 'Запускаем…';
    try {
      const cab = new URLSearchParams(window.location.search).get('cab') || '';
      const data = await api('/api/products/catalog/refresh', { cab });
      toast(data.message, 'ok');
      watch();
      poll();
    } catch (error) {
      refreshButton.disabled = false;
      toast(error.message, 'error', 10000);
      state.innerHTML = `<span style="color:var(--err)">${escapeHtml(error.message)}</span>`;
    }
  };
}

/* ------------------------------------------------ сопоставление карточек

   Все действия однотипны: спросили подтверждение там, где оно нужно, сходили на
   сервер, показали ответ и перечитали страницу. Отдельного состояния на клиенте
   нет намеренно — списки после любого из действий меняются целиком. */
async function act(button, request, { confirmText } = {}) {
  if (confirmText && !confirm(confirmText)) return;
  button.disabled = true;
  try {
    const data = await request();
    toast(data.message, 'ok');
    setTimeout(() => window.location.reload(), 700);
  } catch (error) {
    toast(error.message, 'error', 10000);
    button.disabled = false;
  }
}

document.addEventListener('click', (event) => {
  const confirmOne = event.target.closest('[data-confirm]');
  if (confirmOne) {
    act(confirmOne, () => api('/api/products/links/confirm', { article: confirmOne.dataset.confirm }));
    return;
  }

  const confirmAll = event.target.closest('[data-confirm-all]');
  if (confirmAll) {
    const cab = new URLSearchParams(window.location.search).get('cab') || '';
    act(confirmAll, () => api('/api/products/links/confirm', { all: true, cab }), {
      confirmText: 'Сопоставить все найденные совпадения? Потом любое из них можно отменить.',
    });
    return;
  }

  const skip = event.target.closest('[data-skip]');
  if (skip) {
    act(skip, () => api('/api/products/links/skip', { article: skip.dataset.skip }));
    return;
  }

  const unskip = event.target.closest('[data-unskip]');
  if (unskip) {
    act(unskip, () => api('/api/products/links/skip', { article: unskip.dataset.unskip, undo: true }));
    return;
  }

  /* Отмена сопоставления. Предупреждаем прямо: совпадение вернётся в
     предложения — иначе отмена выглядит как «не сработало». */
  const unlink = event.target.closest('[data-unlink]');
  if (unlink) {
    act(unlink, () => api(`/api/products/links/${encodeURIComponent(unlink.dataset.unlink)}`,
                          undefined, 'DELETE'), {
      confirmText: 'Отменить сопоставление? Карточки снова станут отдельными товарами, '
        + 'а совпадение по артикулу вернётся в предложения.',
    });
    return;
  }

  const drop = event.target.closest('[data-unlink-card]');
  if (drop) {
    const chosen = cardKey(drop.dataset.unlinkCard);
    act(drop, () => api(`/api/products/links/${chosen.account_id}/${encodeURIComponent(chosen.sku)}`,
                        undefined, 'DELETE'));
    return;
  }

  const main = event.target.closest('[data-main]');
  if (main) {
    const chosen = cardKey(main.dataset.main);
    act(main, () => api('/api/products/links/main', chosen));
  }
});

/* «Развернуть» у сопоставленного товара в каталоге: карточки площадок под ним. */
document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-toggle]');
  if (!button) return;
  const rows = document.querySelectorAll(`[data-sub="${button.dataset.toggle}"]`);
  const opening = rows.length && rows[0].hidden;
  for (const row of rows) row.hidden = !opening;
  button.textContent = opening ? 'свернуть' : 'развернуть';
});

/* ------------------------------------------------ ручное сопоставление */
const manual = document.getElementById('manual-link');

if (manual) {
  const mainField = document.getElementById('main-product');
  const mainFound = document.getElementById('main-found');
  const mainChosen = document.getElementById('main-chosen');
  const linkField = document.getElementById('link-product');
  const linkFound = document.getElementById('link-found');
  const cardsBody = document.querySelector('#link-cards tbody');
  const message = document.getElementById('link-msg');

  let main = null;
  let cards = [];

  function renderMain() {
    mainChosen.innerHTML = main
      ? `<b>${escapeHtml(main.name || 'Без названия')}</b>
         <span class="muted mono">· ${escapeHtml(main.offer_id || '—')}</span>
         <span class="muted">· ${escapeHtml(main.shop || '')}</span>`
      : '<span class="muted">товар не выбран</span>';
  }

  function renderCards() {
    if (!cards.length) {
      cardsBody.innerHTML = '<tr><td class="muted small">'
        + 'Добавьте карточки других кабинетов — это тот же товар под другими названиями.</td></tr>';
      return;
    }
    cardsBody.innerHTML = cards.map((item, index) => `
      <tr>
        <td>${escapeHtml(item.name || 'Без названия')}
          <div class="muted small">${escapeHtml(item.shop || '')}</div></td>
        <td class="mono small" style="width:190px">${escapeHtml(item.barcodes.join(', ') || '—')}</td>
        <td style="width:90px"><button class="link-btn danger-link" data-drop-card="${index}">убрать</button></td>
      </tr>`).join('');
  }

  cardsBody.addEventListener('click', (event) => {
    const button = event.target.closest('[data-drop-card]');
    if (!button) return;
    cards.splice(Number(button.dataset.dropCard), 1);
    renderCards();
  });

  suggest(mainField, mainFound, (item) => { main = item; renderMain(); });

  suggest(linkField, linkFound, (item) => {
    if (main && item.account_id === main.account_id && item.sku === main.sku) {
      toast('Это и есть основной товар', 'error');
      return;
    }
    if (cards.some((card) => card.account_id === item.account_id && card.sku === item.sku)) return;
    cards.push(item);
    renderCards();
  });

  function open() {
    manual.hidden = false;
    message.textContent = '';
    main = null;
    cards = [];
    renderMain();
    renderCards();
    manual.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  for (const button of document.querySelectorAll('#link-manual')) button.onclick = open;
  document.getElementById('link-cancel').onclick = () => { manual.hidden = true; };

  document.getElementById('link-save').onclick = async (event) => {
    if (!main) { toast('Выберите основной товар', 'error'); return; }
    if (!cards.length) { toast('Добавьте хотя бы одну карточку', 'error'); return; }
    event.target.disabled = true;
    try {
      const data = await api('/api/products/links', {
        main: { account_id: main.account_id, sku: main.sku },
        cards: cards.map((item) => ({ account_id: item.account_id, sku: item.sku })),
      });
      toast(data.message, 'ok');
      setTimeout(() => window.location.reload(), 700);
    } catch (error) {
      message.innerHTML = `<span style="color:var(--err)">${escapeHtml(error.message)}</span>`;
      toast(error.message, 'error', 10000);
      event.target.disabled = false;
    }
  };
}
