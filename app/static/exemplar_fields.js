/* Required extra information belongs to its physical item, separately from the CZ scanner. */
const ExemplarFields = (() => {
  let drafts = new Map();
  let saving = false;
  let order = '';
  let seen = new Set();
  const editing = () => saving || drafts.size > 0 || Boolean(document.activeElement?.closest('.pack-exemplar-data'));
  function begin(key) {
    if (key !== order) { drafts.clear(); order = key; }
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
      ${u.fields.map((f) => `<label class="pack-exemplar-field">
        <span class="sr-only">${escapeHtml(f.label)}</span>
        <input type="text" name="${escapeHtml(f.name)}" data-unit="${u.unit}" placeholder="${escapeHtml(f.label)}"
          aria-label="${escapeHtml(f.label)}" value="${escapeHtml(String(drafts.get(`${data.key}:${u.unit}:${f.name}`) ?? f.value))}" maxlength="150"
          ${f.name === 'weight' || f.name === 'uin' || f.name === 'imei' ? 'inputmode="numeric"' : ''}
          required ${disabled ? 'disabled' : ''}>
        ${f.error ? `<span class="err small">${escapeHtml(f.error)}</span>` : ''}
      </label>`).join('')}`).join('');
    const message = pending ? 'Проверяется площадкой…' : data.status === 'accepted' ? 'Данные приняты площадкой' : '';
    return `<form class="pack-exemplar-data" data-key="${escapeHtml(data.key)}" data-revision="${data.revision}">
      ${fields}<div class="pack-exemplar-actions"><button class="btn small" type="submit" ${disabled ? 'disabled' : ''}>Передать данные</button>
      <span class="muted small" role="status">${message}</span></div></form>`;
  }
  document.addEventListener('input', (e) => {
    const form = e.target.closest('.pack-exemplar-data');
    if (form) drafts.set(`${form.dataset.key}:${e.target.dataset.unit}:${e.target.name}`, e.target.value);
  });
  document.addEventListener('submit', async (e) => {
    const form = e.target.closest('.pack-exemplar-data');
    if (!form) return;
    e.preventDefault();
    if (saving || busy || CargoPack.editing()) return;
    const rows = new Map();
    form.querySelectorAll('input').forEach((el) => {
      const unit = Number(el.dataset.unit);
      if (!rows.has(unit)) rows.set(unit, { key: form.dataset.key, unit, fields: {} });
      rows.get(unit).fields[el.name] = el.value;
    });
    saving = true;
    form.querySelector('button').disabled = true;
    try {
      const result = await api(at('/api/pack/exemplar-data'), {
        revision: Number(form.dataset.revision), items: [...rows.values()],
      });
      form.querySelectorAll('input').forEach((el) => drafts.delete(`${form.dataset.key}:${el.dataset.unit}:${el.name}`));
      saving = false;
      renderActive(result.state);
      input.focus();
      toast(result.message, 'ok');
    } catch (error) {
      toast(error.message, 'error', 10000);
    } finally {
      saving = false;
      if (form.isConnected) form.querySelector('button').disabled = false;
    }
  });
  return { begin, render, editing };
})();
