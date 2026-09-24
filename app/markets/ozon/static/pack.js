/* Сборка Ozon: чем её рабочее место отличается от общего.

   Общее — в /static/market_pack.js: сканы, замок, история, счётчики, печать.
   Здесь только своё: адреса запросов, слова и карточка открытой сборки —
   у Ozon в ней фото товара, наборы и «Честный знак». */
window.PACK = {
  api: {
    scan: '/api/scan',
    release: '/api/release',
    complete: '/api/complete',
    sync: '/api/sync',
  },
  print: {
    key: 'posting_number',
    url: (number) => `/api/label/${encodeURIComponent(number)}.pdf`,
  },
  words: {
    // Слова замка на наклейки здесь не объявляются: выгрузка общая на все
    // кабинеты, и говорить в ней «стикеры» про ярлыки Маркета было бы неверно.
    label: 'Стикер',
    released: 'Сборка отменена. Сканируйте следующий товар.',
    confirmRelease: null,
    confirmComplete: 'Завершить отправление без сканирования стикера? Действие попадёт в журнал.',
  },
  counters: {
    'c-packaging': 'awaiting_packaging',
    'c-deliver': 'awaiting_deliver',
    'c-packed': 'packed_today',
    'c-returns': 'returns_ready',
  },
  activeId: (active) => active.posting_number,

  renderActive(state) {
    const posting = state.active;
    const percent = state.total ? Math.round((state.done / state.total) * 100) : 0;

    const urgency = () => {
      const map = { overdue: 'Просрочено', urgent: 'Срочно', soon: 'Сегодня', ok: '' };
      const text = map[posting.urgency];
      if (!text) return '';
      return `<span class="tag ${posting.urgency}">${text} · ${escapeHtml(hoursLeftText(posting.hours_left))}</span>`;
    };

    /* Набор на площадке — обычный товар, а на складе это несколько вещей со
       своими штрихкодами. Сборщик сканирует их, поэтому и видеть он должен их, а
       не одну строку «набор 0/1», по которой непонятно, что ещё брать. */
    const setParts = (item) => !item.is_set ? '' : `
      <div class="set-parts">
        ${item.parts.map((part) => `
          <div class="set-part ${part.ok ? 'ok' : ''}">
            <span class="qty">${part.scanned} / ${part.need}</span>
            <span class="grow">${escapeHtml(part.name)}
              ${part.barcode ? `<span class="muted mono"> · ${escapeHtml(part.barcode)}</span>` : ''}
            </span>
            ${part.ok ? '<span class="check">✔</span>' : ''}
          </div>`).join('')}
      </div>`;

    const items = state.items.map((item) => `
      <div class="item-row ${item.ok ? 'ok' : ''}">
        <div class="qty">${item.scanned} / ${item.need}</div>
        ${item.image
          ? `<img class="item-photo" src="${escapeHtml(item.image)}" alt="${escapeHtml(item.name || '')}"
                  data-zoom="${escapeHtml(item.image)}" data-name="${escapeHtml(item.name || '')}" loading="lazy"
                  onerror="this.replaceWith(Object.assign(document.createElement('div'), {className: 'item-photo blank', textContent: '🖼'}))">`
          : '<div class="item-photo blank">🖼</div>'}
        <div class="name">
          ${escapeHtml(item.name || 'Без названия')}
          <div class="meta">
            Артикул: ${escapeHtml(item.offer_id || '—')} · SKU: ${escapeHtml(item.sku)}
            ${item.barcodes?.length ? ' · ШК: ' + escapeHtml(item.barcodes.join(', ')) : ''}
            ${item.mandatory_mark ? ' · <span class="tag mark">Честный знак</span>' : ''}
            ${item.is_set ? ' · <span class="tag">Набор из ' + item.parts.length + '</span>' : ''}
          </div>
          ${setParts(item)}
        </div>
        ${item.ok ? '<div class="check">✔</div>' : ''}
      </div>`).join('');

    return `
      <div class="panel">
        <div class="row between">
          <div>
            <div class="muted small">Собирается отправление</div>
            <div style="font-size:26px;font-weight:800" class="mono">${escapeHtml(posting.posting_number)}</div>
            <div class="tags" style="margin-top:6px">
              ${urgency()}
              ${posting.is_express ? '<span class="tag express">Express</span>' : ''}
              ${posting.requires_mark ? '<span class="tag mark">Требуется маркировка</span>' : ''}
              ${posting.is_multibox ? `<span class="tag">Многоместное: ${posting.multi_box_qty}</span>` : ''}
              ${posting.printed_at ? '<span class="tag">Стикер печатался</span>' : ''}
            </div>
          </div>
          <div class="row">
            <button class="btn" id="btn-print">Печать стикера</button>
            <a class="btn" id="btn-open-label" href="/api/label/${encodeURIComponent(posting.posting_number)}.pdf"
               target="_blank" rel="noopener" title="Открыть PDF в новой вкладке">Открыть PDF</a>
            <button class="btn danger" id="btn-release">Отменить сборку</button>
          </div>
        </div>

        <div class="row" style="margin:14px 0 6px">
          <div class="grow progress"><div style="width:${percent}%"></div></div>
          <div style="font-weight:800;font-size:18px">${state.done} / ${state.total}</div>
        </div>

        <div style="margin-top:10px">${items}</div>

        <dl class="kv" style="margin-top:16px">
          <dt>Отгрузка до</dt><dd>${escapeHtml(posting.shipment_date_local || posting.shipment_date || '—')}</dd>
          <dt>Куда</dt><dd>${escapeHtml([posting.region, posting.city].filter(Boolean).join(', ') || '—')}</dd>
          <dt>Способ</dt><dd>${escapeHtml(posting.delivery_method || '—')} ${posting.tpl_provider ? '· ' + escapeHtml(posting.tpl_provider) : ''}</dd>
          <dt>Склад</dt><dd>${escapeHtml(posting.warehouse_name || '—')}</dd>
          <dt>Оплата</dt><dd>${escapeHtml(posting.payment_type || '—')}</dd>
          <dt>Заказ</dt><dd class="mono">${escapeHtml(posting.order_number || '—')}</dd>
        </dl>

        <div class="row" style="margin-top:14px">
          <span class="muted small">Не читается стикер? Введите номер отправления в поле сканирования вручную.</span>
          <span class="grow"></span>
          <button class="btn small" id="btn-force">Завершить без скана стикера</button>
        </div>
      </div>`;
  },
};
