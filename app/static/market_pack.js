/* Рабочее место сборщика — одно на все площадки.

   Здесь всё, что на складе одинаково: один поток сканов, замок на выгрузку
   наклеек, история сканов, счётчики очереди, печать и опрос сервера. Адреса
   запросов тоже общие: кабинет в шапке больше ничего не решает, а какому
   кабинету принадлежит отсканированный код, разбирается сервер.

   Сборка идёт во всплывающем окне (#pack-modal): открывается само, когда скан
   открыл заказ, и закрывается, когда заказ собран или сборку отменили. Поле
   сканирования одно — сканер это клавиатура — и переезжает в окно и обратно
   вместе с итогом скана.

   Площадки отличаются словами и тем, что показать в окне: шапку, товары,
   строку внизу. Каждая объявляет своё в markets/<код>/static/pack.js
   (pack.card) и кладёт в window.PACKS[код]; подключены сразу все, потому что
   открытый заказ может оказаться из любого кабинета. Ничего «если это Ozon»
   здесь быть не должно. */
const PACKS = window.PACKS || {};

const input = document.getElementById('scan');
const PAGE_PLACEHOLDER = input.placeholder;   // подсказка поля на странице — словами площадки
const banner = document.getElementById('banner');
/* Поле с итогом скана и его два места: на странице и в окне сборки. */
const unit = document.getElementById('scan-unit');
const home = document.getElementById('scan-home');
const modal = document.getElementById('pack-modal');
const slot = document.getElementById('pack-slot');
const packScan = document.getElementById('pack-scan');
/* Фильтр кабинета уезжает в адрес запроса: он задаёт границы сборки — и какие
   заказы сканируются, и чьи наклейки считать. */
const SHOP_FILTER = new URLSearchParams(window.location.search).get('shop') || 'all';
const at = (path) => `${path}?shop=${encodeURIComponent(SHOP_FILTER)}`;
const STATE_URL = at('/api/pack/state');
const LABELS_URL = at('/api/pack/labels.zip');
const SCAN_URL = at('/api/pack/scan');
const RELEASE_URL = at('/api/pack/release');
const COMPLETE_URL = at('/api/pack/complete');
const SYNC_URL = at('/api/pack/sync');
const SHEET_URL = at('/api/pack/orders-sheet.pdf');

const historyBox = document.getElementById('history');

let busy = false;
let hasActive = false;   // открыта ли сборка — при открытой наклейка не печатается
let pack = null;         // площадка открытого заказа: её слова и её карточка
let opened = null;       // состояние открытой сборки — для «Собрано» после неё
const history = [];

/* ---------- Зона сканирования ----------
   Рамка показывает, куда сейчас уйдёт скан. Сканер — это клавиатура: если
   курсор не в поле, коды пропадают, а Enter в конце кода ещё и нажимает
   кнопку, на которой стоит фокус. Поэтому «сканер не слушает» видно сразу. */
const zone = document.getElementById('scan-panel');
const SIGNS = { ok: '✓', error: '✕', warning: '!', busy: '…', idle: '›' };
const STATE_WORDS = {
  lost: 'Сканер не слушает', busy: 'Проверяем…', error: 'Ошибка скана', warning: 'Проверьте', ready: 'Сканер готов',
};
let flash = null;        // итог последнего скана: busy | ok | error | warning
let flashTimer = null;
let lost = false;        // поле без фокуса — коды сканера сейчас теряются
let lostTimer = null;

function listening() {
  return document.hasFocus() && document.activeElement === input;
}

/* Рамка, которая говорит о сканере, — та, где сейчас поле: в окне сборки
   или на странице. Вторая молчит: под окном страница сборку только ждёт. */
function fieldBox() {
  return unit.parentElement === slot ? packScan : zone;
}

function paintZone() {
  const here = fieldBox();
  for (const box of [zone, packScan]) {
    if (!box) continue;
    const mine = box === here;
    box.classList.toggle('is-lost', mine && lost);
    for (const kind of ['busy', 'ok', 'error', 'warning']) {
      box.classList.toggle(`is-${kind}`, mine && !lost && flash === kind);
    }
    const word = box.querySelector('.scan-state span');
    if (word) word.textContent = mine ? STATE_WORDS[lost ? 'lost' : flash in STATE_WORDS ? flash : 'ready'] : 'Идёт сборка';
  }
}

