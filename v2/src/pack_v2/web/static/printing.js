/* A local dispatch needs a one-use server permission; never retry qz.print. */
(() => {
  const csrf = document.querySelector('meta[name=csrf-token]')?.content;
  const queued = new Set(), observed = new Set();
  let tail = Promise.resolve(), library, connecting, prepared = false, planning = false, latest, signature = '';
  const uuid = () => {
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16)); b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    const h = [...b].map(v => v.toString(16).padStart(2, '0')).join('');
    return `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`;
  };
  async function json(path, payload) {
    const response = await fetch('/api/v2/printing' + path, {
      method: payload === undefined ? 'GET' : 'POST', signal: AbortSignal.timeout(20000),
      headers: {'Content-Type':'application/json','X-CSRF-Token':csrf},
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) { const error = new Error(typeof data.detail === 'string' ? data.detail : 'Проверьте параметры печати'); error.status = response.status; throw error; }
    return data;
  }
  function within(promise, ms) {
    let timer;
    return Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('QZ Tray не отвечает')), ms); })]).finally(() => clearTimeout(timer));
  }
  function loadQz() {
    if (window.qz) return Promise.resolve(window.qz);
    if (!library) library = new Promise((resolve, reject) => {
      const script = document.createElement('script'); script.src = '/static-v2/vendor/qz-tray.js';
      script.onload = () => window.qz ? resolve(window.qz) : reject(new Error('QZ Tray не загружен'));
      script.onerror = () => { library = null; reject(new Error('QZ Tray не загружен')); };
      document.head.append(script);
    });
    return library;
  }
  async function qzConnect() {
    const qz = await loadQz();
    if (!prepared) {
      qz.security.setCertificatePromise((resolve, reject) => {
        fetch('/api/v2/printing/qz/certificate').then(r => r.ok ? r.text() : Promise.reject(new Error('Нет сертификата панели'))).then(resolve, reject);
      });
      qz.security.setSignatureAlgorithm('SHA512');
      qz.security.setSignaturePromise(value => (resolve, reject) => {
        fetch('/api/v2/printing/qz/sign', {method:'POST', headers:{'Content-Type':'text/plain','X-CSRF-Token':csrf}, body:value})
          .then(r => r.ok ? r.text() : Promise.reject(new Error('Подпись не выдана'))).then(resolve, reject);
      });
      prepared = true;
    }
    if (!qz.websocket.isActive()) {
      if (!connecting) connecting = within(qz.websocket.connect({host:'localhost', usingSecure:location.protocol === 'https:', retries:0}), 8000).finally(() => { connecting = null; });
      await connecting;
    }
    return qz;
  }
  async function printers() {
    const qz = await qzConnect();
    const names = await within(qz.printers.find(), 8000);
    return Array.isArray(names) ? names : [names].filter(Boolean);
  }
  const base64 = blob => new Promise((resolve, reject) => {
    const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.onerror = reject; reader.readAsDataURL(blob);
  });
  async function browserFrame(file) {
    const url = URL.createObjectURL(file), frame = document.createElement('iframe');
    frame.className = 'printing-frame'; frame.title = 'Документ для печати';
    const ready = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Не удалось открыть PDF')), 10000);
      frame.onload = () => { clearTimeout(timer); resolve(); };
      frame.onerror = () => { clearTimeout(timer); reject(new Error('Не удалось открыть PDF')); };
    });
    frame.src = url; document.body.append(frame);
    const cleanup = () => { frame.remove(); URL.revokeObjectURL(url); };
    try { await ready; return {frame, cleanup}; } catch (error) { cleanup(); throw error; }
  }
  const messages = {
    submitted:['Задание отправлено в QZ Tray.',''], browser:['Открыто окно печати браузера. Подтвердите печать.',''],
    dispatching:['Результат печати не подтверждён. Проверьте этикетку; повторная печать — вручную.','warn'],
    uncertain:['Результат печати не подтверждён. Проверьте этикетку; повторная печать — вручную.','warn'],
    failed:['Не удалось отправить документ. Повторите печать вручную.','error'],
  };
  async function deliveryState(job, state) {
    for (let attempt = 0; state === 'dispatching' && attempt < 5; attempt++) {
      await new Promise(resolve => setTimeout(resolve,500));
      try { state = (await json(`/jobs/${job.id}`)).state; } catch (_) { break; }
    }
    return state;
  }
  async function deliver(job, report) {
    if (job.state !== 'prepared') { if (!observed.has(job.id)) { observed.add(job.id); const state = await deliveryState(job,job.state); report?.(...(messages[state] || messages.uncertain)); } return; }
    let started = false, browser, deliveryToken = null;
    try {
      report?.('Готовим документ к печати…', '');
      const response = await fetch(job.url, {signal:AbortSignal.timeout(20000)});
      if (!response.ok || !response.headers.get('content-type')?.startsWith('application/pdf')) {
        let detail; try { detail = (await response.json()).detail; } catch (_) { /* No document was sent. */ }
        throw new Error(typeof detail === 'string' ? detail : 'Не удалось получить документ для печати');
      }
      const file = await response.blob();
      if (!file.size || file.size > 16777216 || await file.slice(0,5).text() !== '%PDF-') throw new Error('Файл печати повреждён');
      const row = job.profile; let qz = null;
      if (row.printer) {
        try { qz = await qzConnect(); if (!(await printers()).includes(row.printer)) qz = null; }
        catch (_) { qz = null; }
      }
      const data = qz ? await base64(file) : null;
      if (!qz) browser = await browserFrame(file);
      // No fallback is allowed after a dispatch permission or a local call may have succeeded.
      started = true;
      const permission = await json(`/jobs/${job.id}/start`, {dispatch_token:job.dispatch_token});
      if (!permission.dispatch) { const state = await deliveryState(job,permission.state); report?.(...(messages[state] || messages.uncertain)); return; }
      deliveryToken = permission.delivery_token;
      let state;
      if (qz) {
        const options = {units:'mm', size:{width:row.width, height:row.height + row.gap}, margins:0, scaleContent:false, copies:1};
        // The server already applied orientation to this PDF and its effective paper size.
        // QZ determines it from the prepared page; applying it twice could rotate it again.
        await within(qz.print(qz.configs.create(row.printer, options), [{type:'pixel',format:'pdf',flavor:'base64',data}]), 20000);
        state = 'submitted';
      } else {
        browser.frame.contentWindow.print(); state = 'browser';
        const view = browser; browser = null;
        view.frame.contentWindow.addEventListener('afterprint', view.cleanup, {once:true});
        setTimeout(view.cleanup, 300000);
      }
      await json(`/jobs/${job.id}/finish`, {delivery_token:deliveryToken, state});
      observed.add(job.id); report?.(...messages[state]);
    } catch (error) {
      if (started && !deliveryToken && error.status < 500) started = false;
      const state = started ? 'uncertain' : 'failed';
      try { if (!started || deliveryToken) await json(`/jobs/${job.id}/finish`, {delivery_token:deliveryToken || job.dispatch_token,state}); } catch (_) { /* Persisted dispatches still prevent retries. */ }
      observed.add(job.id); report?.(started ? messages.uncertain[0] : `${error.message || 'Нет связи с панелью'}. Повторите печать вручную.`, started ? 'warn' : 'error');
    } finally { browser?.cleanup(); }
  }
  function enqueue(job, report) {
    if (!job || queued.has(job.id)) return tail;
    queued.add(job.id);
    tail = tail.catch(() => {}).then(() => deliver(job, report)).finally(() => queued.delete(job.id));
    return tail;
  }
  function manual(path, payload, report) {
    const task = tail.catch(() => {}).then(async () => {
      try { const job = await json(path, {request_id:uuid(), ...payload}); await deliver(job, report); }
      catch (error) { report?.(error.message || 'Не удалось подготовить печать', 'error'); }
    });
    tail = task; return task;
  }
  function sync(session, report) {
    if (!session || !['scanning','preparing','ready'].includes(session.status)) { latest = null; return; }
    const key = JSON.stringify([session.id,session.revision,session.data?.status,(session.labels || []).map(l => [l.key,l.status])]);
    latest = {session, report, key};
    if (planning || key === signature) return;
    planning = true; signature = key;
    json(`/assembly/${session.id}`, {}).then(data => {
      if (latest?.session.id === session.id) for (const job of data.jobs) enqueue(job, (text,state) => { if (latest?.session.id === session.id) report?.(text,state); });
    }).catch(error => { report?.(error.message || 'Печать недоступна. Повторите вручную.', 'error'); })
      .finally(() => { planning = false; if (latest && latest.key !== signature) sync(latest.session, latest.report); });
  }
  window.PackPrinting = {uuid, json, printers, sync,
    label:(label, sessionId, report) => manual('/label', {label_id:label.id,generation:label.generation,...(sessionId ? {session_id:sessionId} : {})}, report),
    marking:(labelId, assignments, report) => manual('/marking', {label_id:labelId,assignments}, report),
    test:(profile, report) => manual('/test', {profile}, report),
  };
})();
