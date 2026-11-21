import hashlib
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID

from cryptography.fernet import InvalidToken
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError

from pack_v2.infrastructure.crypto import SecretBox
from pack_v2.infrastructure.database import Database
from pack_v2.modules.identity.public import Principal
from pack_v2.shared.errors import Conflict, DomainError, Forbidden

from . import repository as store
from .contracts import AccessChecker, AccountView, OzonCredentials
from .models import AccountCheck, MarketplaceAccount
from .reading import (
    AccountSource,
    MarketplaceReader,
    OfferSnapshot,
    ReaderFactory,
    ReadError,
    ReadPage,
    ShipmentSnapshot,
)


def _manager(actor: Principal) -> None:
    if actor.role not in {"owner", "admin"}:
        raise Forbidden("Управление кабинетами доступно администратору и владельцу")


def _title(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 200 or any(ord(c) < 32 for c in value):
        raise DomainError("Укажите название кабинета длиной до 200 символов")
    return value


def _client_id(value: str) -> str:
    value = value.strip()
    if not value.isascii() or not value.isdigit() or len(value) > 32 or not value.strip("0"):
        raise DomainError("Client-Id должен содержать положительный числовой идентификатор Ozon")
    return value


def _key(value: SecretStr) -> bytes:
    raw = value.get_secret_value().strip()
    if (
        not raw
        or len(raw) > 512
        or not raw.isascii()
        or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in raw)
    ):
        raise DomainError("Укажите Api-Key без пробелов и управляющих символов")
    return raw.encode("ascii")


