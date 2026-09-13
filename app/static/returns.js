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
    toast(result.message, 'ok');
    setTimeout(() => window.location.reload(), 700);
  } catch (error) {
    toast(error.message, 'error', 10000);
    event.target.disabled = false;
    event.target.textContent = 'Обновить возвраты';
  }
});