/* Фокус ушёл — ждём полсекунды: клик по кнопке или печать уводят его на миг,
   и мигать из-за этого рамкой незачем. Вернулся — гасим сразу. */
function watchFocus() {
  if (listening()) {
    clearTimeout(lostTimer);
    lostTimer = null;
    if (lost) { lost = false; paintZone(); }
  } else if (!lost && !lostTimer) {
    lostTimer = setTimeout(() => {
      lostTimer = null;
      lost = !listening();
      paintZone();
    }, 600);
  }
}

/* Фокус возвращаем сами, если человек не печатает в другом поле (поиск по
   заказам, выбор): после нажатия кнопки следующий скан должен попасть сюда. */
function keepFocus() {
  const current = document.activeElement;
  if (current === input || !document.hasFocus()) return;
  if (current?.matches('input:not([type=checkbox]):not([type=radio]), select, textarea')) return;
  input.focus();
}
setInterval(() => { keepFocus(); watchFocus(); }, 500);
for (const target of [input, window]) {
  target.addEventListener('focus', watchFocus);
  target.addEventListener('blur', watchFocus);
}
document.addEventListener('click', (event) => {
  if (!event.target.closest('button, a, input, select, label, textarea')) input.focus();
});
document.getElementById('scan-refocus')?.addEventListener('click', () => input.focus());

/* Итог скана — в той же рамке. «Найден» гаснет сам, ошибка держится до
   следующего скана: отвернулся на секунду — всё равно увидишь. */
function setBanner(kind, message, code = '') {
  banner.className = `scan-result ${kind}`;
  banner.querySelector('.sign').textContent = SIGNS[kind] || SIGNS.idle;
  banner.querySelector('.text').textContent = message;
  banner.querySelector('.code').textContent = code
    ? `${code} · ${new Date().toLocaleTimeString('ru-RU')}` : '';
  clearTimeout(flashTimer);
  flash = kind === 'idle' ? null : kind;
  if (flash === 'ok') flashTimer = setTimeout(() => { flash = null; paintZone(); }, 1500);
  if (flash === 'error' || flash === 'warning') {
    const box = fieldBox();
    box.classList.remove('is-shake');
    void box.offsetWidth;   // перезапуск встряски при повторной ошибке
    box.classList.add('is-shake');
  }
  paintZone();
}

/* Общие куски окна сборки: фото, части набора, срочность. Площадки зовут их
   из своих pack.js при рисовании — к этому времени файл уже загружен. Фото и наборы теперь бывают у любой площадки: их
   даёт сопоставленная карточка другого кабинета. */
function packPhoto(image, name) {
  if (!image) return '<div class="item-photo blank">🖼</div>';
  return `<img class="item-photo" src="${escapeHtml(image)}" alt="${escapeHtml(name || '')}"
               data-zoom="${escapeHtml(image)}" data-name="${escapeHtml(name || '')}" loading="lazy"
               onerror="this.replaceWith(Object.assign(document.createElement('div'), {className: 'item-photo blank', textContent: '🖼'}))">`;
}

/* Набор на площадке — обычный товар, а на складе это несколько вещей со своими
   штрихкодами. Сборщик сканирует их, поэтому и видеть он должен их, а не одну
   строку «набор 0/1», по которой непонятно, что ещё брать.

   Комплект — сам товар и то, что в него вкладывают: первой строкой товар,
   под ним вложения. Какое вложение ждут сейчас, подсвечено. Подписей
   «основной» и «вложить» нет: порядок строк говорит это сам. В каждой
   строке — название, артикул и штрихкод. */
function packPartRow(part, name, article, barcode) {
  const info = [article ? `арт. ${escapeHtml(article)}` : '', barcode ? escapeHtml(barcode) : '']
    .filter(Boolean).join(' · ');
  return `
    <div class="set-part ${part.ok ? 'ok' : part.next ? 'next' : ''}">
      <span class="qty">${part.scanned} / ${part.need}</span>
      <span class="grow">${escapeHtml(name || '—')}${info ? `<span class="muted mono"> · ${info}</span>` : ''}</span>
      ${part.ok ? '<span class="check">✔</span>' : ''}
    </div>`;
}

