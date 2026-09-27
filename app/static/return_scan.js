/* Приёмка возвратов сканером — вкладка «Ждёт подтверждения».

   Два скана вместо поиска строки глазами. Стикер или штрихкод возврата —
   открывается окно отметки с карточкой возврата. Штрихкод товара — совпал, и
   отметка «Принят» ставится сама, окно закрывается, поле ждёт следующий
   пакет. Не тот товар — отметка не меняется: «Не принят» с комментарием
   ставит человек.

   Код отправления или заказа открывает сразу все его возвраты: каждый
   совпавший товар принимается своему возврату, в любом порядке.

   Окно общее с кнопкой «Отметить» (return_mark.js, ReturnMark). Здесь —
   поле, карточка, счёт отсканированного и ответы сервера. */
(() => {
  const zone = document.getElementById('rscan');
  if (!zone || !window.ReturnMark) return;

  const zoneForm = document.getElementById('rscan-form');
  const zoneInput = document.getElementById('rscan-input');
  const zoneMsg = document.getElementById('rscan-msg');
  const modal = document.getElementById('mark-modal');
  const rowsBox = document.getElementById('mark-rows');
  const step = document.getElementById('mark-step');
  const stepIcon = document.getElementById('mark-step-icon');
  const stepTitle = document.getElementById('mark-step-title');
  const stepText = document.getElementById('mark-step-text');
  const scanInput = document.getElementById('mark-scan-input');

  // Принят — окно закрывается само через две секунды: успеть увидеть зелёное.
  const AUTO_CLOSE_MS = 2000;
  const BARS = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
    stroke-linecap="round"><path d="M4 6v12M7 6v12M9.5 6v12M13 6v12M15.5 6v12M18 6v12M20 6v12"/></svg>`;
  const ICONS = { wait: BARS, ok: '✓', bad: '✕', warn: '!' };

  let session = null;     // {rows: [...]} — возвраты, открытые последним сканом
  let closeTimer = null;
  let busy = false;

  // ------------------------------------------------------------ поле во вкладке
  function zoneError(text) {
    zone.classList.toggle('error', Boolean(text));
    zoneMsg.textContent = text || '';
  }

  zoneInput.addEventListener('focus', () => zone.classList.add('focus'));
  zoneInput.addEventListener('blur', () => zone.classList.remove('focus'));
  zoneInput.addEventListener('input', () => zoneError(''));

  zoneForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const code = zoneInput.value.trim();
    zoneInput.value = '';
    if (!code || busy) return;
    busy = true;
    try {
      const data = await api('/api/returns/scan', { code });
      zoneError('');
      beep('ok');
      start(data);
    } catch (error) {
      zoneError(error.message);
      beep('error');
    } finally {
      busy = false;
    }
  });

  // ------------------------------------------------------------ окно
  const sameRow = (row, ref) => row.marketplace === ref.marketplace && String(row.id) === String(ref.id)
    && String(row.account_id) === String(ref.account_id);
  const remaining = () => session.rows.filter((row) => row.mark !== 'ok');

  function target(row) {
    return {
      marketplace: row.marketplace,
      id: row.id,
      account: String(row.account_id),
      mark: row.mark,
      note: row.note,
      subject: `${row.number} · ${row.title}`,
    };
  }

  function start(data) {
    clearTimeout(closeTimer);
    session = { rows: data.rows.map((row) => ({ ...row, got: {} })) };
    if (session.rows.length === 1) {
      ReturnMark.open(target(session.rows[0]), { scan: true, title: data.title, subject: data.sub });
    } else {
      ReturnMark.open(null, { scan: true, title: data.title, subject: data.sub });
    }
    render();
    waiting();
  }

  /* Из списка отправления — к одному возврату: там есть «Не принят» и комментарий. */
  function focusRow(row) {
    clearTimeout(closeTimer);
    session = { rows: [row] };
    ReturnMark.open(target(row), {
      scan: true,
      title: 'Отметка о возврате',
      subject: `${row.market} · кабинет «${row.shop}» · ${row.act_title}`,
    });
    render();
    waiting();
  }

  function photo(image, name) {
    if (!image) return '<div class="rmark-photo" aria-hidden="true">🖼</div>';
    return `<img class="rmark-photo" src="${escapeHtml(image)}" alt="${escapeHtml(name || '')}" loading="lazy"
      onerror="this.replaceWith(Object.assign(document.createElement('div'), {className: 'rmark-photo', textContent: '🖼'}))">`;
  }

  /* Номера и коды — моноширинным, как на наклейке; причина возврата — обычным текстом. */
  function facts(row) {
    return row.facts.map(([label, value]) => (
      `${escapeHtml(label)} <b class="${/\s/.test(value) ? '' : 'mono'}">${escapeHtml(value)}</b>`
    )).join(' · ');
  }

  /* Товары возврата со счётом — когда их больше одной штуки. */
  function goodsList(row) {
    const pieces = row.goods.reduce((sum, item) => sum + item.need, 0);
    if (pieces <= 1) return '';
    return `<div class="rmark-goods">${row.goods.map((item) => {
      const got = row.got[item.key] || 0;
      const done = row.mark === 'ok' || got >= item.need;
      return `<div class="${done ? 'done' : ''}"><span>${done ? '✓' : `${got}/${item.need}`}</span>
        <span>${escapeHtml(item.name)}</span></div>`;
    }).join('')}</div>`;
  }

  function status(row) {
    if (row.mark === 'ok') return '✓ Принят';
    if (row.mark === 'bad') return '✗ Не принят';
    const got = Object.values(row.got).reduce((sum, value) => sum + value, 0);
    const need = row.goods.reduce((sum, item) => sum + item.need, 0);
    return got ? `${got}/${need}` : 'ждёт скана';
  }

  function render() {
    if (session.rows.length === 1) {
      const row = session.rows[0];
      rowsBox.innerHTML = `
        <div class="rmark-card">
          ${photo(row.image, row.title)}
          <div class="grow">
            <div class="rmark-name">${escapeHtml(row.title)}</div>
            <div class="rmark-meta">${facts(row)}</div>
            ${goodsList(row)}
          </div>
        </div>`;
      return;
    }
    rowsBox.innerHTML = session.rows.map((row, index) => `
      <button type="button" class="rmark-item ${row.mark === 'ok' ? 'done' : row.mark === 'bad' ? 'bad' : ''}"
              data-scan-row="${index}" title="Открыть этот возврат: «Не принят», комментарий">
        ${photo(row.image, row.title)}
        <div class="grow">
          <div style="font-weight:700">${escapeHtml(row.title)}</div>
          <div class="muted small">Возврат ${escapeHtml(row.number)} · ${escapeHtml(row.shop)}</div>
        </div>
        <div class="rmark-count">${escapeHtml(status(row))}</div>
      </button>`).join('');
  }

  rowsBox.addEventListener('click', (event) => {
    const item = event.target.closest('[data-scan-row]');
    if (item && session) focusRow(session.rows[Number(item.dataset.scanRow)]);
  });

  function setStep(kind, title, html) {
    step.className = `rmark-step ${kind === 'wait' ? '' : kind}`;
    stepIcon.innerHTML = ICONS[kind] || BARS;
    stepTitle.textContent = title;
    stepText.innerHTML = html;
    scanInput.focus();
  }

  /* Что сканировать дальше. */
  function waiting() {
    const left = remaining();
    if (!left.length) {
      const row = session.rows[0];
      setStep('ok', session.rows.length > 1 ? 'Все возвраты уже приняты' : 'Возврат уже принят',
              escapeHtml(row.mark_by ? `Отметил ${row.mark_by} · ${row.mark_at_local}.` : ''));
      return;
    }
    if (session.rows.length > 1) {
      setStep('wait', `Отсканируйте товар — осталось ${left.length} из ${session.rows.length}`,
              'Каждый совпавший товар сразу отмечается «Принят». Не хватает товара — нажмите на его строку '
              + 'и поставьте «Не принят».');
      return;
    }
    const expect = left[0].goods.map((item) => item.expect).filter(Boolean);
    setStep('wait', 'Отсканируйте штрихкод товара',
            'Совпадёт с возвратом — отметка сама станет «Принят». '
            + (expect.length
              ? `Ожидается: ${expect.map((code) => `<span class="mono">${escapeHtml(code)}</span>`).join(', ')}`
              : 'Штрихкода товара в каталоге нет — засчитается любой, кроме штрихкода другого товара.'));
  }

  // ------------------------------------------------------------ скан товара
  step.addEventListener('submit', async (event) => {
    event.preventDefault();
    const code = scanInput.value.trim();
    scanInput.value = '';
    if (!code || !session || busy) return;
    clearTimeout(closeTimer);
    const open = remaining();
    if (!open.length) { waiting(); return; }
    busy = true;
    try {
      const result = await api('/api/returns/scan/goods', {
        code,
        rows: open.map((row) => ({
          marketplace: row.marketplace, id: row.id, account_id: row.account_id, got: row.got,
        })),
      });
      apply(result, code);
    } catch (error) {
      setStep('bad', 'Скан не принят', escapeHtml(error.message));
      beep('error');
    } finally {
      busy = false;
      scanInput.focus();
    }
  });

  function apply(result, code) {
    if (!session) return;
    if (result.status === 'error') {
      setStep('bad', 'Не тот товар', escapeHtml(result.message));
      beep('error');
      return;
    }
    if (result.action === 'extra') {
      setStep('warn', 'Лишний скан', escapeHtml(result.message));
      beep('warning');
      return;
    }
    const row = session.rows.find((item) => sameRow(item, result.row));
    if (!row) return;
    row.got = result.got || {};
    if (result.action === 'accepted') {
      Object.assign(row, {
        mark: 'ok', mark_by: result.mark.mark_by, mark_at_local: result.mark.mark_at_local,
      });
      ReturnMark.paint(target(row), result.mark);
    }
    render();
    if (result.action === 'counted') {
      setStep('wait', 'Засчитано', escapeHtml(result.message));
      beep('ok');
      return;
    }
    // Сверить было не с чем — сказать прямо: «Принят» тут без проверки товара.
    const unchecked = result.checked ? '' : ' Штрихкода товара в каталоге нет — сверить было не с чем.';
    const left = remaining().length;
    if (left) {
      setStep('ok', `Принят: ${row.title}`,
              escapeHtml(`Осталось ${left} из ${session.rows.length}. Сканируйте следующий товар.${unchecked}`));
      beep('ok');
      return;
    }
    setStep('ok', session.rows.length > 1 ? 'Все возвраты приняты' : 'Товар совпал — возврат принят',
            `Возврат <span class="mono">${escapeHtml(row.number)}</span> · отсканировано `
            + `<span class="mono">${escapeHtml(code)}</span>.${escapeHtml(unchecked)} Окно закроется через 2 с.`);
    beep('done');
    closeTimer = setTimeout(() => ReturnMark.close(), AUTO_CLOSE_MS);
  }

  /* Взялись за окно руками — пишут комментарий или жмут кнопку, — значит,
     закрывать его само не надо. */
  modal.addEventListener('pointerdown', (event) => {
    if (event.target !== modal) clearTimeout(closeTimer);
  });
  modal.addEventListener('input', (event) => {
    if (event.target !== scanInput) clearTimeout(closeTimer);
  });

  /* Окно закрылось — чем угодно: сам, крестиком, решением. Поле ждёт следующий пакет. */
  modal.addEventListener('mark:closed', () => {
    clearTimeout(closeTimer);
    session = null;
    zoneInput.focus();
  });
})();
