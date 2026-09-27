/* Окно отметки о возврате: принят / не принят плюс комментарий.

   Отметку ставит человек в пункте выдачи, площадка о ней не знает: у Ozon и
   Avito в статусах есть только «лежит в ПВЗ» и «уехал дальше». Поэтому всё
   хранится в панели и переживает синхронизацию.

   Окно открывают двумя путями: кнопкой «Отметить» в строке акта и сканом
   стикера при приёмке (return_scan.js). Второму окно отдаёт ReturnMark:
   открыть, закрыть и перерисовать строку после отметки, поставленной сканом. */
const markModal = document.getElementById('mark-modal');

if (markModal) {
  const noteField = document.getElementById('mark-note');
  const subject = document.getElementById('mark-subject');
  const titleNode = document.getElementById('mark-title');
  const clearButton = document.getElementById('mark-clear');
  const scanBox = document.getElementById('mark-scan');
  const decide = document.getElementById('mark-decide');
  const doneRow = document.getElementById('mark-done-row');
  let current = null;   // {button, marketplace, id, account, mark, note}

  /* Что стоит сейчас — видно по подсвеченной кнопке. Нажатие на неё же
     сохраняет: выбор и есть решение, второго нажатия «Сохранить» нет. */
  function paintChoice(mark) {
    markModal.querySelectorAll('.mark-choice').forEach((button) => {
      /* Выбранное красим по смыслу, как в списке: принят — зелёным, не
         принят — красным. Синим, «как главное действие», оба цвета читались
         одинаково, и отличить одно от другого можно было только по тексту. */
      const chosen = button.dataset.mark === mark;
      button.classList.toggle('ok', chosen && mark === 'ok');
      button.classList.toggle('danger', chosen && mark === 'bad');
    });
    /* Снимать нечего, пока отметки нет: кнопка в этот момент повторяла бы
       крестик, только через запрос к серверу. */
    clearButton.disabled = !mark;
  }

  /* Кнопка строки акта — её перекрашивают после отметки. Возврата может не
     быть на странице (скан нашёл его в акте другого кабинета) — тогда null. */
  function findButton(marketplace, id, account) {
    return [...document.querySelectorAll('[data-mark-open]')].find((node) => (
      (node.dataset.marketplace || 'ozon') === marketplace && node.dataset.id === String(id)
      && (!account || !node.dataset.account || node.dataset.account === String(account))
    )) || null;
  }

  function targetOf(button) {
    return {
      button,
      marketplace: button.dataset.marketplace || 'ozon',
      id: button.dataset.id,
      // Кабинет строки: возвраты в «Ждёт подтверждения» бывают из разных кабинетов.
      account: button.dataset.account || '',
      mark: button.dataset.mark || '',
      note: button.dataset.note || '',
      subject: button.dataset.subject || button.dataset.id,
    };
  }

  /* target — возврат; null — сканом открыто сразу несколько (все возвраты
     отправления), и решений по одному тогда нет, есть «Готово».
     options.scan — окно открыл скан: виден блок приёмки, курсор в поле товара. */
  function openMark(target, options = {}) {
    current = target
      ? { ...target, button: target.button || findButton(target.marketplace, target.id, target.account) }
      : null;
    titleNode.textContent = options.title || 'Отметка о возврате';
    subject.textContent = options.subject || (current ? current.subject || current.id : '—');
    markModal.classList.toggle('scan-mode', Boolean(options.scan));
    scanBox.hidden = !options.scan;
    decide.hidden = !current;
    doneRow.hidden = Boolean(current) || !options.scan;
    noteField.value = current ? current.note || '' : '';
    paintChoice(current ? current.mark : '');
    markModal.hidden = false;
    (options.scan ? document.getElementById('mark-scan-input') : noteField).focus();
  }

  function closeMark() {
    if (markModal.hidden) return;
    markModal.hidden = true;
    current = null;
    markModal.dispatchEvent(new CustomEvent('mark:closed'));
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
    button.classList.toggle('primary', !result.mark);
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
    /* Ищем перебором, а не селектором по id: экранировать ничего не нужно и
       сломаться не на чем — а окно к этому моменту уже закрыто. */
    const root = [...document.querySelectorAll('.act[data-act]')]
      .find((node) => node.dataset.act === act.id);
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

  /* Отметка записана — перерисовать строку, шапку акта и само окно, если оно
     открыто на этом возврате (отметку поставил скан товара). */
  function paint(target, result) {
    const button = target.button || findButton(target.marketplace, target.id, target.account);
    if (button) paintRow(button, result);
    paintAct(result.act);
    if (current && current.marketplace === target.marketplace && String(current.id) === String(target.id)) {
      current.mark = result.mark || '';
      current.note = result.note || '';
      paintChoice(current.mark);
    }
  }

  /* Пока запрос в пути, кнопки окна заперты: нажатие сохраняет сразу, и два
     нажатия подряд ушли бы двумя отметками по одному возврату. */
  function lockModal(locked) {
    markModal.querySelectorAll('.btn').forEach((button) => {
      button.disabled = locked;
    });
    if (!locked) paintChoice(current ? current.mark : '');
  }

  async function saveMark(mark) {
    if (!current) return;
    const target = current;
    const { button, marketplace, id, account } = target;
    const note = noteField.value.trim();
    if (button) button.disabled = true;
    lockModal(true);
    try {
      const result = await api('/api/returns/mark', {
        marketplace, id, mark, note, account_id: account ? Number(account) : null,
      });
      /* Сервер ответил — отметка записана, и окно закрываем первым делом.
         Перерисовка списка идёт после: если споткнётся она, отметка всё равно
         сохранена, а открытое окно с ошибкой говорило бы обратное. */
      closeMark();
      toast(result.message, mark === 'bad' ? 'warning' : 'ok');
      paint(target, result);
    } catch (error) {
      toast(error.message, 'error', 10000);
    } finally {
      if (button) button.disabled = false;
      lockModal(false);
    }
  }

  document.addEventListener('click', (event) => {
    const opener = event.target.closest('[data-mark-open]');
    if (opener) {
      event.preventDefault();
      openMark(targetOf(opener));
      return;
    }
    /* Клик мимо окна закрывает его — как и крестик. */
    if (event.target === markModal) closeMark();
  });

  /* Нажали решение — оно и записано. Раньше выбор только подсвечивался, а
     записывало его второе нажатие, «Сохранить»: закрыл окно крестиком, решив,
     что дело сделано, — и отметки нет. */
  markModal.querySelectorAll('.mark-choice').forEach((button) => {
    button.addEventListener('click', () => saveMark(button.dataset.mark));
  });

  clearButton.onclick = () => { noteField.value = ''; saveMark(''); };
  document.getElementById('mark-close').onclick = closeMark;
  document.getElementById('mark-done').onclick = closeMark;
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !markModal.hidden) closeMark();
  });

  window.ReturnMark = { open: openMark, close: closeMark, paint };
}
