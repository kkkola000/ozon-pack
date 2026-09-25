/* Раздел «Заказы»: один список на все кабинеты.

   Выбирать можно заказы разных кабинетов сразу, а площадка у каждого своя.
   Поэтому любое действие раскладывается по кабинетам: «Собрать в Ozon» берёт
   только заказы Ozon, наклейки печатаются по кабинету — своим файлом и на
   принтер своей площадки (см. «Принтеры»). Кабинет в шапке ни при чём: каждый
   запрос несёт кабинет своего заказа. */
(() => {
  const rows = [...document.querySelectorAll('#order-rows tr')];
  const checkAll = document.getElementById('check-all');
  const counter = document.getElementById('selected-count');

  const picked = () => rows.filter((row) => row.querySelector('.pick').checked);
  const fits = (row, id) => (id === 'labels'
    ? row.dataset.label === '1'
    : row.dataset.actions.split(' ').includes(id));

  function refresh() {
    const chosen = picked();
    counter.textContent = `выбрано: ${chosen.length}`;
    document.querySelectorAll('[data-bulk]').forEach((button) => {
      const n = chosen.filter((row) => fits(row, button.dataset.bulk)).length;
      button.disabled = !n;
      button.querySelector('[data-n]').textContent = n ? `(${n})` : '';
    });
  }

  /* Заказы по кабинетам, в порядке списка. */
  function byShop(list) {
    const groups = new Map();
    list.forEach((row) => {
      if (!groups.has(row.dataset.shop)) groups.set(row.dataset.shop, []);
      groups.get(row.dataset.shop).push(row);
    });
    return [...groups.values()];
  }

  function busy(button, text) {
    const was = button.innerHTML;
    button.disabled = true;
    button.textContent = text;
    return () => { button.innerHTML = was; button.disabled = false; };
  }

  /* Действие площадки: по запросу на кабинет. После — перечитать страницу:
     заказы переезжают между статусами. */
  async function run(id, list, button, ask = '') {
    const targets = list.filter((row) => fits(row, id));
    if (!targets.length) return;
    if (ask && !confirm(ask.replace('{n}', targets.length))) return;
    const done = busy(button, 'Отправляем…');
    let changed = false;
    for (const group of byShop(targets)) {
      const shop = group[0].dataset.shopTitle;
      try {
        const result = await api('/api/orders/action', {
          account_id: Number(group[0].dataset.shop),
          action: id.split(':')[1],
          ids: group.map((row) => row.dataset.id),
        });
        changed = true;
        toast(`${shop}: ${result.message}`, result.status === 'ok' ? 'ok' : 'warning', 8000);
        (result.results || []).filter((item) => item.status !== 'ok')
          .forEach((item) => toast(item.message, 'error', 12000));
      } catch (error) {
        toast(`${shop}: ${error.message}`, 'error', 12000);
      }
    }
    if (changed) setTimeout(() => window.location.reload(), 1200);
    else done();
  }

  /* По кабинетам — и ещё на части, если площадка столько за раз не отдаёт. */
  function batches(list) {
    const out = [];
    byShop(list).forEach((group) => {
      const size = Number(group[0].dataset.max) || 50;
      for (let at = 0; at < group.length; at += size) out.push(group.slice(at, at + size));
    });
    return out;
  }

  /* Наклейки: файл на кабинет, не больше, чем площадка отдаёт за раз. Каждый
     следующий файл ждёт, пока напечатается предыдущий. */
  async function printLabels(list, button) {
    const targets = list.filter((row) => row.dataset.label === '1');
    if (!targets.length) return;
    const done = busy(button, 'Печатаем…');
    try {
      for (const group of batches(targets)) {
        const first = group[0];
        const name = group.length > 1
          ? `${first.dataset.shopTitle}: наклейки (${group.length} шт)`
          : `${first.dataset.labelWord} ${first.dataset.number}`;
        try {
          const response = await fetch('/api/orders/labels.pdf', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
            body: JSON.stringify({ account_id: Number(first.dataset.shop), ids: group.map((row) => row.dataset.id) }),
          });
          if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(data.detail || `Ошибка ${response.status}`);
          }
          // Размер листа читаем до blob-адреса: по нему выбирается принтер («Принтеры»).
          const pageSize = response.headers.get('X-Page-Size');
          const url = URL.createObjectURL(await response.blob());
          const ok = await printPdf(url, {
            name, asBlob: false, kind: `${first.dataset.market}:label`, pageSize, wait: true,
          });
          if (ok) toast(`${name}: отправлено на печать`, 'ok');
        } catch (error) {
          toast(`${name}: ${error.message}`, 'error', 12000);
        }
      }
    } finally {
      done();
      refresh();
    }
  }

  checkAll?.addEventListener('change', () => {
    rows.forEach((row) => { row.querySelector('.pick').checked = checkAll.checked; });
    refresh();
  });
  rows.forEach((row) => row.querySelector('.pick').addEventListener('change', refresh));

  document.querySelectorAll('[data-bulk]').forEach((button) => {
    button.addEventListener('click', () => {
      if (button.dataset.bulk === 'labels') printLabels(picked(), button);
      else run(button.dataset.bulk, picked(), button, button.dataset.ask);
    });
  });

  rows.forEach((row) => {
    row.querySelectorAll('[data-act]').forEach((button) => {
      button.addEventListener('click', () => run(button.dataset.act, [row], button));
    });
    row.querySelector('[data-print]')?.addEventListener('click', (event) => printLabels([row], event.target));
    /* Снять отметку «Собран». Заказ вернётся в работу, и его соберут заново, а
       отметка о том, кто собирал, пропадёт, — поэтому спрашиваем. */
    row.querySelector('[data-reset]')?.addEventListener('click', async (event) => {
      if (!confirm(`Снять отметку «Собран» с ${row.dataset.number}?`)) return;
      const done = busy(event.target, 'Снимаем…');
      try {
        const result = await api('/api/orders/reset', { account_id: Number(row.dataset.shop), id: row.dataset.id });
        toast(result.message, 'ok');
        setTimeout(() => window.location.reload(), 800);
      } catch (error) {
        toast(error.message, 'error');
        done();
      }
    });
  });

  document.getElementById('btn-sync')?.addEventListener('click', async (event) => {
    const done = busy(event.target, 'Обновляем…');
    try {
      const shop = encodeURIComponent(event.target.dataset.shop || 'all');
      const result = await api(`/api/orders/sync?shop=${shop}`, {});
      toast(result.message, result.failed?.length ? 'warning' : 'ok', 8000);
      setTimeout(() => window.location.reload(), 700);
    } catch (error) {
      toast(error.message, 'error', 10000);
      done();
    }
  });

  refresh();
})();
