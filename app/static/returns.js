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
