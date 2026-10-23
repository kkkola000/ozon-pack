/* Просмотр и печать используют общий обработчик data-print-pdf в qz.js. */
const stickerChoices = [...document.querySelectorAll('.sticker-select')];
const stickerDownload = document.getElementById('stickers-download');
const stickerAll = document.getElementById('stickers-all');
function updateStickerSelection() {
  const count = stickerChoices.filter((box) => box.checked).length;
  stickerDownload.disabled = count === 0;
  stickerAll.checked = count > 0 && count === stickerChoices.length;
  stickerAll.indeterminate = count > 0 && count < stickerChoices.length;
}
stickerAll.addEventListener('change', () => {
  stickerChoices.forEach((box) => { box.checked = stickerAll.checked; });
  updateStickerSelection();
});
stickerChoices.forEach((box) => box.addEventListener('change', updateStickerSelection));
stickerDownload.addEventListener('click', async () => {
  stickerDownload.disabled = true;
  try {
    const response = await fetch('/api/stickers/archive.zip', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
      body: JSON.stringify(stickerChoices.filter((box) => box.checked).map((box) => Number(box.value))),
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
for (const button of document.querySelectorAll('.sticker-retry')) {
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const result = await api(`/api/stickers/${button.dataset.id}/retry`);
      toast(result.message);
    } catch (error) { toast(error.message, 'error'); }
    finally { button.disabled = false; }
  });
}
