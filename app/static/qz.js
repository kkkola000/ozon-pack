/* Печать через QZ Tray — второй путь рядом с браузерным.

   QZ Tray — программа на компьютере склада. Она принимает PDF и отправляет его
   на названный принтер сразу, без окна печати браузера. Настройка — на странице
   «Принтеры»: для каждого документа (стикеры Ozon, ярлыки Маркета, этикетки
   Avito, лист и акт возвратов) — размер листа и принтер. Она приходит в
   страницу как window.PRINTERS. Пустой принтер — печать через браузер, как
   было всегда.

   У документа бывает несколько размеров: этикетка Avito приходит то 58×40, то
   100×150. Сервер пишет настоящий размер листа в заголовок X-Page-Size, и по
   нему выбирается строка; не совпала ни одна — берётся первая.

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

  let library = null;     // обещание загрузки qz-tray.js
  let prepared = false;   // подпись настроена
  let connecting = null;  // обещание подключения — одно на все одновременные печати

  const setup = () => window.PRINTERS || {};
  const rowsOf = (kind) => (setup().rows || []).filter((row) => row.kind === kind);

  /* Есть ли у документа хоть один принтер QZ Tray — стоит ли вообще пробовать. */
  function hasPrinter(kind) {
    return rowsOf(kind).some((row) => row.printer);
  }

  /* Все размеры документа печатаются через QZ Tray — окно браузера не нужно. */
  function onlyQz(kind) {
    const rows = rowsOf(kind);
    return rows.length > 0 && rows.every((row) => row.printer);
  }

  /* Строка настройки под файл: документ и размер листа («58x40» из заголовка
     X-Page-Size). Размер сравниваем с допуском и без учёта поворота: 58×40 и
     40×58 — одна и та же лента. */
  function pick(kind, measured) {
    const rows = rowsOf(kind);
    if (!rows.length) return null;
    const [width, height] = String(measured || '').split('x').map(Number);
    if (width && height) {
      const paper = setup().paper || {};
      const slack = setup().match_mm || 5;
      const near = (a, b) => Math.abs(a - b) <= slack;
      const fits = rows.find((row) => {
        const [w, h] = paper[row.size] || [];
        return (near(w, width) && near(h, height)) || (near(w, height) && near(h, width));
      });
      if (fits) return fits;
    }
    return rows[0];
  }

  /* Параметры бумаги для QZ Tray. Наклейке размер задаём явно: без него драйвер
     термопринтера берёт свой лист по умолчанию. A4 — на то, что стоит в
     принтере: офисный принтер и так знает свой лист. */
  function paperFor(size) {
    const mm = (setup().paper || {})[size];
    if (size === 'a4' || !mm) return { scaleContent: true };
    return { size: { width: mm[0], height: mm[1] }, units: 'mm', margins: 0, scaleContent: true };
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
    return { data: await toBase64(await response.blob()), size: response.headers.get('X-Page-Size') };
  }

  /* Напечатать документ. Возвращает имя принтера, false — этот документ (или
     этот его размер) печатается через браузер. Ошибка — QZ Tray не смог.

     pageSize передают те, кто скачал файл сам (пачка стикеров в «Заказах»):
     из blob-адреса заголовок уже не прочитать. */
  async function printPdf(url, kind, pageSize = null) {
    if (!hasPrinter(kind)) return false;
    const file = await fetchPdf(url);
    const row = pick(kind, pageSize || file.size);
    if (!row || !row.printer) return false;
    const qz = await connect();
    const config = qz.configs.create(row.printer, paperFor(row.size));
    try {
      await qz.print(config, [{ type: 'pixel', format: 'pdf', flavor: 'base64', data: file.data }]);
    } catch (error) {
      throw plain(error, row.printer);
    }
    return row.printer;
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

  /* Пробная страница со страницы «Принтеры»: принтер ещё не сохранён — берём выбранный. */
  async function printTest(printer, size, title) {
    const qz = await connect();
    const config = qz.configs.create(printer, paperFor(size));
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

  return { hasPrinter, onlyQz, pick, connect, version, printers, printPdf, printTest };
})();

/* Листы A4 в «Возвратах»: ссылка «Печать листа» открывает страницу печати в
   браузере. Если для документа выбран принтер QZ Tray, тот же лист уходит на
   него PDF-файлом, а ссылка не открывается. Не вышло — даём открыть страницу
   печати как раньше. */
document.addEventListener('click', async (event) => {
  const link = event.target.closest('a[data-print-pdf]');
  if (!link) return;
  const kind = link.dataset.printKind;
  if (!kind || !QZ.hasPrinter(kind)) return;
  event.preventDefault();
  const name = link.dataset.printName || 'Лист';
  try {
    const printer = await QZ.printPdf(link.dataset.printPdf, kind);
    if (printer) {
      toast(`${name}: отправлен на принтер «${printer}»`, 'ok', 4000);
    } else {
      offerManualPrint(link.href, name, 'этот размер печатается через браузер.');
    }
  } catch (error) {
    if (error.fromServer) {
      toast(`${name}: ${error.message}`, 'error', 15000);
      return;
    }
    offerManualPrint(link.href, name, `QZ Tray: ${error.message}.`);
  }
});
