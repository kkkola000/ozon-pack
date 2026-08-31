/* Общие утилиты: запросы к API, всплывающие сообщения, звук и печать. */
const CSRF = document.querySelector('meta[name=csrf-token]')?.content || '';

async function api(url, body, method = 'POST') {
  const options = {
    method,
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
  };
  if (body !== undefined && method !== 'GET') options.body = JSON.stringify(body);
  const response = await fetch(url, options);
  let data = {};
  try { data = await response.json(); } catch (e) { /* пустой ответ */ }
  if (!response.ok) {
    throw new Error(data.detail || data.message || `Ошибка ${response.status}`);
  }
  return data;
}

function toast(message, kind = 'ok', timeout = 5000) {
  const box = document.getElementById('toasts');
  if (!box) return;
  const element = document.createElement('div');
  element.className = `toast ${kind}`;
  element.textContent = message;
  box.appendChild(element);
  setTimeout(() => element.remove(), timeout);
}

/* Звуковые сигналы через WebAudio — не нужны файлы и не блокируются как автоплей. */
let audioContext = null;
function beep(kind = 'ok') {
  try {
    audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
    if (audioContext.state === 'suspended') audioContext.resume();
    const sequences = {
      ok: [[880, 0.08]],
      done: [[880, 0.09], [1320, 0.16]],
      warning: [[600, 0.12], [600, 0.12]],
      error: [[220, 0.25], [180, 0.3]],
    };
    let start = audioContext.currentTime;
    for (const [frequency, duration] of (sequences[kind] || sequences.ok)) {
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      oscillator.type = kind === 'error' ? 'square' : 'sine';
      oscillator.frequency.value = frequency;
      gain.gain.setValueAtTime(0.13, start);
      gain.gain.exponentialRampToValueAtTime(0.001, start + duration);
      oscillator.connect(gain).connect(audioContext.destination);
      oscillator.start(start);
      oscillator.stop(start + duration);
      start += duration + 0.03;
    }
  } catch (e) { /* звук не критичен */ }
}

/* Печать PDF.

   Тонкое место: iframe со style="display:none" браузер не отрисовывает, и
   команда печати уходит в пустой документ — на бумагу выходит чистый лист.
   Поэтому фрейм остаётся в разметке (просто прозрачный), а сам PDF сначала
   скачивается через fetch: так видно ошибку от Ozon вместо печати страницы
   с текстом ошибки, а blob-адрес печатается стабильнее прямой ссылки. */
let printBlobUrl = null;
let printFallbackTimer = null;

function releasePrintBlob() {
  if (printBlobUrl) {
    URL.revokeObjectURL(printBlobUrl);
    printBlobUrl = null;
  }
}

function offerManualPrint(url, name) {
  const box = document.getElementById('toasts');
  if (!box) { window.open(url, '_blank'); return; }
  const element = document.createElement('div');
  element.className = 'toast warning';
  element.innerHTML = `<div>${name}: окно печати не открылось.</div>`;
  const link = document.createElement('a');
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener';
  link.textContent = 'Открыть и напечатать вручную';
  link.style.cssText = 'display:inline-block;margin-top:6px;font-weight:700';
  /* Переход по ссылке — жест пользователя, всплывающее окно не блокируется. */
  link.onclick = () => setTimeout(() => element.remove(), 500);
  element.appendChild(link);
  box.appendChild(element);
  setTimeout(() => element.remove(), 30000);
}

/* Safari печатает PDF во фрейме пустым листом — для него готовим HTML с
   картинкой стикера. Определяем именно Safari, а не WebKit вообще: Chrome и
   Edge на macOS тоже содержат Safari в строке браузера. */
const IS_SAFARI = /^((?!chrome|chromium|crios|edg|android|fxios).)*safari/i.test(navigator.userAgent);

/* Печать стикера: на принтер уходит файл, который отдал Ozon, без изменений. */
async function printLabelDocument({ pdfUrl, name = 'Стикер', window: preopened = null }) {
  return printPdf(pdfUrl, { name, window: preopened });
}

/* Вкладку под стикер надо открыть, пока Safari видит нажатие клавиши.
   Имя окна делает вкладку одной на все стикеры: иначе за смену их накопятся
   десятки. */
const LABEL_WINDOW_NAME = 'ozp-label';

function reservePrintWindow() {
  if (!IS_SAFARI) return null;
  try { return window.open('', LABEL_WINDOW_NAME); } catch (error) { return null; }
}

async function printPdf(url, { name = 'Стикер', asBlob = true, window: preopened = null } = {}) {
  /* Safari не печатает PDF из скрытого фрейма — выходит пустой лист. Открываем
     файл в отдельной вкладке: печатается именно то, что отдал Ozon. Вкладку
     заготавливает вызывающий код в момент нажатия клавиши, иначе Safari сочтёт
     её всплывающим окном и заблокирует. */
  if (IS_SAFARI) {
    const target = preopened || window.open(url, LABEL_WINDOW_NAME);
    if (!target) {
      offerManualPrint(url, name);
      return false;
    }
    if (preopened) target.location = url;
    setTimeout(() => {
      try { target.focus(); target.print(); } catch (error) { /* оператор нажмёт Cmd+P */ }
    }, 1200);
    return true;
  }

  let printUrl = url;
  if (asBlob && !url.startsWith('blob:')) {
    try {
      const response = await fetch(url, { headers: { 'X-Requested-With': 'fetch' } });
      if (!response.ok) {
        let detail = `Ошибка ${response.status}`;
        try { detail = (await response.json()).detail || detail; } catch (e) { /* не JSON */ }
        toast(`${name}: ${detail}`, 'error', 15000);
        return false;
      }
      releasePrintBlob();
      printBlobUrl = URL.createObjectURL(await response.blob());
      printUrl = printBlobUrl;
    } catch (error) {
      toast(`${name}: не удалось получить файл — ${error.message}`, 'error', 12000);
      return false;
    }
  }

  document.getElementById('print-frame')?.remove();
  const frame = document.createElement('iframe');
  frame.id = 'print-frame';
  frame.title = 'Печать';
  frame.setAttribute('aria-hidden', 'true');
  frame.style.cssText =
    'position:fixed;inset:0;width:100vw;height:100vh;opacity:0;pointer-events:none;border:0;z-index:-1';

  let printed = false;
  frame.onload = () => {
    /* Плагину PDF нужно время на отрисовку, иначе печатается пустая страница. */
    setTimeout(() => {
      try {
        frame.contentWindow.focus();
        frame.contentWindow.print();
        printed = true;
      } catch (error) {
        offerManualPrint(printUrl, name);
      }
    }, 500);
  };
  frame.onerror = () => offerManualPrint(printUrl, name);
  document.body.appendChild(frame);
  frame.src = printUrl;

  clearTimeout(printFallbackTimer);
  printFallbackTimer = setTimeout(() => {
    if (!printed) offerManualPrint(printUrl, name);
  }, 8000);
  return true;
}

function plural(n, one, few, many) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
  return many;
}

function hoursLeftText(hours) {
  if (hours === null || hours === undefined) return '';
  if (hours < 0) return `просрочено на ${Math.round(-hours)} ч`;
  if (hours < 1) return `осталось ${Math.round(hours * 60)} мин`;
  return `осталось ${Math.round(hours)} ${plural(Math.round(hours), 'час', 'часа', 'часов')}`;
}
