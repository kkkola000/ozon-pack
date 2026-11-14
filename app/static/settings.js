/* Компактные настройки используют прежние API кабинетов, сотрудников и отчёта. */
const MANAGER_ONLY = window.SETTINGS_MANAGER_ONLY || ['settings'];
for (const button of document.querySelectorAll('[data-open-dialog]')) {
  button.addEventListener('click', () => document.getElementById(button.dataset.openDialog)?.showModal());
}
for (const button of document.querySelectorAll('[data-close-dialog]')) {
  button.addEventListener('click', () => button.closest('dialog')?.close());
}
for (const dialog of document.querySelectorAll('.settings-dialog')) {
  dialog.addEventListener('click', (event) => {
    if (event.target !== dialog) return;
    const r = dialog.getBoundingClientRect();
    if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) dialog.close();
  });
}
// Обе кнопки вкладки сборки используют общее сохранение в printers.js.

function cabinetCard(id) {
  return document.querySelector(`.cabinet-card[data-account="${id}"]`);
}

document.querySelectorAll('[data-cab-save]').forEach((button) => {
  button.addEventListener('click', async () => {
    const id = button.dataset.cabSave;
    const card = cabinetCard(id);
    const payload = { title: card.querySelector('.cab-title').value.trim() };
    const idField = card.querySelector('.cab-client-id');
    const clientId = idField.value.trim();
    const apiKey = card.querySelector('.cab-api-key').value.trim();
    if (apiKey || clientId !== idField.dataset.original) {
      if (!clientId || !apiKey) { toast('Для изменения ключей заполните идентификатор и ключ API', 'error'); return; }
      payload.client_id = clientId;
      payload.api_key = apiKey;
      payload.skip_test = card.querySelector('.cab-skip-test').checked;
    }
    button.disabled = true;
    button.textContent = 'Сохраняем…';
    try {
      const result = await api(`/api/accounts/${id}`, payload);
      toast(result.message, 'ok', 8000);
      setTimeout(() => window.location.reload(), 1200);
    } catch (error) {
      toast(error.message, 'error', 15000);
      button.disabled = false;
      button.textContent = 'Сохранить';
    }
  });
});

document.querySelectorAll('[data-cab-test]').forEach((button) => {
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { toast((await api(`/api/accounts/${button.dataset.cabTest}/test`, {})).message, 'ok'); }
    catch (error) { toast(error.message, 'error', 12000); }
    finally { button.disabled = false; }
  });
});

document.querySelectorAll('[data-cab-toggle]').forEach((button) => {
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const result = await api(`/api/accounts/${button.dataset.cabToggle}`, { active: button.dataset.active === '1' ? false : true });
      toast(result.message, 'ok');
      setTimeout(() => window.location.reload(), 800);
    } catch (error) {
      toast(error.message, 'error', 10000);
      button.disabled = false;
    }
  });
});

document.querySelectorAll('[data-cab-delete]').forEach((button) => {
  button.addEventListener('click', async () => {
    const card = cabinetCard(button.dataset.cabDelete);
    const title = card.querySelector('.cab-title').value.trim();
    if (!confirm(`Удалить кабинет «${title}» вместе с его заказами, товарами и возвратами? Отменить нельзя.`)) return;
    button.disabled = true;
    try {
      const result = await api(`/api/accounts/${button.dataset.cabDelete}/delete`, {});
      toast(result.message, 'ok', 8000);
      setTimeout(() => window.location.reload(), 1200);
    } catch (error) {
      toast(error.message, 'error', 12000);
      button.disabled = false;
    }
  });
});

const addAccount = document.getElementById('btn-add-account');
if (addAccount) addAccount.onclick = async (event) => {
  const title = document.getElementById('new-title').value.trim();
  if (!title) { toast('Укажите название кабинета', 'error'); return; }
  event.target.disabled = true;
  event.target.textContent = 'Проверяем…';
  try {
    const result = await api('/api/accounts', {
      marketplace: document.getElementById('new-marketplace').value,
      title,
      client_id: document.getElementById('new-client-id').value.trim(),
      api_key: document.getElementById('new-api-key').value.trim(),
      skip_test: document.getElementById('new-skip-test').checked,
    });
    toast(result.message, 'ok', 8000);
    setTimeout(() => window.location.reload(), 1200);
  } catch (error) {
    toast(error.message, 'error', 15000);
    event.target.disabled = false;
    event.target.textContent = 'Проверить и добавить';
  }
};

const addUser = document.getElementById('btn-add');
if (addUser) addUser.onclick = async () => {
  try {
    const result = await api('/api/users', {
      login: document.getElementById('new-login').value,
      password: document.getElementById('new-password').value,
      role: document.getElementById('new-role').value,
    });
    toast(result.message, 'ok');
    setTimeout(() => window.location.reload(), 800);
  } catch (error) { toast(error.message, 'error'); }
};

async function updateUser(id, payload) {
  try {
    const result = await api(`/api/users/${id}`, payload);
    toast(result.message, 'ok');
    setTimeout(() => window.location.reload(), 800);
  } catch (error) { toast(error.message, 'error'); }
}

document.querySelectorAll('[data-toggle]').forEach((button) => {
  button.onclick = () => updateUser(button.dataset.toggle, { active: button.dataset.active === '1' });
});
/* Роль и разделы сохраняются одной кнопкой: менять их по одной галочке значило
   бы дёргать сервер на каждый щелчок и перезагружать страницу посреди правки. */
document.querySelectorAll('[data-save-user]').forEach((button) => {
  button.onclick = () => {
    const id = button.dataset.saveUser;
    const role = document.querySelector(`[data-role-for="${id}"]`);
    const boxes = [...document.querySelectorAll(`[data-section-for="${id}"]`)];
    const payload = {};
    if (role) payload.role = role.value;
    /* У владельца галочек нет вовсе — и посылать пустой список нельзя: сервер
       ответит «у владельца доступны все разделы». */
    const password = document.querySelector(`[data-user-password="${id}"]`);
    if (password?.value) payload.password = password.value;
    if (boxes.length && role?.value !== 'owner') payload.sections = boxes.filter((b) => b.checked).map((b) => b.value);
    updateUser(id, payload);
  };
});

/* Сменили роль на «Сборщик» — «Настройки» ему не выдать: там ключи площадок и
   выдача прав. Показываем это сразу, а не отказом после сохранения. */
document.querySelectorAll('[data-role-for]').forEach((select) => {
  const sync = () => {
    const packer = select.value === 'packer';
    document.querySelectorAll(`[data-section-for="${select.dataset.roleFor}"]`).forEach((box) => {
      if (!MANAGER_ONLY.includes(box.value)) return;
      box.disabled = packer;
      if (packer) box.checked = false;
      box.closest('label')?.classList.toggle('muted', packer);
    });
  };
  select.addEventListener('change', sync);
  sync();
});
