"""Ключи Ozon, введённые в настройках панели."""
import pytest

from app import credentials, db, ozon
from app.config import settings


def test_demo_without_keys(monkeypatch):
    monkeypatch.setattr(settings, "demo_forced", False)
    monkeypatch.setattr(settings, "ozon_client_id", "")
    monkeypatch.setattr(settings, "ozon_api_key", "")
    assert credentials.is_demo()
    assert credentials.get_credentials()[2] == "none"
    assert isinstance(ozon.get_client(), ozon.DemoOzonClient)


def test_panel_keys_win_over_env(monkeypatch):
    monkeypatch.setattr(settings, "demo_forced", False)
    monkeypatch.setattr(settings, "ozon_client_id", "env-id")
    monkeypatch.setattr(settings, "ozon_api_key", "env-key")
    assert credentials.get_credentials() == ("env-id", "env-key", "env")

    credentials.set_credentials("panel-id", "panel-key")
    assert credentials.get_credentials() == ("panel-id", "panel-key", "panel")
    assert not credentials.is_demo()
    client = ozon.get_client()
    assert not isinstance(client, ozon.DemoOzonClient)
    assert client.client_id == "panel-id"


def test_clear_returns_to_env_then_demo(monkeypatch):
    monkeypatch.setattr(settings, "demo_forced", False)
    monkeypatch.setattr(settings, "ozon_client_id", "env-id")
    monkeypatch.setattr(settings, "ozon_api_key", "env-key")
    credentials.set_credentials("panel-id", "panel-key")

    credentials.clear_credentials()
    assert credentials.get_credentials()[2] == "env"

    monkeypatch.setattr(settings, "ozon_client_id", "")
    monkeypatch.setattr(settings, "ozon_api_key", "")
    assert credentials.is_demo()


def test_forced_demo_ignores_keys(monkeypatch):
    monkeypatch.setattr(settings, "demo_forced", True)
    credentials.set_credentials("panel-id", "panel-key")
    assert credentials.is_demo(), "OZON_DEMO=1 должен перекрывать любые ключи"
    assert isinstance(ozon.get_client(), ozon.DemoOzonClient)


def test_key_is_masked():
    assert credentials.mask("abcdefghij") .endswith("ghij")
    assert "abcdef" not in credentials.mask("abcdefghij")
    assert credentials.mask("") == ""


def test_saving_keys_is_logged():
    credentials.set_credentials("id-1", "key-1", user={"id": 1, "login": "admin"})
    row = db.query_one("SELECT * FROM events WHERE kind = 'ozon_credentials_set' ORDER BY id DESC")
    assert row is not None
    assert "key-1" not in (row["message"] or ""), "секрет не должен попадать в журнал"


@pytest.mark.parametrize(
    "client_id,api_key",
    [
        ("", ""),
        ("123456", ""),
        ("кириллица", "ключ"),          # неверная раскладка
        ("123456", "ключ-по-русски"),
        ("123456", "с пробелом внутри"),
        ("1" * 300, "x" * 300),
    ],
)
def test_invalid_credentials_rejected(client_id, api_key):
    assert credentials.validate(client_id, api_key) is not None


def test_valid_credentials_accepted():
    assert credentials.validate("123456", "a1b2c3d4-e5f6-7890-abcd-ef1234567890") is None
