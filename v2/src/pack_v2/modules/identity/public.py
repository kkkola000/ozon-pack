import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy.exc import IntegrityError

from pack_v2.infrastructure.config import Settings
from pack_v2.infrastructure.database import Database
from pack_v2.shared.errors import Conflict, DomainError, Forbidden, RateLimited

from . import repository as store
from .domain import ROLES, SECTIONS, Authenticated, Principal, normalize_login

_hasher = PasswordHasher()
_dummy_hash = _hasher.hash(secrets.token_urlsafe(32))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _password(value: str) -> str:
    if not 12 <= len(value) <= 128:
        raise DomainError("Пароль должен содержать от 12 до 128 символов")
    return _hasher.hash(value)


class Identity:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings

    def bootstrap_owner(self, login: str, password: str) -> Principal:
        normalized, hashed = normalize_login(login), _password(password)
        try:
            with self.db.transaction() as tx:
                # Serialize first-owner creation across CLI processes.
                if store.lock_bootstrap(tx):
                    raise Conflict("Владелец уже создан")
                user = store.add_user(tx, login.strip(), normalized, hashed, "owner", SECTIONS)
                store.audit(tx, "owner_created", subject_id=user.id)
                return user
        except IntegrityError as exc:
            raise Conflict("Этот логин уже занят") from exc

    def create_user(
        self, actor: Principal, login: str, password: str, role: str, sections: frozenset[str]
    ) -> Principal:
        if actor.role not in {"owner", "admin"} or role == "owner" and actor.role != "owner":
            raise Forbidden("Недостаточно прав для создания сотрудника")
        if role not in ROLES or not sections <= SECTIONS:
            raise DomainError("Неизвестная роль или раздел")
        normalized, hashed = normalize_login(login), _password(password)
        try:
            with self.db.transaction() as tx:
                result = store.add_user(tx, login.strip(), normalized, hashed, role, sections)
                store.audit(tx, "user_created", actor.id, result.id)
                return result
        except IntegrityError as exc:
            raise Conflict("Этот логин уже занят") from exc

    def authenticate(self, login: str, password: str, address: str, previous: str | None) -> str | None:
        try:
            normalized = normalize_login(login)
        except DomainError:
            normalized = None
        secret = self.settings.secret_key.get_secret_value().encode()

        def key(value: str) -> str:
            return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()

        with self.db.transaction() as tx:
            exceeded = store.take_budget(
                tx,
                [
                    (key("login:" + (normalized or "<invalid>")), self.settings.login_limit),
                    (key("ip:" + address), self.settings.login_limit * 10),
                ],
                self.settings.login_window_seconds,
            )
        if exceeded:
            raise RateLimited("Слишком много попыток. Попробуйте позже")
        with self.db.transaction() as tx:
            user = store.find_user(tx, normalized) if normalized is not None else None
            stored = user.password_hash if user else _dummy_hash
            try:
                valid = len(password) <= 128 and _hasher.verify(stored, password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                valid = False
            if not valid or not user or not user.active:
                store.audit(tx, "login_failed")
                return None
            if _hasher.check_needs_rehash(user.password_hash):
                user.password_hash = _hasher.hash(password)
            if previous and len(previous) <= 128 and previous.isascii():
                store.revoke(tx, _digest(previous))
            token = secrets.token_urlsafe(32)
            store.add_session(
                tx, user, _digest(token), secrets.token_urlsafe(32), self.settings.session_hours
            )
            store.audit(tx, "login_succeeded", user.id)
            return token

    def current(self, token: str | None) -> Authenticated | None:
        if not token or len(token) > 128 or not token.isascii():
            return None
        with self.db.transaction() as tx:
            return store.read_session(tx, _digest(token))

    def logout(self, token: str, current: Authenticated) -> None:
        with self.db.transaction() as tx:
            store.revoke(tx, _digest(token))
            store.audit(tx, "logout", current.user.id)

    def cleanup(self) -> None:
        with self.db.transaction() as tx:
            store.prune(tx)


__all__ = ["Identity", "Principal", "Authenticated", "ROLES", "SECTIONS"]
