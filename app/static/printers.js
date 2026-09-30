/* Страница «Принтеры»: для каждого документа — наклейка от площадки, бумага,
   принтер, ориентация и подгонка печати (⚙).

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

  /* Пробная печать, ориентация и подгонка — только у принтера QZ Tray: через
     браузер PDF печатается как есть, ориентацию там выбирает окно печати. */
  function syncTest(row) {
    const viaQz = Boolean(row.querySelector('.printer-select').value);
    row.querySelector('.btn-test').disabled = !viaQz;
    row.querySelector('.btn-fit').disabled = !viaQz;
    const orientation = row.querySelector('.orientation-select');
    orientation.disabled = !viaQz;
    orientation.title = viaQz ? 'Как печатать на этом принтере'
      : 'Через браузер ориентацию выбирают в окне печати';
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
    copy.querySelector('.orientation-select').value = '';
    // Новая строка — новый принтер: подгонку предыдущей не переносим.
    setFit(copy, { gap: 0, top: 0, right: 0, bottom: 0, left: 0 });
    syncPaper(copy);
    copy.cells[5].innerHTML = '<button class="btn small btn-remove" title="Убрать этот размер">×</button>';
    rows[rows.length - 1].after(copy);
    bind(copy);
    fillRow(copy);
    syncAdd(kind);
  }

  function bind(row) {
    row.querySelector('.printer-select').addEventListener('change', () => syncTest(row));
    row.querySelector('.size-select').addEventListener('change', () => syncPaper(row));
    row.querySelector('.btn-fit').addEventListener('click', () => openFit(row));
    row.querySelector('.btn-remove')?.addEventListener('click', () => {
      const kind = row.dataset.kind;
      row.remove();
      syncAdd(kind);
    });
    row.querySelector('.btn-test').addEventListener('click', (event) => testPrint(row, readFit(row), event.target));
  }

  /* ---------- бумага: готовый размер или свой ---------- */
  function syncPaper(row) {
    const custom = row.querySelector('.size-select').value === 'custom';
    row.querySelector('.paper-custom').hidden = !custom;
  }

  const num = (value) => Number(String(value ?? '').replace(',', '.').trim() || 0);
  const text = (value) => String(Math.round(num(value) * 10) / 10).replace('.', ',');

  /* ---------- подгонка строки (⚙): держится в data-атрибутах кнопки ---------- */
  const SIDES = ['top', 'right', 'bottom', 'left'];
  const WORDS = { top: 'вверх', right: 'вправо', bottom: 'вниз', left: 'влево' };

  function readFit(row) {
    const data = row.querySelector('.btn-fit').dataset;
    return Object.fromEntries(['gap', ...SIDES].map((key) => [key, num(data[key])]));
  }

  function fitNote(fit) {
    const parts = fit.gap ? [`зазор ${text(fit.gap)}`] : [];
    for (const side of SIDES) if (fit[side]) parts.push(`${WORDS[side]} ${text(fit[side])}`);
    return parts.join(' · ');
  }

  function setFit(row, fit) {
    const button = row.querySelector('.btn-fit');
    for (const key of ['gap', ...SIDES]) button.dataset[key] = text(fit[key]);
    const note = fitNote(fit);
    button.classList.toggle('set', Boolean(note));
    row.querySelector('.fit-note').textContent = note;
  }

  /* Строка, как она сейчас на экране (ещё не сохранённая), — для печати и сохранения. */
  function rowData(row, fit = readFit(row)) {
    const data = {
      kind: row.dataset.kind,
      size: row.querySelector('.size-select').value,
      printer: row.querySelector('.printer-select').value,
      orientation: row.querySelector('.orientation-select').value,
      gap: fit.gap,
      shift: Object.fromEntries(SIDES.map((side) => [side, fit[side]])),
    };
    if (data.size === 'custom') {
      data.width = num(row.querySelector('.paper-w').value);
      data.height = num(row.querySelector('.paper-h').value);
    }
    return data;
  }

  function paperText(row) {
    const select = row.querySelector('.size-select');
    if (select.value !== 'custom') return select.options[select.selectedIndex].text;
    return `${text(row.querySelector('.paper-w').value)}×${text(row.querySelector('.paper-h').value)} мм`;
  }

  async function testPrint(row, fit, button) {
    const data = rowData(row, fit);
    if (!data.printer) return;
    button.disabled = true;
    try {
      const orientation = row.querySelector('.orientation-select');
      const note = fitNote(fit);
      await QZ.printTest(data, `${row.dataset.title} · ${paperText(row)}`
                               + ` · ${orientation.options[orientation.selectedIndex].text}`
                               + (note ? ` · ${note}` : ''));
      toast(`Пробная этикетка ушла на «${data.printer}»`, 'ok');
    } catch (error) {
      toast(`QZ Tray: ${error.message}`, 'error', 10000);
    } finally {
      button.disabled = false;
    }
  }

  /* ---------- окно ⚙ ---------- */
  const modal = document.getElementById('fit-modal');
  const gapField = document.getElementById('fit-gap');
  const sideFields = [...modal.querySelectorAll('[data-side]')];
  let fitting = null;   // строка, чью подгонку правим

  function modalFit() {
    const fit = { gap: num(gapField.value) };
    for (const field of sideFields) fit[field.dataset.side] = num(field.value);
    return fit;
  }

  /* Схема в окне: пунктир — бумага, синяя рамка — куда ляжет печать. */
  function preview() {
    const fit = modalFit();
    const scale = 3;   // пикселей на мм — чтобы сдвиг был заметен
    const dx = (fit.right - fit.left) * scale;
    const dy = (fit.bottom - fit.top) * scale;
    document.getElementById('fit-label').style.transform = `translate(${dx}px, ${dy}px)`;
    document.getElementById('fit-summary').textContent = fitNote(fit) || 'без подгонки';
  }

  function openFit(row) {
    fitting = row;
    const fit = readFit(row);
    gapField.value = text(fit.gap);
    for (const field of sideFields) field.value = text(fit[field.dataset.side]);
    const printer = row.querySelector('.printer-select').value;
    document.getElementById('fit-subject').textContent =
      `${row.dataset.title} · ${printer} · бумага ${paperText(row)}`;
    // У листа A4 зазора нет: офисный принтер подаёт листы сам.
    gapField.disabled = row.querySelector('.size-select').value === 'a4';
    preview();
    modal.hidden = false;
    gapField.focus();
  }

  function closeFit() {
    modal.hidden = true;
    fitting = null;
  }

  modal.addEventListener('input', preview);
  document.getElementById('fit-close').addEventListener('click', closeFit);
  modal.addEventListener('click', (event) => { if (event.target === modal) closeFit(); });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && !modal.hidden) closeFit(); });
  document.getElementById('fit-reset').addEventListener('click', () => {
    gapField.value = '0';
    for (const field of sideFields) field.value = '0';
    preview();
  });
  document.getElementById('fit-test').addEventListener('click', (event) => {
    if (fitting) testPrint(fitting, modalFit(), event.target);
  });
  document.getElementById('fit-save').addEventListener('click', async (event) => {
    if (!fitting) return;
    const was = readFit(fitting);
    setFit(fitting, modalFit());
    // Подгонку сохраняем сразу: забыть нажать «Сохранить» внизу страницы легко.
    if (await saveAll(event.target)) closeFit();
    else setFit(fitting, was);
  });

  table.querySelectorAll('tr.printer-row').forEach(bind);
  table.querySelectorAll('tr.printer-add .btn-add').forEach((button) => {
    button.addEventListener('click', () => addRow(button.closest('tr').dataset.kind));
  });
  document.getElementById('btn-qz-check').addEventListener('click', check);

  async function saveAll(button) {
    const rows = [...table.querySelectorAll('tr.printer-row')].map((row) => rowData(row));
    const formats = Object.fromEntries([...table.querySelectorAll('.format-select')]
      .map((select) => [select.dataset.kind, select.value]));
    button.disabled = true;
    try {
      const result = await api('/api/printers', { rows, formats });
      // Страница уже знает новый выбор — печать с неё идёт по нему сразу.
      window.PRINTERS = result.printers;
      table.querySelectorAll('tr.printer-row').forEach((row) => {
        const select = row.querySelector('.printer-select');
        select.dataset.saved = select.value;
      });
      toast('Принтеры сохранены', 'ok');
      return true;
    } catch (error) {
      toast(error.message, 'error', 8000);
      return false;
    } finally {
      button.disabled = false;
    }
  }

  document.getElementById('btn-save-printers').addEventListener('click', (event) => saveAll(event.target));

  check();
})();
