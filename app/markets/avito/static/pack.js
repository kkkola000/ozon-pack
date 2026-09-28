/* Сборка Avito: чем её рабочее место отличается от общего.

   Общее — в /static/market_pack.js, там же и окно сборки. Своё здесь: порядок обратный, чем у Ozon.
   Сборку открывает скан этикетки, а не товара, — поэтому печати по скану нет,
   как нет и «завершить без скана»: закрывает заказ последняя единица товара.

   Своих штрихкодов у Avito нет, поэтому позиция — это единица товара. Если
   объявление сопоставлено с карточкой другой площадки, скан сверяется с её
   штрихкодами (метка «сверка»), у набора — по частям. Иначе панель записывает
   то, что отсканировали, без сверки. */
window.PACKS = window.PACKS || {};
window.PACKS.avito = {
  title: 'Avito',
  print: null,
  words: {
    // Слова замка на наклейки — общие: выгрузка идёт сразу по всем кабинетам.
    label: 'Этикетка',
    released: 'Сборка отменена. Сканируйте этикетку следующего заказа.',
    confirmRelease: 'Отменить сборку этого заказа?',
    confirmComplete: '',
    close: 'Отсканируйте стикер — заказ закроется',   // все товары на месте
    done: 'Заказ собран',
  },
  activeId: (active) => active.id,
  number: (active) => active.marketplace_id || active.id,

  /* Что показать в окне сборки. Позиция — единица товара: у каждой свой
     скан. Печати по скану нет — этикетка открывается ссылкой, как и раньше. */
  card(state) {
    const order = state.active;
    const service = order.service_name || order.service_label || '';
    return {
      tags: service ? `<span class="tag">${escapeHtml(service)}</span>` : '',
      actions: `
        <a class="btn" href="/api/pack/label/avito/${encodeURIComponent(order.id)}.pdf"
           target="_blank" rel="noopener"
           data-print-pdf="/api/pack/label/avito/${encodeURIComponent(order.id)}.pdf"
           data-print-kind="avito:label" data-print-name="Этикетка">Этикетка</a>`,
      items: state.items.map((item) => ({
        name: item.title,
        image: item.image,
        scanned: item.scanned ? 1 : 0,
        need: 1,
        ok: Boolean(item.ok),
        // Своих артикула и ШК у объявления может не быть — они берутся у
        // сопоставленных карточек: артикул основной, ШК всех (article, barcodes).
        meta: (item.article ? `Артикул <b>${escapeHtml(item.article)}</b> · ` : '')
          + `единица ${item.unit_no}`
          + (item.barcodes?.length ? ` · ШК <b>${escapeHtml(item.barcodes.join(', '))}</b>`
            : item.barcode ? ` · штрихкод <b>${escapeHtml(item.barcode)}</b>` : '')
          + (item.is_set && item.kind !== 'kit' ? ` · <span class="tag">Набор из ${item.parts.length}</span>`
            : item.checked && !item.is_set ? ' · <span class="tag" title="Объявление сопоставлено: подходят только эти штрихкоды">сверка</span>' : ''),
        extra: packSetParts(item),
        wait: item.waiting,
      })),
      details: [['Заказ', order.marketplace_id || order.id], ['Доставка', service]],
      force: '',
    };
  },
};
