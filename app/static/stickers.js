/* Просмотр и печать используют общий обработчик data-print-pdf в qz.js. */
const stickerChoices = () => [...document.querySelectorAll('.sticker-select')];
const stickerDownload = document.getElementById('stickers-download');
const stickerAll = document.getElementById('stickers-all');
function updateStickerSelection() {
  const count = stickerChoices().filter((box) => box.checked).length;
  stickerDownload.disabled = count === 0;
  stickerAll.checked = count > 0 && count === stickerChoices().length;
  stickerAll.indeterminate = count > 0 && count < stickerChoices().length;
}
stickerAll.addEventListener('change', () => {
  stickerChoices().forEach((box) => { box.checked = stickerAll.checked; });
  updateStickerSelection();
});
document.getElementById('sticker-rows').addEventListener('change', (event) => {
  if (event.target.matches('.sticker-select')) updateStickerSelection();
});
stickerDownload.addEventListener('click', async () => {
  stickerDownload.disabled = true;
  try {
    if (stickerChoices().filter((box) => box.checked).length > 100) throw new Error('Выберите не больше 100 файлов за одну выгрузку');
    const response = await fetch('/api/stickers/archive.zip', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
      body: JSON.stringify(stickerChoices().filter((box) => box.checked).map((box) => Number(box.value))),
    });
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Не удалось скачать архив');
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url; link.download = 'stickers.zip';
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (error) { toast(error.message, 'error'); }
  finally { updateStickerSelection(); }
});
document.getElementById('sticker-rows').addEventListener('click', async (event) => {
  const button = event.target.closest('.sticker-retry');
  if (!button || button.disabled) return;
  button.disabled = true;
  try {
    const result = await api(`/api/stickers/${button.dataset.id}/retry`);
    toast(result.message);
  } catch (error) { toast(error.message, 'error'); }
  finally { button.disabled = false; }
});

for (const input of document.querySelectorAll('.sticker-date-control input')) {
  input.addEventListener('input', () => { input.dataset.empty = String(!input.value); });
  input.addEventListener('change', () => { input.dataset.empty = String(!input.value); });
}

const stickerMore = document.getElementById('stickers-more');
stickerMore?.addEventListener('click', async (event) => {
  event.preventDefault();
  if (stickerMore.getAttribute('aria-disabled') === 'true') return;
  stickerMore.setAttribute('aria-disabled', 'true');
  stickerMore.textContent = 'Загружаем…';
  try {
    const response = await fetch(stickerMore.href, { headers: { Accept: 'text/html' } });
    if (!response.ok) throw new Error('Не удалось загрузить стикеры');
    const nextPage = new DOMParser().parseFromString(await response.text(), 'text/html');
    const rows = nextPage.getElementById('sticker-rows');
    if (!rows) throw new Error('Обновите страницу и войдите в панель заново');
    const currentRows = document.getElementById('sticker-rows');
    for (const row of rows.querySelectorAll('tr[data-archive-id]')) {
      // Если за время просмотра появились новые файлы, не добавляем дубли.
      if (!currentRows.querySelector(`tr[data-archive-id="${row.dataset.archiveId}"]`)) currentRows.appendChild(row);
    }
    const next = nextPage.getElementById('stickers-more');
    if (next) stickerMore.href = new URL(next.getAttribute('href'), window.location.href).href;
    else stickerMore.closest('.sticker-pagination').remove();
    stickerAll.disabled = stickerChoices().length === 0;
    updateStickerSelection();
  } catch (error) { toast(error.message, 'error'); }
  finally {
    stickerMore.removeAttribute('aria-disabled');
    stickerMore.textContent = 'Ещё';
  }
});
