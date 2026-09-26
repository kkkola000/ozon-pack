/* Сборка Avito: чем её рабочее место отличается от общего.

   Общее — в /static/market_pack.js. Своё здесь: порядок обратный, чем у Ozon.
   Сборку открывает скан этикетки, а не товара, — поэтому печати по скану нет,
   как нет и «завершить без скана»: закрывает заказ последняя единица товара.

   Своих штрихкодов у Avito нет, поэтому позиция — это единица товара. Если
   объявление сопоставлено с карточкой другой площадки, скан сверяется с её
   штрихкодами (метка «сверка»), у набора — по частям. Иначе панель записывает
   то, что отсканировали, без сверки. */
window.PACKS = window.PACKS || {};
window.PACKS.avito = {
  print: null,
  words: {
    // Слова замка на наклейки — общие: выгрузка идёт сразу по всем кабинетам.
    label: 'Этикетка',
    released: 'Сборка отменена. Сканируйте этикетку следующего заказа.',
    confirmRelease: 'Отменить сборку этого заказа?',
    confirmComplete: '',
    close: 'Отсканируйте стикер — заказ закроется',   // все товары на месте
  },
  activeId: (active) => active.id,
  number: (active) => active.marketplace_id || active.id,

  /* Что ещё отсканировать. Позиция у Avito — единица товара, поэтому
     одинаковые строки складываем в одну «×2»; у набора — недостающие части. */
  left(state) {
    const rows = new Map();
    const units = (state.items || []).map((item) => ({ ...item, need: 1, scanned: item.scanned ? 1 : 0 }));
    for (const row of packLeft(units, (item) => item.title || 'Без названия',
      (item) => (item.seller_id ? `артикул ${item.seller_id}` : ''))) {
      const key = `${row.name}\u0000${row.note}`;
      const same = rows.get(key);
      if (same) same.count += row.count;
      else rows.set(key, { ...row });
    }
    return [...rows.values()];
  },

  renderActive(state) {
    const order = state.active;
    const percent = state.total ? Math.round((state.done / state.total) * 100) : 0;
    const rows = state.items.map((item) => `
      <div class="item-row ${item.scanned ? 'ok' : ''}">
        <div class="qty">${item.scanned ? '✓' : '—'}</div>
        ${packPhoto(item.image, item.title)}
        <div class="name">
          ${escapeHtml(item.title || 'Без названия')}
          <div class="meta">
            ${item.seller_id ? `артикул ${escapeHtml(item.seller_id)} · ` : ''}единица ${item.unit_no}
            ${item.barcode ? ` · штрихкод <b>${escapeHtml(item.barcode)}</b>` : ''}
            ${item.is_set ? ` · <span class="tag">Набор из ${item.parts.length}</span>`
              : item.checked ? ` · <span class="tag" title="Объявление сопоставлено: подходят только штрихкоды ${escapeHtml(item.barcodes.join(', '))}">сверка</span>` : ''}
          </div>
          ${packSetParts(item)}
        </div>
      </div>`).join('');

    return `
      <div class="panel active">
        <div class="row between">
          <div>
            <h2 style="margin:0">Заказ ${escapeHtml(order.marketplace_id || order.id)}</h2>
            <div class="muted small">${escapeHtml(order.service_name || order.service_label || '')}</div>
          </div>
          <div class="row">
            <a class="btn" href="/api/pack/label/avito/${encodeURIComponent(order.id)}.pdf"
               target="_blank" rel="noopener"
               data-print-pdf="/api/pack/label/avito/${encodeURIComponent(order.id)}.pdf"
               data-print-kind="avito:label" data-print-name="Этикетка">Этикетка</a>
            <button class="btn danger" id="btn-release">Отменить сборку</button>
          </div>
        </div>
        <div class="row" style="margin:14px 0 6px">
          <div class="grow progress"><div style="width:${percent}%"></div></div>
          <div style="font-weight:800;font-size:18px">${state.done} / ${state.total}</div>
        </div>
        <div class="items" style="margin-top:10px">${rows}</div>
      </div>`;
  },
};
