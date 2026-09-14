/* Наборы: из чего физически собирается товар площадки.

   На Ozon набор — обычный товар с одним SKU. На складе его собирают из
   нескольких вещей со своими штрихкодами, и сборщик сканирует их, а не набор:
   такой наклейки на полке нет. Состав задаётся здесь.

   Состав редактируется целиком и сохраняется одной кнопкой: так на экране
   видно ровно то, что уедет в базу, и не бывает набора, у которого половина
   состава от прошлой версии. */
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

  let setSku = '';
  let parts = [];

  function renderChosen(item) {
    productChosen.innerHTML = item
      ? `<b>${escapeHtml(item.name || 'Без названия')}</b>
         <span class="muted mono">· ${escapeHtml(item.offer_id || '—')} · SKU ${escapeHtml(item.sku)}</span>`
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

  /* Подсказка по каталогу. Запрос уходит не на каждую букву: склад большой,
     а искать по трём символам всё равно бессмысленно. */
  function suggest(field, box, onPick) {
    let timer = null;
    field.addEventListener('input', () => {
      clearTimeout(timer);
      const query = field.value.trim();
      if (query.length < 2) { box.hidden = true; return; }
      timer = setTimeout(async () => {
        try {
          const data = await api(`/api/products/search?q=${encodeURIComponent(query)}`, undefined, 'GET');
          if (!data.items.length) {
            box.innerHTML = '<div class="muted small" style="padding:8px">Ничего не нашлось</div>';
            box.hidden = false;
            return;
          }
          box.innerHTML = data.items.map((item) => `
            <div class="pick" data-sku="${escapeHtml(item.sku)}">
              ${escapeHtml(item.name || 'Без названия')}
              <div class="muted small mono">${escapeHtml(item.offer_id || '—')} · SKU ${escapeHtml(item.sku)}
                ${item.barcodes.length ? ' · ' + escapeHtml(item.barcodes.join(', ')) : ''}</div>
            </div>`).join('');
          box.hidden = false;
          box.querySelectorAll('.pick').forEach((row) => {
            row.onclick = () => {
              onPick(data.items.find((item) => String(item.sku) === row.dataset.sku));
              box.hidden = true;
              field.value = '';
            };
          });
        } catch (error) {
          box.innerHTML = `<div class="small" style="padding:8px;color:var(--err)">${escapeHtml(error.message)}</div>`;
          box.hidden = false;
        }
      }, 250);
    });
    field.addEventListener('blur', () => setTimeout(() => { box.hidden = true; }, 200));
  }

  suggest(productField, productFound, (item) => {
    setSku = item.sku;
    renderChosen(item);
    if (!titleField.value) titleField.value = item.name || '';
  });

  suggest(partSearch, partFound, (item) => addPart({
    sku: item.sku,
    barcode: item.barcodes[0] || '',
    title: item.name,
    quantity: 1,
  }));

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

  function open(sku) {
    editor.hidden = false;
    message.textContent = '';
    setSku = sku || '';
    parts = [];
    titleField.value = '';
    renderChosen(null);
    renderParts();
    editor.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    if (sku) load(sku);
  }

  async function load(sku) {
    try {
      const data = await api(`/api/products/${encodeURIComponent(sku)}`, undefined, 'GET');
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
    if (!setSku) { toast('Выберите товар-набор', 'error'); return; }
    if (!parts.length) { toast('Добавьте хотя бы одну часть', 'error'); return; }
    event.target.disabled = true;
    try {
      const data = await api('/api/products/sets', {
        sku: setSku, title: titleField.value.trim(), parts,
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
    const data = await api(`/api/products/sets/${encodeURIComponent(button.dataset.delSet)}`,
                           undefined, 'DELETE');
    toast(data.message, 'ok');
    setTimeout(() => window.location.reload(), 700);
  } catch (error) {
    toast(error.message, 'error', 10000);
    button.disabled = false;
  }
});
