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


def test_setup_installs_panel_only():
    """Установщик ставит панель и HTTPS; кто может входить — отдельная команда."""
    proc = subprocess.run(
        ["bash", str(DEPLOY / "setup.sh"), "--help"], capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert "--vpn-only" not in proc.stdout
    # но подсказка, чем настраивается доступ, в справке остаётся
    assert "vpn-only.sh" in proc.stdout


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


def test_install_resets_before_checkout():
    """«checkout -B» падает на правках, сделанных прямо на сервере, — reset идёт первым.

    Именно так ломалось обновление: если на сервере поправили отслеживаемый файл
    (или положили руками тот, что появляется в новой версии), git отказывался
    переключать ветку и установка обрывалась.
    """
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    reset_at = text.find("reset --hard FETCH_HEAD")
    checkout_at = text.find('checkout -B "$BRANCH"')
    assert reset_at > 0 and checkout_at > 0
    assert reset_at < checkout_at, "reset --hard должен идти до checkout -B"
    assert 'checkout -B "$BRANCH" FETCH_HEAD' not in text


@pytest.mark.parametrize("script", ["install.sh", "ssl.sh"], ids=lambda n: n)
def test_error_trap_names_the_command(script):
    """Сообщение об ошибке должно называть команду, а не строку с объявлением функции."""
    text = (DEPLOY / script).read_text(encoding="utf-8")
    assert "trap 'on_error $LINENO' ERR" in text
    assert "BASH_COMMAND" in text


@pytest.mark.skipif(os.geteuid() != 0, reason="установщик работает только от root")
def test_install_error_message_is_useful():
    proc = subprocess.run(
        ["bash", str(DEPLOY / "install.sh"), "--dir", "/proc/nope", "--yes"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    output = proc.stdout + proc.stderr
    assert "Команда: mkdir" in output


# ---------------------------------------------------------------- прямой доступ
# Панель ставится за nginx, но слушает она свой порт сама. Если этот порт открыт
# наружу, в панель заходят по адресу сервера мимо VPN и мимо https — правило
# nginx такой вход не видит. Раньше скрипты «закрывали» дыру, записывая HOST в
# .env, но юнит systemd держал адрес захардкоженным, и правка ни на что не
# влияла: оператору сообщали об успехе, а порт оставался открыт.

def test_service_unit_takes_address_from_env():
    """Адрес и порт службы — из .env, иначе HOST=127.0.0.1 ничего не изменит."""
    unit = (DEPLOY / "ozon-pack.service").read_text(encoding="utf-8")
    exec_line = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
    assert len(exec_line) == 1, unit
    assert "--host ${HOST}" in exec_line[0], exec_line[0]
    assert "--host 0.0.0.0" not in exec_line[0], "адрес снова захардкожен"
    assert "--port ${PORT}" in exec_line[0], exec_line[0]

    # Умолчания на случай отсутствующего .env, и он же подключён следом
    assert "Environment=HOST=0.0.0.0" in unit
    assert unit.index("Environment=HOST=") < unit.index("EnvironmentFile=")


def test_install_does_not_patch_address_or_port_in_unit():
    """install.sh правит юнит по каталогу и пользователю, но не по порту."""
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    assert "s#--port 8080#" not in text, "порт снова подставляется в юнит"
    assert "s#--host" not in text


def test_install_leaves_port_closed_for_localhost_panel():
    """Панели за nginx порт в ufw не открываем — правило вводило бы в заблуждение."""
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    firewall = text[text.index('step "Файрвол"'):]
    firewall = firewall[:firewall.index("\nfi\n")]
    assert "127.0.0.1" in firewall, "порт открывается независимо от HOST"
    assert 'ufw allow "$PORT"/tcp' in firewall


def test_env_example_documents_access_variables():
    text = (BASE_DIR / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^HOST=127\.0\.0\.1$", text, re.M), "по умолчанию панель должна слушать localhost"
    assert re.search(r"^FORWARDED_ALLOW_IPS=127\.0\.0\.1$", text, re.M)


def test_only_one_way_to_install():
    """Путь установки один: служба systemd за nginx с ограничением по VPN.

    Альтернативы убраны намеренно — каждая была ещё одной дверью, которую
    можно было открыть по невнимательности.
    """
    for gone in ("Dockerfile", "docker-compose.yml", "deploy/nginx.conf"):
        assert not (BASE_DIR / gone).exists(), f"{gone} вернулся"
    assert sorted(p.name for p in (BASE_DIR / "deploy").iterdir()) == [
        "install.sh", "ozon-pack.service", "setup.sh", "ssl.sh", "vpn-only.sh"
    ]


@pytest.fixture
def port_sandbox(tmp_path):
    """Песочница, где ss показывает порт панели и слушается он снаружи.

    ss отвечает по содержимому .env: пока в нём нет HOST=127.0.0.1, порт «висит»
    на 0.0.0.0 — ровно как на сервере до правки.
    """
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "nginx").write_text("#!/bin/sh\nexit 0\n")
    (stub / "curl").write_text("#!/bin/sh\nexit 0\n")
    (stub / "systemctl").write_text(
        "#!/bin/sh\n"
        'case "$1" in list-unit-files) echo "ozon-pack.service enabled enabled" ;; esac\n'
        "exit 0\n"
    )
    (stub / "ss").write_text(
        "#!/bin/sh\n"
        'if grep -q "^HOST=127.0.0.1" "$APP_DIR/.env" 2>/dev/null; then\n'
        '  echo "LISTEN 0 511 127.0.0.1:8080 0.0.0.0:*"\n'
        "else\n"
        '  echo "LISTEN 0 511 0.0.0.0:8080 0.0.0.0:*"\n'
        "fi\n"
    )
    for name in ("nginx", "systemctl", "ss", "curl"):
        os.chmod(stub / name, 0o755)
    return stub


@pytest.mark.skipif(os.geteuid() != 0, reason="скрипт работает только от root")
def test_vpn_only_closes_direct_port_and_checks_it(tmp_path, port_sandbox):
    """Скрипт пишет HOST=127.0.0.1 и убеждается, что порт действительно закрылся."""
    env, _snippet, _wg = _sandbox_env(tmp_path, port_sandbox)
    env.update(VERIFY_TRIES="2", VERIFY_DELAY="0")
    (tmp_path / ".env").write_text("HOST=0.0.0.0\nPORT=8080\n", encoding="utf-8")

    proc = _run(DEPLOY / "vpn-only.sh", ["--subnet", "10.8.0.0/24", "--yes"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "HOST=127.0.0.1" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "прямой доступ закрыт" in proc.stdout
    assert "панель доступна только через VPN" in proc.stdout


@pytest.mark.skipif(os.geteuid() != 0, reason="скрипт работает только от root")
def test_vpn_only_reports_port_that_stays_open(tmp_path, port_sandbox):
    """Порт остался открыт — это провал, а не «Готово»."""
    env, _snippet, _wg = _sandbox_env(tmp_path, port_sandbox)
    env.update(VERIFY_TRIES="2", VERIFY_DELAY="0")
    (tmp_path / ".env").write_text("HOST=0.0.0.0\nPORT=8080\n", encoding="utf-8")
    # Панель игнорирует .env — так вёл себя юнит с захардкоженным --host 0.0.0.0
    (port_sandbox / "ss").write_text(
        '#!/bin/sh\necho "LISTEN 0 511 0.0.0.0:8080 0.0.0.0:*"\n'
    )
    os.chmod(port_sandbox / "ss", 0o755)

    proc = _run(DEPLOY / "vpn-only.sh", ["--subnet", "10.8.0.0/24", "--yes"], env)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "прямой доступ НЕ закрыт" in proc.stdout
    assert "панель доступна только через VPN" not in proc.stdout


@pytest.mark.skipif(os.geteuid() != 0, reason="скрипт работает только от root")
def test_vpn_only_status_warns_about_open_port(tmp_path, port_sandbox):
    """--status показывает открытый порт, но ничего не меняет."""
    env, snippet, _wg = _sandbox_env(tmp_path, port_sandbox)
    snippet.write_text("allow 127.0.0.1;\nallow 10.8.0.0/24;\ndeny all;\n")
    (tmp_path / ".env").write_text("HOST=0.0.0.0\nPORT=8080\n", encoding="utf-8")

    proc = _run(DEPLOY / "vpn-only.sh", ["--status"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "панель слушает мимо nginx" in proc.stdout
    assert "HOST=0.0.0.0" in (tmp_path / ".env").read_text(encoding="utf-8")


# ---------------------------------------------------------------- обновление
# Настройки, появившиеся в новой версии, должны доходить до уже установленных
# панелей: иначе оператор о них не узнает, а прежние значения трогать нельзя.

def _run_env_migration(tmp_path, env_text):
    """Выполнить add_missing_env_keys из install.sh над подготовленным .env."""
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    shutil.copy(BASE_DIR / ".env.example", tmp_path / ".env.example")
    script = textwrap.dedent(f"""
        set -eu
        APP_DIR={tmp_path}
        info() {{ printf '%s\\n' "$*"; }}
        {_shell_function(DEPLOY / "install.sh", "add_missing_env_keys")}
        add_missing_env_keys
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return (tmp_path / ".env").read_text(encoding="utf-8"), proc.stdout


def _shell_function(script, name):
    text = script.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def test_update_adds_new_env_keys(tmp_path):
    old_env = "OZON_CLIENT_ID=123456\nPORT=8080\nIP_ALLOWLIST=10.8.0.0/24\n"
    result, output = _run_env_migration(tmp_path, old_env)

    # Появившиеся настройки доступа дописаны со значениями по умолчанию
    assert re.search(r"^FORWARDED_ALLOW_IPS=127\.0\.0\.1$", result, re.M)
    assert "FORWARDED_ALLOW_IPS" in output


def test_update_keeps_operator_values(tmp_path):
    old_env = "OZON_CLIENT_ID=123456\nPORT=9000\nIP_ALLOWLIST=10.8.0.0/24\nHOST=127.0.0.1\n"
    result, _ = _run_env_migration(tmp_path, old_env)

    for line in ("OZON_CLIENT_ID=123456", "PORT=9000", "IP_ALLOWLIST=10.8.0.0/24", "HOST=127.0.0.1"):
        assert re.search(rf"^{re.escape(line)}$", result, re.M), line
    # Ни одного дубля: значение оператора должно остаться единственным
    for key in ("PORT", "HOST", "IP_ALLOWLIST", "OZON_CLIENT_ID"):
        assert len(re.findall(rf"^{key}=", result, re.M)) == 1, key


def test_update_is_idempotent(tmp_path):
    once, _ = _run_env_migration(tmp_path, "PORT=8080\n")
    twice, output = _run_env_migration(tmp_path, once)
    assert once == twice
    assert "добавлены новые настройки" not in output


def test_readme_warns_that_restart_alone_is_not_enough():
    """Юнит с захардкоженным адресом обновляется только пересборкой."""
    text = (BASE_DIR / "README.md").read_text(encoding="utf-8")
    assert "### Обновление установленной панели" in text
    section = text[text.index("### Обновление установленной панели"):]
    section = section[:section.index("### Полезные команды")]
    assert "install.sh" in section
    assert "systemctl restart` недостаточно" in section
    assert "vpn-only.sh" in section and "ssl.sh" in section


# ---------------------------------------------------------------- только из VPN
# Ограничение должно стоять на двух уровнях сразу: правило nginx и список
# адресов в самой панели. Один nginx — единственная точка отказа: конфиг сайта
# может потерять строку include, и панель молча откроется всем.

@pytest.mark.skipif(os.geteuid() != 0, reason="скрипт работает только от root")
def test_vpn_only_closes_panel_itself_too(tmp_path, port_sandbox):
    env, _snippet, _wg = _sandbox_env(tmp_path, port_sandbox)
    env.update(VERIFY_TRIES="1", VERIFY_DELAY="0")
    (tmp_path / ".env").write_text("HOST=0.0.0.0\nPORT=8080\nIP_ALLOWLIST=\n", encoding="utf-8")

    proc = _run(DEPLOY / "vpn-only.sh", ["--subnet", "10.8.0.0/24", "--allow", "203.0.113.10", "--yes"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    allowlist = re.search(r"^IP_ALLOWLIST=(.*)$", env_text, re.M).group(1)
    assert "10.8.0.0/24" in allowlist
    assert "203.0.113.10" in allowlist
    # Свой сервер в списке обязателен: через него ходят проверки и путь по SSH
    assert "127.0.0.1" in allowlist


@pytest.mark.skipif(os.geteuid() != 0, reason="скрипт работает только от root")
def test_vpn_only_off_opens_both_levels(tmp_path, port_sandbox):
    """Иначе nginx открыт, а панель закрыта — и это ищут как поломку."""
    env, snippet, _wg = _sandbox_env(tmp_path, port_sandbox)
    env.update(VERIFY_TRIES="1", VERIFY_DELAY="0")
    (tmp_path / ".env").write_text(
        "HOST=127.0.0.1\nPORT=8080\nIP_ALLOWLIST=127.0.0.1,10.8.0.0/24\n", encoding="utf-8"
    )

    assert _run(DEPLOY / "vpn-only.sh", ["--off"], env).returncode == 0
    assert "allow all;" in snippet.read_text(encoding="utf-8")
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert re.search(r"^IP_ALLOWLIST=\s*$", env_text, re.M), env_text


@pytest.mark.skipif(os.geteuid() != 0, reason="скрипт работает только от root")
def test_vpn_only_status_reports_both_levels(tmp_path, port_sandbox):
    env, snippet, _wg = _sandbox_env(tmp_path, port_sandbox)
    snippet.write_text("allow 127.0.0.1;\nallow 10.8.0.0/24;\ndeny all;\n")
    (tmp_path / ".env").write_text("IP_ALLOWLIST=127.0.0.1,10.8.0.0/24\n", encoding="utf-8")

    proc = _run(DEPLOY / "vpn-only.sh", ["--status"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Ограничение в самой панели" in proc.stdout
    assert "IP_ALLOWLIST=127.0.0.1,10.8.0.0/24" in proc.stdout


def test_install_vpn_subnet_closes_panel(tmp_path):
    """--vpn-subnet: панель слушает localhost и сверяет адрес сама."""
    (tmp_path / ".env").write_text("OZON_CLIENT_ID=123\nHOST=0.0.0.0\nPORT=8080\n", encoding="utf-8")
    script = textwrap.dedent(f"""
        set -eu
        APP_DIR={tmp_path}
        info() {{ printf '%s\\n' "$*"; }}
        warn() {{ printf '%s\\n' "$*"; }}
        {_shell_function(DEPLOY / "install.sh", "apply_vpn_settings")}
        apply_vpn_settings "10.8.0.0/24"
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    result = (tmp_path / ".env").read_text(encoding="utf-8")
    assert re.search(r"^HOST=127\.0\.0\.1$", result, re.M)
    assert re.search(r"^IP_ALLOWLIST=127\.0\.0\.1,10\.8\.0\.0/24$", result, re.M)
    # Значения оператора не тронуты, дублей нет
    assert re.search(r"^OZON_CLIENT_ID=123$", result, re.M)
    assert len(re.findall(r"^HOST=", result, re.M)) == 1


def test_install_with_vpn_does_not_open_port_in_firewall():
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    firewall = text[text.index('step "Файрвол"'):]
    firewall = firewall[:firewall.index("\nfi\n")]
    assert "VPN_SUBNET" in firewall, "порт открывается даже при установке с VPN"


def test_ssl_creates_restrictive_rule_when_allowlist_is_set(tmp_path):
    """Иначе nginx открыл бы то, что панель закрывает, и вместо отказа на
    входе оператор увидел бы 403 из самой панели."""
    (tmp_path / ".env").write_text(
        "HOST=127.0.0.1\nIP_ALLOWLIST=127.0.0.1,10.8.0.0/24,203.0.113.10\n", encoding="utf-8"
    )
    snippet = tmp_path / "access.conf"
    script = textwrap.dedent(f"""
        set -eu
        APP_DIR={tmp_path}
        ACCESS_SNIPPET={snippet}
        info() {{ printf '%s\\n' "$*"; }}
        {_shell_function(DEPLOY / "ssl.sh", "ensure_access_snippet")}
        ensure_access_snippet
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    rule = snippet.read_text(encoding="utf-8")
    assert "allow 10.8.0.0/24;" in rule
    assert "allow 203.0.113.10;" in rule
    assert rule.rstrip().endswith("deny all;")
    assert "allow all;" not in rule


def test_ssl_keeps_rule_open_without_allowlist(tmp_path):
    """Установка без VPN не должна внезапно закрываться."""
    (tmp_path / ".env").write_text("HOST=0.0.0.0\nIP_ALLOWLIST=\n", encoding="utf-8")
    snippet = tmp_path / "access.conf"
    script = textwrap.dedent(f"""
        set -eu
        APP_DIR={tmp_path}
        ACCESS_SNIPPET={snippet}
        info() {{ printf '%s\\n' "$*"; }}
        {_shell_function(DEPLOY / "ssl.sh", "ensure_access_snippet")}
        ensure_access_snippet
    """)
    assert subprocess.run(["bash", "-c", script], capture_output=True, text=True).returncode == 0
    assert "allow all;" in snippet.read_text(encoding="utf-8")
