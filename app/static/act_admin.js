/* Переделать принятое: вернуть акт в работу или удалить его совсем.

   Оба действия — владельца, и сервер это проверяет сам; у остальных кнопок
   просто нет. Оба меняют не строку, а весь экран: акт переезжает между
   разделами или исчезает вместе со страницей, на которой стоит кнопка.
   Поэтому здесь не перерисовка на месте, а переход. */
async function remakeAct(button, { url, ask, go }) {
  if (!confirm(ask)) return;
  button.disabled = true;
  try {
    const result = await api(url, {});
    /* Сообщение длиннее обычного: в нём сказано, что делать дальше —
       за какое число составлять акт заново. Его читают. */
    toast(result.message, 'ok', 12000);
    setTimeout(() => { window.location.href = go; }, 1200);
  } catch (error) {
    toast(error.message, 'error', 10000);
    button.disabled = false;
  }
}

document.addEventListener('click', (event) => {
  const back = event.target.closest('[data-unconfirm-act]');
  if (back) {
    event.preventDefault();
    remakeAct(back, {
      url: `/api/returns/acts/${encodeURIComponent(back.dataset.unconfirmAct)}/unconfirm`,
      ask: `Вернуть «${back.dataset.actTitle}» в работу?\n\n`
         + 'Акт уйдёт из «Отчётов» во вкладку «Ждёт подтверждения». '
         + 'Отметки останутся — поправить можно одну строку.',
      go: '/returns?tab=acts',
    });
    return;
  }

  const gone = event.target.closest('[data-delete-act]');
  if (!gone) return;
  event.preventDefault();
  remakeAct(gone, {
    url: `/api/returns/acts/${encodeURIComponent(gone.dataset.deleteAct)}/delete`,
    ask: `Удалить «${gone.dataset.actTitle}»?\n\n`
       + 'Возвраты освободятся, отметки с них снимутся, и акт придётся '
       + 'составить заново. Отменить это нельзя — отметки прошлого раза '
       + 'останутся только в журнале.',
    go: gone.dataset.after || window.location.pathname + window.location.search,
  });
});
