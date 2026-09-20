/* Заказы Яндекс Маркета: печать ярлыков и снятие отметки «собрано». */
const checkAll = document.getElementById('check-all');
const picks = () => Array.from(document.querySelectorAll('.pick'));
const selected = () => picks().filter((box) => box.checked).map((box) => box.value);

function refreshButtons() {
  const count = selected().length;
  document.getElementById('selected-count').textContent = `выбрано: ${count}`;
  const button = document.getElementById('btn-labels');
  if (button) button.disabled = count === 0;
}

checkAll?.addEventListener('change', () => {
  picks().forEach((box) => { box.checked = checkAll.checked; });
  refreshButtons();
});
picks().forEach((box) => box.addEventListener('change', refreshButtons));

/* Печать: файл приходит от Маркета как есть, панель его не перерисовывает. */
document.getElementById('btn-labels')?.addEventListener('click', async (event) => {
  const ids = selected();
  if (!ids.length) return;
  event.target.disabled = true;
  const name = ids.length > 1 ? `Ярлыки (${ids.length} шт)` : 'Ярлык Маркета';
  try {
    const response = await fetch('/api/yandex/labels.pdf', {
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
    event.target.disabled = false;
  }
});

/* Снять отметку «собрано». Заказ вернётся в работу, отметка о сборщике пропадёт. */
document.querySelectorAll('[data-reset]').forEach((button) => {
  button.addEventListener('click', async () => {
    const id = button.dataset.reset;
    if (!confirm(`Снять отметку «собрано» с заказа ${id}?`)) return;
    try {
      const result = await api(`/api/yandex/orders/${encodeURIComponent(id)}/reset`, {});
      toast(result.message, 'ok');
      setTimeout(() => window.location.reload(), 800);
    } catch (error) {
      toast(error.message, 'error');
    }
  });
});

document.getElementById('btn-sync')?.addEventListener('click', async (event) => {
  event.target.disabled = true;
  event.target.textContent = 'Обновляем…';
  try {
    const result = await api('/api/yandex/sync', {});
    toast(result.message, 'ok');
    setTimeout(() => window.location.reload(), 700);
  } catch (error) {
    toast(error.message, 'error', 10000);
    event.target.disabled = false;
    event.target.textContent = 'Обновить из Маркета';
  }
});

refreshButtons();
