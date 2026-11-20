/* Сохранённый «Честный знак» печатается тем же способом, что при сборке. */
const stickerChoices = () => [...document.querySelectorAll('.sticker-select')];
const stickerMarkingModal = document.getElementById('sticker-marking-modal');
let stickerMarkingPrinting = false;
document.getElementById('sticker-marking-content').addEventListener('click', async (event) => {
  const unbind = event.target.closest('[data-unbind-mark]');
  if (unbind) {
    if (unbind.disabled) return;
    unbind.disabled = true;
    try {
      const data = await api(`/api/stickers/${unbind.dataset.archive}/marking/${unbind.dataset.unbindMark}/unbind`,
        { version: unbind.dataset.version });
      renderStickerMarking(data, unbind.dataset.archive);
    } catch (error) { toast(error.message, 'error'); }
    finally { unbind.disabled = false; }
    return;
  }
  const link = event.target.closest('a[data-print-kind="marking:duplicate"]');
  if (!link) return;
  event.preventDefault();
  event.stopPropagation(); // Не запускаем второй обработчик печати из qz.js.
  if (stickerMarkingPrinting) return;
  stickerMarkingPrinting = true;
  const reserved = reservePrintWindow(link.dataset.printKind);
  link.setAttribute('aria-disabled', 'true');
  try {
    await printPdf(link.dataset.printPdf, {
      name: link.dataset.printName, kind: link.dataset.printKind, window: reserved, wait: true,
    });
  } catch (error) {
    reserved?.close();
    toast(error.message, 'error');
  } finally {
    link.removeAttribute('aria-disabled');
    stickerMarkingPrinting = false;
  }
});
document.getElementById('sticker-marking-close').onclick = () => { stickerMarkingModal.hidden = true; };
stickerMarkingModal.addEventListener('click', (event) => { if (event.target === stickerMarkingModal) stickerMarkingModal.hidden = true; });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') stickerMarkingModal.hidden = true; });
function renderStickerMarking(data, archiveId) {
  document.getElementById('sticker-marking-title').textContent = `Честный знак отправления ${data.order_id}`;
  const rows = data.items.map((item) => {
    const ok = item.status === 'accepted';
    const bad = item.status === 'error';
    const detached = item.status === 'detached';
    const url = item.history ? `/api/stickers/marking/history/${item.id}.pdf` : `/api/stickers/marking/${item.id}.pdf`;
    const unbind = data.can_unbind && item.can_unbind
      ? `<button class="btn small" type="button" data-unbind-mark="${item.id}" data-archive="${archiveId}" data-version="${escapeHtml(item.unbind_version)}">Отвязать от заказа</button>` : '';
    return `<tr><td>${escapeHtml(item.name)}<div class="small muted">Экземпляр ${item.unit_no}</div></td>
      <td class="mono small">${escapeHtml(item.gtin)}<div>${escapeHtml(item.serial)}</div></td>
      <td><span class="badge ${ok ? 'ok' : bad ? 'err' : detached ? '' : 'warn'}">${ok ? 'Принят' : bad ? 'Не принят' : detached ? 'Отвязан' : 'Ожидает проверки'}</span></td>
      <td>${item.printable === false ? '<span class="muted small">Замените марку</span>' : `<a class="btn small" href="${url}" target="_blank" rel="noopener" data-print-pdf="${url}" data-print-kind="marking:duplicate" data-print-name="Дубликат «Честный знак»">Печать</a>`} ${unbind}</td></tr>`;
  }).join('');
  const all = `/api/stickers/${archiveId}/marking.pdf`;
  document.getElementById('sticker-marking-content').innerHTML =
    `${data.error ? `<p class="muted small">${escapeHtml(data.error)}</p>` : ''}<div style="overflow-x:auto"><table><thead><tr><th>Товар</th><th>GTIN / серийный номер</th><th>Проверка площадкой</th><th>Действия</th></tr></thead><tbody>${rows}</tbody></table></div>
     ${data.items.length ? `<div class="row" style="justify-content:flex-end;margin-top:16px"><a class="btn" href="${all}" target="_blank" rel="noopener" data-print-pdf="${all}" data-print-kind="marking:duplicate" data-print-name="Дубликаты «Честный знак»">Печать всех кодов «Честный знак»</a></div>` : ''}`;
}
document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-marking-archive]');
  if (!button || button.disabled) return;
  button.disabled = true;
  try {
    const data = await api(`/api/stickers/${button.dataset.markingArchive}/marking`, undefined, 'GET');
    renderStickerMarking(data, button.dataset.markingArchive);
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
