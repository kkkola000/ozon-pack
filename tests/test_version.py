"""Версия сборки: файл VERSION, коммит из .git и выдача в /healthz."""
import pytest
from fastapi.testclient import TestClient

from app import version
from app.config import BASE_DIR
from app.main import app


@pytest.fixture
def client():
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def test_version_from_file():
    assert version.get_version() == (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()


def test_build_label_starts_with_version():
    assert version.build_label().startswith(version.get_version())


def test_commit_is_short_hash_or_unknown():
    commit = version.get_commit()
    assert commit == "—" or (len(commit) == 7 and all(c in "0123456789abcdef" for c in commit))


def test_healthz_reports_build(client):
    payload = client.get("/healthz").json()
    assert payload["status"] == "ok"
    assert payload["version"] == version.get_version()
    assert payload["commit"] == version.get_commit()
