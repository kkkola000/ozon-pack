/* «Настройки → Настройка принтеров»: для каждого размера листа — браузер или
   принтер QZ Tray.

   Принтеры берём у QZ Tray этого компьютера. Не отвечает — в списке остаются
   «через браузер» и уже сохранённый принтер: выбор можно снять и без QZ Tray. */
(() => {
  const panel = document.getElementById('printers-panel');
  if (!panel) return;
  const status = document.getElementById('qz-status');
  const rows = [...panel.querySelectorAll('tr[data-size]')];

  function setStatus(text, kind) {
    status.textContent = text;
    status.style.color = kind === 'ok' ? 'var(--ok)' : kind === 'warn' ? '#b06a00' : '';
  }

  function option(select, value, label) {
    const element = document.createElement('option');
    element.value = value;
    element.textContent = label;
    select.appendChild(element);
  }

  /* Пересобрать списки: сохранённый принтер не теряем, даже если на этом
     компьютере его нет, — иначе «Сохранить» молча сбросил бы его на браузер. */
  function fill(names) {
    for (const row of rows) {
      const select = row.querySelector('select');
      const chosen = select.value;
      const saved = select.dataset.saved;
      select.innerHTML = '';
      option(select, '', 'Через браузер (как сейчас)');
      for (const name of names) option(select, name, name);
      for (const name of new Set([saved, chosen])) {
        if (name && !names.includes(name)) option(select, name, `${name} — нет на этом компьютере`);
      }
      select.value = chosen;
      syncTest(row);
    }
  }

  function syncTest(row) {
    row.querySelector('.btn-test').disabled = !row.querySelector('select').value;
  }

  async function check() {
    setStatus('QZ Tray: подключаюсь…');
    try {
      const [version, names] = await Promise.all([QZ.version(), QZ.printers()]);
      setStatus(`QZ Tray ${version}: подключён, принтеров — ${names.length}`, 'ok');
      fill(names);
    } catch (error) {
      setStatus(`QZ Tray: ${error.message}. Печать через браузер работает как раньше.`, 'warn');
      fill([]);
    }
  }

  for (const row of rows) {
    row.querySelector('select').addEventListener('change', () => syncTest(row));
    row.querySelector('.btn-test').addEventListener('click', async (event) => {
      const printer = row.querySelector('select').value;
      if (!printer) return;
      event.target.disabled = true;
      try {
        await QZ.printTest(printer, row.dataset.size, row.dataset.title);
        toast(`Пробная страница ушла на «${printer}»`, 'ok');
      } catch (error) {
        toast(`QZ Tray: ${error.message}`, 'error', 10000);
      } finally {
        event.target.disabled = false;
      }
    });
  }

  document.getElementById('btn-qz-check').addEventListener('click', check);

  document.getElementById('btn-save-printers').addEventListener('click', async (event) => {
    const chosen = {};
    for (const row of rows) chosen[row.dataset.size] = row.querySelector('select').value;
    event.target.disabled = true;
    try {
      const result = await api('/api/printers', { printers: chosen });
      // Страница уже знает новый выбор — печать с неё идёт по нему сразу.
      window.PRINTERS = result.printers;
      for (const row of rows) row.querySelector('select').dataset.saved = result.printers[row.dataset.size] || '';
      toast('Принтеры сохранены', 'ok');
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      event.target.disabled = false;
    }
  });

  check();
})();
