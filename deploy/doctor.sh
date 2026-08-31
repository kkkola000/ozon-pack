#!/usr/bin/env bash
#
# Что сейчас происходит с панелью Ozon Pack: служба, порты, nginx, сертификат,
# доступ. Ничего не меняет — только показывает.
#
#   sudo bash doctor.sh
#   curl -fsSL https://raw.githubusercontent.com/kkkola000/ozon-pack/HEAD/deploy/doctor.sh | sudo bash
#
set -uo pipefail

APP_DIR=${APP_DIR:-/opt/ozon-pack}
SERVICE=${SERVICE:-ozon-pack}
SNIPPET=${SNIPPET:-/etc/nginx/snippets/ozon-pack-access.conf}

GREEN=$'\033[32m'; RED=$'\033[31m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
# Имя section, а не head: функция с именем head перекрыла бы команду head
section() { printf '\n%s%s%s\n' "$BOLD" "$*" "$OFF"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$RED" "$OFF" "$*"; }
note() { printf '    %s\n' "$*"; }

PORT=$(awk -F= '/^PORT=/{print $2}' "$APP_DIR/.env" 2>/dev/null | tail -1); PORT=${PORT:-8080}
HOST=$(awk -F= '/^HOST=/{print $2}' "$APP_DIR/.env" 2>/dev/null | tail -1); HOST=${HOST:-0.0.0.0}
DOMAIN=$(grep -oP 'server_name \K[^;]+' /etc/nginx/sites-available/ozon-pack 2>/dev/null | head -1)

section "1. Каталог и настройки"
if [ -d "$APP_DIR" ]; then
  ok "$APP_DIR на месте"
  note "версия: $(git -c safe.directory="$APP_DIR" -C "$APP_DIR" log --oneline -1 2>/dev/null || echo 'не git-репозиторий')"
  note "порт панели: $PORT, HOST=$HOST"
  [ -f "$APP_DIR/.env" ] && ok ".env есть" || bad ".env отсутствует"
else
  bad "$APP_DIR не найден — панель не установлена?"
fi

section "2. Как запущена панель"
FOUND=0
if [ -f "/etc/systemd/system/$SERVICE.service" ] || systemctl cat "$SERVICE.service" >/dev/null 2>&1; then
  FOUND=1
  STATE=$(systemctl is-active "$SERVICE" 2>/dev/null || echo unknown)
  [ "$STATE" = "active" ] && ok "служба $SERVICE: $STATE" || bad "служба $SERVICE: $STATE"
  note "включена в автозапуск: $(systemctl is-enabled "$SERVICE" 2>/dev/null || echo нет)"
else
  note "службы $SERVICE нет"
fi
if command -v docker >/dev/null 2>&1; then
  CONTAINERS=$(docker ps -a --format '{{.Names}} — {{.Status}}' 2>/dev/null | grep -i ozon || true)
  if [ -n "$CONTAINERS" ]; then FOUND=1; ok "контейнеры:"; printf '    %s\n' "$CONTAINERS"; fi
fi
UVICORN=$(pgrep -af "uvicorn.*app.main" 2>/dev/null | head -3 || true)
if [ -n "$UVICORN" ]; then FOUND=1; ok "процессы uvicorn:"; printf '    %s\n' "$UVICORN"; fi
[ "$FOUND" = "0" ] && bad "панель не запущена ни как служба, ни в контейнере, ни вручную"

section "3. Порты"
if command -v ss >/dev/null 2>&1; then
  LISTEN=$(ss -ltnp 2>/dev/null | grep -E ":(80|443|$PORT)\b" || true)
  [ -n "$LISTEN" ] && printf '    %s\n' "$LISTEN" || bad "никто не слушает 80, 443 и $PORT"
fi

section "4. Панель отвечает?"
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/healthz" 2>/dev/null); CODE=${CODE:-000}
[ "$CODE" = "200" ] && ok "напрямую на порту $PORT: 200" || bad "напрямую на порту $PORT: $CODE"

section "5. nginx"
if command -v nginx >/dev/null 2>&1; then
  systemctl is-active nginx >/dev/null 2>&1 && ok "nginx запущен" || bad "nginx не запущен"
  nginx -t >/dev/null 2>&1 && ok "конфигурация корректна" || { bad "ошибка конфигурации:"; nginx -t 2>&1 | sed 's/^/    /'; }
  [ -n "$DOMAIN" ] && note "домен в конфиге: $DOMAIN"
  CODE=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 "https://127.0.0.1/healthz" 2>/dev/null); CODE=${CODE:-000}
  case "$CODE" in
    200) ok "через nginx: 200" ;;
    403) ok "через nginx: 403 — доступ ограничен списком (см. ниже)" ;;
    502|504) bad "через nginx: $CODE — nginx работает, а панель за ним не отвечает" ;;
    *) bad "через nginx: $CODE" ;;
  esac
else
  note "nginx не установлен — панель отдаётся напрямую по порту $PORT"
fi

section "6. Доступ"
if [ -f "$SNIPPET" ] && grep -qE '^\s*deny all;' "$SNIPPET"; then
  ok "панель закрыта, пускаем только:"
  grep -E '^\s*allow' "$SNIPPET" | sed 's/^\s*allow /      /; s/;$//'
else
  note "панель открыта всем, кто знает адрес"
fi
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  note "файрвол ufw включён:"; ufw status | grep -E "^(80|443|22|$PORT)" | sed 's/^/      /'
fi

section "7. Сертификат"
CERT=$(ls -d /etc/letsencrypt/live/*/ 2>/dev/null | head -1)
if [ -n "$CERT" ] && [ -f "$CERT/fullchain.pem" ]; then
  ok "Let's Encrypt: $(basename "$CERT")"
  note "действует до: $(openssl x509 -enddate -noout -in "$CERT/fullchain.pem" 2>/dev/null | cut -d= -f2)"
elif [ -f /etc/ssl/ozon-pack/fullchain.pem ]; then
  note "самоподписанный сертификат (браузер предупреждает)"
else
  bad "сертификат не найден"
fi

section "8. Последние ошибки панели"
if [ "$FOUND" = "1" ] && systemctl cat "$SERVICE.service" >/dev/null 2>&1; then
  journalctl -u "$SERVICE" -n 15 --no-pager 2>/dev/null | grep -iE "error|traceback|exception|refused" | tail -5 | sed 's/^/    /' \
    || note "ошибок в журнале не видно"
else
  note "журнал недоступен — служба не найдена"
fi
echo
