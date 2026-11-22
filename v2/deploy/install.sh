#!/usr/bin/env bash
# First installation of the separate v2 development instance. Contains no server credentials.
set -euo pipefail
umask 077
v2_checkout=/opt/ozon-pack-v2
v2_app=$v2_checkout/v2
v2_user=ozon-v2
v2_branch=refactor/modular-architecture
v2_units_dir=/etc/systemd/system
v2_runtime_dir=/run/systemd/system
v2_python=
v2_db_port=
v2_pg_version=
fail() { printf '%s\n' "$*" >&2; exit 1; }

# Sourcing defines functions without executing the installation; used by isolated contract tests.
v2_require_root() { [[ $EUID -eq 0 ]] || fail 'Запустите скрипт под root.'; }

v2_find_python() {
    local v2_candidate
    for v2_candidate in python3 python3.14 python3.13 python3.12; do
        if command -v "$v2_candidate" >/dev/null && "$v2_candidate" -c \
            'import sys, venv, ensurepip; assert sys.version_info >= (3, 12)' >/dev/null 2>&1; then
            v2_python=$v2_candidate
            return 0
        fi
    done
    fail 'Нужен Python 3.12 или новее с модулем venv. Установите python3 и python3-venv.'
}

v2_preflight() {
    local v2_env
    v2_require_root
    while IFS= read -r v2_env; do
        [[ $v2_env != PACK_V2_* ]] || fail 'В окружении уже заданы PACK_V2_*. Откройте чистую сессию для первой установки.'
    done < <(compgen -e)
    for v2_program in apt-get systemctl; do
        command -v "$v2_program" >/dev/null || fail "Не установлено: $v2_program. Установка остановлена до изменений."
    done
    [[ -d $v2_runtime_dir ]] || fail 'Нужен сервер с systemd.'
    [[ ! -e $v2_checkout ]] || fail "$v2_checkout уже существует. Этот скрипт предназначен для первой установки; ничего не перезаписано."
    if command -v ss >/dev/null; then v2_check_port; fi
    v2_check_services
}

v2_check_services() {
    local v2_listing v2_existing_units v2_unit v2_details
    for v2_listing in list-unit-files list-units; do
        # A filtered list-unit-files returns 1 for no matches on recent systemd.
        # Enumerate without a glob; actual enumeration/bus errors still stop installation.
        v2_existing_units=$(SYSTEMD_COLORS=0 SYSTEMD_URLIFY=0 systemctl "$v2_listing" --all --type=service --no-legend --no-pager --plain) || \
            fail "Не удалось проверить службы v2 ($v2_listing). Подробности systemctl — выше."
        while read -r v2_unit v2_details; do
            case "$v2_unit" in
                ozon-pack-v2-*.service) fail "Службы v2 уже существуют ($v2_unit). Ничего не изменено." ;;
            esac
        done <<< "$v2_existing_units"
    done
}

v2_check_port() {
    local v2_listeners
    v2_listeners=$(ss -H -ltn '( sport = :8001 )') || fail 'Не удалось проверить занятость порта панели.'
    [[ -z $v2_listeners ]] || fail 'Порт 8001 занят. Ничего не перезаписано.'
}

v2_psql() {
    (cd /; runuser -u postgres -- psql -X --quiet --no-password \
        --set=ON_ERROR_STOP=1 --set=VERBOSITY=terse --host=/var/run/postgresql \
        --port="$v2_db_port" --username=postgres --dbname=postgres "$@")
}

