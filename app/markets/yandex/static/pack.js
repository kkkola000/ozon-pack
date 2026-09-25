/* Сборка Яндекс Маркета: чем её рабочее место отличается от общего.

   Общее — в /static/market_pack.js. Здесь адреса запросов, слова и карточка
   открытой сборки: у Маркета нет фото и наборов, зато видно, когда штрихкода
   нет в каталоге, — сканировать такую позицию нечем. */
window.PACKS = window.PACKS || {};
window.PACKS.yandex = {
  print: {
    key: 'order_id',
    kind: 'yandex:label',     // строка на странице «Принтеры»
    // Адрес общий: кабинет заказа ищет ядро, а тот, что в шапке, тут ни при чём.
    url: (id) => `/api/pack/label/yandex/${encodeURIComponent(id)}.pdf`,
  },
  words: {
    // Слова замка на наклейки — общие: выгрузка идёт сразу по всем кабинетам.
    label: 'Ярлык',
    released: 'Сборка отменена. Сканируйте следующий товар.',
    confirmRelease: null,
    confirmComplete: 'Завершить заказ без сканирования ярлыка? Действие попадёт в журнал.',
  },
  activeId: (active) => active.id,

  renderActive(state) {
    const order = state.active;
    const percent = state.total ? Math.round((state.done / state.total) * 100) : 0;

    const urgency = () => {
      const map = { overdue: 'Просрочено', urgent: 'Срочно', soon: 'Сегодня', ok: '' };
      const text = map[order.urgency];
      if (!text) return '';
      return `<span class="tag ${order.urgency}">${text} · ${escapeHtml(hoursLeftText(order.hours_left))}</span>`;
    };

    const items = state.items.map((item) => `
      <div class="item-row ${item.ok ? 'ok' : ''}">
        <div class="qty">${item.scanned} / ${item.need}</div>
        <div class="name">
          ${escapeHtml(item.name || 'Без названия')}
          <div class="meta">
            Артикул: ${escapeHtml(item.offer_id || '—')}
            ${item.barcodes?.length
              ? ' · ШК: ' + escapeHtml(item.barcodes.join(', '))
              : ' · <span class="tag overdue">нет штрихкода в каталоге</span>'}
          </div>
        </div>
        ${item.ok ? '<div class="check">✔</div>' : ''}
      </div>`).join('');

    return `
      <div class="panel">
        <div class="row between">
          <div>
            <div class="muted small">Собирается заказ Маркета</div>
            <div style="font-size:26px;font-weight:800" class="mono">${escapeHtml(order.id)}</div>
            <div class="tags" style="margin-top:6px">
              ${urgency()}
              <span class="tag">${escapeHtml(order.status_label || '')}</span>
              ${order.printed_at ? '<span class="tag">Ярлык печатался</span>' : ''}
            </div>
          </div>
          <div class="row">
            <button class="btn" id="btn-print">Печать ярлыка</button>
            <a class="btn" id="btn-open-label" href="/api/pack/label/yandex/${encodeURIComponent(order.id)}.pdf"
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
          <dt>Отгрузка до</dt><dd>${escapeHtml(order.deadline_local || '—')}</dd>
          <dt>Доставка</dt><dd>${escapeHtml(order.delivery_label || '—')} ${order.service_name ? '· ' + escapeHtml(order.service_name) : ''}</dd>
          <dt>Номер у продавца</dt><dd class="mono">${escapeHtml(order.external_id || '—')}</dd>
          <dt>Комментарий</dt><dd>${escapeHtml(order.notes || '—')}</dd>
        </dl>

        <div class="row" style="margin-top:14px">
          <span class="muted small">Не читается ярлык? Введите номер заказа в поле сканирования вручную.</span>
          <span class="grow"></span>
          <button class="btn small" id="btn-force">Завершить без скана ярлыка</button>
        </div>
      </div>`;
  },
};
