#!/usr/bin/env bash
#
# Установка Ozon Pack на Ubuntu/Debian сразу с HTTPS — одной командой.
#
#   curl -fsSL https://raw.githubusercontent.com/kkkola000/ozon-pack/HEAD/deploy/setup.sh \
#     | sudo bash -s -- --domain panel.example.com --email admin@example.com
#
# Скрипт последовательно выполняет два шага:
#   1) deploy/install.sh — панель, служба systemd, база;
#   2) deploy/ssl.sh     — nginx и бесплатный сертификат.
# Повторный запуск обновляет код и продлевает настройку, не трогая данные.
#
set -Eeuo pipefail

REPO_URL=${REPO_URL:-https://github.com/kkkola000/ozon-pack.git}
RAW_BASE=${RAW_BASE:-https://raw.githubusercontent.com/kkkola000/ozon-pack}
BRANCH=${BRANCH:-HEAD}
APP_DIR=${APP_DIR:-/opt/ozon-pack}
PORT=${PORT:-8080}
DOMAIN=${DOMAIN:-}
EMAIL=${EMAIL:-}
OZON_CLIENT_ID=${OZON_CLIENT_ID:-}
OZON_API_KEY=${OZON_API_KEY:-}
ADMIN_LOGIN=${ADMIN_LOGIN:-admin}
ADMIN_PASSWORD=${ADMIN_PASSWORD:-}
TLS_MODE=""          # domain | ip | self-signed | none
EXTRA_SSL=()
EXTRA_INSTALL=()

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
head() { printf '\n%s%s%s\n' "$BOLD" "$*" "$OFF"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%s[!] %s%s\n' "$YELLOW" "$*" "$OFF"; }
die()  { printf '%s[x] %s%s\n' "$RED" "$*" "$OFF" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Установка панели Ozon Pack вместе с HTTPS.

  --domain NAME      домен панели; на него выпускается сертификат Let's Encrypt
  --email MAIL       почта для уведомлений Let's Encrypt (рекомендуется)
  --ip               сертификат на IP-адрес, если домена нет (срок 160 часов)
  --self-signed      самоподписанный сертификат (браузер будет предупреждать)
  --no-ssl           только панель, без HTTPS
  --port N           порт панели за nginx (по умолчанию 8080)
  --dir PATH         каталог установки (по умолчанию /opt/ozon-pack)
  --branch NAME      ветка репозитория
  --demo             демо-режим на сгенерированных данных
  --hsts             включить HSTS (браузеры запомнят https на 180 дней)
  -h, --help         справка

Ключи Ozon можно передать переменными окружения (sudo -E) или ввести потом
в самой панели: Настройки -> Ключи Seller API.

Пример:
  curl -fsSL <адрес>/deploy/setup.sh | sudo bash -s -- \
    --domain panel.example.com --email admin@example.com
USAGE
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN=$2; TLS_MODE=domain; shift 2 ;;
    --email) EMAIL=$2; shift 2 ;;
    --ip) TLS_MODE=ip; shift ;;
    --self-signed) TLS_MODE=self-signed; shift ;;
    --no-ssl) TLS_MODE=none; shift ;;
    --port) PORT=$2; shift 2 ;;
    --dir) APP_DIR=$2; shift 2 ;;
    --branch) BRANCH=$2; shift 2 ;;
    --demo) EXTRA_INSTALL+=(--demo); shift ;;
    --hsts) EXTRA_SSL+=(--hsts); shift ;;
    -h|--help) usage ;;
    *) die "Неизвестный аргумент: $1 (--help для справки)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "Запустите с правами root: sudo bash $0 ..."

# Домен можно ввести с терминала, даже если скрипт пришёл через curl | bash
if [ -z "$TLS_MODE" ]; then
  if [ -r /dev/tty ]; then
    printf '\nДомен панели (например panel.example.com).\n'
    printf 'Оставьте пустым, чтобы выпустить сертификат на IP-адрес сервера: '
    read -r DOMAIN </dev/tty || true
    DOMAIN=$(printf '%s' "$DOMAIN" | tr -d '[:space:]')
    if [ -n "$DOMAIN" ]; then
      TLS_MODE=domain
      printf 'Почта для уведомлений Let'"'"'s Encrypt (можно пропустить): '
      read -r EMAIL </dev/tty || true
      EMAIL=$(printf '%s' "$EMAIL" | tr -d '[:space:]')
    else
      TLS_MODE=ip
    fi
  else
    die "Укажите режим: --domain NAME, --ip, --self-signed или --no-ssl"
  fi
fi

# Пароль генерируем здесь, чтобы показать его в итоговой сводке
NEW_INSTALL=1
[ -f "$APP_DIR/.env" ] && NEW_INSTALL=0
if [ "$NEW_INSTALL" = "1" ] && [ -z "$ADMIN_PASSWORD" ]; then
  command -v python3 >/dev/null 2>&1 || { apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 >/dev/null; }
  ADMIN_PASSWORD=$(python3 -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(14)))")
