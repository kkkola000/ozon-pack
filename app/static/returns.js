/* Составление акта за число — у всех, кто работает с возвратами.

   Два действия, как в жизни: выбрали дату — нажали «Составить акт». Сколько
   возвратов попадёт, панель считает сама при выборе даты и пишет прямо на
   кнопке: акт удалить нельзя, и вслепую его заводить не надо. */
const byDay = document.getElementById('act-byday');

if (byDay) {
  const dayField = document.getElementById('act-day');
  const createButton = document.getElementById('act-create');
  const result = document.getElementById('act-byday-result');

  function send(dryRun) {
    return api('/api/returns/acts/by-day', { day: dayField.value, dry_run: dryRun });
  }

  /* Сколько попадёт в акт за выбранную дату. Кнопка заперта, пока это
     неизвестно или пока за дату нечего собирать. */
  async function count() {
    createButton.disabled = true;
    createButton.textContent = 'Составить акт';
    if (!dayField.value) { result.textContent = 'Выберите дату'; return; }
    result.textContent = 'Считаем…';
    try {
      const data = await send(true);
      result.textContent = data.found ? '' : data.message;
      createButton.disabled = !data.found;
      if (data.found) createButton.textContent = `Составить акт на ${data.found}`;
    } catch (error) {
      result.innerHTML = `<span style="color:var(--err)">${escapeHtml(error.message)}</span>`;
    }
  }

  dayField.addEventListener('change', count);
  count();

  createButton.onclick = async () => {
    createButton.disabled = true;
    createButton.textContent = 'Составляем…';
    try {
      const data = await send(false);
      toast(data.message, data.status === 'ok' ? 'ok' : 'warning', 10000);
      if (data.act_id) { setTimeout(() => window.location.reload(), 900); return; }
      /* Составлять нечего: возвраты забрал акт из соседней вкладки. */
      count();
    } catch (error) {
      toast(error.message, 'error', 10000);
      count();
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
