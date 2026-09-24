/* Рабочее место сборщика — одно на все площадки.

   Здесь всё, что на складе одинаково: один поток сканов, замок на выгрузку
   ярлыков, история сканов, счётчики очереди, печать и опрос сервера.

   Чем площадки отличаются — адресами запросов, словами и тем, как выглядит
   карточка открытой сборки. Это площадка объявляет сама в своём файле
   markets/<код>/static/pack.js: он подключается раньше и кладёт настройки в
   window.PACK. Ничего «если это Ozon» здесь быть не должно. */
const PACK = window.PACK;

const input = document.getElementById('scan');
const banner = document.getElementById('banner');
const activePanel = document.getElementById('active-panel');
const historyBox = document.getElementById('history');

let busy = false;
let hasActive = false;   // открыта ли сборка — при открытой ярлык не печатается
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

/* Карточку сборки рисует площадка, кнопки на ней — общие: печать, отмена,
   завершение без скана. Каких кнопок у площадки нет, те она просто не рисует. */
function renderActive(state) {
  hasActive = Boolean(state?.active);
  const idle = document.getElementById('idle-panel');
  if (!hasActive) {
    activePanel.innerHTML = '';
    idle.style.display = '';
    return;
  }
  idle.style.display = 'none';
  activePanel.innerHTML = PACK.renderActive(state);

  const active = state.active;
  const print = document.getElementById('btn-print');
  if (print) print.onclick = () => printLabel(PACK.activeId(active), reservePrintWindow());
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
  const toPrint = PACK.print && result.print?.[PACK.print.key];
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

/* Замок: без выгруженных ярлыков сканировать нечего, поэтому поле прячется
   целиком. Ярлык площадка отдаёт, пока заказ в работе, — не забрали вовремя,
   и его уже не получить.

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
    if (pending && hasActive) setBanner('warning', PACK.words.arrived(pending));
    return;
  }
  document.getElementById('gate-title').textContent = `${PACK.words.gate} — ${unitWord(pending)}`;
  document.getElementById('btn-labels').textContent = `${PACK.words.download} (${pending})`;
}

/* «1 отправление», «2 заказа», «5 заказов» — формы слова даёт площадка. */
function unitWord(count) {
  const tail = count % 100 >= 11 && count % 100 <= 14 ? 0 : count % 10;
  const [one, few, many] = PACK.words.unit;
  return `${count} ${tail === 1 ? one : tail >= 2 && tail <= 4 ? few : many}`;
}

function applyCounters(counters) {
  for (const [id, key] of Object.entries(PACK.counters)) {
    const element = document.getElementById(id);
    if (element && counters[key] !== undefined) element.textContent = counters[key];
  }
}

async function submitScan(code, printWindow = null) {
  if (busy || !code) {
    printWindow?.close();
    return;
  }
  busy = true;
  try {
    const result = await api(PACK.api.scan, { code });
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
  if (PACK.words.confirmRelease && !confirm(PACK.words.confirmRelease)) return;
  try {
    const result = await api(PACK.api.release, {});
    applyResult(result);
    setBanner('idle', PACK.words.released);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function forceComplete() {
  if (!PACK.api.complete) return;
  if (!confirm(PACK.words.confirmComplete)) return;
  try {
    const result = await api(PACK.api.complete, { reason: 'ручное завершение' });
    applyResult(result);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function printLabel(id, printWindow = null) {
  if (!PACK.print) return;
  const ok = await printLabelDocument({
    pdfUrl: PACK.print.url(id),
    name: `${PACK.words.label} ${id}`,
    window: printWindow,
  });
  if (ok) toast(`${PACK.words.label} ${id} отправлен на печать`, 'ok', 3500);
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
    const reserve = PACK.print && !hasActive ? reservePrintWindow() : null;
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
    const result = await api(PACK.api.sync, {});
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
  if (await downloadArchive(PACK.api.labels, event.target, PACK.words.archive)) {
    toast(PACK.words.downloaded, 'ok');
    await refreshState();
  }
};

/* Опрашиваем сервер сами: новые заказы подъезжают фоновой синхронизацией, и
   без этого замок опускался бы только после ручного обновления страницы. */
async function refreshState() {
  try {
    const data = await api(PACK.api.state, undefined, 'GET');
    renderActive(data.state);
    applyCounters(data.counters);
    applyGate(data.labels);
    /* «Обновлено» здесь не трогаем: это время похода на площадку, а не опроса
       панели. Опрос идёт каждые 30 секунд и к свежести данных площадки
       отношения не имеет. */
  } catch (error) { /* пересинхронизируемся на следующем цикле */ }
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
