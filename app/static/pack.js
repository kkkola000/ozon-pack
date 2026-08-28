/* Рабочее место сборщика: один поток сканов, состояние приходит с сервера. */
const input = document.getElementById('scan');
const banner = document.getElementById('banner');
const candidatesBox = document.getElementById('candidates');
const activePanel = document.getElementById('active-panel');
const historyBox = document.getElementById('history');

let busy = false;
let lastChoice = null;   // {sku, candidates} — когда нужно выбрать отправление
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

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

function urgencyTag(posting) {
  const map = { overdue: 'Просрочено', urgent: 'Срочно', soon: 'Сегодня', ok: '' };
  const text = map[posting.urgency];
  if (!text) return '';
  return `<span class="tag ${posting.urgency}">${text} · ${escapeHtml(hoursLeftText(posting.hours_left))}</span>`;
}

function renderActive(state) {
  if (!state.active) {
    activePanel.innerHTML = '';
    document.getElementById('idle-panel').style.display = '';
    return;
  }
  document.getElementById('idle-panel').style.display = 'none';
  const posting = state.active;
  const percent = state.total ? Math.round((state.done / state.total) * 100) : 0;
  const items = state.items.map((item) => `
    <div class="item-row ${item.ok ? 'ok' : ''}">
      <div class="qty">${item.scanned} / ${item.need}</div>
      <div class="name">
        ${escapeHtml(item.name || 'Без названия')}
        <div class="meta">
          Артикул: ${escapeHtml(item.offer_id || '—')} · SKU: ${escapeHtml(item.sku)}
          ${item.barcodes?.length ? ' · ШК: ' + escapeHtml(item.barcodes.join(', ')) : ''}
          ${item.mandatory_mark ? ' · <span class="tag mark">Честный знак</span>' : ''}
        </div>
      </div>
      ${item.ok ? '<div style="font-size:26px;color:#7ee2a8">✔</div>' : ''}
    </div>`).join('');

  activePanel.innerHTML = `
    <div class="panel">
      <div class="row between">
        <div>
          <div class="muted small">Собирается отправление</div>
          <div style="font-size:26px;font-weight:800" class="mono">${escapeHtml(posting.posting_number)}</div>
          <div class="tags" style="margin-top:6px">
            ${urgencyTag(posting)}
            ${posting.is_express ? '<span class="tag express">Express</span>' : ''}
            ${posting.requires_mark ? '<span class="tag mark">Требуется маркировка</span>' : ''}
            ${posting.is_multibox ? `<span class="tag">Многоместное: ${posting.multi_box_qty}</span>` : ''}
            ${posting.printed_at ? '<span class="tag">Стикер печатался</span>' : ''}
          </div>
        </div>
        <div class="row">
          <button class="btn" id="btn-print">Печать стикера</button>
          <button class="btn danger" id="btn-release">Отменить сборку</button>
        </div>
      </div>

      <div class="row" style="margin:14px 0 6px">
        <div class="grow progress"><div style="width:${percent}%"></div></div>
        <div style="font-weight:800;font-size:18px">${state.done} / ${state.total}</div>
      </div>

      <div style="margin-top:10px">${items}</div>

      <dl class="kv" style="margin-top:16px">
        <dt>Отгрузка до</dt><dd>${escapeHtml(posting.shipment_date_local || posting.shipment_date || '—')}</dd>
        <dt>Куда</dt><dd>${escapeHtml([posting.region, posting.city].filter(Boolean).join(', ') || '—')}</dd>
        <dt>Способ</dt><dd>${escapeHtml(posting.delivery_method || '—')} ${posting.tpl_provider ? '· ' + escapeHtml(posting.tpl_provider) : ''}</dd>
        <dt>Склад</dt><dd>${escapeHtml(posting.warehouse_name || '—')}</dd>
        <dt>Оплата</dt><dd>${escapeHtml(posting.payment_type || '—')}</dd>
        <dt>Заказ</dt><dd class="mono">${escapeHtml(posting.order_number || '—')}</dd>
      </dl>

      <div class="row" style="margin-top:14px">
        <span class="muted small">Не читается стикер? Введите номер отправления в поле сканирования вручную.</span>
        <span class="grow"></span>
        <button class="btn small" id="btn-force">Завершить без скана стикера</button>
      </div>
    </div>`;

  document.getElementById('btn-print').onclick = () => printLabel(posting.posting_number);
  document.getElementById('btn-release').onclick = releaseActive;
  document.getElementById('btn-force').onclick = forceComplete;
}

