"""Отметка о возврате: «Принят» / «Не принят» и комментарий.

Ставят её двумя путями — кнопкой в окне отметки и сканом товара при приёмке
(core/return_scan.py). Запись одна на оба: иначе у одного пути отметка
оставляла бы след в журнале и обновляла шапку акта, а у другого — нет.
"""
from __future__ import annotations

from . import db, return_acts, store


def save(source, account: dict, return_id: str, act_id: str | None, mark: str, note: str,
         user: dict, *, how: str = "") -> dict:
    """Записать отметку и вернуть всё, что перерисовывает строку и шапку акта.

    how — как отметку поставили, для журнала: «скан товара 4600…».
    """
    # Пустая отметка без комментария — это «снять»: следов в строке остаться
    # не должно, иначе в списке будет висеть имя и время неизвестно чего.
    keeps = bool(mark or note)
    now = db.now_iso() if keeps else None
    db.execute(
        f"UPDATE {source.table} SET mark = ?, note = ?, mark_at = ?, mark_by = ? WHERE account_id = ? AND id = ?",
        (mark or None, note or None, now, user["login"] if keeps else None, account["id"], return_id),
    )
    db.log_event(
        "return_mark", account_id=account["id"], user=user,
        message=f"{return_id}: {store.mark_label(mark) or 'отметка снята'}"
                + (f" — {note}" if note else "") + (f" ({how})" if how else ""),
    )
    return {
        "status": "ok",
        "id": return_id,
        "mark": mark,
        "mark_label": store.mark_label(mark),
        "mark_sign": store.RETURN_MARK_SIGNS.get(mark, ""),
        "note": note,
        "mark_by": user["login"] if keeps else "",
        "mark_at_local": store.local_time(now) if keeps else "",
        "message": f"Отметка сохранена: {store.mark_label(mark) or 'снята'}",
        # Возврат из акта: отметка меняет и счётчики шапки, и право подтвердить.
        # Отдаём их сразу — иначе кнопка появляется только после перезагрузки.
        "act": return_acts.progress(act_id) if act_id else None,
    }
