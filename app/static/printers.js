/* Страница «Принтеры»: для каждого документа — размер листа и принтер.

   Строки рисует сервер. Здесь — список принтеров из QZ Tray этого компьютера,
   добавление и удаление размеров у документа, пробная печать и сохранение.
   QZ Tray не отвечает — в списках остаются «через браузер» и уже сохранённые
   принтеры: выбор можно поменять и без него. */
(() => {
  const panel = document.getElementById('printers-panel');
  if (!panel) return;
  const table = document.getElementById('printer-table');
  const status = document.getElementById('qz-status');
  const LIMIT = window.PRINTER_LIMIT || 4;
  let found = [];   // принтеры QZ Tray этого компьютера

  const rowsOf = (kind) => [...table.querySelectorAll(`tr.printer-row[data-kind="${CSS.escape(kind)}"]`)];

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

  /* Пересобрать список принтеров строки. Сохранённый принтер не теряем, даже
     если на этом компьютере его нет, — иначе «Сохранить» молча сбросил бы его
     на браузер. */
  function fillRow(row) {
    const select = row.querySelector('.printer-select');
    const chosen = select.value;
    const saved = select.dataset.saved || '';
    select.innerHTML = '';
    option(select, '', 'Через браузер (как сейчас)');
    for (const name of found) option(select, name, name);
    for (const name of new Set([saved, chosen])) {
      if (name && !found.includes(name)) option(select, name, `${name} — нет на этом компьютере`);
    }
    select.value = chosen;
    syncTest(row);
  }

  function syncTest(row) {
    row.querySelector('.btn-test').disabled = !row.querySelector('.printer-select').value;
  }

  function syncAdd(kind) {
    const add = table.querySelector(`tr.printer-add[data-kind="${CSS.escape(kind)}"] .btn-add`);
    if (add) add.hidden = rowsOf(kind).length >= LIMIT;
  }

  async function check() {
    setStatus('QZ Tray: подключаюсь…');
    try {
      const [version, names] = await Promise.all([QZ.version(), QZ.printers()]);
      found = names;
      setStatus(`QZ Tray ${version}: подключён, принтеров — ${names.length}`, 'ok');
    } catch (error) {
      found = [];
      setStatus(`QZ Tray: ${error.message}. Печать через браузер работает как раньше.`, 'warn');
    }
    table.querySelectorAll('tr.printer-row').forEach(fillRow);
  }

  /* Ещё размер у документа: копия его строки, без подписи и с первым
     свободным размером. Пример — этикетки Avito: 58×40 и 100×150. */
  function addRow(kind) {
    const rows = rowsOf(kind);
    if (!rows.length || rows.length >= LIMIT) return;
    const copy = rows[0].cloneNode(true);
    copy.cells[0].innerHTML = '';
    const size = copy.querySelector('.size-select');
    const used = new Set(rows.map((row) => row.querySelector('.size-select').value));
    const free = [...size.options].find((item) => !used.has(item.value));
    if (free) size.value = free.value;
    const printer = copy.querySelector('.printer-select');
    printer.dataset.saved = '';
    printer.value = '';
    copy.cells[4].innerHTML = '<button class="btn small btn-remove" title="Убрать этот размер">×</button>';
    rows[rows.length - 1].after(copy);
    bind(copy);
    fillRow(copy);
    syncAdd(kind);
  }

  function bind(row) {
    row.querySelector('.printer-select').addEventListener('change', () => syncTest(row));
    row.querySelector('.btn-remove')?.addEventListener('click', () => {
      const kind = row.dataset.kind;
      row.remove();
      syncAdd(kind);
    });
    row.querySelector('.btn-test').addEventListener('click', async (event) => {
      const printer = row.querySelector('.printer-select').value;
      const size = row.querySelector('.size-select');
      if (!printer) return;
      event.target.disabled = true;
      try {
        await QZ.printTest(printer, size.value,
                           `${row.dataset.title} · ${size.options[size.selectedIndex].text}`);
        toast(`Пробная страница ушла на «${printer}»`, 'ok');
      } catch (error) {
        toast(`QZ Tray: ${error.message}`, 'error', 10000);
      } finally {
        event.target.disabled = false;
      }
    });
  }

  table.querySelectorAll('tr.printer-row').forEach(bind);
  table.querySelectorAll('tr.printer-add .btn-add').forEach((button) => {
    button.addEventListener('click', () => addRow(button.closest('tr').dataset.kind));
  });
  document.getElementById('btn-qz-check').addEventListener('click', check);

  document.getElementById('btn-save-printers').addEventListener('click', async (event) => {
    const rows = [...table.querySelectorAll('tr.printer-row')].map((row) => ({
      kind: row.dataset.kind,
      size: row.querySelector('.size-select').value,
      printer: row.querySelector('.printer-select').value,
    }));
    event.target.disabled = true;
    try {
      const result = await api('/api/printers', { rows });
      // Страница уже знает новый выбор — печать с неё идёт по нему сразу.
      window.PRINTERS = result.printers;
      table.querySelectorAll('tr.printer-row').forEach((row) => {
        const select = row.querySelector('.printer-select');
        select.dataset.saved = select.value;
      });
      toast('Принтеры сохранены', 'ok');
    } catch (error) {
      toast(error.message, 'error', 8000);
    } finally {
      event.target.disabled = false;
    }
  });

  check();
})();
