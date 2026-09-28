/* Сборка Ozon: чем её рабочее место отличается от общего.

   Общее — в /static/market_pack.js: сканы, замок, история, счётчики, печать и
   само окно сборки. Здесь только своё: печать стикера, слова и что показать в
   окне — у Ozon это фото товара, наборы и «Честный знак».

   Карточки всех площадок подключены к странице сразу: открытый заказ может
   оказаться из любого кабинета, и рисует его та площадка, чей он. */
window.PACKS = window.PACKS || {};
window.PACKS.ozon = {
  print: {
    key: 'posting_number',
    kind: 'ozon:label',       // строка на странице «Принтеры»
    // Адрес общий: кабинет заказа ищет ядро, а тот, что в шапке, тут ни при чём.
    url: (number) => `/api/pack/label/ozon/${encodeURIComponent(number)}.pdf`,
  },
  title: 'Ozon',
  words: {
    // Слова замка на наклейки здесь не объявляются: выгрузка общая на все
    // кабинеты, и говорить в ней «стикеры» про ярлыки Маркета было бы неверно.
    label: 'Стикер',
    released: 'Сборка отменена. Сканируйте следующий товар.',
    confirmRelease: null,
    confirmComplete: 'Завершить отправление без сканирования стикера? Действие попадёт в журнал.',
    close: 'Наклейте и отсканируйте стикер отправления',   // все товары на месте
    done: 'Отправление собрано',
  },
  activeId: (active) => active.posting_number,
  number: (active) => active.posting_number,

  /* Что показать в окне сборки. Рамку скана, счёт и «что дальше» рисует ядро;
     здесь — метки отправления, кнопки стикера, товары и строка внизу. */
  card(state) {
    const posting = state.active;
    return {
      tags: [
        packUrgency(posting),
        posting.is_express ? '<span class="tag express">Express</span>' : '',
        posting.requires_mark ? '<span class="tag mark">Требуется маркировка</span>' : '',
        posting.is_multibox ? `<span class="tag">Многоместное: ${posting.multi_box_qty}</span>` : '',
        posting.printed_at ? '<span class="tag">Стикер печатался</span>' : '',
      ].join(''),
      actions: `
        <button class="btn" id="btn-print">Печать стикера</button>
        <a class="btn" id="btn-open-label" href="/api/pack/label/ozon/${encodeURIComponent(posting.posting_number)}.pdf"
           target="_blank" rel="noopener" title="Открыть PDF в новой вкладке">Открыть PDF</a>`,
      // Фото и части набора рисует общий market_pack.js: они есть у любой площадки.
      items: state.items.map((item) => ({
        name: item.name,
        image: item.image,
        scanned: item.scanned,
        need: item.need,
        ok: item.ok,
        // Артикул — основной карточки группы (article), ШК — всех сопоставленных.
        meta: `Артикул <b>${escapeHtml(item.article || item.offer_id || '—')}</b> · SKU <b>${escapeHtml(item.sku)}</b>`
          + (item.barcodes?.length ? ` · ШК <b>${escapeHtml(item.barcodes.join(', '))}</b>` : '')
          + (item.mandatory_mark ? ' · <span class="tag mark">Честный знак</span>' : '')
          + (item.is_set && item.kind !== 'kit' ? ` · <span class="tag">Набор из ${item.parts.length}</span>` : ''),
        extra: packSetParts(item),
        wait: item.waiting,
      })),
      details: [
        ['Отгрузка до', posting.shipment_date_local || posting.shipment_date],
        ['Куда', [posting.region, posting.city].filter(Boolean).join(', ')],
        ['Склад', posting.warehouse_name],
        ['Заказ', posting.order_number],
      ],
      force: 'Завершить без скана стикера',
      forceHint: 'Не читается стикер? Можно и ввести номер отправления в поле сканирования вручную.',
    };
  },
};
