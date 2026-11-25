"""Six-month sticker archive. Search tokens never expose complete marks in URLs."""

import calendar
import json
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from cryptography.fernet import InvalidToken
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import delete, exists, func, or_, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer

from pack_v2.infrastructure.jobs import LeaseLost, Queue, enqueue
from pack_v2.infrastructure.models import AuditEvent, Job
from pack_v2.shared.errors import Conflict, DomainError, Forbidden, NotFound

from .models import ShipmentLabel, StickerSearch

MSK = ZoneInfo("Europe/Moscow")


def cutoff(now):
    months = now.year * 12 + now.month - 1 - 6
    year, month = divmod(months, 12)
    month += 1
    return now.replace(year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1]))


def filters(account_id=None, since="", until="", query=""):
    query = query.strip()
    if len(query) > 4096 or "\x00" in query:
        raise DomainError("Проверьте строку поиска")
    try:
        start = date.fromisoformat(since) if since else None
        end = date.fromisoformat(until) if until else None
        account = UUID(str(account_id)) if account_id else None
    except (ValueError, TypeError):
        raise DomainError("Проверьте кабинет и диапазон дат") from None
    if end == date.max:
        raise DomainError("Проверьте окончание диапазона дат")
    if start and end and start > end:
        raise DomainError("Начало диапазона дат должно быть не позднее окончания")
    return {"account_id": str(account) if account else "", "since": since, "until": until, "query": query}


