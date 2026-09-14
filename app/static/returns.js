/* Загрузка полученных возвратов за число — только у администратора.

   Обычно акт собирается сам, как только возврат перешёл в «Получен». Ручная
   загрузка нужна, когда обновление не работало или статус пришёл с задержкой.
   Сначала показываем, что попадёт в акт, и лишь потом создаём: акт удалить
   нельзя, а число легко перепутать. */
const byDay = document.getElementById('act-byday');

if (byDay) {
  const dayField = document.getElementById('act-day');
  const checkButton = document.getElementById('act-check');
  const createButton = document.getElementById('act-create');
  const result = document.getElementById('act-byday-result');

  /* Подсказка «есть полученные за такое-то число» — она же и выбор числа:
     переписывать дату руками, глядя на список рядом, незачем. */
  byDay.addEventListener('click', (event) => {
    const link = event.target.closest('[data-pick-day]');
    if (!link) return;
    event.preventDefault();
    dayField.value = link.dataset.pickDay;
    createButton.hidden = true;
    checkButton.click();
  });

  /* Новое число — старый ответ уже не про него: прятать кнопку обязательно,
     иначе акт создастся за то число, которое человек только что заменил. */
  dayField.addEventListener('change', () => {
    createButton.hidden = true;
    result.textContent = '';
  });

  async function send(dryRun) {
    if (!dayField.value) { toast('Выберите число', 'error'); return null; }
    return api('/api/returns/acts/by-day', { day: dayField.value, dry_run: dryRun });
  }

  checkButton.onclick = async () => {
    checkButton.disabled = true;
    createButton.hidden = true;
    result.textContent = 'Считаем…';
    try {
      const data = await send(true);
      if (!data) { result.textContent = ''; return; }
      result.innerHTML = `<b>${escapeHtml(data.message)}</b>`;
      createButton.hidden = !data.found;
    } catch (error) {
      result.innerHTML = `<span style="color:var(--err)">${escapeHtml(error.message)}</span>`;
    } finally {
      checkButton.disabled = false;
    }
  };

  createButton.onclick = async () => {
    createButton.disabled = true;
    try {
      const data = await send(false);
      if (data) {
        toast(data.message, data.status === 'ok' ? 'ok' : 'warning', 10000);
        if (data.act_id) setTimeout(() => window.location.reload(), 900);
        else createButton.disabled = false;
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
