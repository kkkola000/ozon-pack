/* Сборка Яндекс Маркета — то же рабочее место, что у Ozon: один поток сканов,
   состояние приходит с сервера. */
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

function urgencyTag(order) {
  const map = { overdue: 'Просрочено', urgent: 'Срочно', soon: 'Сегодня', ok: '' };
  const text = map[order.urgency];
  if (!text) return '';
  return `<span class="tag ${order.urgency}">${text} · ${escapeHtml(hoursLeftText(order.hours_left))}</span>`;
}

function renderActive(state) {
  hasActive = Boolean(state.active);
  if (!state.active) {
    activePanel.innerHTML = '';
    document.getElementById('idle-panel').style.display = '';
    return;
  }
  document.getElementById('idle-panel').style.display = 'none';
  const order = state.active;
  const percent = state.total ? Math.round((state.done / state.total) * 100) : 0;

  const items = state.items.map((item) => `
    <div class="item-row ${item.ok ? 'ok' : ''}">
      <div class="qty">${item.scanned} / ${item.need}</div>
      <div class="name">
        ${escapeHtml(item.name || 'Без названия')}
        <div class="meta">
          Артикул: ${escapeHtml(item.offer_id || '—')}
          ${item.barcodes?.length
            ? ' · ШК: ' + escapeHtml(item.barcodes.join(', '))
            : ' · <span class="tag overdue">нет штрихкода в каталоге</span>'}
        </div>
      </div>
      ${item.ok ? '<div class="check">✔</div>' : ''}
    </div>`).join('');

  activePanel.innerHTML = `
    <div class="panel">
      <div class="row between">
        <div>
          <div class="muted small">Собирается заказ Маркета</div>
          <div style="font-size:26px;font-weight:800" class="mono">${escapeHtml(order.id)}</div>
          <div class="tags" style="margin-top:6px">
            ${urgencyTag(order)}
            <span class="tag">${escapeHtml(order.status_label || '')}</span>
            ${order.printed_at ? '<span class="tag">Ярлык печатался</span>' : ''}
          </div>
        </div>
        <div class="row">
          <button class="btn" id="btn-print">Печать ярлыка</button>
          <a class="btn" id="btn-open-label" href="/api/yandex/label/${encodeURIComponent(order.id)}.pdf"
             target="_blank" rel="noopener" title="Открыть PDF в новой вкладке">Открыть PDF</a>
          <button class="btn danger" id="btn-release">Отменить сборку</button>
        </div>
      </div>

      <div class="row" style="margin:14px 0 6px">
        <div class="grow progress"><div style="width:${percent}%"></div></div>
        <div style="font-weight:800;font-size:18px">${state.done} / ${state.total}</div>
      </div>

      <div style="margin-top:10px">${items}</div>

      <dl class="kv" style="margin-top:16px">
        <dt>Отгрузка до</dt><dd>${escapeHtml(order.deadline_local || '—')}</dd>
        <dt>Доставка</dt><dd>${escapeHtml(order.delivery_label || '—')} ${order.service_name ? '· ' + escapeHtml(order.service_name) : ''}</dd>
        <dt>Номер у продавца</dt><dd class="mono">${escapeHtml(order.external_id || '—')}</dd>
        <dt>Комментарий</dt><dd>${escapeHtml(order.notes || '—')}</dd>
      </dl>

      <div class="row" style="margin-top:14px">
        <span class="muted small">Не читается ярлык? Введите номер заказа в поле сканирования вручную.</span>
        <span class="grow"></span>
        <button class="btn small" id="btn-force">Завершить без скана ярлыка</button>
      </div>
    </div>`;

  document.getElementById('btn-print').onclick = () => printLabel(order.id, reservePrintWindow());
  document.getElementById('btn-release').onclick = releaseActive;
  document.getElementById('btn-force').onclick = forceComplete;
}

