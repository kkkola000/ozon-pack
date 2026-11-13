/* Папка на этом компьютере: разрешение и handle хранятся только в браузере.
   На сервер не передаются ни путь, ни файлы из выбранной папки. */
const StickerDownloads = (() => {
  const DB = 'ozon-pack-local-downloads';
  const STORE = 'settings';
  const KEY = 'assembly-sticker-folder';
  const supported = () => window.isSecureContext && typeof window.showDirectoryPicker === 'function';
  let database;
  let queue = Promise.resolve();

  function openDatabase() {
    if (!database) database = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB, 1);
      request.onupgradeneeded = () => request.result.createObjectStore(STORE);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    return database;
  }

  async function folder(value, write = false) {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE, write ? 'readwrite' : 'readonly');
      const store = transaction.objectStore(STORE);
      const request = write ? (value ? store.put(value, KEY) : store.delete(KEY)) : store.get(KEY);
      transaction.oncomplete = () => resolve(write ? value : request.result || null);
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error || new Error('Настройка папки не сохранена'));
    });
  }

  async function showFolder() {
    const field = document.getElementById('sticker-folder');
    if (!field) return;
    const choose = document.getElementById('choose-sticker-folder');
    const reset = document.getElementById('reset-sticker-folder');
    choose.disabled = !supported();
    let chosen = null;
    try { chosen = await folder(); } catch (error) { /* Доступны обычные загрузки браузера. */ }
    field.value = chosen?.name || 'Загрузки браузера';
    field.title = chosen ? 'Выбранная папка на этом компьютере' : 'Папка загрузок из настроек браузера';
    reset.hidden = !chosen;
    document.getElementById('sticker-folder-note').textContent = supported()
      ? 'Только этот браузер и компьютер. Выберите папку и разрешите запись. Полный путь браузер не раскрывает.'
      : 'Выбор папки доступен в Chrome / Edge через HTTPS. Сейчас файлы скачиваются в папку загрузок браузера.';
  }

  document.getElementById('choose-sticker-folder')?.addEventListener('click', async (event) => {
    try {
      // Системный диалог вызывается сразу из нажатия, иначе браузер его запретит.
      const chosen = await window.showDirectoryPicker({ id: 'assembly-stickers', mode: 'readwrite', startIn: 'downloads' });
      if (await chosen.requestPermission({ mode: 'readwrite' }) !== 'granted') {
        throw new Error('Разрешите запись в папку, чтобы сохранять стикеры автоматически');
      }
      event.target.disabled = true;
      await folder(chosen, true);
      toast('Папка для стикеров сохранена на этом компьютере', 'ok');
    } catch (error) {
      if (error.name !== 'AbortError') toast(error.message || 'Не удалось выбрать папку', 'error', 10000);
    } finally { await showFolder(); }
  });
  document.getElementById('reset-sticker-folder')?.addEventListener('click', async () => {
    try { await folder(null, true); await showFolder(); }
    catch (error) { toast('Не удалось сбросить папку', 'error'); }
  });
  showFolder();

  function browserDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }

  async function saveFile(url, filename) {
    const response = await fetch(url, { headers: { 'X-Requested-With': 'fetch' } });
    if (!response.ok) {
      let message = 'Не удалось скачать стикер';
      try { message = (await response.json()).detail || message; } catch (error) { /* Не JSON. */ }
      throw new Error(message);
    }
    const blob = await response.blob();
    if (!response.headers.get('Content-Type')?.includes('application/pdf') || blob.size > 20 * 1024 * 1024
        || await blob.slice(0, 5).text() !== '%PDF-') throw new Error('Панель не отдала PDF стикера');
    filename = filename.replace(/[\x00-\x1f<>:"/\\|?*]/g, '_').slice(0, 180);
    let chosen;
    try { chosen = await folder(); } catch (error) { /* В приватном режиме используем загрузки. */ }
    if (chosen) {
      let writer;
      try {
        if (await chosen.queryPermission({ mode: 'readwrite' }) !== 'granted') {
          throw new Error('Доступ к папке нужно подтвердить в настройках');
        }
        const file = await chosen.getFileHandle(filename, { create: true });
        writer = await file.createWritable();
        await writer.write(blob);
        await writer.close();
        return;
      } catch (error) {
        try { await writer?.abort(); } catch (abortError) { /* Поток уже закрыт. */ }
        toast('Нет записи в выбранную папку. Стикер отправлен в загрузки браузера; выберите папку заново в настройках.', 'error', 12000);
      }
    }
    browserDownload(blob, filename);
  }

  return {
    save(url, filename) {
      queue = queue.catch(() => {}).then(() => saveFile(url, filename));
      return queue;
    },
  };
})();
