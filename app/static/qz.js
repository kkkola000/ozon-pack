/* Печать через QZ Tray — второй путь рядом с браузерным.

   QZ Tray — программа на компьютере склада. Она принимает PDF и отправляет его
   на названный принтер сразу, без окна печати браузера. Какой принтер на какой
   размер листа, выбирает владелец в «Настройки → Настройка принтеров»; выбор
   приходит в страницу как window.PRINTERS. Пусто — печать через браузер, как
   было всегда.

   Главное правило: из-за QZ Tray печать не должна пропасть. Не запущена
   программа, нет на компьютере принтера с таким именем, отказал сам принтер —
   вызывающий код получает ошибку и печатает через браузер. Исключение одно:
   если сама панель не отдала файл (площадка не выдала стикер), браузеру
   печатать тоже нечего, и об этом говорится прямо.

   Библиотека qz-tray.js подгружается только при первой печати через QZ Tray:
   на компьютерах, где его нет, она не нужна. Запросы подписывает сервер
   панели — без подписи QZ Tray спрашивал бы разрешение на каждое подключение. */
const QZ = (() => {
  const LIBRARY = '/static/vendor/qz-tray.js?v=2.3.0';

  /* Параметры бумаги для каждого размера листа. Код размера — тот же, что в
     core/printers.py. Наклейке задаём размер явно: без него драйвер термо-
     принтера берёт свой лист по умолчанию. A4 печатается на то, что стоит в
     принтере, — офисный принтер и так знает свой лист. */
  const PAPER = {
    label: { size: { width: 75, height: 120 }, units: 'mm', margins: 0, scaleContent: true },
    a4: { scaleContent: true },
  };

  let library = null;     // обещание загрузки qz-tray.js
  let prepared = false;   // подпись настроена
  let connecting = null;  // обещание подключения — одно на все одновременные печати

  function printerFor(size) {
    return ((window.PRINTERS || {})[size] || '').trim();
  }

  function load() {
    if (window.qz) return Promise.resolve(window.qz);
    if (!library) {
      library = new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = LIBRARY;
        script.onload = () => resolve(window.qz);
        script.onerror = () => { library = null; reject(new Error('библиотека QZ Tray не загрузилась')); };
        document.head.appendChild(script);
      });
    }
    return library;
  }

  /* Подпись: сертификат панели и подпись каждого запроса берём у сервера.
     Ключ остаётся на сервере, в браузер уходит только готовая подпись. */
  function prepare(qz) {
    if (prepared) return qz;
    prepared = true;
    qz.security.setCertificatePromise((resolve, reject) => {
      fetch('/api/printers/qz/certificate', { cache: 'no-store' })
        .then((response) => (response.ok ? response.text() : Promise.reject(new Error('нет сертификата'))))
        .then(resolve, reject);
    });
    qz.security.setSignatureAlgorithm('SHA512');
    qz.security.setSignaturePromise((toSign) => (resolve, reject) => {
      fetch('/api/printers/qz/sign', {
        method: 'POST',
        cache: 'no-store',
        headers: { 'Content-Type': 'text/plain', 'X-CSRF-Token': CSRF },
        body: toSign,
      })
        .then((response) => (response.ok ? response.text() : Promise.reject(new Error('подпись не выдана'))))
        .then(resolve, reject);
    });
    return qz;
  }

  function within(promise, ms, message) {
    let timer;
    const limit = new Promise((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error(message)), ms);
    });
    return Promise.race([promise, limit]).finally(() => clearTimeout(timer));
  }

  async function connect() {
    const qz = prepare(await load());
    if (qz.websocket.isActive()) return qz;
    if (!connecting) {
      connecting = within(qz.websocket.connect({ retries: 1, delay: 1 }), 10000,
                          'QZ Tray не отвечает')
        .catch((error) => {
          throw new Error(/closed|refused|unable|failed/i.test(String(error?.message || error))
            ? 'QZ Tray не запущен на этом компьютере' : String(error?.message || error));
        })
        .finally(() => { connecting = null; });
    }
    await connecting;
    return qz;
  }

  async function version() {
    const qz = await connect();
    return qz.api.getVersion();
  }

  async function printers() {
    const qz = await connect();
    const found = await qz.printers.find();
    return Array.isArray(found) ? found : [found];
  }

  function toBase64(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '');
      reader.onerror = () => reject(new Error('файл не прочитался'));
      reader.readAsDataURL(blob);
    });
  }

  /* Файл берём сами: ошибку площадки нужно показать как есть, а не отправить
     на принтер страницу с её текстом. */
  async function fetchPdf(url) {
    let response;
    try {
      response = await fetch(url, { headers: { 'X-Requested-With': 'fetch' } });
    } catch (error) {
      const failure = new Error(`не удалось получить файл — ${error.message}`);
      failure.fromServer = true;
      throw failure;
    }
    if (!response.ok) {
      let detail = `Ошибка ${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch (error) { /* не JSON */ }
      const failure = new Error(detail);
      failure.fromServer = true;
      throw failure;
    }
    return toBase64(await response.blob());
  }

  /* Напечатать PDF на принтер своего размера. false — для этого размера
     принтер не выбран, печатать через браузер. Ошибка — QZ Tray не смог. */
  async function printPdf(url, size, printer = printerFor(size)) {
    if (!printer) return false;
    const data = await fetchPdf(url);
    const qz = await connect();
    const config = qz.configs.create(printer, PAPER[size] || {});
    try {
      await qz.print(config, [{ type: 'pixel', format: 'pdf', flavor: 'base64', data }]);
    } catch (error) {
      throw plain(error, printer);
    }
    return true;
  }

  /* QZ Tray отвечает по-английски. Самый частый отказ — принтера с таким
     именем на этом компьютере нет: его и переводим, остальное — как есть. */
  function plain(error, printer) {
    const text = String(error?.message || error);
    if (/could not be found|not found/i.test(text)) {
      return new Error(`принтера «${printer}» нет на этом компьютере`);
    }
    return error instanceof Error ? error : new Error(text);
  }

  /* Пробная страница из настроек: принтер ещё не сохранён — берём выбранный. */
  async function printTest(printer, size, title) {
    const qz = await connect();
    const config = qz.configs.create(printer, PAPER[size] || {});
    const stamp = new Date().toLocaleString('ru-RU');
    const html = `<div style="font:14px sans-serif;padding:4mm">
      <b style="font-size:18px">Ozon Pack</b><br>Пробная печать через QZ Tray<br>
      ${escapeHtml(title)}<br>${escapeHtml(printer)}<br>${escapeHtml(stamp)}</div>`;
    try {
      await qz.print(config, [{ type: 'pixel', format: 'html', flavor: 'plain', data: html }]);
    } catch (error) {
      throw plain(error, printer);
    }
  }

  return { printerFor, connect, version, printers, printPdf, printTest };
})();

/* Листы A4 в «Возвратах»: ссылка «Печать листа» открывает страницу печати в
   браузере. Если владелец выбрал для A4 принтер QZ Tray, тот же лист уходит на
   него PDF-файлом, а ссылка не открывается. Не вышло — даём открыть страницу
   печати как раньше. */
document.addEventListener('click', async (event) => {
  const link = event.target.closest('a[data-print-pdf]');
  if (!link) return;
  const size = link.dataset.printSize || 'a4';
  if (!QZ.printerFor(size)) return;
  event.preventDefault();
  const name = link.dataset.printName || 'Лист';
  try {
    await QZ.printPdf(link.dataset.printPdf, size);
    toast(`${name}: отправлен на принтер «${QZ.printerFor(size)}»`, 'ok', 4000);
  } catch (error) {
    if (error.fromServer) {
      toast(`${name}: ${error.message}`, 'error', 15000);
      return;
    }
    offerManualPrint(link.href, name, `QZ Tray: ${error.message}.`);
  }
});
