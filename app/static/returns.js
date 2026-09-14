/* Загрузка акта файлом — только у администратора.

   Сначала показываем, что нашлось в файле, и лишь потом заводим акт: акт
   удалить нельзя, а файл легко перепутать с чужим кабинетом. */
const actUpload = document.getElementById('act-upload');

if (actUpload) {
  const fileField = document.getElementById('act-file');
  const checkButton = document.getElementById('act-check');
  const createButton = document.getElementById('act-create');
  const result = document.getElementById('act-upload-result');

  async function sendAct(dryRun) {
    const file = fileField.files[0];
    if (!file) { toast('Выберите файл акта', 'error'); return null; }
    const form = new FormData();
    form.append('file', file);
    form.append('dry_run', dryRun ? 'true' : 'false');
    const response = await fetch('/api/returns/acts/upload', {
      method: 'POST',
      headers: { 'X-CSRF-Token': CSRF, 'X-Requested-With': 'fetch' },
      body: form,
    });
    let data = {};
    try { data = await response.json(); } catch (error) { /* пустой ответ */ }
    if (!response.ok) throw new Error(data.detail || `Ошибка ${response.status}`);
    return data;
  }

  function showFound(data) {
    const rows = (data.returns || []).map((item) => `
      <div style="padding:3px 0">
        <span class="mono">${escapeHtml(item.id)}</span>
        <span class="muted mono">${escapeHtml(item.barcode || '')}</span>
        ${escapeHtml(item.name || '')}
        ${item.in_act ? '<span class="badge warn">уже в акте</span>' : ''}
      </div>`).join('');
    result.innerHTML = `<div style="margin-bottom:8px"><b>${escapeHtml(data.message)}</b>
      <span class="muted">· кодов в файле: ${data.codes}</span></div>${rows}`;
  }

  checkButton.onclick = async () => {
    checkButton.disabled = true;
    createButton.hidden = true;
    result.textContent = 'Читаем файл…';
    try {
      const data = await sendAct(true);
      if (!data) { result.textContent = ''; return; }
      showFound(data);
      /* Заводить акт не из чего, если все возвраты уже разнесены. */
      createButton.hidden = !(data.found && data.free);
    } catch (error) {
      result.innerHTML = `<span style="color:var(--err)">${escapeHtml(error.message)}</span>`;
    } finally {
      checkButton.disabled = false;
    }
  };

  createButton.onclick = async () => {
    createButton.disabled = true;
    try {
      const data = await sendAct(false);
      if (data) {
        toast(data.message, 'ok');
        setTimeout(() => window.location.reload(), 700);
      }
    } catch (error) {
      toast(error.message, 'error', 10000);
      createButton.disabled = false;
    }
  };
}

/* Подтверждение акта: по всем возвратам поездки решение принято.

   Кнопка живёт внутри summary, поэтому клик по ней иначе сворачивал бы акт —
   сборщик нажимал бы «Подтвердить», а блок просто закрывался. */
document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-confirm-act]');
  if (!button) return;
  event.preventDefault();
  if (button.disabled) return;
  if (!confirm('Подтвердить акт? Он уйдёт из списка, отметки останутся в журнале.')) return;
  button.disabled = true;
  try {
    const result = await api(`/api/returns/acts/${encodeURIComponent(button.dataset.confirmAct)}/confirm`, {});
    toast(result.message, 'ok');
    setTimeout(() => window.location.reload(), 700);
  } catch (error) {
    toast(error.message, 'error', 10000);
    button.disabled = false;
  }
});

/* Возвраты: список только читается — отмечать забранное вручную не нужно,
   Ozon сам меняет статус, как только возврат получен в пункте выдачи. */
document.getElementById('btn-sync-returns')?.addEventListener('click', async (event) => {
  event.target.disabled = true;
  event.target.textContent = 'Обновляем…';
  try {
    const result = await api('/api/returns/sync', {});
    /* Про акты бывает что сказать: сообщение длиннее обычного, и его читают. */
    toast(result.message, result.status === 'warning' ? 'warning' : 'ok', 12000);
    setTimeout(() => window.location.reload(), 1500);
  } catch (error) {
    toast(error.message, 'error', 10000);
    event.target.disabled = false;
    event.target.textContent = 'Обновить возвраты';
  }
});
