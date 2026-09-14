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

  /* Список актов Ozon за последние дни: выбрать нужные и завести в панель.
     Это основной способ — акт целиком, с его номером, временем и составом. */
  const actList = document.getElementById('act-list');
  const daysField = document.getElementById('act-days');

  function renderActs(acts) {
    if (!acts.length) {
      actList.innerHTML = '<div class="muted small">За выбранный срок актов у Ozon нет.</div>';
      return;
    }
    const rows = acts.map((act) => {
      /* Нечего добавлять — отмечать тоже нечего: иначе человек нажмёт и
         не поймёт, почему ничего не произошло. */
      const locked = act.in_panel || !act.free;
      const why = act.in_panel ? 'уже в панели'
        : !act.matched ? 'возвраты не опознаны'
        : !act.free ? 'возвраты уже в других актах' : '';
      return `<tr>
        <td style="width:34px">
          <input type="checkbox" class="act-pick" value="${escapeHtml(act.id)}"
                 ${locked ? 'disabled' : 'checked'}>
        </td>
        <td class="mono">${escapeHtml(act.id)}</td>
        <td class="small">${escapeHtml(act.created_local || '—')}</td>
        <td class="small">${escapeHtml(act.status_label || act.status || '—')}</td>
        <td class="small">${act.items} поз. · опознано ${act.matched}
          ${why ? `<span class="badge warn">${escapeHtml(why)}</span>` : ''}
          <div class="muted">${act.names.map(escapeHtml).join(', ')}</div>
        </td>
      </tr>`;
    }).join('');
    actList.innerHTML = `
      <table><thead><tr>
        <th></th><th style="width:130px">Акт Ozon</th><th style="width:140px">Составлен</th>
        <th style="width:130px">Статус</th><th>Состав</th>
      </tr></thead><tbody>${rows}</tbody></table>
      <div class="row" style="margin-top:12px">
        <span class="grow"></span>
        <button class="btn primary" id="act-import">Добавить выбранные</button>
      </div>`;
    document.getElementById('act-import').onclick = importPicked;
  }

  async function importPicked(event) {
    const picked = [...actList.querySelectorAll('.act-pick:checked')].map((box) => box.value);
    if (!picked.length) { toast('Отметьте хотя бы один акт', 'error'); return; }
    event.target.disabled = true;
    try {
      const data = await api('/api/returns/acts/import', { giveout_ids: picked });
      toast(data.message, data.status === 'ok' ? 'ok' : 'warning', 10000);
      if (data.added) setTimeout(() => window.location.reload(), 900);
      else event.target.disabled = false;
    } catch (error) {
      toast(error.message, 'error', 10000);
      event.target.disabled = false;
    }
  }

  document.getElementById('act-fetch').onclick = async (event) => {
    event.target.disabled = true;
    actList.innerHTML = '<div class="muted small">Спрашиваем Ozon…</div>';
    try {
      const data = await api(`/api/returns/acts/available?days=${daysField.value}`, undefined, 'GET');
      renderActs(data.acts || []);
    } catch (error) {
      actList.innerHTML = `<span style="color:var(--err)">${escapeHtml(error.message)}</span>`;
    } finally {
      event.target.disabled = false;
    }
  };

  /* Запасной ход: списка актов у кабинета нет, но есть текущий документ. */
  document.getElementById('act-from-ozon').onclick = async (event) => {
    const button = event.target;
    button.disabled = true;
    actList.textContent = 'Запрашиваем документ у Ozon…';
    try {
      const data = await api('/api/returns/acts/from-ozon', {});
      actList.innerHTML = `<b>${escapeHtml(data.message)}</b>`;
      toast(data.message, data.status === 'ok' ? 'ok' : 'warning', 10000);
      if (data.act_id) setTimeout(() => window.location.reload(), 1200);
    } catch (error) {
      actList.innerHTML = `<span style="color:var(--err)">${escapeHtml(error.message)}</span>`;
    } finally {
      button.disabled = false;
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
