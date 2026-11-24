const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const window = {};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../app/static/pack_label_queue.js'), 'utf8'), { window });
const keyOf = (state) => `${state.market}:${state.active.account_id}:${state.active.id}`;
const state = (market, ready = false) => ({ market, active: { account_id: 5, id: '123' }, sticker_download: { ready } });
const closeWindow = (popup) => { if (popup) popup.closed = true; };

for (const market of ['ozon', 'yandex']) {
  test(`${market}: ready product scan prints once across repeated polls`, async () => {
    let calls = 0;
    let complete;
    const queue = new window.PackLabelQueue({ keyOf, closeWindow, print: async () => {
      calls += 1;
      return new Promise((resolve) => { complete = resolve; });
    } });
    const ready = state(market, true);
    queue.request(ready);
    const first = queue.flush(ready);
    await queue.flush(ready);
    assert.equal(calls, 1);
    complete(true);
    assert.equal(await first, true);
    await queue.flush(ready);
    assert.equal(calls, 1);
  });

  test(`${market}: marking wait retains the original browser window`, async () => {
    const popup = { closed: false };
    const calls = [];
    const queue = new window.PackLabelQueue({ keyOf, closeWindow, print: async (order, reserved) => {
      calls.push([order, reserved]);
      return true;
    } });
    const waiting = state(market);
    queue.request(waiting, popup);
    await queue.flush(waiting);
    await queue.flush(waiting);
    assert.equal(calls.length, 0);
    assert.equal(popup.closed, false);
    const ready = state(market, true);
    await queue.flush(ready);
    assert.equal(calls.length, 1);
    assert.equal(calls[0][1], popup);
    await queue.flush(ready);
    assert.equal(calls.length, 1);
  });
}

test('failed printing remains queued and retries with a delay', async () => {
  let now = 0;
  let calls = 0;
  const queue = new window.PackLabelQueue({ keyOf, closeWindow, now: () => now,
    print: async () => (++calls > 1) });
  const ready = state('yandex', true);
  queue.request(ready);
  assert.equal(await queue.flush(ready), false);
  now = 5000;
  await queue.flush(ready);
  assert.equal(calls, 1);
  now = 10000;
  assert.equal(await queue.flush(ready), true);
  await queue.flush(ready);
  assert.equal(calls, 2);
});

test('cancelling an assembly closes its unused window and cancels its print', async () => {
  let calls = 0;
  const popup = { closed: false };
  const queue = new window.PackLabelQueue({ keyOf, closeWindow, print: async () => { calls += 1; return true; } });
  queue.request(state('yandex'), popup);
  queue.clear();
  assert.equal(popup.closed, true);
  await queue.flush(state('yandex', true));
  assert.equal(calls, 0);
});

test('cancel while waiting for a marking duplicate prevents dispatch', async () => {
  let release;
  let dispatched = 0;
  const queue = new window.PackLabelQueue({ keyOf, closeWindow, print: async (_order, _popup, current) => {
    await new Promise((resolve) => { release = resolve; });
    if (!current()) return false;
    dispatched += 1;
    return true;
  } });
  const ready = state('yandex', true);
  queue.request(ready);
  const pending = queue.flush(ready);
  queue.clear();
  release();
  await pending;
  assert.equal(dispatched, 0);
  assert.equal(queue.jobs.size, 0);
});

test('a matching order number in another account or marketplace cannot print this job', async () => {
  let calls = 0;
  const queue = new window.PackLabelQueue({ keyOf, closeWindow, print: async () => { calls += 1; return true; } });
  queue.request(state('yandex'));
  await queue.flush(state('ozon', true));
  const other = state('yandex', true);
  other.active.account_id = 6;
  await queue.flush(other);
  assert.equal(calls, 0);
  await queue.flush(state('yandex', true));
  assert.equal(calls, 1);
});
