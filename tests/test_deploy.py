"""Скрипты развёртывания: синтаксис, правило доступа и его подключение в nginx."""
import os
import re
import shutil
import subprocess
import textwrap

import pytest

from app.config import BASE_DIR

DEPLOY = BASE_DIR / "deploy"
SCRIPTS = sorted(DEPLOY.glob("*.sh"))
SNIPPET_LINE = "include $ACCESS_SNIPPET;"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_syntax(script):
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_vpn_only_help():
    proc = subprocess.run(
        ["bash", str(DEPLOY / "vpn-only.sh"), "--help"], capture_output=True, text=True
    )
    assert proc.returncode == 0
    for flag in ("--off", "--status", "--subnet", "--allow"):
        assert flag in proc.stdout
    # Как узнать свою сеть — прямо в справке
    assert "wg0" in proc.stdout


def _location_blocks(text):
    """Куски конфига от каждого `location ... {` до следующего."""
    starts = [m.start() for m in re.finditer(r"^\s*location [^\n]*\{", text, re.M)]
    bounds = starts + [len(text)]
    return [text[bounds[i]:bounds[i + 1]] for i in range(len(starts))]


def test_ssl_puts_access_rule_into_panel_locations():
    """Правило доступа стоит в location панели и не стоит на проверке Let's Encrypt."""
    text = (DEPLOY / "ssl.sh").read_text(encoding="utf-8")
    blocks = _location_blocks(text)
    panel = [b for b in blocks if "proxy_pass" in b or "return 301 https" in b]
    acme = [b for b in blocks if "acme-challenge" in b]

    assert panel, "в ssl.sh не нашлось location панели"
    assert acme, "в ssl.sh не нашлось location для Let's Encrypt"
    assert all(SNIPPET_LINE in b for b in panel if "proxy_pass" in b)
    assert not any(SNIPPET_LINE in b for b in acme), "сертификат перестанет продлеваться"


LEGACY_SITE = """\
server {
    listen 80;
    server_name panel.example.com;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        proxy_pass http://127.0.0.1:%(port)s;
        proxy_set_header Host $host;
    }
}
"""


