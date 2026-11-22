"""IP discovery and an atomic host change preserve the installed application's secrets."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import login
from fastapi.testclient import TestClient

from pack_v2.bootstrap.app import create_app
from pack_v2.cli import generate_env
from pack_v2.infrastructure.config import Settings

spec = importlib.util.spec_from_file_location(
    "deploy_public_access", Path(__file__).resolve().parents[1] / "deploy" / "public_access.py"
)
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)


@pytest.fixture(autouse=True)
def no_real_discovery(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Discovery must use synthetic interfaces and HTTP responses")

    monkeypatch.setattr(access.subprocess, "run", blocked)
    monkeypatch.setattr(access, "urlopen", blocked)


def interfaces(monkeypatch, values):
    result = SimpleNamespace(stdout=json.dumps([{"addr_info": values}]))
    monkeypatch.setattr(access.subprocess, "run", lambda *args, **kwargs: result)


def test_public_interface_address_does_not_query_an_external_service(monkeypatch):
    interfaces(
        monkeypatch,
        [
            {"family": "inet", "local": "10.0.0.5"},
            {"family": "inet", "local": None},
            {"family": "inet", "local": "224.0.0.1"},
            {"family": "inet", "local": "8.8.8.8"},
        ],
    )
    assert access.discover_ip() == "8.8.8.8"


def test_nat_uses_a_bounded_https_request(monkeypatch):
    interfaces(monkeypatch, [{"family": "inet", "local": "10.0.0.5"}])
    calls = []

    def response(url, timeout):
        calls.append((url, timeout))
        return io.BytesIO(b"8.8.4.4\n")

    monkeypatch.setattr(access, "urlopen", response)
    assert access.discover_ip() == "8.8.4.4"
    assert calls == [("https://api.ipify.org", 5)]


@pytest.mark.parametrize("value", [b"*", b"127.0.0.1", b"10.0.0.1", b"224.0.0.1", b"::1"])
def test_nat_response_cannot_allow_an_arbitrary_host(monkeypatch, value):
    interfaces(monkeypatch, [])
    monkeypatch.setattr(access, "urlopen", lambda *args, **kwargs: io.BytesIO(value))
    with pytest.raises(ValueError):
        access.discover_ip()


@pytest.mark.parametrize(
    "value", ["0.0.0.0", "127.0.0.1", "224.0.0.1", "http://8.8.8.8", "8.8.8.8:8001", "::1"]
)
def test_ip_override_requires_a_concrete_ipv4(value):
    with pytest.raises(ValueError):
        access.validate_ip(value)


def test_configuration_changes_only_hosts_and_preserves_private_ownership(tmp_path):
    path = tmp_path / ".env.v2"
    generate_env(path, native_postgres=True)
    original = path.read_text()
    before = path.stat()
    access.configure(path, "203.0.113.10")
    after = path.stat()

    def strip_hosts(value):
        return [line for line in value.splitlines() if not line.startswith("PACK_V2_ALLOWED_HOSTS=")]

    assert strip_hosts(original) == strip_hosts(path.read_text())
    assert (before.st_uid, before.st_gid) == (after.st_uid, after.st_gid)
    assert after.st_mode & 0o777 == 0o600
    settings = Settings(_env_file=path)
    assert settings.allowed_hosts == ["localhost", "127.0.0.1", "203.0.113.10"]
    assert "*" not in settings.allowed_hosts
    access.configure(path, "203.0.113.10")
    assert path.stat().st_ino == after.st_ino
    assert list(tmp_path.iterdir()) == [path]


def test_configuration_refuses_a_symlink(tmp_path):
    original, link = tmp_path / "original.env", tmp_path / "link.env"
    generate_env(original, native_postgres=True)
    content = original.read_bytes()
    link.symlink_to(original)
    with pytest.raises(ValueError):
        access.configure(link, "203.0.113.10")
    assert original.read_bytes() == content


def test_application_accepts_the_configured_ip_and_rejects_another_host(tmp_path, settings, db, owner):
    path = tmp_path / ".env.v2"
    generate_env(path, native_postgres=True)
    access.configure(path, "203.0.113.10")
    configured = Settings(_env_file=path)
    fields = settings.model_dump()
    fields["allowed_hosts"] = configured.allowed_hosts
    with TestClient(
        create_app(Settings(**fields)), base_url="http://203.0.113.10:8001", follow_redirects=False
    ) as client:
        assert client.get("/login").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.get("/api/v2/me").status_code == 401
        assert login(client).status_code == 303
        assert client.get("/api/v2/me").status_code == 200
        assert client.get("/login", headers={"host": "attacker.example"}).status_code == 400
