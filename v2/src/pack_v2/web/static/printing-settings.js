(() => {
  const root = document.querySelector('[data-printing-settings]'); if (!root) return;
  const api = window.PackPrinting, body = root.querySelector('[data-printer-rows]');
  let setup = JSON.parse(root.dataset.setup), rows = structuredClone(setup.profiles), names = [], busy = false;
  const sizes = {'58x40':'58×40 мм','75x120':'75×120 мм','100x150':'100×150 мм',a4:'A4',custom:'Свой размер…'};
  const papers = {'58x40':[58,40],'75x120':[75,120],'100x150':[100,150],a4:[210,297]};
  const make = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const report = (text, state='') => { const n = root.querySelector('[data-print-settings-notice]'); n.hidden = !text; n.textContent = text; n.className = `notice ${state}`; };
  function select(values, value, change, label) {
    const n = make('select'); n.setAttribute('aria-label',label);
    for (const [key,title] of values) { const option = make('option','',title); option.value = key; n.append(option); }
    n.value = value; n.addEventListener('change', () => change(n.value)); return n;
  }
  function number(row, key, title, low, high) {
    const label = make('label','small',title), input = make('input');
    Object.assign(input,{type:'number',min:String(low),max:String(high),step:'0.1',value:row[key],required:true});
    input.setAttribute('aria-label',title); input.addEventListener('input', () => { row[key] = Number(input.value); }); label.append(input); return label;
  }
  function render() {
    body.replaceChildren(); const labelNumbers = {};
    rows.forEach((row, index) => {
      const line = make('tr'), doc = make('td');
      doc.append(make('b','',row.kind === 'ozon:label' ? 'Ozon: стикеры' : row.kind === 'yandex:label' ? 'Яндекс Маркет: ярлыки' : 'Дубликаты «Честный знак»'));
      if (row.kind === 'marking:duplicate') doc.append(make('div','muted small','Один общий принтер для всех площадок.'));
      const paper = make('td'); paper.append(select(Object.entries(sizes),row.size,value => {
        row.size = value; if (papers[value]) [row.width,row.height] = papers[value]; render();
      }, 'Бумага для печати'));
      if (row.size === 'custom') { const custom = make('div','printing-custom'); custom.append(number(row,'width','Ширина, мм',30,500),number(row,'height','Высота, мм',30,500)); paper.append(custom); }
      const printer = make('td'), options = [['','Через браузер'],...names.map(name => [name,name])];
      if (row.printer && !names.includes(row.printer)) options.push([row.printer, `${row.printer} — не проверен на этом компьютере`]);
      printer.append(select(options,row.printer,value => { row.printer = value; },'Принтер'));
      const orientation = make('td'); orientation.append(select([['','Как в файле'],['portrait','Вертикальная'],['landscape','Горизонтальная']],row.orientation,value => { row.orientation = value; },'Ориентация'));
      const actions = make('td'), buttons = make('div','printing-actions');
      const test = make('button','btn small','Пробная печать'); test.type = 'button'; test.disabled = busy;
      test.addEventListener('click',async () => { if (busy) return; test.disabled = true; await api.test(row,report); test.disabled = false; }); buttons.append(test);
      const calibration = make('details','printing-calibration'); calibration.append(make('summary','btn small','⚙ Подгонка'));
      const fields = make('div','printing-calibration-fields'); fields.append(number(row,'gap','Зазор, мм',0,20),number(row,'x','Сдвиг X, мм',-30,30),number(row,'y','Сдвиг Y, мм',-30,30)); calibration.append(fields);
      if (row.kind.endsWith(':label')) {
        labelNumbers[row.kind] = (labelNumbers[row.kind] || 0) + 1;
        if (labelNumbers[row.kind] > 1) { const remove = make('button','btn small','Убрать размер'); remove.type = 'button'; remove.addEventListener('click',() => { rows.splice(index,1); render(); }); buttons.append(remove); }
      }
      actions.append(buttons,calibration); line.append(doc,paper,printer,orientation,actions); body.append(line);
      if (row.kind.endsWith(':label') && rows[index + 1]?.kind !== row.kind && labelNumbers[row.kind] < 4) {
        const extra = make('tr'), cell = make('td'); cell.colSpan = 5;
        const add = make('button','btn small','+ ещё размер'); add.type = 'button';
        add.addEventListener('click',() => { const used = new Set(rows.filter(r => r.kind === row.kind).map(r => r.size)); const size = Object.keys(papers).find(s => !used.has(s)) || 'custom'; rows.splice(index + 1,0,{...row,size,width:(papers[size] || [60,40])[0],height:(papers[size] || [60,40])[1]}); render(); });
        cell.append(add); extra.append(cell); body.append(extra);
      }
    });
  }
  root.querySelector('[data-qz-check]').addEventListener('click',async event => {
    const button = event.currentTarget, status = root.querySelector('[data-qz-status]'); button.disabled = true; status.textContent = 'Проверяем QZ Tray…';
    try { names = await api.printers(); status.textContent = `QZ Tray подключён · принтеров: ${names.length}`; status.className = 'small access-ok'; render(); }
    catch (_) { status.textContent = 'QZ Tray недоступен — печать через браузер'; status.className = 'small access-warning'; }
    finally { button.disabled = false; }
  });
  root.querySelector('[data-print-save]').addEventListener('click', async event => {
    if (busy || ![...root.querySelectorAll('input[type=number]')].every(n => n.reportValidity())) return;
    const button = event.currentTarget; busy = true; button.disabled = true; button.textContent = 'Сохраняем…';
    try { setup = await api.json('/settings',{revision:setup.revision,product_barcodes:root.querySelector('[data-print-product]').checked,marking_duplicates:root.querySelector('[data-print-marking]').checked,profiles:rows}); rows = structuredClone(setup.profiles); report('Настройки сохранены'); }
    catch (error) { report(error.message || 'Не удалось сохранить настройки','error'); }
    finally { busy = false; button.disabled = false; button.textContent = 'Сохранить'; render(); }
  });
  render();
})();
