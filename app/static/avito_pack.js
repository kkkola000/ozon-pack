/* Сборка Avito: стикер открывает заказ, дальше сканируются штрихкоды товаров. */
const input = document.getElementById('scan');
const banner = document.getElementById('banner');
const activePanel = document.getElementById('active-panel');
const historyBox = document.getElementById('history');

let busy = false;
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

function renderActive(state) {
  const idle = document.getElementById('idle-panel');
  if (!state.active) {
    activePanel.innerHTML = '';
    idle.style.display = '';
    return;
  }
  idle.style.display = 'none';
  const order = state.active;
  const percent = state.total ? Math.round((state.done / state.total) * 100) : 0;
  const rows = state.items.map((item) => `
    <div class="item-row ${item.scanned ? 'ok' : ''}">
      <div class="qty">${item.scanned ? '✓' : '—'}</div>
      <div class="name">
        ${escapeHtml(item.title || 'Без названия')}
        <div class="meta">
          ${item.seller_id ? `артикул ${escapeHtml(item.seller_id)} · ` : ''}единица ${item.unit_no}
          ${item.barcode ? ` · штрихкод <b>${escapeHtml(item.barcode)}</b>` : ''}
        </div>
      </div>
    </div>`).join('');

  activePanel.innerHTML = `
    <div class="panel active">
      <div class="row between">
        <div>
          <h2 style="margin:0">Заказ ${escapeHtml(order.marketplace_id || order.id)}</h2>
          <div class="muted small">${escapeHtml(order.service_name || order.service_label || '')}</div>
        </div>
        <div class="row">
          <a class="btn" href="/api/avito/label/${encodeURIComponent(order.id)}.pdf" target="_blank">Стикер</a>
          <button class="btn danger" id="btn-release">Отменить сборку</button>
        </div>
      </div>
      <div class="progress"><div class="bar" style="width:${percent}%"></div></div>
      <div class="muted small">Отсканировано ${state.done} из ${state.total}</div>
      <div class="items">${rows}</div>
    </div>`;

  document.getElementById('btn-release').onclick = async () => {
    if (!confirm('Отменить сборку этого заказа?')) return;
    await send('/api/avito/pack/release', {});
  };
}

function renderCounters(counters) {
  if (!counters) return;
  const set = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value; };
  set('c-to-pack', counters.to_pack);
  set('c-packed', counters.packed_today);
  set('c-confirm', counters.confirm);
}

function pushHistory(code, result) {
  history.unshift({ code, status: result.status, message: result.message, at: new Date() });
  history.splice(12);
  historyBox.innerHTML = history.map((row) => `
    <div class="hist ${row.status}">
      <span class="mono">${escapeHtml(row.code)}</span>
      <span>${escapeHtml(row.message)}</span>
      <span class="muted">${row.at.toLocaleTimeString('ru-RU')}</span>
    </div>`).join('');
}

async function send(url, payload) {
  const result = await api(url, payload);
  setBanner(result.status, result.message);
  renderActive(result.state);
  renderCounters(result.counters);
  beep(result.sound || result.status);
  return result;
}

async function submitScan(code) {
  if (!code || busy) return;
  busy = true;
  try {
    const result = await send('/api/avito/pack/scan', { code });
    pushHistory(code, result);
  } catch (error) {
    setBanner('error', error.message);
    pushHistory(code, { status: 'error', message: error.message });
  } finally {
    busy = false;
    input.value = '';
    input.focus();
  }
}

input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    submitScan(input.value.trim());
  }
});
document.getElementById('btn-clear').onclick = () => { input.value = ''; input.focus(); };
document.getElementById('btn-sync')?.addEventListener('click', async (event) => {
  event.target.disabled = true;
  try {
    const result = await api('/api/avito/sync', {});
    toast(result.message || 'Обновлено', 'ok');
    const fresh = await fetch('/api/avito/pack/state').then((r) => r.json());
    renderCounters(fresh.counters);
  } catch (error) {
    toast(error.message, 'error');
  }
  event.target.disabled = false;
});

fetch('/api/avito/pack/state')
  .then((r) => r.json())
  .then((data) => { renderActive(data.state); renderCounters(data.counters); })
  .catch(() => {});
