#!/usr/bin/env bash
#
# Автоустановка Ozon Pack на Ubuntu/Debian прямо с GitHub.
#
#   curl -fsSL https://raw.githubusercontent.com/kkkola000/ozon-pack/HEAD/deploy/install.sh | sudo bash
#
# Повторный запуск обновляет код до свежего коммита и перезапускает службу,
# не трогая .env, базу и журнал сборки.
#
# Настройки — переменными окружения или флагами:
#   OZON_CLIENT_ID=123 OZON_API_KEY=xxx PORT=8080 sudo -E bash install.sh
#   sudo bash install.sh --port 9000 --branch ИМЯ-ВЕТКИ --demo
#
set -Eeuo pipefail

REPO_URL=${REPO_URL:-https://github.com/kkkola000/ozon-pack.git}
BRANCH=${BRANCH:-}
BRANCH_EXPLICIT=0
[ -n "$BRANCH" ] && BRANCH_EXPLICIT=1
APP_DIR=${APP_DIR:-/opt/ozon-pack}
APP_USER=${APP_USER:-ozon}
SERVICE=${SERVICE:-ozon-pack}
PORT=${PORT:-8080}
PORT_EXPLICIT=0
OZON_CLIENT_ID=${OZON_CLIENT_ID:-}
OZON_API_KEY=${OZON_API_KEY:-}
ADMIN_LOGIN=${ADMIN_LOGIN:-admin}
ADMIN_PASSWORD=${ADMIN_PASSWORD:-}
OZON_DEMO=${OZON_DEMO:-0}
SKIP_FIREWALL=${SKIP_FIREWALL:-0}
SKIP_SERVICE=${SKIP_SERVICE:-0}
NONINTERACTIVE=${NONINTERACTIVE:-0}

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$OFF"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%s[!] %s%s\n' "$YELLOW" "$*" "$OFF"; }
die()  { printf '%s[x] %s%s\n' "$RED" "$*" "$OFF" >&2; exit 1; }

trap 'die "Установка прервана на строке $LINENO. Вывод выше объясняет причину."' ERR

usage() {
  [ -r "$0" ] && sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'USAGE'

Флаги:
  --repo URL         репозиторий (по умолчанию kkkola000/ozon-pack)
  --branch NAME      ветка (по умолчанию — ветка по умолчанию в репозитории)
  --dir PATH         каталог установки (по умолчанию /opt/ozon-pack)
  --port N           порт панели (по умолчанию 8080)
  --user NAME        системный пользователь службы (по умолчанию ozon)
  --demo             поставить в демо-режиме, без ключей Ozon
  --no-firewall      не трогать ufw
  --no-service       не ставить службу systemd (для контейнеров/WSL)
  --yes              ничего не спрашивать
  -h, --help         эта справка
USAGE
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO_URL=$2; shift 2 ;;
    --branch) BRANCH=$2; BRANCH_EXPLICIT=1; shift 2 ;;
    --dir) APP_DIR=$2; shift 2 ;;
    --port) PORT=$2; PORT_EXPLICIT=1; shift 2 ;;
    --user) APP_USER=$2; shift 2 ;;
    --demo) OZON_DEMO=1; shift ;;
    --no-firewall) SKIP_FIREWALL=1; shift ;;
    --no-service) SKIP_SERVICE=1; shift ;;
    --yes|-y) NONINTERACTIVE=1; shift ;;
    -h|--help) usage ;;
    *) die "Неизвестный аргумент: $1 (--help для справки)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "Запустите с правами root: sudo bash $0"
command -v apt-get >/dev/null || die "Скрипт рассчитан на Ubuntu/Debian (нет apt-get)"
case "$PORT" in ''|*[!0-9]*) die "Некорректный порт: $PORT" ;; esac

UPDATE=0
[ -d "$APP_DIR/.git" ] && UPDATE=1

# ------------------------------------------------------------------ пакеты
step "Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates sqlite3 >/dev/null
info "python $(python3 -V 2>&1 | awk '{print $2}'), git $(git --version | awk '{print $3}')"

