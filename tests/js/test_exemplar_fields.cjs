const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function browser() {
  const handlers = {};
  const sent = [];
  const errors = [];
  const document = { activeElement: null, addEventListener: (event, callback) => { handlers[event] = callback; } };
  const context = { document, CargoPack: { editing: () => false }, busy: false,
    escapeHtml: (value) => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;'),
    at: (url) => url, api: async (url, payload) => { sent.push({ url, payload }); return { state: {}, message: 'ok' }; },
    renderActive: () => {}, input: { focus: () => {} }, toast: (...args) => errors.push(args),
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../app/static/exemplar_fields.js'), 'utf8') +
    '\nglobalThis.fields = ExemplarFields;', context);
  context.fields.begin('ozon:1:123');
  return { ...context, context, handlers, sent, errors };
}

function formWithToggle(checked = false) {
  const input = { name: 'gtd', dataset: { unit: '1' }, value: 'draft-number', disabled: checked, required: !checked };
  const toggle = { dataset: { unit: '1' }, checked, disabled: false,
    hasAttribute: (name) => name === 'data-gtd-absent' };
  const button = { disabled: false };
  const form = { dataset: { key: '42', revision: '2' }, isConnected: true,
    querySelectorAll: (selector) => ({
      'input[name]': [input], '[data-gtd-absent]': [toggle], 'input, button': [input, toggle, button],
    })[selector],
  };
  input.closest = () => form;
  toggle.closest = (selector) => selector === '.pack-exemplar-field' ? { querySelector: () => input } : form;
  return { input, toggle, form, button };
}

const data = (status = 'collecting', omitted = false) => ({ key: '42', revision: 2, status, units: [{ unit: 1, fields: [
  { name: 'gtd', label: 'Указать ГТД', value: '', can_omit: true, omitted },
  { name: 'rnpt', label: 'Указать РНПТ', value: '', can_omit: false, omitted: false },
] }] });

test('only GTD has an omission checkbox, with the input required by default', () => {
  const b = browser();
  const html = b.fields.render(data());
  assert.equal((html.match(/type="checkbox"/g) || []).length, 1);
  assert.match(html, /Не указывать<\/label>/);
  assert.match(html, /name="gtd"[^>]+required/);
  assert.doesNotMatch(html, /type="checkbox"[^>]+checked/);
});

test('saved omission disables only GTD and preserves other requirements', () => {
  const html = browser().fields.render(data('accepted', true));
  assert.match(html, /name="gtd"[^>]+disabled/);
  assert.doesNotMatch(html, /name="gtd"[^>]+required/);
  assert.match(html, /name="rnpt"[^>]+required/);
  assert.match(html, /type="checkbox"[^>]+checked/);
});

test('toggle keeps the typed draft, restores required and survives a rerender', () => {
  const b = browser();
  const { input, toggle } = formWithToggle();
  b.handlers.input({ target: input });
  toggle.checked = true;
  b.handlers.change({ target: toggle });
  assert.equal(input.disabled, true);
  assert.equal(input.required, false);
  assert.equal(input.value, 'draft-number');
  assert.equal(b.fields.editing(), true);
  let html = b.fields.render(data());
  assert.match(html, /type="checkbox"[^>]+checked/);
  assert.match(html, /value="draft-number"/);
  toggle.checked = false;
  b.handlers.change({ target: toggle });
  assert.equal(input.disabled, false);
  assert.equal(input.required, true);
  b.fields.begin('ozon:1:123');
  html = b.fields.render(data('collecting', true));
  assert.doesNotMatch(html, /type="checkbox"[^>]+checked/);
});

test('omission submits an explicit choice without the retained number and clears drafts', async () => {
  const b = browser();
  const { input, toggle, form } = formWithToggle(true);
  b.handlers.input({ target: input });
  b.handlers.change({ target: toggle });
  await b.handlers.submit({ target: { closest: () => form }, preventDefault: () => {} });
  assert.equal(b.sent.length, 1);
  const row = b.sent[0].payload.items[0];
  assert.equal(row.gtd_absent, true);
  assert.equal(row.fields.gtd, '');
  assert.equal(b.fields.editing(), false);
});

test('normal GTD submission still sends the number', async () => {
  const b = browser();
  const { form } = formWithToggle();
  await b.handlers.submit({ target: { closest: () => form }, preventDefault: () => {} });
  assert.equal(b.sent[0].payload.items[0].fields.gtd, 'draft-number');
  assert.equal(b.sent[0].payload.items[0].gtd_absent, false);
});

test('pending submission freezes controls and failure restores omission and drafts', async () => {
  const b = browser();
  const { input, toggle, form, button } = formWithToggle(true);
  let reject;
  b.context.api = () => new Promise((_, failure) => { reject = failure; });
  b.handlers.change({ target: toggle });
  const promise = b.handlers.submit({ target: { closest: () => form }, preventDefault: () => {} });
  assert.equal(toggle.disabled, true);
  assert.equal(button.disabled, true);
  reject(new Error('test refusal'));
  await promise;
  assert.equal(toggle.disabled, false);
  assert.equal(button.disabled, false);
  assert.equal(input.disabled, true);
  assert.equal(input.required, false);
  assert.equal(b.fields.editing(), true);
  assert.equal(b.errors[0][0], 'test refusal');
});

for (const status of ['queued', 'checking', 'uncertain']) {
  test(`checkbox is locked while the marketplace is ${status}`, () => {
    const html = browser().fields.render(data(status));
    assert.match(html, /type="checkbox"[^>]+disabled/);
    assert.match(html, /name="gtd"[^>]+disabled/);
  });
}
