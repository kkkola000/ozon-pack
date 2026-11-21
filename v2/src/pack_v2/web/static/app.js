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