function packSetParts(item) {
  if (!item.is_set) return '';
  return `
    <div class="set-parts">
      ${item.parts.map((part) => (part.main
        ? packPartRow(part, item.name || item.title, item.article, (item.barcodes || [])[0])
        : packPartRow(part, part.name, part.article, part.barcode))).join('')}
    </div>`;
}

/* Срок отгрузки — меткой в шапке окна: «Срочно · осталось 3 часа». */
function packUrgency(active) {
  const text = { overdue: 'Просрочено', urgent: 'Срочно', soon: 'Сегодня' }[active.urgency];
  if (!text) return '';
  return `<span class="tag ${active.urgency}">${text} · ${escapeHtml(hoursLeftText(active.hours_left))}</span>`;
}

/* ---------- Окно сборки ----------
   Поле переезжает туда, где идёт работа: открыли заказ — в окно, закрыли —
   обратно на страницу. Фокус возвращаем сразу: перенос его снимает, а
   следующий код сканера должен попасть в поле. Не переехало — фокус не
   трогаем: опрос сервера не должен выдёргивать курсор из поиска по заказам. */
function moveField(target) {
  if (unit.parentElement === target) return;
  target.appendChild(unit);
  // На странице поле говорит своими словами — в окне оно говорит, что дальше.
  if (target === home) input.placeholder = PAGE_PLACEHOLDER;
  input.focus();
}

let doneTimer = null;   // «Собрано» на экране — через две секунды окно закроется
const DONE_MS = 2000;

function showWork() {
  clearTimeout(doneTimer);
  doneTimer = null;
  document.getElementById('pack-work').hidden = false;
  document.getElementById('pack-done').hidden = true;
  if (modal.hidden) {
    modal.hidden = false;
    modal.scrollTop = 0;
    document.body.classList.add('pack-open');
  }
  moveField(slot);
}

function closeModal() {
  clearTimeout(doneTimer);
  doneTimer = null;
  if (modal.hidden && unit.parentElement === home) return;
  modal.hidden = true;
  document.body.classList.remove('pack-open');
  moveField(home);
  paintZone();
}

/* Заказ собран: окно говорит это крупно и закрывается само. Поле уже на
   странице — следующий скан откроет следующую сборку, не дожидаясь. */
function showDone(finished) {
  document.getElementById('pack-work').hidden = true;
  document.getElementById('pack-done').hidden = false;
  document.getElementById('pack-actions').innerHTML = '';
  document.getElementById('pack-done-title').textContent = finished.words.done || 'Собрано';
  document.getElementById('pack-done-text').textContent =
    `${finished.number} · отсканировано ${finished.done} из ${finished.total} шт · окно закроется через 2 с`;
  modal.hidden = false;
  document.body.classList.add('pack-open');
  moveField(home);
  clearTimeout(doneTimer);
  doneTimer = setTimeout(closeModal, DONE_MS);
}

/* Товар в окне: фото, название, подробности от площадки и счёт. Следующий,
   за которым идти к полке, выделен — он первый из несобранных. */
function packItem(item, next) {
  const mark = item.ok ? 'done' : next ? 'next' : '';
  // Комплект ждёт вложение — флажок называет, что вложить.
  const flag = item.wait ? `Вложите «${escapeHtml(item.wait)}» и отсканируйте его` : 'Сканируйте этот товар';
  return `
    <div class="pack-item ${mark}">
      ${packPhoto(item.image, item.name)}
      <div class="grow">
        <div class="pack-name">${escapeHtml(item.name || 'Без названия')}</div>
        ${item.meta ? `<div class="pack-meta">${item.meta}</div>` : ''}
        ${next ? `<span class="pack-flag">${flag}</span>` : ''}
        ${item.extra || ''}
      </div>
      <div class="pack-qty">${item.ok ? '✓ ' : ''}${item.scanned} / ${item.need}<small>${item.ok ? 'собрано' : 'шт'}</small></div>
    </div>`;
}

/* Все товары на месте — последней карточкой списка: что сделать, чтобы
   закрыть заказ. Слова площадки: стикер отправления, этикетка, ярлык. */