function pushHistory(code, result) {
  history.unshift({ code, status: result.status, message: result.message, at: new Date() });
  if (history.length > 12) history.pop();
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
  setBanner(result.status, result.message);
  beep(result.sound || result.status);
  renderActive(result.state || { active: null });
  if (result.counters) applyCounters(result.counters);
  if (code) pushHistory(code, result);
  if (result.print?.order_id) {
    printLabel(result.print.order_id, printWindow);
  } else if (printWindow) {
    // Вкладка не понадобилась: пустую закрываем, с прошлым ярлыком — оставляем.
    try {
      if ((printWindow.location.href || 'about:blank') === 'about:blank') printWindow.close();
    } catch (error) {
      printWindow.close();
    }
    window.focus();
    input.focus();
  }
}

/* Замок: без выгруженных ярлыков сканировать нечего, поле прячется целиком.
   Открытую сборку не трогаем — дадим закрыть начатое, запрём после. */
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
      setBanner('warning', `Подъехали новые заказы (${pending}). Закройте текущий — дальше понадобится скачать ярлыки.`);
    }
    return;
  }
  document.getElementById('gate-title').textContent = `Скачайте ярлыки — ${ordersWord(pending)}`;
  document.getElementById('btn-labels').textContent = `Скачать ярлыки (${pending})`;
}

function ordersWord(count) {
  const tail = count % 100 >= 11 && count % 100 <= 14 ? 0 : count % 10;
  const word = tail === 1 ? 'заказ' : tail >= 2 && tail <= 4 ? 'заказа' : 'заказов';
  return `${count} ${word}`;
}

function applyCounters(counters) {
  const map = {
    'c-packaging': counters.awaiting_packaging,
    'c-deliver': counters.awaiting_deliver,
    'c-packed': counters.packed_today,
  };
  for (const [id, value] of Object.entries(map)) {
    const element = document.getElementById(id);
    if (element && value !== undefined) element.textContent = value;
  }
}

async function submitScan(code, printWindow = null) {
  if (busy || !code) {
    printWindow?.close();
    return;
  }
  busy = true;
  try {
    const result = await api('/api/yandex/pack/scan', { code });
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
  try {
    const result = await api('/api/yandex/pack/release', {});
    applyResult(result);
    setBanner('idle', 'Сборка отменена. Сканируйте следующий товар.');
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function forceComplete() {
  if (!confirm('Завершить заказ без сканирования ярлыка? Действие попадёт в журнал.')) return;
  try {
    const result = await api('/api/yandex/pack/complete', { reason: 'ручное завершение' });
    applyResult(result);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function printLabel(orderId, printWindow = null) {
  const ok = await printLabelDocument({
    pdfUrl: `/api/yandex/label/${encodeURIComponent(orderId)}.pdf`,
    name: `Ярлык ${orderId}`,
    window: printWindow,
  });
  if (ok) toast(`Ярлык ${orderId} отправлен на печать`, 'ok', 3500);
}

input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    /* Вкладку под ярлык резервируем на нажатии клавиши — после запроса Safari
       её не откроет. При открытой сборке ярлык не печатается, вкладка не нужна. */
    submitScan(input.value.trim(), hasActive ? null : reservePrintWindow());
  }
});

document.getElementById('btn-clear').onclick = () => { input.value = ''; input.focus(); };
document.getElementById('btn-sync').onclick = async (event) => {
  event.target.disabled = true;
  try {
    const result = await api('/api/yandex/sync', {});
    toast(result.message, 'ok');
    refreshState();
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    event.target.disabled = false;
  }
};

document.getElementById('btn-labels').onclick = async (event) => {
  if (await downloadArchive('/api/yandex/labels/archive.zip', event.target, 'yandex-labels.zip')) {
    toast('Ярлыки скачаны — можно начинать сборку', 'ok');
    await refreshState();
  }
};

async function refreshState() {
  try {
    const data = await api('/api/yandex/pack/state', undefined, 'GET');
    renderActive(data.state);
    applyCounters(data.counters);
    applyGate(data.labels);
    document.getElementById('sync-time').textContent = new Date().toLocaleTimeString('ru-RU');
  } catch (error) { /* пересинхронизируемся на следующем цикле */ }
}

refreshState();
setInterval(() => { if (!busy) refreshState(); }, 30000);