function renderCandidates(result) {
  if (result.action !== 'need_choice') {
    candidatesBox.innerHTML = '';
    lastChoice = null;
    return;
  }
  lastChoice = { sku: result.sku, candidates: result.candidates };
  candidatesBox.innerHTML = result.candidates.map((posting, index) => `
    <div class="candidate ${index === 0 ? 'first' : ''}" data-number="${escapeHtml(posting.posting_number)}">
      <div>
        <div class="num mono">${index + 1}. ${escapeHtml(posting.posting_number)}</div>
        <div class="muted small">
          ${posting.positions_count} поз. / ${posting.items_count} шт ·
          ${escapeHtml(posting.city || '')} ·
          ${escapeHtml(posting.delivery_method || '')}
        </div>
      </div>
      <div class="tags">${urgencyTag(posting)}</div>
    </div>`).join('');
  candidatesBox.querySelectorAll('.candidate').forEach((element) => {
    element.onclick = () => selectPosting(element.dataset.number, result.sku);
  });
}

function pushHistory(code, result) {
  history.unshift({ code, status: result.status, message: result.message, at: new Date() });
  if (history.length > 12) history.pop();
  historyBox.innerHTML = history.map((entry) => `
    <div style="padding:4px 0;border-bottom:1px solid var(--line)">
      <span class="mono">${entry.at.toLocaleTimeString('ru-RU')}</span> ·
      <span class="mono">${escapeHtml(entry.code)}</span> ·
      <span style="color:${entry.status === 'error' ? 'var(--err)' : entry.status === 'warning' ? 'var(--warn)' : 'var(--ok)'}">
        ${escapeHtml(entry.message)}
      </span>
    </div>`).join('');
}

function applyResult(result, code) {
  setBanner(result.status, result.message);
  beep(result.sound || result.status);
  renderActive(result.state || { active: null });
  renderCandidates(result);
  if (result.counters) applyCounters(result.counters);
  if (code) pushHistory(code, result);
  if (result.print?.posting_number) printLabel(result.print.posting_number);
}

function applyCounters(counters) {
  const map = {
    'c-packaging': counters.awaiting_packaging,
    'c-deliver': counters.awaiting_deliver,
    'c-packed': counters.packed_today,
    'c-returns': counters.returns_ready,
  };
  for (const [id, value] of Object.entries(map)) {
    const element = document.getElementById(id);
    if (element && value !== undefined) element.textContent = value;
  }
}

async function submitScan(code) {
  if (busy || !code) return;
  busy = true;
  try {
    const result = await api('/api/scan', { code });
    applyResult(result, code);
  } catch (error) {
    setBanner('error', error.message);
    beep('error');
    toast(error.message, 'error');
  } finally {
    busy = false;
    input.value = '';
    input.focus();
  }
}

async function selectPosting(postingNumber, sku) {
  try {
    const result = await api('/api/select', { posting_number: postingNumber, sku });
    applyResult(result);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function releaseActive() {
  try {
    const result = await api('/api/release', {});
    applyResult(result);
    setBanner('idle', 'Сборка отменена. Сканируйте следующий товар.');
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function forceComplete() {
  if (!confirm('Завершить отправление без сканирования стикера? Действие попадёт в журнал.')) return;
  try {
    const result = await api('/api/complete', { reason: 'ручное завершение' });
    applyResult(result);
  } catch (error) {
    toast(error.message, 'error');
  }
}

function printLabel(postingNumber) {
  printPdf(`/api/label/${encodeURIComponent(postingNumber)}.pdf`);
  toast(`Стикер ${postingNumber} отправлен на печать`, 'ok', 3500);
}

input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    submitScan(input.value.trim());
  }
});

/* Выбор отправления цифрой 1..9, когда система предложила несколько. */
document.addEventListener('keydown', (event) => {
  if (!lastChoice || !/^[1-9]$/.test(event.key) || input.value) return;
  const posting = lastChoice.candidates[Number(event.key) - 1];
  if (posting) {
    event.preventDefault();
    selectPosting(posting.posting_number, lastChoice.sku);
  }
});

document.getElementById('btn-clear').onclick = () => { input.value = ''; input.focus(); };
document.getElementById('btn-sync').onclick = async (event) => {
  event.target.disabled = true;
  try {
    const result = await api('/api/sync', {});
    toast(result.message, 'ok');
    refreshState();
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    event.target.disabled = false;
  }
};

async function refreshState() {
  try {
    const data = await api('/api/state', undefined, 'GET');
    renderActive(data.state);
    applyCounters(data.counters);
    document.getElementById('sync-time').textContent = new Date().toLocaleTimeString('ru-RU');
  } catch (error) { /* пересинхронизируемся на следующем цикле */ }
}

refreshState();
setInterval(() => { if (!busy && !lastChoice) refreshState(); }, 30000);
