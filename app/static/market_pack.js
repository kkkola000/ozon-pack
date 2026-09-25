/* Рабочее место сборщика — одно на все площадки.

   Здесь всё, что на складе одинаково: один поток сканов, замок на выгрузку
   наклеек, история сканов, счётчики очереди, печать и опрос сервера. Адреса
   запросов тоже общие: кабинет в шапке больше ничего не решает, а какому
   кабинету принадлежит отсканированный код, разбирается сервер.

   Площадки отличаются словами и тем, как выглядит карточка открытой сборки.
   Каждая объявляет своё в markets/<код>/static/pack.js и кладёт в
   window.PACKS[код]; подключены сразу все, потому что открытый заказ может
   оказаться из любого кабинета. Ничего «если это Ozon» здесь быть не должно. */
const PACKS = window.PACKS || {};

const input = document.getElementById('scan');
const banner = document.getElementById('banner');
const activePanel = document.getElementById('active-panel');
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

const historyBox = document.getElementById('history');

let busy = false;
let hasActive = false;   // открыта ли сборка — при открытой наклейка не печатается
let pack = null;         // площадка открытого заказа: её слова и её карточка
const history = [];

function keepFocus() {
  if (document.activeElement !== input && !document.activeElement?.closest('input, select, button, a')) {
    input.focus();
  }
}
setInterval(keepFocus, 800);
document.addEventListener('click', (event) => {
  if (!event.target.closest('button, a, input, select, label')) input.focus();
});

function setBanner(kind, message) {
  banner.className = `banner ${kind} flash`;
  banner.textContent = message;
  setTimeout(() => banner.classList.remove('flash'), 500);
}

/* Карточку сборки рисует площадка открытого заказа, кнопки на ней — общие:
   печать, отмена, завершение без скана. Каких кнопок у площадки нет, те она
   просто не рисует.

   Какая это площадка, говорит сам ответ сервера (state.market): заказ мог
   открыться в любом кабинете, и гадать по шапке нельзя. */
function renderActive(state) {
  pack = PACKS[state?.market] || null;
  hasActive = Boolean(state?.active) && pack !== null;
  const idle = document.getElementById('idle-panel');
  if (!hasActive) {
    activePanel.innerHTML = '';
    idle.style.display = '';
    return;
  }
  idle.style.display = 'none';
  activePanel.innerHTML = pack.renderActive(state);

  const active = state.active;
  const print = document.getElementById('btn-print');
  if (print) print.onclick = () => printLabel(pack.activeId(active), reservePrintWindow());
  const release = document.getElementById('btn-release');
  if (release) release.onclick = releaseActive;
  const force = document.getElementById('btn-force');
  if (force) force.onclick = forceComplete;
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
  setBanner(result.status, result.message);
  beep(result.sound || result.status);
  renderActive(result.state || { active: null });
  if (result.counters) applyCounters(result.counters);
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
    if (pending && hasActive) {
      setBanner('warning', `Подъехали новые заказы (${pending}). Закройте текущий — `
                         + 'дальше понадобится скачать наклейки.');
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
  for (const element of document.querySelectorAll('#idle-panel .value[data-key]')) {
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
  try {
    const result = await api(SCAN_URL, { code });
    applyResult(result, code, printWindow);
  } catch (error) {
    printWindow?.close();
    setBanner('error', error.message);
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

   Строки приходят с сервера вместе со страницей, поэтому «только в работе» и
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