class Connections:
    def __init__(
        self,
        db: Database,
        box: SecretBox,
        checker: AccessChecker,
        reader_factory: ReaderFactory | None = None,
    ):
        self.db, self.box, self.checker = db, box, checker
        self.reader_factory = reader_factory

    @contextmanager
    def reader(self, actor: Principal, account_id: UUID):
        """Read an active account without holding a database connection during HTTP."""
        _manager(actor)
        if self.reader_factory is None:
            raise DomainError("Адаптер чтения не настроен")
        with self.db.transaction() as tx:
            saved = store.account(tx, account_id)
            if not saved.active:
                raise Conflict("Кабинет отключён")
            if saved.marketplace != "ozon":
                raise DomainError("Адаптер этой площадки ещё не подключён")
            source = AccountSource(saved.id, saved.marketplace, saved.credential_revision)
            credentials = self._credentials(tx, account_id)
        with self.reader_factory(source, credentials) as reader:
            yield reader
        self.assert_source_current(actor, source)

    def assert_source_current(self, actor: Principal, source: AccountSource) -> None:
        """Freshness check only. Future writes must revalidate under their own UoW lock."""
        _manager(actor)
        with self.db.transaction() as tx:
            saved = store.account(tx, source.account_id)
            if (
                not saved.active
                or saved.marketplace != source.marketplace
                or saved.credential_revision != source.credential_revision
            ):
                raise Conflict("Кабинет отключён или ключ изменился. Повторите загрузку")

    def _credentials(self, tx, account_id: UUID) -> OzonCredentials:
        values = store.secrets_for(tx, account_id)
        try:
            return OzonCredentials(
                self.box.open(values["client_id"]).decode("ascii"),
                SecretStr(self.box.open(values["api_key"]).decode("ascii")),
            )
        except (KeyError, InvalidToken, UnicodeError):
            raise DomainError(
                "Не удалось прочитать ключи кабинета. Проверьте ключ шифрования панели"
            ) from None

    def create(self, actor: Principal, title: str, client_id: str, api_key: SecretStr) -> UUID:
        _manager(actor)
        title, client_id, api_key_bytes = _title(title), _client_id(client_id), _key(api_key)
        fingerprint = hashlib.sha256(("ozon\0" + client_id.lstrip("0")).encode("ascii")).hexdigest()
        try:
            with self.db.transaction() as tx:
                saved = MarketplaceAccount(
                    marketplace="ozon",
                    title=title,
                    active=True,
                    identity_fingerprint=fingerprint,
                    credential_revision=1,
                )
                tx.add(saved)
                tx.flush()
                store.save_secret(tx, saved.id, "client_id", self.box.seal(client_id.encode("ascii")))
                store.save_secret(tx, saved.id, "api_key", self.box.seal(api_key_bytes))
                store.audit(tx, actor.id, saved.id, "account_created")
                return saved.id
        except IntegrityError as exc:
            raise Conflict("Кабинет с этим Client-Id уже добавлен. Найдите его в списке кабинетов") from exc

    def update(
        self, actor: Principal, account_id: UUID, revision: int, title: str, api_key: SecretStr | None = None
    ) -> None:
        _manager(actor)
        title = _title(title)
        raw = _key(api_key) if api_key and api_key.get_secret_value().strip() else None
        with self.db.transaction() as tx:
            saved = store.account(tx, account_id, lock=True)
            self._revision(saved, revision)
            saved.title, saved.revision = title, saved.revision + 1
            if raw is not None:
                try:
                    # Do not mix keys from different encryption configurations.
                    self.box.open(store.secrets_for(tx, account_id)["client_id"])
                except (KeyError, InvalidToken) as exc:
                    raise DomainError(
                        "Не удалось прочитать Client-Id. Проверьте ключ шифрования панели"
                    ) from exc
                store.save_secret(tx, saved.id, "api_key", self.box.seal(raw))
                saved.credential_revision += 1
                store.invalidate_running(tx, account_id)
            store.audit(tx, actor.id, saved.id, "account_credentials_updated" if raw else "account_updated")

    def set_active(self, actor: Principal, account_id: UUID, revision: int, active: bool) -> None:
        _manager(actor)
        with self.db.transaction() as tx:
            saved = store.account(tx, account_id, lock=True)
            self._revision(saved, revision)
            saved.active, saved.revision = active, saved.revision + 1
            store.audit(tx, actor.id, saved.id, "account_enabled" if active else "account_disabled")

    def list_accounts(self, actor: Principal) -> list[AccountView]:
        _manager(actor)
        now, result = datetime.now(UTC), []
        with self.db.transaction() as tx:
            accounts, secrets, checks = store.listing(tx)
            for saved in accounts:
                values, checked = secrets.get(saved.id, {}), checks.get(saved.id)
                client_id, error = None, None
                try:
                    if "client_id" in values:
                        client_id = self.box.open(values["client_id"]).decode("ascii")
                except (InvalidToken, UnicodeError):
                    error = "Не удалось прочитать Client-Id. Проверьте ключ шифрования панели"
                status, checked_at, details = "unverified", None, []
                if (
                    checked
                    and checked.credential_revision == saved.credential_revision
                    and checked.status != "stale"
                ):
                    status = checked.status
                    if status == "checking" and checked.lease_until <= now:
                        status = "unavailable"
                    checked_at, details = checked.finished_at, checked.results
                if error or not client_id or "api_key" not in values:
                    status, checked_at, details = "unverified", None, []
                result.append(
                    AccountView(
                        saved.id,
                        saved.marketplace,
                        saved.title,
                        saved.active,
                        saved.revision,
                        client_id,
                        "api_key" in values,
                        status,
                        checked_at,
                        details,
                        error,
                    )
                )
        return result

    def verify(self, actor: Principal, account_id: UUID, revision: int) -> list[dict]:
        _manager(actor)
        now = datetime.now(UTC)
        with self.db.transaction() as tx:
            saved = store.account(tx, account_id, lock=True)
            self._revision(saved, revision)
            if saved.marketplace != "ozon":
                raise DomainError("Проверка этой площадки ещё не подключена")
            pending = store.running(tx, account_id)
            if pending and pending.lease_until > now:
                raise Conflict("Проверка этого кабинета уже выполняется")
            store.invalidate_running(tx, account_id)
            credentials = self._credentials(tx, account_id)
            check = AccountCheck(
                account_id=account_id,
                credential_revision=saved.credential_revision,
                lease_until=now + timedelta(seconds=120),
            )
            tx.add(check)
            tx.flush()
            check_id, credential_revision = check.id, saved.credential_revision
        # All network work is outside the database transaction and account lock.
        results = [asdict(item) for item in self.checker.check(credentials)]
        if len(results) != 3 or {r["code"] for r in results} != {"orders", "products", "returns"}:
            results = [
                {
                    "code": code,
                    "title": title,
                    "status": "invalid_response",
                    "message": "Проверка вернула неполный результат. Повторите позже",
                }
                for code, title in (("orders", "Заказы FBS"), ("products", "Товары"), ("returns", "Возвраты"))
            ]
        now = datetime.now(UTC)
        with self.db.transaction() as tx:
            saved = store.account(tx, account_id, lock=True)
            check = tx.get(AccountCheck, check_id)
            if (
                saved.credential_revision != credential_revision
                or check.status != "checking"
                or check.lease_until <= now
            ):
                raise Conflict("Ключи изменились или проверка устарела. Повторите проверку кабинета")
            statuses = {r["status"] for r in results}
            if statuses == {"passed"}:
                status = "passed"
            elif "passed" in statuses:
                status = "partial"
            elif statuses & {"denied", "unauthorized"}:
                status = "failed"
            else:
                status = "unavailable"
            check.status, check.finished_at, check.results = status, now, results
            store.audit(tx, actor.id, account_id, "account_access_checked")
        return results

    @staticmethod
    def _revision(saved, expected: int) -> None:
        if expected != saved.revision:
            raise Conflict("Настройки кабинета изменились. Обновите страницу и повторите действие")


__all__ = [
    "Connections",
    "AccountView",
    "AccountSource",
    "MarketplaceReader",
    "OfferSnapshot",
    "ShipmentSnapshot",
    "ReadPage",
    "ReadError",
]
