/* Заказы Avito: подтверждение, отправка и печать оригинальных этикеток. */
const checkAll = document.getElementById('check-all');
const picks = () => Array.from(document.querySelectorAll('.pick'));
const selected = () => picks().filter((box) => box.checked).map((box) => box.value);

function refreshButtons() {
  const count = selected().length;
  document.getElementById('selected-count').textContent = `выбрано: ${count}`;
  for (const id of ['btn-confirm', 'btn-ship', 'btn-labels']) {
    const button = document.getElementById(id);
    if (button) button.disabled = count === 0;
  }
}

checkAll?.addEventListener('change', () => {
  picks().forEach((box) => { box.checked = checkAll.checked; });
  refreshButtons();
});
picks().forEach((box) => box.addEventListener('change', refreshButtons));

async function transition(url, ids, question, button) {
  if (!ids.length) return;
  if (question && !confirm(question)) return;
  const previous = button.textContent;
  button.disabled = true;
  button.textContent = 'Отправляем…';
  try {
    const result = await api(url, { order_ids: ids });
    toast(result.message, result.status === 'ok' ? 'ok' : 'warning', 8000);
    (result.results || []).filter((item) => item.status !== 'ok')
      .forEach((item) => toast(item.message, 'error', 12000));
    setTimeout(() => window.location.reload(), 1200);
  } catch (error) {
    toast(error.message, 'error', 10000);
    button.disabled = false;
    button.textContent = previous;
  }
}

document.getElementById('btn-confirm')?.addEventListener('click', (event) => {
  const ids = selected();
  transition('/api/avito/confirm', ids, `Подтвердить ${ids.length} заказ(ов) в Avito?`, event.target);
});

document.getElementById('btn-ship')?.addEventListener('click', (event) => {
  const ids = selected();
  transition('/api/avito/ship', ids, `Отметить ${ids.length} заказ(ов) как отправленные?`, event.target);
});

document.querySelectorAll('[data-confirm]').forEach((button) => {
  button.addEventListener('click', () => {
    transition('/api/avito/confirm', [button.dataset.confirm], '', button);
  });
});

document.querySelectorAll('[data-ship]').forEach((button) => {
  button.addEventListener('click', () => {
    transition('/api/avito/ship', [button.dataset.ship], '', button);
  });
});

/* Печать: файл приходит от Avito как есть, панель его не перерисовывает. */
async function printLabels(ids, button) {
  if (!ids.length) return;
  const name = ids.length > 1 ? `Этикетки (${ids.length} шт)` : 'Этикетка Avito';
  button.disabled = true;
  try {
    const response = await fetch('/api/avito/labels.pdf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
      body: JSON.stringify({ order_ids: ids }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `Ошибка ${response.status}`);
    }
    const url = URL.createObjectURL(await response.blob());
    await printPdf(url, { name, asBlob: false });
    toast(`${name}: отправлено на печать`, 'ok');
  } catch (error) {
    toast(error.message, 'error', 10000);
  } finally {
    button.disabled = false;
  }
}

document.getElementById('btn-labels')?.addEventListener('click', (event) => {
  printLabels(selected(), event.target);
});

document.querySelectorAll('[data-label]').forEach((button) => {
  button.addEventListener('click', () => printLabels([button.dataset.label], button));
});

document.getElementById('btn-avito-sync')?.addEventListener('click', async (event) => {
  event.target.disabled = true;
  event.target.textContent = 'Обновляем…';
  try {
    const result = await api('/api/avito/sync', {});
    toast(result.message, 'ok');
    setTimeout(() => window.location.reload(), 700);
  } catch (error) {
    toast(error.message, 'error', 10000);
    event.target.disabled = false;
    event.target.textContent = 'Обновить из Avito';
  }
});

refreshButtons();