function packStep(words, done, total) {
  const icon = document.getElementById('pack-icon-label').innerHTML;
  return `
    <div class="pack-item pack-step next">
      <div class="pack-step-icon">${icon}</div>
      <div class="grow">
        <div class="pack-name">${escapeHtml(words.close || 'Отсканируйте наклейку — заказ закроется')}</div>
        <div class="pack-meta">Все товары на месте · ${done} из ${total} шт</div>
      </div>
    </div>`;
}

/* Окно рисуют двое. Площадка открытого заказа даёт шапку, товары и строку
   внизу (pack.card), ядро — поле скана, счёт и «что дальше»: они одинаковые
   у всех. Кнопки общие — печать, отмена, завершение без скана; каких у
   площадки нет, те она просто не рисует. */
function paintModal(state) {
  const card = pack.card(state);
  const active = state.active;
  document.getElementById('pack-dot').className = `dot ${state.market}`;
  document.getElementById('pack-kicker').textContent =
    [pack.title, state.shop ? `кабинет «${state.shop}»` : ''].filter(Boolean).join(' · ');
  document.getElementById('pack-number').textContent = (pack.number || pack.activeId)(active);
  document.getElementById('pack-tags').innerHTML = card.tags || '';
  document.getElementById('pack-actions').innerHTML =
    `${card.actions || ''}<button class="btn danger" id="btn-release">Отменить сборку</button>`;

  const done = state.done || 0;
  const total = state.total || 0;
  // Все товары на месте — дальше скан наклейки. Решает сервер: у набора он
  // знает, какие части ещё не отсканированы.
  const complete = Boolean(state.complete) && total > 0;
  document.getElementById('pack-icon-scan').hidden = complete;
  document.getElementById('pack-icon-label').hidden = !complete;
  // Комплект ждёт вложение — сервер пропустит только его, и поле так и говорит.
  const items = card.items || [];
  const waiting = items.findIndex((item) => item.wait);
  input.placeholder = complete ? `${pack.words.close || 'Отсканируйте наклейку'}…`
    : waiting >= 0 ? `Вложите «${items[waiting].wait}» и отсканируйте его…` : 'Сканируйте товар…';
  document.getElementById('pack-bar').style.width = `${total ? Math.round((done / total) * 100) : 0}%`;
  document.getElementById('pack-count').textContent = `${done} из ${total}`;

  const next = waiting >= 0 ? waiting : items.findIndex((item) => !item.ok);
  document.getElementById('pack-items').innerHTML = items.map((item, index) => packItem(item, index === next)).join('')
    + (complete ? packStep(pack.words, done, total) : '');

  const foot = document.getElementById('pack-foot');
  const details = (card.details || []).filter(([, value]) => value)
    .map(([label, value]) => `<span>${escapeHtml(label)} <b>${escapeHtml(value)}</b></span>`).join('');
  foot.innerHTML = `${details}<span class="grow"></span>${card.force
    ? `<button class="btn small" id="btn-force" title="${escapeHtml(card.forceHint || '')}">${escapeHtml(card.force)}</button>`
    : ''}`;
  foot.hidden = !details && !card.force;

  const print = document.getElementById('btn-print');
  if (print) print.onclick = () => printLabel(pack.activeId(active), reservePrintWindow());
  document.getElementById('btn-release').onclick = releaseActive;
  const force = document.getElementById('btn-force');
  if (force) force.onclick = forceComplete;
}

/* Какая это площадка, говорит сам ответ сервера (state.market): заказ мог
   открыться в любом кабинете, и гадать по шапке нельзя. */
function renderActive(state) {
  pack = PACKS[state?.market] || null;
  hasActive = Boolean(state?.active) && pack !== null;
  opened = hasActive ? state : null;
  if (!hasActive) {
    // «Собрано» дожидается своих двух секунд — закроет его таймер.
    if (!doneTimer) closeModal();
    paintZone();
    return;
  }
  showWork();
  paintModal(state);
  paintZone();
}

