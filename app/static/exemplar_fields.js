/* Required extra information belongs to its physical item, separately from the CZ scanner. */
const ExemplarFields = (() => {
  let drafts = new Map();
  let originals = new Map();
  let saving = false;
  let order = '';
  let seen = new Set();
  // An omitted GTD keeps its text for unchecking, but that text is not submitted.
  const dirty = () => [...drafts].some(([key, value]) => {
    if (value === originals.get(key)) return false;
    const omission = key.replace(/:gtd$/, ':gtd_absent');
    return !key.endsWith(':gtd') || !(drafts.get(omission) ?? originals.get(omission));
  });
  const editing = () => saving || dirty() || Boolean(document.activeElement?.closest('.pack-exemplar-data'));
  function draft(key, value) {
    if (value === originals.get(key)) drafts.delete(key);
    else drafts.set(key, value);
  }
  function begin(key) {
    if (key !== order) { drafts.clear(); originals.clear(); order = key; }
    seen = new Set();
  }
  function render(data) {
    if (!data?.units?.length) return '';
    const units = data.units.filter((u) => !seen.has(`${data.key}:${u.unit}`));
    units.forEach((u) => seen.add(`${data.key}:${u.unit}`));
    if (!units.length) return '';
    const pending = ['queued', 'checking', 'uncertain'].includes(data.status);
    const disabled = pending || CargoPack.editing();
    const fields = units.map((u) => `${data.units.length > 1 ? `<div class="muted small">Единица ${u.unit}</div>` : ''}
      ${u.fields.map((f) => {
        originals.set(`${data.key}:${u.unit}:${f.name}`, String(f.value));
        if (f.can_omit) originals.set(`${data.key}:${u.unit}:gtd_absent`, Boolean(f.omitted));
        const omitted = f.can_omit && (drafts.get(`${data.key}:${u.unit}:gtd_absent`) ?? f.omitted);
        return `<div class="pack-exemplar-field">
        <span class="sr-only">${escapeHtml(f.label)}</span>
        <input type="text" name="${escapeHtml(f.name)}" data-unit="${u.unit}" placeholder="${escapeHtml(f.label)}"
          aria-label="${escapeHtml(f.label)}" value="${escapeHtml(String(drafts.get(`${data.key}:${u.unit}:${f.name}`) ?? f.value))}" maxlength="150"
          ${f.name === 'weight' || f.name === 'imei' ? 'inputmode="numeric"' : ''}
          ${omitted ? '' : 'required'} ${disabled || omitted ? 'disabled' : ''}>
        ${f.can_omit ? `<label class="pack-exemplar-omit muted small">
          <input type="checkbox" data-gtd-absent data-unit="${u.unit}" aria-label="Не указывать ГТД"
            ${omitted ? 'checked' : ''} ${disabled ? 'disabled' : ''}>Не указывать</label>` : ''}
        ${f.error ? `<span class="err small">${escapeHtml(f.error)}</span>` : ''}
      </div>`;
      }).join('')}`).join('');
    const message = pending ? 'Проверяется площадкой…' : data.status === 'accepted' ? 'Данные приняты площадкой' : '';
    return `<form class="pack-exemplar-data" data-key="${escapeHtml(data.key)}" data-revision="${data.revision}">
      ${fields}<div class="pack-exemplar-actions"><button class="btn small" type="submit" ${disabled ? 'disabled' : ''}>Передать данные</button>
      <span class="muted small" role="status">${message}</span></div></form>`;
  }
  document.addEventListener('input', (e) => {
    const form = e.target.closest('.pack-exemplar-data');
    if (form && e.target.name) draft(`${form.dataset.key}:${e.target.dataset.unit}:${e.target.name}`, e.target.value);
  });
  document.addEventListener('change', (e) => {
    const toggle = e.target;
    const form = toggle.closest('.pack-exemplar-data');
    if (!form || !toggle.hasAttribute('data-gtd-absent')) return;
    draft(`${form.dataset.key}:${toggle.dataset.unit}:gtd_absent`, toggle.checked);
    const field = toggle.closest('.pack-exemplar-field').querySelector('input[name="gtd"]');
    field.disabled = toggle.checked;
    field.required = !toggle.checked;
  });
  document.addEventListener('submit', async (e) => {
    const form = e.target.closest('.pack-exemplar-data');
    if (!form) return;
    e.preventDefault();
    if (saving || busy || CargoPack.editing()) return;
    const rows = new Map();
    form.querySelectorAll('input[name]').forEach((el) => {
      const unit = Number(el.dataset.unit);
      if (!rows.has(unit)) rows.set(unit, { key: form.dataset.key, unit, fields: {} });
      rows.get(unit).fields[el.name] = el.value;
    });
    form.querySelectorAll('[data-gtd-absent]').forEach((el) => {
      const row = rows.get(Number(el.dataset.unit));
      row.gtd_absent = el.checked;
      if (el.checked) row.fields.gtd = '';
    });
    saving = true;
    const controls = [...form.querySelectorAll('input, button')].map((el) => [el, el.disabled]);
    controls.forEach(([el]) => { el.disabled = true; });
    try {
      const result = await api(at('/api/pack/exemplar-data'), {
        revision: Number(form.dataset.revision), items: [...rows.values()],
      });
      form.querySelectorAll('input[name]').forEach((el) => drafts.delete(`${form.dataset.key}:${el.dataset.unit}:${el.name}`));
      form.querySelectorAll('[data-gtd-absent]').forEach((el) => drafts.delete(`${form.dataset.key}:${el.dataset.unit}:gtd_absent`));
      saving = false;
      renderActive(result.state);
      input.focus();
      toast(result.message, 'ok');
    } catch (error) {
      toast(error.message, 'error', 10000);
    } finally {
      saving = false;
      if (form.isConnected) controls.forEach(([el, disabled]) => { el.disabled = disabled; });
    }
  });
  return { begin, render, editing };
})();
