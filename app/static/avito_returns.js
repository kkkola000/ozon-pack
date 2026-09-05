/* Возвраты Avito: отметка «забрано» и обновление списка. */
const checkAll = document.getElementById('check-all');
const picks = () => Array.from(document.querySelectorAll('.pick'));
const selected = () => picks().filter((box) => box.checked).map((box) => box.value);

function refreshButtons() {
  const count = selected().length;
  document.getElementById('selected-count').textContent = `выбрано: ${count}`;
  const taken = document.getElementById('btn-taken');
  if (taken) taken.disabled = count === 0;
}

checkAll?.addEventListener('change', () => {
  picks().forEach((box) => { box.checked = checkAll.checked; });
  refreshButtons();
});
picks().forEach((box) => box.addEventListener('change', refreshButtons));

document.getElementById('btn-taken')?.addEventListener('click', async (event) => {
  const ids = selected();
  if (!ids.length) return;
  event.target.disabled = true;
  try {
    const result = await api('/api/avito/returns/taken', { ids, taken: true });
    toast(result.message, 'ok');
    setTimeout(() => window.location.reload(), 800);
  } catch (error) {
    toast(error.message, 'error');
    event.target.disabled = false;
  }
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