function pushHistory(code, result) {
  history.unshift({ code, status: result.status, message: result.message, at: new Date() });
  // Три строки, не больше: сборщик смотрит сюда, только чтобы убедиться, что
  // предыдущий скан прошёл. Длинная лента отодвигала список заказов вниз.
  if (history.length > 3) history.pop();
  historyBox.innerHTML = history.map((entry) => `
    <div style="padding:4px 0;border-bottom:1px solid var(--line)">
      <span class="mono">${entry.at.toLocaleTimeString('ru-RU')}</span> ·
      <span class="mono">${escapeHtml(entry.code)}</span> ·
      <span style="color:${entry.status === 'error' ? 'var(--err)' : entry.status === 'warning' ? '#b06a00' : 'var(--ok)'}">
        ${escapeHtml(entry.message)}
      </span>
    </div>`).join('');
}

function applyResult(result, code, printWindow = null) {
  document.getElementById('photo-zoom')?.classList.remove('open');
  /* Заказ закрылся — запоминаем, что именно, до того как окно перерисуется. */
  const finished = opened && result.action === 'completed'
    ? { words: pack.words, number: (pack.number || pack.activeId)(opened.active),
        done: opened.done || 0, total: opened.total || 0 }
    : null;
  // Новый скан во время «Собрано» — сразу к нему: ждать две секунды незачем.
  if (doneTimer) closeModal();
  renderActive(result.state || { active: null });
  if (finished) showDone(finished);
  setBanner(result.status, result.message, code || '');
  beep(result.sound || result.status);
  if (result.counters) applyCounters(result.counters);
  /* Замок — сразу по ответу: сборку завершили или отменили, а новые наклейки
     не скачаны — поле закрывается сейчас, а не при следующем опросе. */
  if (result.labels) applyGate(result.labels);
  if (code) pushHistory(code, result);
  /* Печатать наклейку умеет не всякая площадка, и ключ у каждой свой. Берём
     ту, чей заказ только что открылся: renderActive уже поставил её выше. */
  const printer = PACKS[result.market]?.print;
  const toPrint = printer && result.print?.[printer.key];
  if (toPrint) {
    printLabel(toPrint, printWindow);
  } else if (printWindow) {
    // Вкладка не понадобилась. Пустую закрываем, а вкладку с прошлым ярлыком
    // оставляем: оператор мог не успеть её напечатать. В обоих случаях
    // возвращаем фокус в панель — следующий скан должен попасть в поле ввода.
    try {
      if ((printWindow.location.href || 'about:blank') === 'about:blank') printWindow.close();
    } catch (error) {
      printWindow.close();
    }
    window.focus();
    input.focus();
  }
}

/* Замок: без выгруженных наклеек сканировать нечего, поэтому поле прячется
   целиком. Наклейку площадка отдаёт, пока заказ в работе, — не забрали вовремя,
   и её уже не получить.

   Замок общий на все кабинеты под фильтром: сборка объединена, и начинать её,
   скачав наклейки одного магазина, значит наткнуться посреди смены на заказ,
   наклейки которого уже не взять.

   Открытую сборку замок не трогает: товар у сборщика в руках, половина
   отсканирована, и убрать поле сейчас значит бросить его с коробкой. Дадим
   закрыть начатое — запрём после. */
function applyGate(state) {
  const gate = document.getElementById('label-gate');
  const scanPanel = document.getElementById('scan-panel');
  if (!gate || !scanPanel) return;
  const pending = state?.pending || 0;
  const locked = Boolean(state?.locked) && !hasActive;
  gate.hidden = !locked;
  scanPanel.hidden = locked;
  if (!locked) {
    const notice = `Подъехали новые заказы (${pending}). Завершите или отмените текущую сборку — `
                 + 'затем скачайте наклейки.';
    // Опрос идёт каждые 30 секунд: говорим один раз, а не встряхиваем рамку
    // и не затираем итог скана при каждом опросе.
    if (pending && hasActive && banner.querySelector('.text').textContent !== notice) {
      setBanner('warning', notice);
    }
    return;
  }
  document.getElementById('gate-title').textContent = `Скачайте наклейки — ${unitWord(pending)}`;
  /* Разбивка по магазинам: «9 заказов» не отвечает на вопрос «чьих», а у
     каждой площадки наклейка называется по-своему — стикер, этикетка, ярлык. */
  document.getElementById('gate-shops').innerHTML = (state.shops || []).map((shop) => `
    <div class="gate-shop">
      <i class="dot ${escapeHtml(shop.market)}"></i>
      <b>${escapeHtml(shop.title)}</b>
      <span class="muted">${escapeHtml(shop.word)} · ${shop.count}</span>
    </div>`).join('');
  document.getElementById('btn-labels').textContent = `Скачать наклейки (${pending})`;
}

