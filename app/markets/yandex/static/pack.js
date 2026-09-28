/* Сборка Яндекс Маркета: чем её рабочее место отличается от общего.

   Общее — в /static/market_pack.js, там же и окно сборки. Здесь адреса
   запросов, слова и что показать в окне. Фото и штрихкоды Маркет в заказе не отдаёт: они из каталога
   панели — карточки Маркета, сопоставленной с ней или с тем же артикулом. Если
   штрихкода нет нигде, это видно на позиции: сканировать её нечем. */
window.PACKS = window.PACKS || {};
window.PACKS.yandex = {
  title: 'Яндекс Маркет',
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
    close: 'Наклейте и отсканируйте ярлык заказа',   // все товары на месте
    done: 'Заказ собран',
  },
  activeId: (active) => active.id,
  number: (active) => active.id,

  // Что ещё отсканировать — для подсказки «Следующий — …» в окне сборки.
  left: (state) => packLeft(state.items, (item) => item.name || 'Без названия',
    (item) => (item.barcodes?.length || item.is_set ? '' : 'нет штрихкода в каталоге')),

  /* Что показать в окне сборки. Рамку скана, счёт и «что дальше» рисует ядро;
     здесь — метки заказа, кнопки ярлыка, товары и строка внизу. */
  card(state) {
    const order = state.active;
    return {
      tags: [
        packUrgency(order),
        order.status_label ? `<span class="tag">${escapeHtml(order.status_label)}</span>` : '',
        order.printed_at ? '<span class="tag">Ярлык печатался</span>' : '',
      ].join(''),
      actions: `
        <button class="btn" id="btn-print">Печать ярлыка</button>
        <a class="btn" id="btn-open-label" href="/api/pack/label/yandex/${encodeURIComponent(order.id)}.pdf"
           target="_blank" rel="noopener" title="Открыть PDF в новой вкладке">Открыть PDF</a>`,
      items: state.items.map((item) => ({
        name: item.name,
        image: item.image,
        scanned: item.scanned,
        need: item.need,
        ok: item.ok,
        meta: `Артикул <b>${escapeHtml(item.offer_id || '—')}</b>`
          + (item.barcodes?.length
            ? ` · ШК <b>${escapeHtml(item.barcodes.join(', '))}</b>`
            : item.is_set ? '' : ' · <span class="tag overdue">нет штрихкода в каталоге</span>')
          + (item.is_set ? ` · <span class="tag">Набор из ${item.parts.length}</span>` : ''),
        extra: packSetParts(item),
      })),
      details: [
        ['Отгрузка до', order.deadline_local],
        ['Доставка', [order.delivery_label, order.service_name].filter(Boolean).join(' · ')],
        ['Номер у продавца', order.external_id],
        ['Комментарий', order.notes],
      ],
      force: 'Завершить без скана ярлыка',
      forceHint: 'Не читается ярлык? Можно и ввести номер заказа в поле сканирования вручную.',
    };
  },
};