v2_database_preflight() {
    local v2_clusters v2_version v2_name v2_port v2_status v2_owner v2_rest v2_best=0 v2_chosen_status v2_server v2_exists
    v2_clusters=$(pg_lsclusters --no-header) || fail 'Не удалось получить список кластеров PostgreSQL.'
    while read -r v2_version v2_name v2_port v2_status v2_owner v2_rest; do
        if [[ $v2_version =~ ^[0-9]+$ && $v2_name == main && $v2_owner == postgres ]] && \
            (( v2_version >= 16 && v2_version > v2_best )); then
            v2_best=$v2_version
            v2_pg_version=$v2_version
            v2_db_port=$v2_port
            v2_chosen_status=$v2_status
        fi
    done <<< "$v2_clusters"
    (( v2_best >= 16 )) || fail 'Нужна системная PostgreSQL 16 или новее с кластером main.'
    [[ $v2_db_port =~ ^[0-9]+$ ]] && (( v2_db_port >= 1 && v2_db_port <= 65535 )) || fail 'Некорректный порт PostgreSQL.'
    if [[ $v2_chosen_status == down ]]; then
        pg_ctlcluster "$v2_pg_version" main start
    elif [[ $v2_chosen_status != online ]]; then
        fail 'Кластер PostgreSQL недоступен для записи.'
    fi
    v2_server=$(v2_psql -Atc 'SHOW server_version_num') || fail 'Не удалось подключиться к системной PostgreSQL от postgres.'
    [[ $v2_server =~ ^[0-9]+$ ]] && (( v2_server >= 160000 )) || fail 'Нужна PostgreSQL 16 или новее.'
    v2_exists=$(v2_psql -Atc "SELECT (EXISTS (SELECT FROM pg_roles WHERE rolname='ozon_pack_v2') OR EXISTS (SELECT FROM pg_database WHERE datname='ozon_pack_v2'))::int") || fail 'Не удалось проверить базу v2.'
    [[ $v2_exists == 0 ]] || fail 'База или роль ozon_pack_v2 уже существует. Перезапись запрещена.'
}

v2_install() {
    printf '%s\n' 'Установка отдельной v2. Каталог старой панели /opt/ozon-pack не используется.'
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 python3-venv git curl ca-certificates iproute2 postgresql postgresql-client util-linux
    v2_find_python
    v2_check_port
    v2_database_preflight
    git clone --single-branch --branch "$v2_branch" https://github.com/kkkola000/ozon-pack.git "$v2_checkout"
    cd "$v2_app"
    "$v2_python" -m venv .venv
    .venv/bin/python -m pip install -r requirements.lock
    .venv/bin/python -m pip install --no-deps -e .
    .venv/bin/python -m pip check
    .venv/bin/python -m pack_v2 generate-env --native-postgres --database-port "$v2_db_port"
    .venv/bin/python "$v2_app/deploy/native_database.py" --env-file .env.v2 | v2_psql
    .venv/bin/python -m pack_v2 --env-file .env.v2 migrate
    .venv/bin/python -m pack_v2 --env-file .env.v2 check
    printf '%s\n' 'Создайте пароль владельца новой панели (не менее 12 символов).'
    .venv/bin/python -m pack_v2 --env-file .env.v2 create-owner --login admin
    if ! id "$v2_user" >/dev/null 2>&1; then
        useradd --system --user-group --home-dir "$v2_checkout" --no-create-home --shell /usr/sbin/nologin "$v2_user"
    fi
    chown -R "$v2_user:$(id -gn "$v2_user")" "$v2_checkout"
    chmod 600 .env.v2
    for v2_part in web worker; do
        if [[ $v2_part == web ]]; then
            v2_exec="$v2_app/.venv/bin/uvicorn pack_v2.bootstrap.app:create_app --factory --env-file $v2_app/.env.v2 --host 127.0.0.1 --port 8001"
        else
            v2_exec="$v2_app/.venv/bin/python -m pack_v2 --env-file $v2_app/.env.v2 worker"
        fi
        cat > "$v2_units_dir/ozon-pack-v2-$v2_part.service" <<UNIT
[Unit]
Description=Ozon Pack v2 $v2_part
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

[Service]
Type=simple
User=$v2_user
Group=$(id -gn "$v2_user")
WorkingDirectory=$v2_app
ExecStart=$v2_exec
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT
        chmod 644 "$v2_units_dir/ozon-pack-v2-$v2_part.service"
    done
    systemctl daemon-reload
    systemctl enable --now postgresql.service ozon-pack-v2-web.service ozon-pack-v2-worker.service
    curl --fail --silent --show-error --retry 15 --retry-connrefused --retry-delay 1 --retry-max-time 45 http://127.0.0.1:8001/readyz
    printf '\n'
    curl --fail --silent --show-error --output /dev/null http://127.0.0.1:8001/login
    systemctl is-active --quiet ozon-pack-v2-web.service
    systemctl is-active --quiet ozon-pack-v2-worker.service
    printf '%s\n' 'v2 установлена. Логин: admin. Панель слушает только 127.0.0.1:8001.'
    printf '%s\n' 'Для браузера используйте SSH-туннель. Старая панель и её база не изменялись.'

}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    v2_preflight
    v2_install
fi