fi

# ------------------------------------------------------------------ подготовка скриптов
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)

fetch_script() {
  local name=$1 target="$WORK/$1"
  if [ -n "$HERE" ] && [ -f "$HERE/$name" ]; then
    cp "$HERE/$name" "$target"
  else
    command -v curl >/dev/null 2>&1 || { apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates >/dev/null; }
    curl -fsSL "$RAW_BASE/$BRANCH/deploy/$name" -o "$target" \
      || die "Не удалось скачать deploy/$name — проверьте адрес репозитория и ветку"
  fi
  [ -s "$target" ] || die "Пустой файл deploy/$name"
  echo "$target"
}

head "Шаг 1 из 2 — установка панели"
INSTALL_SH=$(fetch_script install.sh)
INSTALL_ARGS=(--yes --port "$PORT" --dir "$APP_DIR" --repo "$REPO_URL")
[ "$BRANCH" != "HEAD" ] && INSTALL_ARGS+=(--branch "$BRANCH")
# Порты откроет второй шаг: наружу должны смотреть только 80 и 443
[ "$TLS_MODE" != "none" ] && INSTALL_ARGS+=(--no-firewall)
[ ${#EXTRA_INSTALL[@]} -gt 0 ] && INSTALL_ARGS+=("${EXTRA_INSTALL[@]}")

OZON_CLIENT_ID="$OZON_CLIENT_ID" OZON_API_KEY="$OZON_API_KEY" \
ADMIN_LOGIN="$ADMIN_LOGIN" ADMIN_PASSWORD="$ADMIN_PASSWORD" NONINTERACTIVE=1 \
  bash "$INSTALL_SH" "${INSTALL_ARGS[@]}"

if [ "$TLS_MODE" = "none" ]; then
  head "${GREEN}Готово (без HTTPS).${OFF}"
  info "Включить позже: sudo bash $APP_DIR/deploy/ssl.sh --domain ВАШ-ДОМЕН --email ПОЧТА"
  exit 0
fi

head "Шаг 2 из 2 — HTTPS"
# После установки скрипт лежит рядом с кодом и точно соответствует его версии
if [ -f "$APP_DIR/deploy/ssl.sh" ]; then
  SSL_SH="$APP_DIR/deploy/ssl.sh"
else
  SSL_SH=$(fetch_script ssl.sh)
fi

SSL_ARGS=(--dir "$APP_DIR")
case "$TLS_MODE" in
  domain) SSL_ARGS+=(--domain "$DOMAIN"); [ -n "$EMAIL" ] && SSL_ARGS+=(--email "$EMAIL") ;;
  ip) SSL_ARGS+=(--ip) ;;
  self-signed) SSL_ARGS+=(--self-signed) ;;
esac
[ ${#EXTRA_SSL[@]} -gt 0 ] && SSL_ARGS+=("${EXTRA_SSL[@]}")

SSL_OK=1
bash "$SSL_SH" "${SSL_ARGS[@]}" || SSL_OK=0

# ------------------------------------------------------------------ итог
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
ADDRESS=${DOMAIN:-$IP}
case "$ADDRESS" in *:*) ADDRESS="[$ADDRESS]" ;; esac

echo
if [ "$SSL_OK" = "1" ]; then
  printf '%s%sПанель установлена и работает по HTTPS.%s\n' "$GREEN" "$BOLD" "$OFF"
  printf '  Адрес:   %shttps://%s%s\n' "$BOLD" "$ADDRESS" "$OFF"
else
  printf '%s%sПанель установлена, но HTTPS настроить не удалось.%s\n' "$YELLOW" "$BOLD" "$OFF"
  printf '  Панель пока доступна по http://%s:%s\n' "${IP:-IP-сервера}" "$PORT"
  printf '  Повторить настройку HTTPS: sudo bash %s/deploy/ssl.sh --domain %s --email ПОЧТА\n' "$APP_DIR" "${DOMAIN:-ВАШ-ДОМЕН}"
fi

if [ "$NEW_INSTALL" = "1" ]; then
  printf '  Вход:    %s%s / %s%s\n' "$BOLD" "$ADMIN_LOGIN" "$ADMIN_PASSWORD" "$OFF"
  printf '           сохраните пароль — второй раз он не показывается\n'
else
  printf '  Вход:    прежние учётные данные (.env не менялся)\n'
fi

printf '  Каталог: %s\n' "$APP_DIR"
if [ -z "$OZON_CLIENT_ID$OZON_API_KEY" ]; then
  printf '\n%sДальше:%s внесите ключи Ozon в панели: Настройки -> Ключи Seller API.\n' "$BOLD" "$OFF"
  printf 'До этого панель работает на демонстрационных данных.\n'
fi
printf '\nОбновление в будущем — этой же командой ещё раз.\n\n'
[ "$SSL_OK" = "1" ]
