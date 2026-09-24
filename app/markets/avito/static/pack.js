/* Сборка Avito: чем её рабочее место отличается от общего.

   Общее — в /static/market_pack.js. Своё здесь: порядок обратный, чем у Ozon.
   Сборку открывает скан этикетки, а не товара, — поэтому печати по скану нет,
   как нет и «завершить без скана»: закрывает заказ последняя единица товара.

   Справочника штрихкодов Avito не отдаёт, поэтому позиция — это единица
   товара, а панель записывает то, что отсканировали, без сверки. */
window.PACKS = window.PACKS || {};
window.PACKS.avito = {
  print: null,
  words: {
    // Слова замка на наклейки — общие: выгрузка идёт сразу по всем кабинетам.
    label: 'Этикетка',
    released: 'Сборка отменена. Сканируйте этикетку следующего заказа.',
    confirmRelease: 'Отменить сборку этого заказа?',
    confirmComplete: '',
  },
  activeId: (active) => active.id,

  renderActive(state) {
    const order = state.active;
    const percent = state.total ? Math.round((state.done / state.total) * 100) : 0;
    const rows = state.items.map((item) => `
      <div class="item-row ${item.scanned ? 'ok' : ''}">
        <div class="qty">${item.scanned ? '✓' : '—'}</div>
        <div class="name">
          ${escapeHtml(item.title || 'Без названия')}
          <div class="meta">
            ${item.seller_id ? `артикул ${escapeHtml(item.seller_id)} · ` : ''}единица ${item.unit_no}
            ${item.barcode ? ` · штрихкод <b>${escapeHtml(item.barcode)}</b>` : ''}
          </div>
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
               target="_blank" rel="noopener">Этикетка</a>
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
