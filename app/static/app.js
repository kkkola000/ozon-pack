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

/* Печать PDF: скрытый iframe печатает напрямую, иначе открываем вкладку. */
function printPdf(url, { fallbackTab = true } = {}) {
  const frame = document.getElementById('print-frame');
  if (!frame) { if (fallbackTab) window.open(url, '_blank'); return; }
  let printed = false;
  frame.onload = () => {
    try {
      frame.contentWindow.focus();
      frame.contentWindow.print();
      printed = true;
    } catch (e) {
      if (fallbackTab) window.open(url, '_blank');
    }
  };
  frame.src = url;
  setTimeout(() => {
    if (!printed && fallbackTab && frame.dataset.opened !== url) {
      /* Некоторые браузеры не печатают PDF из iframe — открываем вкладку. */
    }
  }, 4000);
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