/* «1 заказ», «2 заказа», «5 заказов». Слово общее: в замке заказы всех
   площадок сразу, и назвать их отправлениями или ярлыками уже нельзя. */
function unitWord(count) {
  const tail = count % 100 >= 11 && count % 100 <= 14 ? 0 : count % 10;
  return `${count} ${tail === 1 ? 'заказ' : tail >= 2 && tail <= 4 ? 'заказа' : 'заказов'}`;
}

/* Плитки очереди рисует сервер по фильтру: у каждой в data-key лежит имя
   числа, которое в неё идёт. Так JS не знает, чьи это плитки и сколько их. */
function applyCounters(counters) {
  for (const element of document.querySelectorAll('#queue .value[data-key]')) {
    const value = counters[element.dataset.key];
    if (value !== undefined) element.textContent = value;
  }
}

async function submitScan(code, printWindow = null) {
  if (busy || !code) {
    printWindow?.close();
    return;
  }
  busy = true;
  setBanner('busy', 'Проверяем код…', code);
  try {
    const result = await api(SCAN_URL, { code });
    applyResult(result, code, printWindow);
  } catch (error) {
    printWindow?.close();
    setBanner('error', error.message, code);
    beep('error');
    toast(error.message, 'error');
  } finally {
    busy = false;
    input.value = '';
    input.focus();
  }
}

async function releaseActive() {
  const words = pack?.words || {};
  if (words.confirmRelease && !confirm(words.confirmRelease)) return;
  try {
    const result = await api(RELEASE_URL, {});
    applyResult(result);
    setBanner('idle', words.released || 'Сборка отменена.');
    // Окно закрылось, итог на странице виден только при ошибке — говорим всплывашкой.
    toast(words.released || 'Сборка отменена.', 'ok');
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function forceComplete() {
  if (!confirm(pack?.words?.confirmComplete || 'Завершить заказ без сканирования наклейки?')) return;
  try {
    const result = await api(COMPLETE_URL, { reason: 'ручное завершение' });
    applyResult(result);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function printLabel(id, printWindow = null) {
  if (!pack?.print) return;
  const ok = await printLabelDocument({
    pdfUrl: pack.print.url(id),
    name: `${pack.words.label} ${id}`,
    window: printWindow,
    kind: pack.print.kind,
  });
  if (ok) toast(`${pack.words.label} ${id} отправлен на печать`, 'ok', 3500);
}

input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    /* Скан от сканера — это нажатие клавиши, то есть действие пользователя.
       Пользуемся моментом и резервируем вкладку под ярлык: после запроса к
       серверу Safari открыть её уже не даст. Не пригодится — закроем.
       При открытой сборке вкладку не трогаем вовсе: ярлык тогда не печатается,
       а window.open по имени поднимает поверх панели вкладку с прошлым ярлыком,
       и сборщик принимает это за повторную печать. */
    /* Сборки нет — заказ ещё не открыт, и какой он будет площадки, неизвестно.
       Вкладку резервируем, если печать есть хоть у одной: не пригодится —
       закроем. */
    const reserve = !hasActive && Object.values(PACKS).some((one) => one.print)
      ? reservePrintWindow() : null;
    submitScan(input.value.trim(), reserve);
  }
});

/* Фото товара по клику открывается крупно — рассмотреть мелкую деталь.
   Фото есть не у всех площадок; где их нет, слушатель просто молчит. */
const photoZoom = document.createElement('div');
photoZoom.id = 'photo-zoom';
photoZoom.innerHTML = '<img alt=""><div class="caption"></div>';
document.body.appendChild(photoZoom);

function closePhotoZoom() {
  photoZoom.classList.remove('open');
  input.focus();
}

document.addEventListener('click', (event) => {
  const photo = event.target.closest('.item-photo[data-zoom]');
  if (photo) {
    photoZoom.querySelector('img').src = photo.dataset.zoom;
    photoZoom.querySelector('.caption').textContent = photo.dataset.name || '';
    photoZoom.classList.add('open');
    return;
  }
  if (photoZoom.classList.contains('open')) closePhotoZoom();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && photoZoom.classList.contains('open')) closePhotoZoom();
});