# ------------------------------------------------------------------ ветка
step "Репозиторий $REPO_URL"
if [ -z "$BRANCH" ]; then
  # Ветка по умолчанию в самом репозитории; если её не видно — единственная ветка.
  BRANCH=$(git ls-remote --symref "$REPO_URL" HEAD 2>/dev/null | awk '/^ref:/ {sub("refs/heads/","",$2); print $2; exit}')
  if [ -n "$BRANCH" ] && ! git ls-remote --heads "$REPO_URL" "$BRANCH" | grep -q .; then
    BRANCH=""
  fi
  if [ -z "$BRANCH" ]; then
    mapfile -t heads < <(git ls-remote --heads "$REPO_URL" | awk '{sub("refs/heads/","",$2); print $2}')
    [ "${#heads[@]}" -eq 0 ] && die "В репозитории нет веток — проверьте адрес: $REPO_URL"
    [ "${#heads[@]}" -gt 1 ] && die "Не удалось определить ветку. Укажите явно: --branch <имя>"
    BRANCH=${heads[0]}
  fi
fi
info "ветка: $BRANCH"
# Адрес этого же скрипта на GitHub — пригодится в подсказке про обновление.
# Без явной ветки берём HEAD: он всегда указывает на ветку по умолчанию.
RAW_REF=HEAD
[ "$BRANCH_EXPLICIT" = "1" ] && RAW_REF=$BRANCH
RAW_URL="$(echo "$REPO_URL" | sed 's#github.com#raw.githubusercontent.com#; s#\.git$##')/$RAW_REF/deploy/install.sh"

# ------------------------------------------------------------------ пользователь и код
step "Пользователь $APP_USER и каталог $APP_DIR"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"

# Каталог принадлежит $APP_USER, а скрипт работает от root — без safe.directory
# git отказывается с «dubious ownership». Задаём его точечно, не трогая ~/.gitconfig.
git_app() { git -c safe.directory="$APP_DIR" -C "$APP_DIR" "$@"; }

if [ "$UPDATE" -eq 1 ]; then
  step "Обновление кода"
  git_app remote set-url origin "$REPO_URL"
  git_app fetch --depth 1 origin "$BRANCH"
  # .env, data/ и .venv не в индексе — reset их не затрагивает
  git_app checkout -B "$BRANCH" FETCH_HEAD
  git_app reset --hard FETCH_HEAD
else
  step "Загрузка кода"
  if [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
    die "Каталог $APP_DIR не пуст и не является git-репозиторием. Уберите его или задайте --dir"
  fi
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
info "коммит: $(git_app log --oneline -1)"
BUILD="$(cat "$APP_DIR/VERSION" 2>/dev/null || true) ($(git_app rev-parse --short HEAD))"
mkdir -p "$APP_DIR/data"

# ------------------------------------------------------------------ окружение python
step "Окружение Python"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
info "зависимости установлены"

# ------------------------------------------------------------------ .env
step "Настройки (.env)"
if [ -f "$APP_DIR/.env" ]; then
  info "существующий .env сохранён без изменений"
  ADMIN_PASSWORD=""
  CURRENT_PORT=$(awk -F= '/^PORT=/{print $2}' "$APP_DIR/.env" | tail -1)
  if [ -n "$CURRENT_PORT" ]; then
    if [ "$PORT_EXPLICIT" = "1" ] && [ "$CURRENT_PORT" != "$PORT" ]; then
      warn "Порт берётся из .env ($CURRENT_PORT), флаг --port $PORT игнорируется."
      warn "Чтобы сменить порт: измените PORT в $APP_DIR/.env и запустите скрипт снова."
    fi
    PORT=$CURRENT_PORT
  fi
else
  # Ключи можно ввести с терминала, даже когда скрипт пришёл через curl | bash
  if [ -z "$OZON_CLIENT_ID$OZON_API_KEY" ] && [ "$OZON_DEMO" != "1" ] && [ "$NONINTERACTIVE" != "1" ] && [ -r /dev/tty ]; then
    printf '    Ключи Ozon Seller API (Настройки -> Seller API в личном кабинете).\n'
    printf '    Можно пропустить — панель поднимется в демо-режиме.\n'
    printf '    Client-Id: '; read -r OZON_CLIENT_ID </dev/tty || true
    printf '    Api-Key:   '; read -r OZON_API_KEY </dev/tty || true
  fi
  [ -n "$ADMIN_PASSWORD" ] || ADMIN_PASSWORD=$(python3 -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(14)))")

  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  # Значения передаём через окружение, а не подстановкой в текст скрипта
  OZON_CLIENT_ID="$OZON_CLIENT_ID" OZON_API_KEY="$OZON_API_KEY" OZON_DEMO="$OZON_DEMO" \
  PORT="$PORT" ADMIN_LOGIN="$ADMIN_LOGIN" ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  python3 - "$APP_DIR/.env" <<'PYEOF'
