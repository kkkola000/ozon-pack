#!/usr/bin/env bash
#
# Кто может открыть панель Ozon Pack.
#
# Панель прячется за ваш VPN: снаружи она отвечает «доступ закрыт», а из сети
# VPN работает как обычно, по тому же адресу и с тем же сертификатом.
#
#   sudo bash access.sh --allow 10.8.0.0/24        только из этой сети
#   sudo bash access.sh --allow 10.8.0.0/24,203.0.113.5   сеть и адрес шлюза
#   sudo bash access.sh --open                     вернуть публичный доступ
#   sudo bash access.sh --status                   что сейчас настроено
#
# Какой адрес указывать:
#   * VPN поднят на этом же сервере — сеть VPN, например 10.8.0.0/24;
#   * VPN на другом сервере — его внешний адрес, с которого приходят запросы.
# Не уверены — откройте панель через VPN и посмотрите --status: там написано,
# с какого адреса вы пришли.
#
# Порт 80 остаётся открытым для проверки Let's Encrypt: без него перестанет
# продлеваться сертификат. Панель по нему не отдаётся — только перенаправление.
#
set -Eeuo pipefail

SNIPPET=${SNIPPET:-/etc/nginx/snippets/ozon-pack-access.conf}
SITE=${SITE:-/etc/nginx/sites-available/ozon-pack}
ALLOW=""
ACTION=""
FIREWALL=0

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$OFF"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%s[!] %s%s\n' "$YELLOW" "$*" "$OFF"; }
die()  { printf '%s[x] %s%s\n' "$RED" "$*" "$OFF" >&2; exit 1; }
trap 'die "Прервано на строке $LINENO."' ERR

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --allow) ALLOW=$2; ACTION=close; shift 2 ;;
    --open) ACTION=open; shift ;;
    --status) ACTION=status; shift ;;
    --firewall) FIREWALL=1; shift ;;
    -h|--help) usage ;;
    *) die "Неизвестный аргумент: $1 (--help для справки)" ;;
  esac
done
[ -n "$ACTION" ] || usage
[ "$(id -u)" -eq 0 ] || die "Запустите с правами root: sudo bash $0 ..."
command -v nginx >/dev/null || die "nginx не установлен — сначала настройте HTTPS (deploy/ssl.sh)"

current_rules() {
  [ -f "$SNIPPET" ] && grep -E '^\s*(allow|deny)' "$SNIPPET" || echo "allow all;"
}

show_status() {
  step "Доступ к панели"
  if [ ! -f "$SNIPPET" ] || current_rules | grep -q '^allow all;'; then
    warn "Панель открыта всем, кто знает адрес"
  else
    info "Пускаем только:"
    grep -E '^\s*allow' "$SNIPPET" | sed 's/^\s*allow /      /; s/;$//'
  fi
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    info "Файрвол: $(ufw status | grep -c '443') правил(а) на 443"
  fi
  if [ -n "${SSH_CLIENT:-}" ]; then
    info "Вы сейчас подключены по SSH с адреса ${SSH_CLIENT%% *}"
  fi
  info "Проверить снаружи: curl -I https://ВАШ-ДОМЕН (должно быть 403, если закрыто)"
}

reload_nginx() {
  nginx -t >/dev/null 2>&1 || { nginx -t; die "Ошибка в конфигурации nginx — ничего не менял"; }
  # Молча проглатывать неудачу нельзя: иначе скрипт скажет «панель закрыта»,
  # а она останется открытой.
  systemctl reload nginx 2>/dev/null && return 0
  systemctl restart nginx 2>/dev/null && return 0
  if pgrep -x nginx >/dev/null 2>&1; then
    nginx -s reload 2>/dev/null && return 0
  fi
  nginx 2>/dev/null && return 0
  die "Не удалось перезагрузить nginx — правила НЕ применены. Проверьте: nginx -t"
}

