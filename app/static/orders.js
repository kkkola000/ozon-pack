/* Список заказов FBS: массовая сборка и печать стикеров. */
const checkAll = document.getElementById('check-all');
const picks = () => Array.from(document.querySelectorAll('.pick'));
const selected = () => picks().filter((box) => box.checked).map((box) => box.value);

function refreshButtons() {
  const count = selected().length;
  document.getElementById('selected-count').textContent = `выбрано: ${count}`;
  for (const id of ['btn-ship', 'btn-labels']) {
    const button = document.getElementById(id);
    if (button) button.disabled = count === 0;
  }
}

checkAll?.addEventListener('change', () => {
  picks().forEach((box) => { box.checked = checkAll.checked; });
  refreshButtons();
});
picks().forEach((box) => box.addEventListener('change', refreshButtons));

document.getElementById('btn-ship')?.addEventListener('click', async (event) => {
  const numbers = selected();
  if (!numbers.length) return;
  if (!confirm(`Собрать ${numbers.length} отправл. в Ozon? Они перейдут в статус «Ожидает отгрузки».`)) return;
  event.target.disabled = true;
  try {
    const result = await api('/api/ship', { posting_numbers: numbers });
    toast(result.message, result.status === 'ok' ? 'ok' : 'warning', 8000);
    (result.results || []).filter((item) => item.status !== 'ok')
      .forEach((item) => toast(item.message, 'error', 12000));
    setTimeout(() => window.location.reload(), 1200);
  } catch (error) {
    toast(error.message, 'error');
    event.target.disabled = false;
  }
});

document.getElementById('btn-labels')?.addEventListener('click', async (event) => {
  const numbers = selected();
  if (!numbers.length) return;
  event.target.disabled = true;
  const name = `Стикеры (${numbers.length} шт)`;
  try {
    const response = await fetch('/api/labels.pdf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
      body: JSON.stringify({ posting_numbers: numbers }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `Ошибка ${response.status}`);
    }
    const url = URL.createObjectURL(await response.blob());
    await printPdf(url, { name, asBlob: false });
    toast(`${name} отправлены на печать`, 'ok');
  } catch (error) {
    toast(error.message, 'error', 10000);
  } finally {
    event.target.disabled = false;
  }
});

document.querySelectorAll('[data-reset]').forEach((button) => {
  button.addEventListener('click', async () => {
    const number = button.dataset.reset;
    if (!confirm(`Снять отметку «собрано» с ${number}?`)) return;
    try {
      const result = await api(`/api/postings/${encodeURIComponent(number)}/reset`, {});
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
    const result = await api('/api/sync', {});
    toast(result.message, 'ok');
    setTimeout(() => window.location.reload(), 700);
  } catch (error) {
    toast(error.message, 'error', 10000);
    event.target.disabled = false;
    event.target.textContent = 'Обновить из Ozon';
  }
});

refreshButtons();
