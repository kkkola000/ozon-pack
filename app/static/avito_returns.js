/* Возвраты Avito: список только читается — отмечать забранное вручную не нужно,
   заказ уходит сам, как только Avito переведёт его дальше. */
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