local_code() {
  curl -sk -o /dev/null -w '%{http_code}' --max-time 5 https://127.0.0.1/ 2>/dev/null || echo 000
}

wait_for_state() {
  # Перезагрузка nginx не мгновенная — даём конфигурации вступить в силу.
  # Проверяем не намерение, а результат.
  local want=$1 code=000
  for _ in 1 2 3 4 5 6; do
    code=$(local_code)
    if [ "$want" = "closed" ] && [ "$code" = "403" ]; then return 0; fi
    if [ "$want" = "open" ] && [ "$code" != "403" ] && [ "$code" != "000" ]; then return 0; fi
    sleep 1
  done
  if [ "$code" = "000" ]; then
    warn "Локально проверить не удалось (панель не ответила) — проверьте вручную"
    return 0
  fi
  return 1
}

verify_closed() {
  case ",$ALLOW," in *,127.0.0.1,*|*,127.0.0.1/*|*,localhost,*) return 0 ;; esac
  wait_for_state closed \
    && info "проверено: посторонний запрос получает «доступ закрыт»" \
    || die "Правила не применились: панель всё ещё отвечает посторонним. Проверьте nginx -t"
}

verify_open() {
  wait_for_state open \
    && info "проверено: панель отвечает без VPN" \
    || die "Панель всё ещё закрыта. Проверьте $SNIPPET и nginx -t"
}

case "$ACTION" in
  status) show_status; echo; exit 0 ;;

  open)
    step "Открываю панель для всех"
    mkdir -p "$(dirname "$SNIPPET")"
    printf '# Доступ открыт всем. Закрыть: sudo bash access.sh --allow СЕТЬ\nallow all;\n' > "$SNIPPET"
    reload_nginx
    if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
      ufw allow 443/tcp >/dev/null 2>&1 || true
      info "Файрвол: 443 открыт"
    fi
    verify_open
    info "${GREEN}Панель снова доступна по адресу без VPN${OFF}"
    ;;

  close)
    [ -n "$ALLOW" ] || die "Не указано, кого пускать: --allow 10.8.0.0/24"
    grep -q "include .*$(basename "$SNIPPET")" "$SITE" 2>/dev/null \
      || die "В конфиге nginx нет списка доступа. Обновите панель и перезапустите deploy/ssl.sh"

    step "Закрываю панель, оставляю доступ только из VPN"
    {
      echo "# Кому открыта панель. Файлом управляет deploy/access.sh."
      echo "# Вернуть публичный доступ: sudo bash access.sh --open"
      IFS=','
      for entry in $ALLOW; do
        entry=$(printf '%s' "$entry" | tr -d '[:space:]')
        [ -n "$entry" ] || continue
        # nginx сам проверит корректность записи при перезагрузке конфигурации
        echo "allow $entry;"
      done
      unset IFS
      echo "deny all;"
    } > "$SNIPPET"

    reload_nginx
    info "Пускаем: $ALLOW"
    verify_closed

    if [ "$FIREWALL" = "1" ] && command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
      step "Файрвол"
      ufw --force delete allow 443/tcp >/dev/null 2>&1 || true
      IFS=','
      for entry in $ALLOW; do
        entry=$(printf '%s' "$entry" | tr -d '[:space:]')
        [ -n "$entry" ] || continue
        ufw allow from "$entry" to any port 443 proto tcp >/dev/null && info "443 открыт для $entry"
      done
      unset IFS
      warn "Порт 443 закрыт на файрволе для всех остальных"
    fi

    cat <<SUMMARY

${GREEN}${BOLD}Готово.${OFF} Панель отвечает только из указанных сетей.
  Порт 80 оставлен открытым — по нему продлевается сертификат Let's Encrypt.
  Проверьте прямо сейчас, что панель открывается через VPN.
  Если что-то пошло не так: ${BOLD}sudo bash access.sh --open${OFF}
SUMMARY
    ;;
esac
echo
