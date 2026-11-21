document.querySelectorAll('.dropdown').forEach((menu) => {
  let timer;
  let openedByHover = false;
  menu.addEventListener('pointerenter', (event) => {
    if (event.pointerType === 'touch') return;
    clearTimeout(timer);
    if (!menu.open) {
      menu.open = true;
      openedByHover = true;
    }
  });
  menu.querySelector('summary').addEventListener('click', (event) => {
    // A first mouse click after hover confirms the open menu instead of closing it.
    if (event.detail && openedByHover && menu.open) event.preventDefault();
    openedByHover = false;
  });
  menu.addEventListener('pointerleave', (event) => {
    if (event.pointerType === 'touch') return;
    timer = setTimeout(() => { if (!menu.contains(document.activeElement)) menu.open = false; }, 150);
  });
  menu.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { menu.open = false; menu.querySelector('summary').focus(); }
  });
  menu.addEventListener('focusout', () => {
    setTimeout(() => { if (!menu.contains(document.activeElement)) menu.open = false; }, 0);
  });
  document.addEventListener('click', (event) => { if (!menu.contains(event.target)) menu.open = false; });
});

document.querySelectorAll('form[data-pending]').forEach((form) => {
  form.addEventListener('submit', (event) => {
    if (form.dataset.busy === 'true') { event.preventDefault(); return; }
    const button = event.submitter || form.querySelector('button[type=submit]');
    if (!button) return;
    form.dataset.busy = 'true';
    form.setAttribute('aria-busy', 'true');
    button.dataset.originalText = button.textContent;
    button.disabled = true;
    button.classList.add('is-pending');
    button.textContent = button.dataset.pendingText || 'Сохраняем…';
  });
});

window.addEventListener('pageshow', () => {
  document.querySelectorAll('form[data-busy=true]').forEach((form) => {
    form.dataset.busy = 'false';
    form.removeAttribute('aria-busy');
    const button = form.querySelector('button.is-pending');
    if (button) {
      button.disabled = false;
      button.classList.remove('is-pending');
      button.textContent = button.dataset.originalText;
    }
  });
});