@pytest.fixture
def sandbox(tmp_path):
    """Подставные nginx, systemctl, ss и curl — скрипт можно гонять целиком."""
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "nginx").write_text("#!/bin/sh\nexit 0\n")
    (stub / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    (stub / "ss").write_text("#!/bin/sh\nexit 0\n")
    (stub / "curl").write_text("#!/bin/sh\nexit 0\n")
    for name in ("nginx", "systemctl", "ss", "curl"):
        os.chmod(stub / name, 0o755)
    return stub


def _run(script, args, env):
    return subprocess.run(
        ["bash", str(script), *args], capture_output=True, text=True, env=env
    )


def _sandbox_env(tmp_path, stub, port="8080"):
    """Каталоги nginx и WireGuard во временной папке — скрипт не трогает систему."""
    sites = tmp_path / "sites-enabled"
    sites.mkdir(exist_ok=True)
    (sites / "ozon-pack").write_text(LEGACY_SITE % {"port": port})
    wg = tmp_path / "wireguard"
    wg.mkdir(exist_ok=True)
    snippet = tmp_path / "access.conf"

    env = dict(os.environ)
    env.update(
        PATH=f"{stub}:{env['PATH']}",
        SNIPPET=str(snippet),
        APP_DIR=str(tmp_path),
        APP_PORT=port,
        NGINX_SITES=str(sites),
        NGINX_CONFD=str(tmp_path / "conf.d"),
        WG_DIR=str(wg),
    )
    return env, snippet, wg


@pytest.mark.skipif(os.geteuid() != 0, reason="скрипт работает только от root")
def test_vpn_only_writes_rule_and_include(tmp_path, sandbox):
    env, snippet, _ = _sandbox_env(tmp_path, sandbox)
    site = tmp_path / "sites-enabled" / "ozon-pack"

    proc = _run(DEPLOY / "vpn-only.sh", ["--subnet", "10.8.0.0/24", "--yes"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    rule = snippet.read_text(encoding="utf-8")
    assert "allow 10.8.0.0/24;" in rule
    assert "allow 127.0.0.1;" in rule
    assert rule.rstrip().endswith("deny all;")

    patched = site.read_text(encoding="utf-8")
    blocks = _location_blocks(patched)
    include = f"include {snippet};"
    assert [b for b in blocks if "proxy_pass" in b and include in b]
    assert not [b for b in blocks if "acme-challenge" in b and include in b]

    # Повторный запуск ничего не дублирует
    assert _run(DEPLOY / "vpn-only.sh", ["--subnet", "10.8.0.0/24", "--yes"], env).returncode == 0
    assert site.read_text(encoding="utf-8").count(include) == 1

    # --off снимает ограничение, include остаётся на месте
    assert _run(DEPLOY / "vpn-only.sh", ["--off"], env).returncode == 0
    assert "allow all;" in snippet.read_text(encoding="utf-8")
    assert "deny all;" not in snippet.read_text(encoding="utf-8")


@pytest.mark.skipif(os.geteuid() != 0, reason="скрипт работает только от root")
def test_vpn_only_reads_subnet_from_wireguard_config(tmp_path, sandbox):
    env, snippet, wg = _sandbox_env(tmp_path, sandbox)
    (wg / "wg0.conf").write_text(
        textwrap.dedent(
            """\
            [Interface]
            Address = 10.9.0.1/24
            ListenPort = 51820
            """
        )
    )

    proc = _run(DEPLOY / "vpn-only.sh", ["--yes"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "allow 10.9.0.0/24;" in snippet.read_text(encoding="utf-8")


def test_setup_and_readme_mention_vpn_script():
    assert (DEPLOY / "vpn-only.sh").exists()
    assert shutil.which("bash")
    assert "vpn-only.sh" in (DEPLOY / "setup.sh").read_text(encoding="utf-8")
    assert "vpn-only.sh" in (BASE_DIR / "README.md").read_text(encoding="utf-8")


@pytest.mark.skipif(os.geteuid() != 0, reason="скрипт работает только от root")
@pytest.mark.parametrize(
    "value,expected",
    [
        ("10.8.0.0/24", ["allow 10.8.0.0/24;"]),
        # адрес хоста приводится к его сети
        ("10.8.0.1/24", ["allow 10.8.0.0/24;"]),
        # несколько значений через запятую
        ("10.8.0.0/24,10.9.0.0/24", ["allow 10.8.0.0/24;", "allow 10.9.0.0/24;"]),
        ("10.8.0.0/24, 10.9.0.0/24", ["allow 10.8.0.0/24;", "allow 10.9.0.0/24;"]),
        # один адрес без маски
        ("10.8.0.5", ["allow 10.8.0.5;"]),
        # IPv6-туннель
        ("fd42:42::/64", ["allow fd42:42::/64;"]),
    ],
)
def test_vpn_only_accepts_manual_subnet(tmp_path, sandbox, value, expected):
    env, snippet, _ = _sandbox_env(tmp_path, sandbox)
    proc = _run(DEPLOY / "vpn-only.sh", ["--subnet", value, "--yes"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    rule = snippet.read_text(encoding="utf-8")
    for line in expected:
        assert line in rule
    assert rule.rstrip().endswith("deny all;")


@pytest.mark.skipif(os.geteuid() != 0, reason="скрипт работает только от root")
@pytest.mark.parametrize(
    "value", ["10.8.0.0/33", "10.8.0.*", "10.8.0", "", "10.8.0.0/24,мусор", "10.8.0.0/24;drop"]
)
def test_vpn_only_rejects_bad_subnet(tmp_path, sandbox, value):
    """Опечатка в сети не должна доехать до nginx и переписать правило."""
    env, snippet, _ = _sandbox_env(tmp_path, sandbox)
    snippet.write_text("allow all;\n")

    proc = _run(DEPLOY / "vpn-only.sh", ["--subnet", value, "--yes"], env)
    assert proc.returncode != 0
    assert "Не понимаю" in proc.stdout + proc.stderr
    assert snippet.read_text(encoding="utf-8") == "allow all;\n"


@pytest.mark.skipif(os.geteuid() != 0, reason="скрипт работает только от root")
def test_vpn_only_needs_value_for_subnet(tmp_path, sandbox):
    env, _, _ = _sandbox_env(tmp_path, sandbox)
    proc = _run(DEPLOY / "vpn-only.sh", ["--subnet"], env)
    assert proc.returncode != 0
    assert "не указано значение" in proc.stdout + proc.stderr


@pytest.mark.skipif(os.geteuid() != 0, reason="скрипт работает только от root")
def test_vpn_only_manual_subnet_wins_over_wireguard_config(tmp_path, sandbox):
    """Указанная руками сеть заменяет найденную в /etc/wireguard, а не дополняет."""
    env, snippet, wg = _sandbox_env(tmp_path, sandbox)
    (wg / "wg0.conf").write_text("[Interface]\nAddress = 10.9.0.1/24\n")

    proc = _run(DEPLOY / "vpn-only.sh", ["--subnet", "10.8.0.0/24", "--yes"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    rule = snippet.read_text(encoding="utf-8")
    assert "allow 10.8.0.0/24;" in rule
    assert "10.9.0.0/24" not in rule


def test_setup_help_documents_vpn_only():
    proc = subprocess.run(
        ["bash", str(DEPLOY / "setup.sh"), "--help"], capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert "--vpn-only" in proc.stdout


def test_setup_refuses_vpn_only_without_nginx():
    """Без HTTPS нет и nginx — закрывать доступ нечем, лучше сказать сразу."""
    proc = subprocess.run(
        ["bash", str(DEPLOY / "setup.sh"), "--no-ssl", "--vpn-only"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "несовместим" in proc.stdout + proc.stderr


@pytest.mark.skipif(os.geteuid() != 0, reason="скрипт работает только от root")
def test_status_warns_when_rule_is_not_applied(tmp_path, sandbox):
    """Правило записано, но конфиг его не подключает — панель открыта, и это видно."""
    env, snippet, _ = _sandbox_env(tmp_path, sandbox)
    site = tmp_path / "sites-enabled" / "ozon-pack"
    snippet.write_text("allow 127.0.0.1;\nallow 10.0.0.0/24;\ndeny all;\n")
    assert f"include {snippet};" not in site.read_text(encoding="utf-8")

    proc = _run(DEPLOY / "vpn-only.sh", ["--status"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "панель открыта" in proc.stdout
    assert "включено — панель отвечает только" not in proc.stdout


@pytest.mark.skipif(os.geteuid() != 0, reason="скрипт работает только от root")
def test_status_confirms_applied_rule(tmp_path, sandbox):
    env, snippet, _ = _sandbox_env(tmp_path, sandbox)
    assert _run(DEPLOY / "vpn-only.sh", ["--subnet", "10.0.0.0/24", "--yes"], env).returncode == 0

    proc = _run(DEPLOY / "vpn-only.sh", ["--status"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "включено" in proc.stdout
    assert "10.0.0.0/24" in proc.stdout
