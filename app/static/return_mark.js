/* Окно отметки о возврате: принят / не принят плюс комментарий.

   Отметку ставит человек в пункте выдачи, площадка о ней не знает: у Ozon и
   Avito в статусах есть только «лежит в ПВЗ» и «уехал дальше». Поэтому всё
   хранится в панели и переживает синхронизацию. */
const markModal = document.getElementById('mark-modal');

if (markModal) {
  const noteField = document.getElementById('mark-note');
  const subject = document.getElementById('mark-subject');
  let current = null;   // {button, marketplace, id, mark}

  function paintChoice(mark) {
    markModal.querySelectorAll('.mark-choice').forEach((button) => {
      button.classList.toggle('primary', button.dataset.mark === mark);
    });
  }

  function openMark(button) {
    current = {
      button,
      marketplace: button.dataset.marketplace || 'ozon',
      id: button.dataset.id,
      mark: button.dataset.mark || '',
    };
    subject.textContent = button.dataset.subject || current.id;
    noteField.value = button.dataset.note || '';
    paintChoice(current.mark);
    markModal.hidden = false;
    noteField.focus();
  }

  function closeMark() {
    markModal.hidden = true;
    current = null;
  }

  /* Строка показывает, что сейчас стоит: так список читается одним взглядом и
     не нужно открывать каждый возврат, чтобы вспомнить. Перерисовываем всю
     клетку целиком — иначе рядом с новой отметкой останется старый
     комментарий, и по списку будет видно то, чего уже нет. */
  function paintRow(button, result) {
    button.dataset.mark = result.mark || '';
    button.dataset.note = result.note || '';
    button.textContent = result.mark_sign
      ? `${result.mark_sign} ${result.mark_label}`
      : 'Отметить';
    button.classList.toggle('ok', result.mark === 'ok');
    button.classList.toggle('danger', result.mark === 'bad');
    button.title = result.note || '';

    const cell = button.closest('[data-mark-cell]');
    if (!cell) return;
    const note = cell.querySelector('[data-mark-note]');
    if (note) note.textContent = result.note || '';
    const who = cell.querySelector('[data-mark-who]');
    if (who) {
      who.textContent = result.mark_by ? `${result.mark_by} · ${result.mark_at_local}` : '';
    }
  }

  /* Шапка акта считает отмеченные возвраты и держит кнопку «Подтвердить акт».
     Отметка меняет и то и другое, поэтому шапку перерисовываем сразу: иначе
     кнопка появляется только после обновления страницы, и сборщик, отметив
     последний возврат, не понимает, что акт готов. */
  function paintAct(act) {
    if (!act) return;
    const root = document.querySelector(`.act[data-act="${CSS.escape(act.id)}"]`);
    if (!root) return;

    const badges = root.querySelector('[data-act-badges]');
    if (badges) {
      const marks = [];
      if (act.marked_ok) marks.push(`<span class="badge ok">${act.marked_ok} принято</span>`);
      if (act.marked_bad) marks.push(`<span class="badge err">${act.marked_bad} не принят</span>`);
      if (act.unmarked) marks.push(`<span class="badge warn">${act.unmarked} без отметки</span>`);
      badges.innerHTML = marks.join('');
    }

    const bar = root.querySelector('[data-act-progress] > div');
    if (bar) bar.style.width = `${act.percent}%`;

    const confirmButton = root.querySelector('[data-confirm-act]');
    if (confirmButton) {
      confirmButton.disabled = !act.can_confirm;
      confirmButton.classList.toggle('primary', act.can_confirm);
      confirmButton.title = act.can_confirm
        ? 'Подтвердить: решение принято по всем возвратам акта'
        : 'Сначала отметьте все возвраты акта';
    }
  }

  async function saveMark(mark) {
    if (!current) return;
    const { button, marketplace, id } = current;
    const note = noteField.value.trim();
    button.disabled = true;
    try {
      const result = await api('/api/returns/mark', { marketplace, id, mark, note });
      paintRow(button, result);
      paintAct(result.act);
      toast(result.message, mark === 'bad' ? 'warning' : 'ok');
      closeMark();
    } catch (error) {
      toast(error.message, 'error', 10000);
    } finally {
      button.disabled = false;
    }
  }

  document.addEventListener('click', (event) => {
    const opener = event.target.closest('[data-mark-open]');
    if (opener) {
      event.preventDefault();
      openMark(opener);
      return;
    }
    /* Клик мимо окна закрывает его — как и крестик. */
    if (event.target === markModal) closeMark();
  });

  markModal.querySelectorAll('.mark-choice').forEach((button) => {
    button.addEventListener('click', () => {
      if (current) current.mark = button.dataset.mark;
      paintChoice(button.dataset.mark);
    });
  });

  document.getElementById('mark-save').onclick = () => saveMark(current?.mark || '');
  document.getElementById('mark-clear').onclick = () => { noteField.value = ''; saveMark(''); };
  document.getElementById('mark-cancel').onclick = closeMark;
  document.getElementById('mark-close').onclick = closeMark;
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !markModal.hidden) closeMark();
  });
}
