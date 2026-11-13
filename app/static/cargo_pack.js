/* Те же карточки сборщика. В режиме редактирования добавляются только ручки и зона переноса. */
const CargoPack = (() => {
  let editor = null;
  let requestBusy = false;
  let counter = 0;
  let current = null;
  const controls = document.getElementById('pack-cargo-controls');
  const button = document.getElementById('btn-cargo');
  const keyOf = (state) => `${state?.active?.account_id || ''}:${state?.market || ''}:${state?.active?.id || state?.active?.posting_number || ''}`;
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const newBox = () => ({ id: `new-${Date.now().toString(36)}-${++counter}`, units: [] });

  function reset() {
    editor = null;
    document.getElementById('scan').readOnly = false;
  }

  function syncControls(state) {
    current = state;
    if (editor && (!state?.active || editor.key !== keyOf(state))) reset();
    controls.hidden = !state?.active || !state.cargo?.editable;
    button.textContent = editor ? 'Сохранить' : '＋ Добавить грузоместо';
    button.classList.toggle('primary', Boolean(editor));
    button.disabled = requestBusy || Boolean(state?.marking?.poll);
    button.title = editor ? 'Сохранить. Esc — отменить изменения.' : 'Добавить грузоместо и перетащить в него товар';
    document.getElementById('scan').readOnly = Boolean(editor || requestBusy);
  }

  function itemFor(unit, items) {
    const base = items.find((i) => String(i.key) === unit.key);
    if (!base) return null;
    const scanned = Number(base.scanned || 0) >= unit.unit ? 1 : 0;
    return { ...base, need: 1, scanned, ok: Boolean(scanned), marking: unit.marking || base.marking };
  }

  function render(state, items, next, renderItem, step) {
    syncControls(state);
    const plan = state.cargo;
    if (!plan?.units?.length || !editor && plan.boxes.length < 2) {
      return items.map((item, index) => renderItem(item, index === next)).join('') + step;
    }
    const boxes = editor?.boxes || plan.boxes;
    const units = new Map(plan.units.map((unit) => [unit.id, unit]));
    const nextUnit = plan.units.find((unit) => unit.key === String(items[next]?.key)
      && unit.unit === Number(items[next]?.scanned || 0) + 1);
    let html = '';
    boxes.forEach((box, index) => {
      const shared = (id) => boxes.filter((b) => b.units.includes(id)).length > 1;
      const cards = box.units.map((id) => {
        const unit = units.get(id);
        const item = unit && itemFor(unit, items);
        if (!item) return '';
        const pending = state.marking?.pending_key === unit.key && state.marking?.pending_unit === unit.unit;
        return `<div class="cargo-member" data-cargo-unit="${escapeHtml(id)}" data-cargo-from="${escapeHtml(box.id)}" ${editor ? 'draggable="true"' : ''}>
          ${editor ? '<span class="cargo-handle" aria-hidden="true">⠿</span>' : ''}
          ${renderItem(item, pending || !item.ok && unit.id === nextUnit?.id)}
          ${shared(id) ? '<span class="cargo-part">Часть товара · ШК и ЧЗ сканируются один раз</span>' : ''}
        </div>`;
      }).join('');
      const checked = plan.checked?.includes(box.barcode);
      const title = index ? `<div class="cargo-title">${box.units.length ? 'Грузоместо' : 'Новое грузоместо'} ${index + 1}${checked ? ' <span class="badge ok">✓</span>' : ''}</div>` : '';
      html += `<section class="cargo-place ${index ? 'cargo-extra' : ''}" data-cargo-box="${escapeHtml(box.id)}">
        ${title}<div class="cargo-drop ${!box.units.length ? 'cargo-empty' : ''}">${cards || '<span>Перетащите товар сюда</span>'}</div></section>`;
    });
    if (!editor) html += step;
    return html;
  }

  function move(unit, from, to) {
    const source = editor.boxes.find((box) => box.id === from);
    const target = editor.boxes.find((box) => box.id === to);
    if (!source || !target || from === to || !source.units.includes(unit)) return;
    if (target.units.includes(unit)) {
      source.units = source.units.filter((id) => id !== unit);
    } else if (!target.units.length && source.units.length === 1) {
      // Одна товарная единица физически едет в нескольких коробках.
      target.units.push(unit);
    } else {
      const partial = (id) => editor.boxes.filter((box) => box.units.includes(id)).length > 1;
      if (partial(unit) || target.units.some(partial)) {
        toast('Части товара должны находиться в отдельных грузоместах', 'error');
        return;
      }
      source.units = source.units.filter((id) => id !== unit);
      target.units.push(unit);
    }
    editor.changed = true;
    renderActive(current);
  }

  document.getElementById('pack-items').addEventListener('dragstart', (event) => {
    const item = event.target.closest('[data-cargo-unit]');
    if (!editor || !item) { event.preventDefault(); return; }
    editor.drag = { unit: item.dataset.cargoUnit, from: item.dataset.cargoFrom };
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('application/x-ozon-pack-unit', item.dataset.cargoUnit);
    item.classList.add('cargo-dragging');
  });
  document.getElementById('pack-items').addEventListener('dragover', (event) => {
    const box = event.target.closest('[data-cargo-box]');
    if (!editor?.drag || !box) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    box.classList.add('cargo-over');
  });
  document.getElementById('pack-items').addEventListener('dragleave', (event) => {
    const box = event.target.closest('[data-cargo-box]');
    if (box && !box.contains(event.relatedTarget)) box.classList.remove('cargo-over');
  });
  document.getElementById('pack-items').addEventListener('drop', (event) => {
    const box = event.target.closest('[data-cargo-box]');
    if (!editor?.drag || !box) return;
    event.preventDefault();
    const drag = editor.drag;
    editor.drag = null;
    move(drag.unit, drag.from, box.dataset.cargoBox);
  });
  document.getElementById('pack-items').addEventListener('dragend', () => {
    document.querySelectorAll('.cargo-dragging,.cargo-over').forEach((el) => el.classList.remove('cargo-dragging', 'cargo-over'));
    if (editor) editor.drag = null;
  });

  button.addEventListener('click', async () => {
    if (requestBusy) return;
    requestBusy = true;
    const saving = Boolean(editor);
    const reserved = saving ? reservePrintWindow() : null;
    syncControls(current);
    try {
      if (!saving) {
        const result = await api('/api/pack/cargo/open', {});
        const state = result.state;
        editor = { key: keyOf(state), revision: state.cargo.revision, boxes: clone(state.cargo.boxes), changed: false };
        editor.boxes.push(newBox());
        renderActive(state);
      } else {
        const result = await api('/api/pack/cargo/save', { revision: editor.revision,
          boxes: editor.boxes.map((box) => ({ id: box.id, units: box.units })) });
        reset();
        renderActive(result.state);
        toast(result.state.cargo.status === 'draft' ? 'Раскладка сохранена. Отсканируйте ЧЗ товаров.' : 'Грузоместа подтверждены Маркетом', 'ok');
        if (result.autoprint) await printLabel(pack.activeId(result.state.active), reserved);
        else reserved?.close();
      }
    } catch (error) {
      reserved?.close();
      toast(error.message, 'error', 12000);
    } finally {
      requestBusy = false;
      if (current?.active) renderActive(current);
      else syncControls(current);
      paintZone();
      if (!editor) document.getElementById('scan').focus();
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && editor && !requestBusy) {
      event.preventDefault();
      reset();
      renderActive(current);
      refreshState();
    }
  });
  window.addEventListener('beforeunload', (event) => {
    if (editor?.changed) { event.preventDefault(); event.returnValue = ''; }
  });
  return { render, syncControls, editing: () => Boolean(editor || requestBusy) };
})();