document.getElementById('btn-clear').onclick = () => { input.value = ''; input.focus(); };
document.getElementById('btn-sync').onclick = async (event) => {
  event.target.disabled = true;
  try {
    const result = await api(SYNC_URL, {});
    toast(result.message || 'Обновлено', 'ok');
    const stamp = document.getElementById('sync-time');
    if (stamp) stamp.textContent = new Date().toLocaleTimeString('ru-RU');
    await refreshState();
    /* Список заказов приходит вместе со страницей — после похода на площадку
       он уже старый. Перечитываем страницу: иначе «обновил, а ничего не
       поменялось» — и человек жмёт кнопку второй раз. */
    setTimeout(() => window.location.reload(), 600);
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    event.target.disabled = false;
  }
};

/* «Лист с заказами» — PDF по заказам «К сборке» под фильтром кабинетов: что
   взять с полки. Уходит сразу на печать: на принтер QZ Tray, если он выбран
   для листа на странице «Принтеры», иначе окном печати браузера. Фото сервер
   скачивает сам, поэтому лист собирается не мгновенно — кнопка на это время
   говорит «Готовим лист…». */
document.getElementById('btn-sheet').onclick = async (event) => {
  const button = event.currentTarget;
  const was = button.textContent;
  button.disabled = true;
  button.textContent = 'Готовим лист…';
  try {
    if (await printPdf(SHEET_URL, { name: 'Лист с заказами', kind: 'pack:sheet' })) {
      toast('Лист с заказами отправлен на печать', 'ok');
    }
  } finally {
    button.disabled = false;
    button.textContent = was;
    input.focus();
  }
};

document.getElementById('btn-labels').onclick = async (event) => {
  if (!await downloadArchive(LABELS_URL, event.target, 'naklejki.zip')) return;
  /* Отказ одной площадки не отменяет выгрузку остальных: архив приедет, но её
     заказы останутся в замке. Говорим об этом вслух — иначе «скачал, а сборка
     не открылась» выглядит поломкой панели. */
  const data = await refreshState();
  if (data?.labels?.locked) {
    toast('Часть наклеек площадка не отдала — они остались в списке. Попробуйте ещё раз.',
          'error', 10000);
  } else {
    toast('Наклейки скачаны — можно начинать сборку', 'ok');
  }
};

/* Опрашиваем сервер сами: новые заказы подъезжают фоновой синхронизацией, и
   без этого замок опускался бы только после ручного обновления страницы. */
async function refreshState() {
  try {
    const data = await api(STATE_URL, undefined, 'GET');
    renderActive(data.state);
    applyCounters(data.counters);
    applyGate(data.labels);
    /* «Обновлено» здесь не трогаем: это время похода на площадку, а не опроса
       панели. Опрос идёт каждые 30 секунд и к свежести данных площадки
       отношения не имеет. */
    return data;
  } catch (error) { /* пересинхронизируемся на следующем цикле */ }
  return null;
}

refreshState();
setInterval(() => { if (!busy) refreshState(); }, 30000);

/* Список заказов всех кабинетов: фильтры работают на уже готовых строках.

   Строки приходят с сервера вместе со страницей, поэтому «только к сборке» и
   поиск ничего не запрашивают — просто прячут лишнее. На складе это заметно:
   ответ мгновенный, а сеть на рабочем месте бывает никакая. */
const workBox = document.getElementById('only-work');
const ordersSearch = document.getElementById('orders-search');
const ordersBody = document.getElementById('orders-rows');

function filterOrders() {
  if (!ordersBody) return;
  const onlyWork = !workBox || workBox.checked;
  const needle = (ordersSearch?.value || '').trim().toLowerCase();
  let shown = 0;

  for (const row of ordersBody.rows) {
    const fits = (!onlyWork || row.dataset.work === '1')
      && (!needle || (row.dataset.text || '').includes(needle));
    row.hidden = !fits;
    if (fits) shown += 1;
  }
  const empty = document.getElementById('orders-empty');
  if (empty) empty.hidden = shown > 0;
}

workBox?.addEventListener('change', filterOrders);
ordersSearch?.addEventListener('input', filterOrders);
filterOrders();