class StickerArchive:
    def __init__(self, db, connections, orders, documents, marking, instances, readiness, secret):
        self.db, self.connections, self.orders, self.documents = db, connections, orders, documents
        self.marks, self.instances, self.readiness = marking, instances, readiness
        self.box = documents.box
        self.cursor = URLSafeTimedSerializer(secret, salt="pack-v2-sticker-cursor")

    @staticmethod
    def _authorize(actor):
        actor.require("stickers")

    def search(self, saved, account_id=None, since="", until="", query=""):
        self._authorize(saved.user)
        value = filters(account_id, since, until, query)
        with self.db.transaction() as tx:
            if value["account_id"]:
                self.connections.account_label(tx, UUID(value["account_id"]))
            # Limit retained searches per session; contents include full cryptographic codes.
            old = list(
                tx.scalars(
                    select(StickerSearch.id)
                    .where(StickerSearch.session_id == saved.session_id)
                    .order_by(StickerSearch.created_at.desc(), StickerSearch.id.desc())
                    .offset(19)
                )
            )
            if old:
                tx.execute(delete(StickerSearch).where(StickerSearch.id.in_(old)))
            row = StickerSearch(
                id=uuid4(),
                user_id=saved.user.id,
                session_id=saved.session_id,
                encrypted_filters=self.box.seal(json.dumps(value, ensure_ascii=True).encode()),
                expires_at=datetime.now(UTC) + timedelta(hours=2),
            )
            tx.add(row)
            return row.id

    def _filters(self, tx, saved, search_id):
        if search_id is None:
            return filters()
        row = tx.scalar(
            select(StickerSearch).where(
                StickerSearch.id == search_id,
                StickerSearch.user_id == saved.user.id,
                StickerSearch.session_id == saved.session_id,
                StickerSearch.expires_at > datetime.now(UTC),
            )
        )
        if row is None:
            raise DomainError("Поиск устарел. Нажмите «Найти» заново")
        try:
            return filters(**json.loads(self.box.open(row.encrypted_filters)))
        except (InvalidToken, ValueError, TypeError):
            raise DomainError("Не удалось прочитать поиск. Нажмите «Найти» заново") from None

    @staticmethod
    def _visible(now):
        activity = func.coalesce(ShipmentLabel.received_at, ShipmentLabel.created_at)
        return activity >= cutoff(now)

    def _label(self, tx, label_id, *, lock=False):
        query = (
            select(ShipmentLabel)
            .options(defer(ShipmentLabel.encrypted_pdf))
            .where(ShipmentLabel.id == label_id, self._visible(datetime.now(UTC)))
        )
        row = tx.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise NotFound("Стикер не найден или срок хранения истёк")
        return row

    def listing(self, saved, *, search_id=None, cursor=None):
        self._authorize(saved.user)
        with self.db.transaction() as tx:
            chosen = self._filters(tx, saved, search_id)
            activity = func.coalesce(ShipmentLabel.received_at, ShipmentLabel.created_at)
            conditions = [
                self._visible(datetime.now(UTC)),
                or_(
                    ShipmentLabel.encrypted_pdf.is_not(None),
                    ShipmentLabel.shipment_id.in_(self.marks.archive_shipments()),
                ),
            ]
            if chosen["account_id"]:
                conditions.append(ShipmentLabel.account_id == UUID(chosen["account_id"]))
            if chosen["since"]:
                conditions.append(
                    activity
                    >= datetime.combine(date.fromisoformat(chosen["since"]), time.min, MSK).astimezone(UTC)
                )
            if chosen["until"]:
                conditions.append(
                    activity
                    < datetime.combine(
                        date.fromisoformat(chosen["until"]) + timedelta(days=1), time.min, MSK
                    ).astimezone(UTC)
                )
            if chosen["query"]:
                query = chosen["query"]
                mark_match = ShipmentLabel.shipment_id.in_(self.marks.archive_search(query))
                # A full code goes to the database only as its HMAC, never as a LIKE parameter.
                conditions.append(
                    mark_match
                    if self.marks.looks_like(query)
                    else or_(
                        ShipmentLabel.archive_snapshot["search_text"]
                        .as_string()
                        .icontains(query, autoescape=True),
                        ShipmentLabel.shipment_id.in_(self.orders.archive_search(query)),
                        mark_match,
                    )
                )
            if cursor:
                try:
                    value = self.cursor.loads(cursor, max_age=7200)
                    if value["user"] != str(saved.user.id) or value["search"] != str(search_id or ""):
                        raise ValueError()
                    at = datetime.fromisoformat(value["at"])
                    if at.tzinfo is None:
                        raise ValueError()
                    last = UUID(value["id"])
                except (BadData, ValueError, TypeError, KeyError):
                    raise DomainError("Список устарел. Откройте раздел заново") from None
                conditions.append(tuple_(activity, ShipmentLabel.id) < tuple_(at, last))
            found = tx.execute(
                select(ShipmentLabel, ShipmentLabel.encrypted_pdf.is_not(None))
                .options(defer(ShipmentLabel.encrypted_pdf))
                .where(*conditions)
                .order_by(activity.desc(), ShipmentLabel.id.desc())
                .limit(31)
            ).all()
            more, selected = len(found) > 30, found[:30]
            rows = [row for row, _ in selected]
            available = {row.id: present for row, present in selected}
            info = self.orders.archive_info(tx, [r.shipment_id for r in rows])
            marks = self.marks.archive_counts(tx, [r.shipment_id for r in rows])
            entries = []
            for row in rows:
                snapshot = row.archive_snapshot or info[row.shipment_id]
                count, bound = marks.get(row.shipment_id, (0, 0))
                entries.append(
                    {
                        "id": str(row.id),
                        "generation": row.generation,
                        "shipment_id": str(row.shipment_id),
                        "size": row.size,
                        "account_id": row.account_id,
                        "received_at": row.received_at or row.created_at,
                        "snapshot": snapshot,
                        "has_pdf": row.status == "ready" and available[row.id],
                        "mark_count": count,
                        "bound_count": bound,
                    }
                )
            next_cursor = (
                self.cursor.dumps(
                    {
                        "user": str(saved.user.id),
                        "search": str(search_id or ""),
                        "at": (rows[-1].received_at or rows[-1].created_at).isoformat(),
                        "id": str(rows[-1].id),
                    }
                )
                if more
                else None
            )
            return {"items": entries, "next_cursor": next_cursor, "filters": chosen}

    def marking(self, actor, label_id):
        self._authorize(actor)
        with self.db.transaction() as tx:
            row = self._label(tx, label_id)
            info = row.archive_snapshot or self.orders.archive_info(tx, [row.shipment_id])[row.shipment_id]
            marks = self.marks.archive_history(tx, [row.shipment_id], reveal=True).get(row.shipment_id, [])
            for value in marks:
                value["product"] = info.get("units", {}).get(value["unit_id"], {})
                value["can_release"] = actor.role in ("owner", "admin") and value["binding"] != "unbound"
            return {
                "posting_number": info.get("posting_number", ""),
                "marks": marks,
                "can_manage": actor.role in ("owner", "admin"),
            }

    def file(self, actor, label_id, generation=None):
        self._authorize(actor)
        with self.db.transaction() as tx:
            row = self._label(tx, label_id)
            if generation is not None and generation != row.generation:
                raise Conflict("Стикер обновлён. Обновите список перед скачиванием")
            if row.status != "ready" or row.encrypted_pdf is None:
                raise NotFound("Оригинал стикера не сохранён или больше неактуален")
            info = row.archive_snapshot or self.orders.archive_info(tx, [row.shipment_id])[row.shipment_id]
            number = "".join(
                c for c in info.get("posting_number", "") if c.isascii() and (c.isalnum() or c in "-_")
            )
            return self.documents.content(row), f"ozon-{number or row.id}.pdf"

    def release(self, actor, label_id, assignment_id, revision):
        self._authorize(actor)
        if actor.role not in ("owner", "admin"):
            raise Forbidden("Отвязка доступна только администратору")
        with self.db.transaction() as tx:
            row = self._label(tx, label_id)
            self.connections.archive_account_lock(tx, row.account_id)
            self.orders.packing_lock(tx, row.shipment_id)
            row = self._label(tx, label_id, lock=True)
            self.instances.guard_marking_release(tx, row.shipment_id)
            self.readiness.guard_marking_release(tx, row.shipment_id)
            changed, unit_id, current = self.marks.release_admin(
                tx, actor, row.shipment_id, assignment_id, revision
            )
            if changed and current:
                self.instances.marking_released(tx, unit_id)
        return self.marking(actor, label_id)

    def schedule(self):
        now = datetime.now(UTC)
        with self.db.transaction() as tx:
            job_id = enqueue(tx, "documents.cleanup", "documents.cleanup:" + now.date().isoformat())
            tx.get(Job, job_id).max_attempts = 100

    def perform(self, job):
        try:
            with self.db.transaction() as tx:
                Queue.owned(tx, job)
                now = datetime.now(UTC)
                rows = list(
                    tx.scalars(
                        select(ShipmentLabel)
                        .options(defer(ShipmentLabel.encrypted_pdf))
                        .where(
                            ShipmentLabel.received_at < cutoff(now), ShipmentLabel.encrypted_pdf.is_not(None)
                        )
                        .with_for_update(skip_locked=True)
                        .limit(100)
                    )
                )
                for row in rows:
                    row.encrypted_pdf = row.checksum = row.byte_count = None
                    row.status = "expired"
                tx.execute(delete(StickerSearch).where(StickerSearch.expires_at <= now))
                tx.flush()
                more = tx.scalar(
                    select(
                        exists().where(
                            ShipmentLabel.received_at < cutoff(now), ShipmentLabel.encrypted_pdf.is_not(None)
                        )
                    )
                )
                Queue.finish_in(tx, job, retry=bool(more))
                if rows:
                    tx.add(
                        AuditEvent(
                            kind="sticker_archive_expired", subject_id=job.id, details={"count": len(rows)}
                        )
                    )
        except LeaseLost:
            return
        except SQLAlchemyError:
            Queue(self.db).finish(job, retry=True)
