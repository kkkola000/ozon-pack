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
fail() { printf '%s\n' "$*" >&2; exit 1; }

# Sourcing defines functions without executing the installation; used by isolated contract tests.
v2_require_root() { [[ $EUID -eq 0 ]] || fail 'Запустите скрипт под root.'; }

v2_preflight() {
    local v2_listeners v2_containers v2_volumes v2_volume v2_existing_units v2_listing v2_env
    v2_require_root
    while IFS= read -r v2_env; do
        [[ $v2_env != PACK_V2_* ]] || fail 'В окружении уже заданы PACK_V2_*. Откройте чистую сессию для первой установки.'
    done < <(compgen -e)
    for v2_program in git python3.12 docker systemctl ss curl useradd; do
        command -v "$v2_program" >/dev/null || fail "Не установлено: $v2_program. Установка остановлена до изменений."
    done
    python3.12 -c 'import sys, venv, ensurepip; assert sys.version_info >= (3, 12)' || fail 'Нужен Python 3.12 с модулем venv.'
    docker compose version >/dev/null || fail 'Нужен Docker Compose v2.'
    docker info >/dev/null 2>&1 || fail 'Docker не запущен или недоступен.'
    [[ -d $v2_runtime_dir ]] || fail 'Нужен сервер с systemd.'
    [[ ! -e $v2_checkout ]] || fail "$v2_checkout уже существует. Этот скрипт предназначен для первой установки; ничего не перезаписано."
    v2_listeners=$(ss -H -ltn '( sport = :8001 or sport = :55432 )') || fail 'Не удалось проверить занятость портов.'
    [[ -z $v2_listeners ]] || fail 'Порт 8001 или 55432 занят. Ничего не изменено.'
    v2_containers=$(docker ps -aq --filter label=com.docker.compose.project=ozon-pack-v2) || fail 'Не удалось проверить контейнеры v2.'
    [[ -z $v2_containers ]] || fail 'Контейнеры v2 уже существуют. Установка остановлена для сохранения их данных.'
    v2_volumes=$(docker volume ls --format '{{.Name}}') || fail 'Не удалось проверить тома Docker.'
    while IFS= read -r v2_volume; do
        [[ $v2_volume != ozon-pack-v2_postgres_v2 ]] || fail 'Том PostgreSQL v2 уже существует. Установка остановлена для сохранения его данных.'
    done <<< "$v2_volumes"
    for v2_listing in list-unit-files list-units; do
        v2_existing_units=$(systemctl "$v2_listing" --all --type=service --no-legend --no-pager 'ozon-pack-v2-*.service') || fail 'Не удалось проверить службы v2.'
        [[ -z $v2_existing_units ]] || fail 'Службы v2 уже существуют. Ничего не изменено.'
    done
}

v2_install() {
    printf '%s\n' 'Установка отдельной v2. Каталог старой панели /opt/ozon-pack не используется.'
    git clone --single-branch --branch "$v2_branch" https://github.com/kkkola000/ozon-pack.git "$v2_checkout"
    cd "$v2_app"
    python3.12 -m venv .venv
    .venv/bin/python -m pip install -r requirements.lock
    .venv/bin/python -m pip install --no-deps -e .
    .venv/bin/python -m pip check
    .venv/bin/python -m pack_v2 generate-env
    docker compose --project-name ozon-pack-v2 --file "$v2_app/compose.yaml" \
        --env-file .env.v2 up -d --wait --wait-timeout 120 postgres
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
After=network-online.target docker.service
Wants=network-online.target

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
    systemctl enable --now docker.service ozon-pack-v2-web.service ozon-pack-v2-worker.service
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