import os, re, sys

path = sys.argv[1]
text = open(path, encoding="utf-8").read()
for key in ("OZON_CLIENT_ID", "OZON_API_KEY", "OZON_DEMO", "PORT", "ADMIN_LOGIN", "ADMIN_PASSWORD"):
    value = os.environ.get(key, "").strip()
    text = re.sub(rf"^{key}=.*$", key + "=" + value, text, flags=re.M)
open(path, "w", encoding="utf-8").write(text)
PYEOF
  info "создан $APP_DIR/.env"
fi
chmod 600 "$APP_DIR/.env"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

if grep -q '^OZON_CLIENT_ID=$' "$APP_DIR/.env" 2>/dev/null && grep -q '^OZON_API_KEY=$' "$APP_DIR/.env" 2>/dev/null; then
  DEMO_MODE=1
else
  DEMO_MODE=0
fi

# ------------------------------------------------------------------ служба
have_systemd() { [ -d /run/systemd/system ] && command -v systemctl >/dev/null; }

if [ "$SKIP_SERVICE" = "1" ] || ! have_systemd; then
  [ "$SKIP_SERVICE" = "1" ] || warn "systemd не обнаружен — служба не установлена"
  step "Запуск вручную"
  info "cd $APP_DIR && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $PORT"
  SERVICE_INSTALLED=0
else
  step "Служба systemd ($SERVICE)"
  # Юнит из репозитория, подставляем каталог, пользователя и порт
  sed -e "s#/opt/ozon-pack#$APP_DIR#g" \
      -e "s#--port 8080#--port $PORT#" \
      -e "s#^User=.*#User=$APP_USER#" \
      -e "s#^Group=.*#Group=$APP_USER#" \
      "$APP_DIR/deploy/ozon-pack.service" > "/etc/systemd/system/$SERVICE.service"
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$SERVICE"
  SERVICE_INSTALLED=1
fi

# ------------------------------------------------------------------ файрвол
if [ "$SKIP_FIREWALL" != "1" ] && command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  step "Файрвол"
  ufw allow "$PORT"/tcp >/dev/null && info "открыт порт $PORT/tcp"
fi

# ------------------------------------------------------------------ проверка
if [ "${SERVICE_INSTALLED:-0}" = "1" ]; then
  step "Проверка"
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
      HEALTHY=1; break
    fi
    sleep 1
  done
  if [ "${HEALTHY:-0}" != "1" ]; then
    warn "Панель не ответила на http://127.0.0.1:$PORT/healthz"
    warn "Смотрите журнал: journalctl -u $SERVICE -n 50 --no-pager"
    exit 1
  fi
  info "${GREEN}панель отвечает${OFF}"
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<SUMMARY

${GREEN}${BOLD}Готово.${OFF}
  Адрес:    ${BOLD}http://${IP:-IP-сервера}:$PORT${OFF}
  Версия:   $BUILD
  Каталог:  $APP_DIR
  Настройки: $APP_DIR/.env
SUMMARY

if [ -n "$ADMIN_PASSWORD" ]; then
  cat <<SUMMARY
  Вход:     ${BOLD}$ADMIN_LOGIN / $ADMIN_PASSWORD${OFF}
            (сохраните пароль — второй раз он не показывается)
SUMMARY
else
  echo "  Вход:     прежние учётные данные (.env не менялся)"
fi

if [ "$DEMO_MODE" = "1" ]; then
  cat <<SUMMARY

${YELLOW}Панель работает в ДЕМО-режиме на сгенерированных данных.${OFF}
Для боевой работы впишите ключи и перезапустите:
  sudo nano $APP_DIR/.env      # OZON_CLIENT_ID и OZON_API_KEY
  sudo systemctl restart $SERVICE
SUMMARY
fi

if [ "${SERVICE_INSTALLED:-0}" = "1" ]; then
  cat <<SUMMARY

Управление:
  systemctl status $SERVICE      journalctl -u $SERVICE -f
  systemctl restart $SERVICE     systemctl stop $SERVICE
Обновление до свежей версии — этот же скрипт ещё раз:
  curl -fsSL $RAW_URL | sudo bash
SUMMARY
fi
echo
