/* Просмотр и печать используют общий обработчик data-print-pdf в qz.js. */
const stickerChoices = () => [...document.querySelectorAll('.sticker-select')];
const stickerMarkingModal = document.getElementById('sticker-marking-modal');
document.getElementById('sticker-marking-close').onclick = () => { stickerMarkingModal.hidden = true; };
stickerMarkingModal.addEventListener('click', (event) => { if (event.target === stickerMarkingModal) stickerMarkingModal.hidden = true; });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') stickerMarkingModal.hidden = true; });
document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-marking-archive]');
  if (!button || button.disabled) return;
  button.disabled = true;
  try {
    const data = await api(`/api/stickers/${button.dataset.markingArchive}/marking`, undefined, 'GET');
    document.getElementById('sticker-marking-title').textContent = `ЧЗ отправления ${data.order_id}`;
    const rows = data.items.map((item) => {
      const ok = item.status === 'accepted';
      const bad = item.status === 'error';
      const url = `/api/stickers/marking/${item.id}.pdf`;
      return `<tr><td>${escapeHtml(item.name)}<div class="small muted">Экземпляр ${item.unit_no}</div></td>
        <td class="mono small">${escapeHtml(item.gtin)}<div>${escapeHtml(item.serial)}</div></td>
        <td><span class="badge ${ok ? 'ok' : bad ? 'err' : 'warn'}">${ok ? 'Принят' : bad ? 'Не принят' : 'Ожидает проверки'}</span></td>
        <td>${bad ? '<span class="muted small">Замените марку</span>' : `<a class="btn small" href="${url}" target="_blank" rel="noopener" data-print-pdf="${url}" data-print-kind="marking:duplicate" data-print-name="Дубликат ЧЗ">Печать</a>`}</td></tr>`;
    }).join('');
    const all = `/api/stickers/${button.dataset.markingArchive}/marking.pdf`;
    document.getElementById('sticker-marking-content').innerHTML =
      `${data.error ? `<p class="muted small">${escapeHtml(data.error)}</p>` : ''}<div style="overflow-x:auto"><table><thead><tr><th>Товар</th><th>GTIN / серийный номер</th><th>Проверка площадкой</th><th>Действия</th></tr></thead><tbody>${rows}</tbody></table></div>
       ${data.items.length ? `<div class="row" style="justify-content:flex-end;margin-top:16px"><a class="btn" href="${all}" target="_blank" rel="noopener" data-print-pdf="${all}" data-print-kind="marking:duplicate" data-print-name="Дубликаты ЧЗ">Печать всех ЧЗ</a></div>` : ''}`;
    stickerMarkingModal.hidden = false;
    document.getElementById('sticker-marking-close').focus();
  } catch (error) { toast(error.message, 'error'); }
  finally { button.disabled = false; }
});
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
