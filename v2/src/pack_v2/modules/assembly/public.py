import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.fernet import InvalidToken
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from pack_v2.infrastructure.models import AuditEvent
from pack_v2.shared.errors import Conflict, DomainError, NotFound

from .models import ACTIVE, AssemblyCommand, AssemblyLine, AssemblySession, AssemblyUnit


def _result(ok=True, message="", code="ok", **extra):
    return {"ok": ok, "message": message, "code": code, **extra}


def _mark_like(code):
    return (
        bool(re.search(r"01[0-9]{14}21", code))
        or "\x1d" in code
        or "<gs>" in code.lower()
        or "\\u001d" in code.lower()
    )


def _scan_code(value):
    value = value.rstrip("\r\n")
    if not value or len(value) > 4096 or "\x00" in value:
        raise DomainError("Проверьте код сканирования")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise DomainError("Проверьте код сканирования") from None
    return value


class Assembly:
    def __init__(
        self,
        db,
        connections,
        orders,
        box,
        secret,
        *,
        instance_data,
        readiness=None,
        documents=None,
        lease_seconds=180,
    ):
        self.db, self.connections, self.orders, self.box = db, connections, orders, box
        self.secret = secret.encode()
        self.lease_seconds = lease_seconds
        self.instances = instance_data
        self.readiness = readiness
        self.documents = documents

    def _until(self):
        return datetime.now(UTC) + timedelta(seconds=self.lease_seconds)

    @staticmethod
    def _owned(tx, actor, session_id, *, lock=False):
        query = select(AssemblySession).where(
            AssemblySession.id == session_id, AssemblySession.user_id == actor.id
        )
        row = tx.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)
        if row is None:
            raise NotFound("Сборка не найдена")
        return row

    def _view(self, tx, actor, row):
        if row is None:
            return None
        status, message = row.status, row.message
        readiness = self.readiness.view(tx, row) if self.readiness else None
        split_done = status == "ready" and bool(readiness and readiness["children"])
        split_pending = (
            status == "preparing"
            and readiness
            and readiness["package_count"] > 1
            and readiness["status"] in ("queued", "checking", "uncertain")
        )
        label = self.connections.account_label(tx, row.account_id)
        manifest = (
            self.orders.packing_ready_manifest(tx, row.shipment_id)
            if row.status == "preparing" and self.readiness
            else self.orders.packing_manifest(tx, row.shipment_id)
        )
        refresh_status, refresh_message = self.orders.packing_refresh_status(
            tx, row.refresh_run_id, row.account_id, row.shipment_id
        )
        if status in ACTIVE and status != "preparing" and row.lease_until <= datetime.now(UTC):
            status, message = "expired", "Время сборки истекло. Откройте отправление заново"
        elif status in (*ACTIVE, "verified", "ready") and (
            not label.active or label.credential_revision != row.credential_revision
        ):
            status, message = "stale", "Кабинет отключён или ключи изменились. Откройте отправление заново"
        elif (
            not (split_done or split_pending)
            and status in ("scanning", "verified", "preparing", "ready")
            and (
                not manifest.eligible
                or row.source_revision != manifest.assembly_revision
                or row.basis != manifest.basis
                or status == "ready"
                and manifest.state != "awaiting_dispatch"
            )
        ):
            status, message = "stale", "Состав или состояние отправления изменились. Нужна повторная проверка"
        elif status == "opening" and refresh_status in ("failed", "stale"):
            status, message = (
                "blocked",
                refresh_message or "Не удалось проверить отправление. Повторите проверку",
            )
        scanned = {}
        physical = list(tx.scalars(select(AssemblyUnit).where(AssemblyUnit.session_id == row.id)))
        for unit in physical:
            scanned[unit.item_id] = scanned.get(unit.item_id, 0) + int(unit.scanned_at is not None)
        data = self.instances.view(
            tx,
            row.instance_context_id,
            manifest,
            {u.unit_id for u in physical if u.scanned_at is not None},
            row.last_unit_id,
        )
        if status == "stale":
            data["can_verify"] = False
            for info in data["items"].values():
                info["mark_status"] = "pending"
        if split_pending:
            data["status"], data["can_verify"] = "checking", False
            for info in data["items"].values():
                info["mark_status"] = "pending"
        if status == "opening" and data["status"] == "blocked":
            status, message = "blocked", data["message"]
        current = {i.item_id: i for i in manifest.items}
        items = []
        lines = list(
            tx.scalars(
                select(AssemblyLine)
                .where(AssemblyLine.session_id == row.id)
                .order_by(AssemblyLine.source_line_id)
            )
        )
        if split_done:
            data = self.instances.completed_split_view(tx, row.instance_context_id, lines)
        for line in lines:
            live = current.get(line.item_id)
            items.append(
                {
                    "id": str(line.item_id),
                    "name": line.name,
                    "article": line.article,
                    "need": line.quantity,
                    "scanned": scanned.get(line.item_id, 0),
                    "references": line.references,
                    "codes": list(live.codes) if live else [],
                    "image_url": live.image_url if live else None,
                    "marking_hint": line.marking_hint or bool(live and live.marking_hint),
                }
            )
        if not items and status in ("opening", "blocked"):
            items = [
                {
                    "id": str(i.item_id),
                    "name": i.name,
                    "article": i.article,
                    "need": i.quantity,
                    "scanned": 0,
                    "references": list(i.references),
                    "codes": list(i.codes),
                    "image_url": i.image_url,
                    "marking_hint": i.marking_hint,
                }
                for i in manifest.items
            ]
        return {
            "id": str(row.id),
            "shipment_id": str(row.shipment_id),
            "account_id": str(row.account_id),
            "account_title": label.title,
            "marketplace": label.marketplace,
            "market_title": "Яндекс Маркет" if label.marketplace == "yandex" else "Ozon",
            "shipping_supported": label.marketplace in ("ozon", "yandex"),
            "posting_number": row.posting_number,
            "status": status,
            "revision": row.revision,
            "lease_token": str(row.lease_token),
            "lease_until": row.lease_until.isoformat(),
            "message": message or "",
            "refresh_status": refresh_status,
            "refresh_message": refresh_message or "",
            "deadline": manifest.deadline.isoformat() if manifest.deadline else None,
            "warehouse": manifest.warehouse_name,
            "last_unit_id": str(row.last_unit_id) if row.last_unit_id else None,
            "items": items,
            "scanned": sum(i["scanned"] for i in items),
            "need": sum(i["need"] for i in items),
            "data": data,
            "readiness": readiness,
            "labels": self.documents.view(
                tx,
                [UUID(c["shipment_id"]) for c in readiness["children"]] if split_done else [row.shipment_id],
            )
            if self.documents and status in ("scanning", "preparing", "ready") and not split_pending
            else [],
            "layout": {
                "marketplace": label.marketplace,
                "editable": status == "scanning"
                and manifest.state == "awaiting_assembly"
                and (label.marketplace == "yandex" or len(physical) > 1),
                "groups": self.instances.yandex_layout(tx, row.instance_context_id, manifest, physical)
                if label.marketplace == "yandex" and row.instance_context_id and physical
                else self.orders.packing_layout(
                    tx,
                    manifest,
                    physical,
                    automatic=bool(
                        label.marketplace == "ozon" and self.readiness and self.readiness.auto_split
                    ),
                )
                if status == "scanning" and manifest.eligible and row.basis == manifest.basis and physical
                else [],
                "units": [
                    {"id": str(u.unit_id), "item_id": str(u.item_id), "scanned": u.scanned_at is not None}
                    for u in physical
                ],
            },
        }

    def current(self, actor):
        actor.require("pack")
        with self.db.transaction() as tx:
            row = tx.scalar(
                select(AssemblySession)
                .where(AssemblySession.user_id == actor.id, AssemblySession.status.in_(ACTIVE))
                .order_by(AssemblySession.created_at.desc())
                .limit(1)
            )
            return self._view(tx, actor, row)

    def printing_context(self, tx, actor, session_id):
        """Authorized read boundary for printing; never starts readiness or changes scans."""
        actor.require("pack")
        row = self._owned(tx, actor, session_id)
        self.connections.lock_source(
            tx, self.connections.source_at_revision(tx, row.account_id, row.credential_revision)
        )
        self.orders.packing_lock(tx, row.shipment_id)
        row = self._owned(tx, actor, session_id, lock=True)
        view = self._view(tx, actor, row)
        if view["status"] not in ("scanning", "preparing", "ready"):
            raise Conflict("Печать недоступна: откройте актуальное окно сборки")
        units = list(
            tx.scalars(
                select(AssemblyUnit.unit_id).where(
                    AssemblyUnit.session_id == row.id, AssemblyUnit.scanned_at.is_not(None)
                )
            )
        )
        return {"labels": view["labels"], "unit_ids": units}

    def session(self, actor, session_id):
        actor.require("pack")
        with self.db.transaction() as tx:
            return self._view(tx, actor, self._owned(tx, actor, session_id))

    def queue(self, actor, account_id=None):
        actor.require("pack")
        labels = {
            a.id: a
            for a in self.connections.account_labels(actor, section="pack")
            if a.active and a.marketplace in ("ozon", "yandex")
        }
        with self.db.transaction() as tx:
            _, rows = self.orders.packing_candidates(tx, account_id=account_id, account_ids=labels, limit=50)
            ids = [r.id for r in rows if r.account_id in labels]
            active = {
                r.shipment_id: r
                for r in tx.scalars(
                    select(AssemblySession).where(
                        AssemblySession.shipment_id.in_(ids),
                        AssemblySession.status.in_(ACTIVE),
                        or_(
                            AssemblySession.status == "preparing",
                            AssemblySession.lease_until > datetime.now(UTC),
                        ),
                    )
                )
            }
            proofs = {
                (r.shipment_id, r.source_revision)
                for r in tx.scalars(
                    select(AssemblySession).where(
                        AssemblySession.shipment_id.in_(ids),
                        AssemblySession.status.in_(("verified", "ready")),
                    )
                )
                if r.account_id in labels
                and r.credential_revision == labels[r.account_id].credential_revision
            }
            items = []
            for row in rows:
                if row.account_id not in labels:
                    continue
                owner = active.get(row.id)
                items.append(
                    {
                        "id": str(row.id),
                        "posting_number": row.posting_number,
                        "account_title": labels[row.account_id].title,
                        "state": row.state,
                        "deadline": row.deadline,
                        "warehouse": row.warehouse_name,
                        "quantity": row.quantity,
                        "verified": (row.id, row.assembly_revision) in proofs,
                        "busy": bool(owner),
                        "mine": bool(owner and owner.user_id == actor.id),
                    }
                )
            return items

    def _reply(self, tx, actor, command):
        row = self._owned(tx, actor, command.session_id) if command.session_id else None
        return {
            **command.outcome,
            "command_id": str(command.command_id),
            "session": self._view(tx, actor, row),
        }

    def command(self, actor, command_id):
        actor.require("pack")
        with self.db.transaction() as tx:
            saved = tx.get(AssemblyCommand, (actor.id, command_id))
            if saved is None:
                raise NotFound("Запрос ещё не завершён")
            return self._reply(tx, actor, saved)

    def _execute(self, actor, command_id, action, payload, operation):
        actor.require("pack")
        raw = json.dumps(
            {"action": action, **payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        try:
            encoded = raw.encode()
        except UnicodeError:
            raise DomainError("Проверьте формат переданных данных") from None
        fingerprint = hmac.new(self.secret, encoded, hashlib.sha256).hexdigest()
        try:
            with self.db.transaction() as tx:
                previous = tx.get(AssemblyCommand, (actor.id, command_id))
                if previous:
                    if previous.fingerprint != fingerprint:
                        raise Conflict("ID запроса уже использован для другого действия")
                    return self._reply(tx, actor, previous)
                row, outcome = operation(tx)
                if outcome.get("pending"):
                    return {**outcome, "command_id": str(command_id), "session": self._view(tx, actor, row)}
                saved = AssemblyCommand(
                    user_id=actor.id,
                    command_id=command_id,
                    action=action,
                    fingerprint=fingerprint,
                    session_id=row.id if row else None,
                    outcome=outcome,
                )
                tx.add(saved)
                tx.flush()
                return self._reply(tx, actor, saved)
        except IntegrityError:
            with self.db.transaction() as tx:
                previous = tx.get(AssemblyCommand, (actor.id, command_id))
                if previous and previous.fingerprint == fingerprint:
                    return self._reply(tx, actor, previous)
            raise Conflict("Сборка уже изменена другим запросом. Обновите окно") from None

    def open(self, actor, command_id, *, shipment_id=None, code=None, account_id=None):
        if (shipment_id is None) == (code is None):
            raise DomainError("Укажите отправление или код сканирования")
        code = _scan_code(code) if code is not None else None
        labels = {
            a.id: a.credential_revision
            for a in self.connections.account_labels(actor, section="pack")
            if a.active and a.marketplace in ("ozon", "yandex") and (account_id is None or a.id == account_id)
        }

        def operation(tx):
            initial = None
            if shipment_id is None:
                if not code or _mark_like(code):
                    return None, _result(
                        False, "Сейчас нужен штрихкод товара или стикер отправления", "wrong_code"
                    )
                proofs = select(AssemblySession.shipment_id, AssemblySession.source_revision).where(
                    AssemblySession.status.in_(("verified", "ready")),
                    or_(
                        *(
                            (AssemblySession.account_id == key) & (AssemblySession.credential_revision == rev)
                            for key, rev in labels.items()
                        ),
                        False,
                    ),
                )
                busy = select(AssemblySession.shipment_id).where(
                    AssemblySession.status.in_(ACTIVE),
                    or_(
                        AssemblySession.status == "preparing", AssemblySession.lease_until > datetime.now(UTC)
                    ),
                    AssemblySession.user_id != actor.id,
                )
                kind, rows = self.orders.packing_candidates(
                    tx, code=code, proofs=proofs, busy=busy, account_ids=labels, limit=100
                )
                rows = [r for r in rows if r.account_id in labels]
                if not rows:
                    return None, _result(False, "Не найдено доступное отправление по этому коду", "not_found")
                if kind == "label" and len(rows) > 1:
                    return None, _result(
                        False,
                        "Код наклейки совпадает у нескольких отправлений. Введите номер отправления",
                        "ambiguous",
                    )
                target = rows[0].id
                initial = code if kind == "product" else None
            else:
                target = shipment_id
            manifest = self.orders.packing_manifest(tx, target)
            if manifest.account_id not in labels:
                raise NotFound("Отправление не найдено в выбранном кабинете")
            source = self.connections.source_for_sync(tx, manifest.account_id)
            self.orders.packing_lock(tx, target)
            manifest = self.orders.packing_manifest(tx, target)
            if self.readiness and self.readiness.blocks_other_shipment(tx, manifest):
                return None, _result(
                    False,
                    "Ozon разделяет этот заказ. Сначала дождитесь сверки новых отправлений",
                    "split_pending",
                )
            now = datetime.now(UTC)
            tx.execute(
                update(AssemblySession)
                .where(
                    AssemblySession.status.in_(tuple(s for s in ACTIVE if s != "preparing")),
                    AssemblySession.lease_until <= now,
                    or_(AssemblySession.user_id == actor.id, AssemblySession.shipment_id == target),
                )
                .values(
                    status="expired",
                    revision=AssemblySession.revision + 1,
                    finished_at=now,
                    initial_code=None,
                )
            )
            own = tx.scalar(
                select(AssemblySession)
                .where(AssemblySession.user_id == actor.id, AssemblySession.status.in_(ACTIVE))
                .with_for_update()
            )
            if own:
                if own.shipment_id != target:
                    return own, _result(
                        False, "Сначала сохраните или отмените текущую проверку", "other_session"
                    )
                return own, _result(True, "Продолжайте открытую проверку", "already_open")
            occupied = tx.scalar(
                select(AssemblySession.id).where(
                    AssemblySession.shipment_id == target, AssemblySession.status.in_(ACTIVE)
                )
            )
            if occupied:
                return None, _result(False, "Отправление собирает другой сотрудник", "busy")
            verified = tx.scalar(
                select(AssemblySession).where(
                    AssemblySession.shipment_id == target,
                    AssemblySession.status.in_(("verified", "ready")),
                    AssemblySession.source_revision == manifest.assembly_revision,
                )
            )
            if verified and (
                verified.basis != manifest.basis
                or not manifest.eligible
                or verified.credential_revision != source.credential_revision
                or verified.status == "ready"
                and manifest.state != "awaiting_dispatch"
            ):
                verified.status, verified.message = "stale", "Состав или состояние изменились"
                verified.revision += 1
                tx.flush()
                verified = None
            if verified:
                if verified.user_id == actor.id:
                    return verified, _result(
                        True, "Товары этого отправления уже проверены", "already_verified"
                    )
                return None, _result(False, "Товары этого отправления уже проверены", "already_verified")
            if self.readiness:
                from .models import AssemblyDispatch

                ongoing = tx.scalar(
                    select(AssemblyDispatch)
                    .where(
                        AssemblyDispatch.shipment_id == target,
                        or_(
                            AssemblyDispatch.status.in_(("queued", "checking", "uncertain")),
                            AssemblyDispatch.ship_started_at.is_not(None)
                            & (AssemblyDispatch.status != "accepted"),
                        ),
                    )
                    .limit(1)
                )
                if ongoing:
                    previous_session = tx.get(AssemblySession, ongoing.proof_session_id)
                    if previous_session.user_id == actor.id:
                        return previous_session, _result(
                            True, "Проверяем результат предыдущей сборки", "readiness_pending"
                        )
                    return None, _result(False, "Ozon ещё обрабатывает сборку этого отправления", "busy")
            previous = tx.scalar(
                select(AssemblySession)
                .where(AssemblySession.shipment_id == target)
                .order_by(AssemblySession.created_at.desc(), AssemblySession.id.desc())
                .limit(1)
            )
            refresh = self.orders.packing_refresh(tx, source, target, actor.id)
            if (
                previous
                and previous.user_id == actor.id
                and previous.status == "expired"
                and previous.credential_revision == source.credential_revision
                and previous.source_revision == manifest.assembly_revision
            ):
                row = previous
                row.status, row.revision = "opening", row.revision + 1
                row.lease_token, row.lease_until = uuid4(), self._until()
                row.refresh_run_id, row.finished_at, row.message = refresh, None, None
            else:
                row = AssemblySession(
                    id=uuid4(),
                    user_id=actor.id,
                    account_id=manifest.account_id,
                    order_id=manifest.order_id,
                    shipment_id=target,
                    credential_revision=source.credential_revision,
                    source_revision=manifest.assembly_revision,
                    posting_number=manifest.posting_number,
                    lease_token=uuid4(),
                    lease_until=self._until(),
                    refresh_run_id=refresh,
                    initial_code=self.box.seal(initial.encode()) if initial else None,
                    status="opening",
                    revision=0,
                )
                tx.add(row)
            tx.add(
                AuditEvent(
                    actor_id=actor.id,
                    kind="assembly_opened",
                    subject_id=row.id,
                    details={"shipment_id": str(target)},
                )
            )
            tx.flush()
            return row, _result(True, "Проверяем отправление на площадке", "opening")

        return self._execute(
            actor,
            command_id,
            "open",
            {"shipment_id": shipment_id, "code": code, "account_id": account_id},
            operation,
        )

    def _locked(self, tx, actor, session_id, token, revision=None):
        row = self._owned(tx, actor, session_id)
        source_error = None
        try:
            self.connections.source_at_revision(tx, row.account_id, row.credential_revision)
        except DomainError as error:
            source_error = str(error)
        self.orders.packing_lock(tx, row.shipment_id)
        row = self._owned(tx, actor, session_id, lock=True)
        if row.lease_token != token:
            return row, None, _result(False, "Окно сборки устарело. Откройте его заново", "lease_changed")
        if revision is not None and row.revision != revision:
            return (
                row,
                None,
                _result(False, "Сборка изменена другим запросом. Обновите окно", "revision_conflict"),
            )
        if row.status not in ACTIVE:
            return row, None, _result(False, "Эта проверка уже закрыта", "closed")
        if row.status != "preparing" and row.lease_until <= datetime.now(UTC):
            row.status, row.message = "expired", "Время сборки истекло. Откройте отправление заново"
        elif source_error:
            row.status, row.message = "stale", source_error
        else:
            manifest = self.orders.packing_manifest(tx, row.shipment_id)
            if (
                row.status in ("opening", "blocked")
                or manifest.eligible
                and row.source_revision == manifest.assembly_revision
                and row.basis == manifest.basis
            ):
                return row, manifest, None
            row.status, row.message = (
                "stale",
                "Состав или состояние отправления изменились. Нужна повторная проверка",
            )
        row.revision += 1
        row.finished_at = datetime.now(UTC)
        row.initial_code = None
        return row, None, _result(False, row.message, "stale")

    def activate(self, actor, command_id, session_id, token, revision):
        def operation(tx):
            row, manifest, error = self._locked(tx, actor, session_id, token, revision)
            if error:
                return row, error
            if row.status == "scanning":
                return row, _result(True, "Проверка уже открыта")
            status, message = self.orders.packing_refresh_status(
                tx, row.refresh_run_id, row.account_id, row.shipment_id
            )
            if status in ("queued", "running", "retrying"):
                return row, _result(False, "Площадка проверяет отправление", "waiting", pending=True)
            if status != "succeeded" or not manifest.eligible:
                row.status, row.message = (
                    "blocked",
                    message or "Ozon не подтвердил доступность отправления для сборки",
                )
                row.revision += 1
                return row, _result(False, row.message, "blocked")
            if row.basis and (
                row.basis != manifest.basis or row.source_revision != manifest.assembly_revision
            ):
                row.status, row.message = "stale", "Состав изменился. Нужна повторная проверка"
                row.revision += 1
                return row, _result(False, row.message, "stale")
            data_status, data_error = self.instances.ensure_context(tx, row, manifest)
            if data_status in ("queued", "reading"):
                return row, _result(
                    False, "Получаем требования к Честному знаку и ГТД", "waiting_data", pending=True
                )
            if data_status != "ready":
                row.status, row.message = (
                    "blocked",
                    data_error or "Не удалось получить требования площадки. Повторите сверку",
                )
                row.revision += 1
                return row, _result(False, row.message, "blocked")
            try:
                with tx.begin_nested():
                    planned = self.orders.packing_plan(tx, manifest)
                    self.instances.bind(tx, actor, row.instance_context_id, manifest, planned)
            except DomainError as error:
                row.status, row.message = "blocked", str(error)
                row.revision += 1
                return row, _result(False, row.message, "blocked")
            existing = list(tx.scalars(select(AssemblyUnit).where(AssemblyUnit.session_id == row.id)))
            if existing:
                if {u.unit_id for u in existing} != {u.unit_id for u in planned}:
                    row.status, row.message = (
                        "stale",
                        "Распределение единиц изменилось. Нужна повторная проверка",
                    )
                    row.revision += 1
                    return row, _result(False, row.message, "stale")
            else:
                tx.add_all(
                    AssemblyLine(
                        session_id=row.id,
                        item_id=i.item_id,
                        name=i.name,
                        article=i.article,
                        source_line_id=i.source_line_id,
                        quantity=i.quantity,
                        references=list(i.references),
                        marking_hint=i.marking_hint,
                    )
                    for i in manifest.items
                )
                tx.flush()
                tx.add_all(
                    AssemblyUnit(
                        session_id=row.id,
                        account_id=row.account_id,
                        order_id=row.order_id,
                        shipment_id=row.shipment_id,
                        item_id=u.item_id,
                        unit_id=u.unit_id,
                        allocation_id=u.allocation_id,
                        ordinal=u.ordinal,
                    )
                    for u in planned
                )
            row.basis, row.source_revision = manifest.basis, manifest.assembly_revision
            row.status, row.message, row.lease_until = "scanning", None, self._until()
            if self.documents:
                self.documents.request(
                    tx,
                    self.connections.source_at_revision(tx, row.account_id, row.credential_revision),
                    row.shipment_id,
                    defer=self.connections.account_label(tx, row.account_id).marketplace == "yandex"
                    and not self.instances.view(tx, row.instance_context_id, manifest)["can_verify"],
                )
            row.revision += 1
            tx.flush()
            if row.initial_code:
                try:
                    code = self.box.open(row.initial_code).decode()
                except (InvalidToken, UnicodeError):
                    row.initial_code = None
                    return row, _result(
                        False,
                        "Не удалось восстановить первый скан. Отсканируйте товар повторно",
                        "initial_scan_lost",
                    )
                row.initial_code = None
                return row, self._count(tx, actor, row, manifest, code)
            return row, _result(True, "Сканируйте товар")

        return self._execute(
            actor,
            command_id,
            "activate",
            {"session_id": session_id, "token": token, "revision": revision},
            operation,
        )

    def _count(self, tx, actor, row, manifest, code):
        checked = list(
            tx.scalars(
                select(AssemblyUnit).where(
                    AssemblyUnit.session_id == row.id, AssemblyUnit.scanned_at.is_not(None)
                )
            )
        )
        data = self.instances.view(
            tx, row.instance_context_id, manifest, {u.unit_id for u in checked}, row.last_unit_id
        )
        pending = data.get("pending_unit_id")
        if _mark_like(code) and pending:
            try:
                with tx.begin_nested():
                    saved = self.instances.capture(
                        tx, actor, row.instance_context_id, manifest, UUID(pending), code
                    )
            except DomainError as error:
                self.instances.input_error(tx, row.instance_context_id, UUID(pending), "cis")
                return _result(False, str(error), "data_error")
            row.revision += 1
            row.lease_until = self._until()
            return _result(
                True,
                "Честный знак сохранён. Ожидаем подтверждения "
                + (
                    "Яндекс Маркета"
                    if self.connections.account_label(tx, row.account_id).marketplace == "yandex"
                    else "Ozon"
                )
                if saved["changed"]
                else "Честный знак уже сохранён",
                "mark_saved" if saved["changed"] else "mark_duplicate",
            )
        if _mark_like(code):
            return _result(False, "Сейчас нужен штрихкод товара", "wrong_phase")
        if pending:
            return _result(
                False, "Сначала отсканируйте Честный знак выбранной единицы товара", "mark_required"
            )
        candidates = [i.item_id for i in manifest.items if code in i.codes]
        pending = tx.scalars(
            select(AssemblyUnit)
            .where(
                AssemblyUnit.session_id == row.id,
                AssemblyUnit.item_id.in_(candidates),
                AssemblyUnit.scanned_at.is_(None),
            )
            .order_by(AssemblyUnit.ordinal)
        ).all()
        ids = {u.item_id for u in pending}
        if len(ids) > 1:
            return _result(
                False, "Код совпадает у нескольких товаров. Отсканируйте уникальный штрихкод", "ambiguous"
            )
        if not pending:
            return _result(
                False,
                "Все единицы этого товара уже отсканированы"
                if candidates
                else "Товар не входит в состав отправления",
                "already_counted" if candidates else "wrong_product",
            )
        unit = pending[0]
        unit.scanned_at = datetime.now(UTC)
        row.last_unit_id = unit.unit_id
        row.lease_until = self._until()
        row.revision += 1
        tx.add(
            AuditEvent(
                actor_id=actor.id,
                kind="assembly_unit_scanned",
                subject_id=row.id,
                details={
                    "shipment_id": str(row.shipment_id),
                    "unit_id": str(unit.unit_id),
                    "source_revision": row.source_revision,
                },
            )
        )
        return _result(True, "Товар проверен", "accepted", unit_id=str(unit.unit_id))

    def scan(self, actor, command_id, session_id, token, revision, code):
        code = _scan_code(code)

        def operation(tx):
            row, manifest, error = self._locked(tx, actor, session_id, token, revision)
            if error:
                return row, error
            if row.status != "scanning":
                return row, _result(False, "Дождитесь проверки отправления", "not_scanning")
            if self.readiness and self.orders.packing_label_matches(tx, row.shipment_id, code):
                return self._begin_ready(tx, row)
            return row, self._count(tx, actor, row, manifest, code)

        return self._execute(
            actor,
            command_id,
            "scan",
            {"session_id": session_id, "token": token, "revision": revision, "code": code},
            operation,
        )

    def verify(self, actor, command_id, session_id, token, revision):
        def operation(tx):
            row, _, error = self._locked(tx, actor, session_id, token, revision)
            if error:
                return row, error
            if row.status != "scanning":
                return row, _result(False, "Проверка товаров ещё не открыта", "not_scanning")
            units = list(tx.scalars(select(AssemblyUnit).where(AssemblyUnit.session_id == row.id)))
            if not units or any(u.scanned_at is None for u in units):
                return row, _result(False, "Сначала отсканируйте все товары", "incomplete")
            manifest = self.orders.packing_manifest(tx, row.shipment_id)
            data = self.instances.view(
                tx, row.instance_context_id, manifest, {u.unit_id for u in units}, row.last_unit_id
            )
            if not data["can_verify"]:
                return row, _result(
                    False,
                    data.get("message")
                    or "Сначала передайте Честный знак и ГТД и дождитесь подтверждения площадки",
                    "instance_data_required",
                    pending=data["status"] in ("prepared", "checking"),
                )
            row.status, row.verified_at, row.finished_at = "verified", datetime.now(UTC), datetime.now(UTC)
            row.revision += 1
            tx.add(
                AuditEvent(
                    actor_id=actor.id,
                    kind="assembly_products_verified",
                    subject_id=row.id,
                    details={
                        "shipment_id": str(row.shipment_id),
                        "units": len(units),
                        "source_revision": row.source_revision,
                    },
                )
            )
            return row, _result(True, "Проверка товаров сохранена", "products_verified")

        return self._execute(
            actor,
            command_id,
            "verify",
            {"session_id": session_id, "token": token, "revision": revision},
            operation,
        )

    def _begin_ready(self, tx, row):
        manifest = self.orders.packing_manifest(tx, row.shipment_id)
        if (
            not manifest.eligible
            or manifest.basis != row.basis
            or manifest.assembly_revision != row.source_revision
        ):
            return row, _result(
                False, "Состав или состояние отправления изменились. Повторите проверку", "stale"
            )
        units = list(tx.scalars(select(AssemblyUnit).where(AssemblyUnit.session_id == row.id)))
        if not units or any(u.scanned_at is None for u in units):
            return row, _result(False, "Сначала отсканируйте все товары", "incomplete")
        if self.readiness is None:
            raise DomainError("Операция готовности не настроена")
        dispatch = self.readiness.request(tx, row, manifest)
        row.status, row.verified_at, row.finished_at = (
            ("ready" if dispatch.status == "accepted" else "preparing"),
            datetime.now(UTC),
            None,
        )
        row.revision += 1
        return row, _result(
            True,
            "Яндекс Маркет обрабатывает сборку. Ожидаем подтверждения готовности к отгрузке"
            if self.connections.account_label(tx, row.account_id).marketplace == "yandex"
            else "Ozon обрабатывает сборку. Ожидаем подтверждения готовности к отгрузке",
            "readiness_queued",
        )

    def complete(self, actor, command_id, session_id, token, revision):
        def operation(tx):
            row = self._owned(tx, actor, session_id)
            self.connections.lock_source(
                tx, self.connections.source_at_revision(tx, row.account_id, row.credential_revision)
            )
            self.orders.packing_lock(tx, row.shipment_id)
            row = self._owned(tx, actor, session_id, lock=True)
            if row.lease_token != token or row.revision != revision:
                return row, _result(False, "Окно сборки устарело. Обновите его", "revision_conflict")
            if row.status not in ("scanning", "verified", "preparing", "ready"):
                return row, _result(False, "Проверка товаров ещё не завершена", "not_scanning")
            if row.status in ("preparing", "ready"):
                return row, _result(True, "Проверяем результат сборки", "readiness_existing")
            if row.status == "scanning" and row.lease_until <= datetime.now(UTC):
                return row, _result(False, "Время сборки истекло. Откройте отправление заново", "expired")
            return self._begin_ready(tx, row)

        return self._execute(
            actor,
            command_id,
            "complete",
            {"session_id": session_id, "token": token, "revision": revision},
            operation,
        )

    def layout(self, actor, command_id, session_id, token, revision, groups):
        def operation(tx):
            row, manifest, error = self._locked(tx, actor, session_id, token, revision)
            if error:
                return row, error
            if row.status != "scanning":
                return row, _result(
                    False, "Распределение меняется только до отправки сборки Ozon", "not_scanning"
                )
            if self.connections.account_label(tx, row.account_id).marketplace == "yandex":
                try:
                    with tx.begin_nested():
                        batch_id = self.instances.save_yandex_layout(
                            tx, actor, row.instance_context_id, manifest, groups
                        )
                except DomainError as error:
                    return row, _result(False, str(error), "layout_error")
                row.revision += 1
                row.lease_until = self._until()
                return row, _result(
                    True,
                    "Передаём грузоместа Яндекс Маркету. Используйте новые ярлыки после подтверждения"
                    if batch_id
                    else "Грузоместа сохранены",
                    "layout_saved",
                )
            old = self.readiness.find(tx, row) if self.readiness else None
            if old and old.ship_started_at:
                return row, _result(False, "Результат предыдущей сборки требует сверки", "readiness_pending")
            units = list(tx.scalars(select(AssemblyUnit).where(AssemblyUnit.session_id == row.id)))
            self.orders.packing_save_layout(tx, manifest, units, groups)
            row.revision += 1
            row.lease_until = self._until()
            return row, _result(True, "Распределение товаров сохранено", "layout_saved")

        return self._execute(
            actor,
            command_id,
            "layout",
            {"session_id": session_id, "token": token, "revision": revision, "groups": groups},
            operation,
        )

    def check_ready(self, actor, command_id, session_id, token, revision):
        def operation(tx):
            row = self._owned(tx, actor, session_id)
            self.connections.lock_source(
                tx, self.connections.source_at_revision(tx, row.account_id, row.credential_revision)
            )
            self.orders.packing_lock(tx, row.shipment_id)
            row = self._owned(tx, actor, session_id, lock=True)
            if row.lease_token != token or row.revision != revision:
                return row, _result(False, "Окно сборки устарело. Обновите его", "revision_conflict")
            if self.readiness is None:
                raise DomainError("Операция готовности не настроена")
            self.readiness.recheck(tx, row)
            row.revision += 1
            return row, _result(
                True, "Проверяем фактическую готовность отправления в Ozon", "readiness_checking"
            )

        return self._execute(
            actor,
            command_id,
            "check_ready",
            {"session_id": session_id, "token": token, "revision": revision},
            operation,
        )

    def cancel(self, actor, command_id, session_id, token, revision):
        def operation(tx):
            row = self._owned(tx, actor, session_id, lock=True)
            if row.lease_token != token or row.revision != revision:
                return row, _result(False, "Окно сборки устарело. Обновите его", "revision_conflict")
            if row.status != "cancelled":
                row.status, row.finished_at, row.initial_code = "cancelled", datetime.now(UTC), None
                row.revision += 1
                tx.add(
                    AuditEvent(
                        actor_id=actor.id,
                        kind="assembly_cancelled",
                        subject_id=row.id,
                        details={"shipment_id": str(row.shipment_id)},
                    )
                )
            return row, _result(True, "Проверка отменена", "cancelled")

        return self._execute(
            actor,
            command_id,
            "cancel",
            {"session_id": session_id, "token": token, "revision": revision},
            operation,
        )

    def heartbeat(self, actor, session_id, token):
        actor.require("pack")
        with self.db.transaction() as tx:
            row, _, error = self._locked(tx, actor, session_id, token)
            if error:
                return {**error, "session": self._view(tx, actor, row)}
            row.lease_until = self._until()
            return {"ok": True, "session": self._view(tx, actor, row)}

    def retry(self, actor, command_id, session_id, token, revision):
        def operation(tx):
            row, _, error = self._locked(tx, actor, session_id, token, revision)
            if error:
                return row, error
            source = self.connections.source_at_revision(tx, row.account_id, row.credential_revision)
            row.refresh_run_id = self.orders.packing_refresh(tx, source, row.shipment_id, actor.id)
            row.instance_context_id = None
            row.status, row.message = "opening", None
            row.revision += 1
            return row, _result(True, "Проверяем отправление", "opening")

        return self._execute(
            actor,
            command_id,
            "retry",
            {"session_id": session_id, "token": token, "revision": revision},
            operation,
        )

    def gtd(self, actor, command_id, session_id, token, revision, unit_id, value, *, absent=False):
        def operation(tx):
            row, manifest, error = self._locked(tx, actor, session_id, token, revision)
            if error:
                return row, error
            unit = tx.scalar(
                select(AssemblyUnit).where(AssemblyUnit.session_id == row.id, AssemblyUnit.unit_id == unit_id)
            )
            if row.status != "scanning" or unit is None or unit.scanned_at is None:
                return row, _result(False, "Сначала отсканируйте товар", "not_scanned")
            try:
                with tx.begin_nested():
                    self.instances.save_gtd(
                        tx, actor, row.instance_context_id, manifest, unit_id, value, absent=absent
                    )
            except DomainError as error:
                return row, _result(False, str(error), "data_error")
            row.revision += 1
            row.lease_until = self._until()
            return row, _result(True, "Выбор ГТД сохранён" if absent else "ГТД сохранена", "gtd_saved")

        return self._execute(
            actor,
            command_id,
            "gtd",
            {
                "session_id": session_id,
                "token": token,
                "revision": revision,
                "unit_id": unit_id,
                "value": value,
                **({"absent": True} if absent else {}),
            },
            operation,
        )

    def retry_label(self, actor, command_id, session_id, token, revision):
        def operation(tx):
            row = self._owned(tx, actor, session_id)
            source = self.connections.source_at_revision(tx, row.account_id, row.credential_revision)
            self.connections.lock_source(tx, source)
            self.orders.packing_lock(tx, row.shipment_id)
            row = self._owned(tx, actor, session_id, lock=True)
            if row.lease_token != token or row.revision != revision:
                return row, _result(False, "Окно сборки изменилось. Обновите страницу", "revision_conflict")
            if not self.documents or row.status not in ("scanning", "ready"):
                return row, _result(False, "Откройте актуальную сборку для получения стикера", "not_scanning")
            view = self._view(tx, actor, row)
            if view["status"] not in ("scanning", "ready"):
                return row, _result(False, "Отправление изменилось. Повторите сверку", "stale")
            targets = [UUID(c["shipment_id"]) for c in (view.get("readiness") or {}).get("children", [])] or [
                row.shipment_id
            ]
            for shipment_id in targets:
                self.orders.packing_lock(tx, shipment_id)
                self.documents.request(tx, source, shipment_id, retry=True)
            row.revision += 1
            return row, _result(True, "Получаем стикер Ozon", "label_queued")

        return self._execute(
            actor,
            command_id,
            "retry_label",
            {"session_id": session_id, "token": token, "revision": revision},
            operation,
        )

    def check_data(self, actor, command_id, session_id, token, revision):
        def operation(tx):
            row, _, error = self._locked(tx, actor, session_id, token, revision)
            if error:
                return row, error
            try:
                self.instances.recheck(tx, row.instance_context_id)
            except DomainError as error:
                return row, _result(False, str(error), "data_error")
            row.revision += 1
            return row, _result(True, "Проверяем результат передачи в Ozon", "data_checking")

        return self._execute(
            actor,
            command_id,
            "check_data",
            {"session_id": session_id, "token": token, "revision": revision},
            operation,
        )


__all__ = ["Assembly"]
