#!/usr/bin/env bash
#
# Доступ к панели только через VPN (WireGuard), снаружи — отказ.
#
#   sudo bash /opt/ozon-pack/deploy/vpn-only.sh            # включить
#   sudo bash /opt/ozon-pack/deploy/vpn-only.sh --off      # вернуть как было
#   sudo bash /opt/ozon-pack/deploy/vpn-only.sh --status   # что сейчас
#
# Ограничение ставится в nginx: панель отвечает только адресам из сети
# WireGuard, всем остальным — 403. SSH и сам WireGuard скрипт не трогает,
# запереть себя на сервере он не может. Порт 80 остаётся открытым для
# проверки Let's Encrypt, поэтому сертификат продолжает продлеваться.
#
set -Eeuo pipefail

SNIPPET=${SNIPPET:-/etc/nginx/snippets/ozon-pack-access.conf}
NGINX_SITES=${NGINX_SITES:-/etc/nginx/sites-enabled}
NGINX_CONFD=${NGINX_CONFD:-/etc/nginx/conf.d}
WG_DIR=${WG_DIR:-/etc/wireguard}
APP_DIR=${APP_DIR:-/opt/ozon-pack}
SERVICE=${SERVICE:-ozon-pack}
APP_PORT=${APP_PORT:-}
SUBNETS=()
EXTRA=()
MODE=on
NONINTERACTIVE=0

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$OFF"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%s[!] %s%s\n' "$YELLOW" "$*" "$OFF"; }
die()  { printf '%s[x] %s%s\n' "$RED" "$*" "$OFF" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Доступ к панели только через VPN.

  (без флагов)     закрыть панель: пускать только из сети WireGuard
  --off            открыть панель для всех адресов
  --status         показать текущее состояние и кто заходил
  --subnet CIDR    указать сеть VPN вручную (можно повторять)
  --allow ADDR     дополнительно разрешить адрес или сеть (можно повторять)
  --yes            не задавать вопросов
  -h, --help       эта справка

Пример: закрыть панель, но оставить доступ с офисного адреса
  sudo bash vpn-only.sh --allow 203.0.113.10
USAGE
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --off) MODE=off; shift ;;
    --status) MODE=status; shift ;;
    --subnet) SUBNETS+=("$2"); shift 2 ;;
    --allow) EXTRA+=("$2"); shift 2 ;;
    --yes|-y) NONINTERACTIVE=1; shift ;;
    -h|--help) usage ;;
    *) die "Неизвестный аргумент: $1 (--help для справки)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "Запустите с правами root: sudo bash $0"
command -v nginx >/dev/null || die "nginx не установлен — панель за ним и живёт"

# ------------------------------------------------------------------ сеть VPN
network_of() {
  # 10.8.0.1/24 -> 10.8.0.0/24
  python3 - "$1" <<'PY' 2>/dev/null || true
import ipaddress, sys
try:
    print(ipaddress.ip_interface(sys.argv[1]).network)
except ValueError:
    pass
PY
}

detect_subnets() {
  local found=() iface addr
  # Поднятые интерфейсы WireGuard
  for iface in $(ip -o link show type wireguard 2>/dev/null | awk -F': ' '{print $2}'); do
    while read -r addr; do
      if [ -n "$addr" ]; then found+=("$(network_of "$addr")"); fi
    done < <(ip -o addr show "$iface" 2>/dev/null | awk '$3 ~ /^inet6?$/ {print $4}')
  done
  # Если интерфейс сейчас опущен — берём адрес из конфигурации
  if [ ${#found[@]} -eq 0 ] && [ -d "$WG_DIR" ]; then
    while read -r addr; do
      if [ -n "$addr" ]; then found+=("$(network_of "$addr")"); fi
    done < <(grep -hi '^ *Address' "$WG_DIR"/*.conf 2>/dev/null |
             sed 's/.*= *//; s/,/\n/g' | tr -d ' ')
  fi
  printf '%s\n' "${found[@]}" | awk 'NF && !seen[$0]++'
}

app_port() {
  [ -n "$APP_PORT" ] && { echo "$APP_PORT"; return; }
  local port=""
  [ -f "$APP_DIR/.env" ] && port=$(awk -F= '/^PORT=/{print $2}' "$APP_DIR/.env" | tail -1 | tr -d ' ')
  echo "${port:-8080}"
}

wg_port() {
  local port=""
  port=$(grep -hi '^ *ListenPort' "$WG_DIR"/*.conf 2>/dev/null | sed 's/.*= *//' | tr -d ' ' | head -1)
  [ -n "$port" ] || port=$(wg show all listen-port 2>/dev/null | awk '{print $2}' | head -1)
  echo "$port"
}

# Панель, поднятая не на localhost, открыта мимо nginx — правило её не закроет.
check_direct_port() {
  local port=$1 outside
  command -v ss >/dev/null 2>&1 || return 0
  outside=$(ss -ltnH 2>/dev/null | awk '{print $4}' |
            grep -E "[:.]${port}\$" | grep -Ev '^(127\.|\[::1\])' || true)
  [ -n "$outside" ] || return 0

  warn "Панель слушает не только localhost: $(echo "$outside" | tr '\n' ' ')"
  warn "В неё можно зайти мимо nginx — по адресу сервера с портом $port."
  if [ -f "$APP_DIR/.env" ] && systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE.service"; then
    if grep -q '^HOST=' "$APP_DIR/.env"; then
      sed -i 's/^HOST=.*/HOST=127.0.0.1/' "$APP_DIR/.env"
    else
      echo "HOST=127.0.0.1" >> "$APP_DIR/.env"
    fi
    systemctl restart "$SERVICE" 2>/dev/null || true
    sleep 2
    info "исправлено: HOST=127.0.0.1 в $APP_DIR/.env, служба $SERVICE перезапущена"
  else
    warn "Закройте вручную: HOST=127.0.0.1 в $APP_DIR/.env и перезапуск панели."
    warn "Если панель в Docker — привяжите порт: ports: [\"127.0.0.1:$port:8080\"]"
  fi
  return 0
}

# Файрвол не должен резать сам туннель — иначе VPN отвалится вместе с панелью.
check_firewall() {
  local port
  command -v ufw >/dev/null 2>&1 || return 0
  ufw status 2>/dev/null | grep -q 'Status: active' || return 0
  port=$(wg_port)
  [ -n "$port" ] || return 0
  if ufw status 2>/dev/null | grep -q "$port"; then
    info "файрвол пропускает WireGuard (порт $port) — туннель не пострадает"
  else
    warn "В ufw не видно правила для порта WireGuard ($port)."
    warn "Если туннель перестанет подниматься: sudo ufw allow $port/udp"
  fi
  return 0
}

# В режиме --status ничего не меняем, только сообщаем.
check_direct_port_note() {
  local port=$1 outside
  command -v ss >/dev/null 2>&1 || return 0
  outside=$(ss -ltnH 2>/dev/null | awk '{print $4}' |
            grep -E "[:.]${port}\$" | grep -Ev '^(127\.|\[::1\])' || true)
  if [ -n "$outside" ]; then
    warn "панель слушает мимо nginx: $(echo "$outside" | tr '\n' ' ') — запустите скрипт без --status"
  else
    info "панель слушает только localhost — снаружи только через nginx"
  fi
  return 0
}

# ------------------------------------------------------------------ состояние
show_status() {
  step "Ограничение доступа"
  if [ -f "$SNIPPET" ] && grep -q '^ *deny all;' "$SNIPPET"; then
    info "${GREEN}включено${OFF} — панель отвечает только этим адресам:"
    grep '^ *allow' "$SNIPPET" | sed 's/^ *allow /      /; s/;$//'
  elif [ -f "$SNIPPET" ]; then
    warn "выключено — панель открыта со всех адресов"
  else
    warn "выключено — файл $SNIPPET не создан"
  fi

  step "WireGuard"
  local nets
  nets=$(detect_subnets)
  if [ -n "$nets" ]; then
    info "сети туннеля: $(echo "$nets" | tr '\n' ' ')"
  else
    warn "интерфейс WireGuard не найден (укажите сеть флагом --subnet)"
  fi
  if command -v wg >/dev/null; then
    local peers
    peers=$(wg show all latest-handshakes 2>/dev/null | wc -l)
    info "подключённых устройств в конфигурации: $peers"
  fi

  step "Файрвол и прямой доступ"
  check_firewall
  check_direct_port_note "$(app_port)"

  step "Кто заходил в панель"
  local log=/var/log/nginx/access.log
  if [ -s "$log" ]; then
    info "последние адреса (сверьте со своей сетью VPN):"
    awk '{print $1}' "$log" | sort | uniq -c | sort -rn | head -10 | sed 's/^/      /'
  else
    info "журнал $log пуст"
  fi
  exit 0
}

[ "$MODE" = "status" ] && show_status

# ------------------------------------------------------------------ запись правила
write_snippet() {
  mkdir -p "$(dirname "$SNIPPET")"
  local backup=""
  if [ -f "$SNIPPET" ]; then
    backup=$(mktemp)
    cp "$SNIPPET" "$backup"
  fi

  if [ "$MODE" = "off" ]; then
    cat > "$SNIPPET" <<'CONF'
# Создано deploy/vpn-only.sh: панель открыта со всех адресов.
allow all;
CONF
  else
    {
      echo "# Создано deploy/vpn-only.sh: панель доступна только через VPN."
      echo "# Открыть обратно: sudo bash $APP_DIR/deploy/vpn-only.sh --off"
      echo "allow 127.0.0.1;"
      echo "allow ::1;"
      for net in "${SUBNETS[@]}"; do echo "allow $net;"; done
      for net in "${EXTRA[@]}"; do echo "allow $net;"; done
      echo "deny all;"
    } > "$SNIPPET"
  fi

  if ! nginx -t >/dev/null 2>&1; then
    nginx -t || true
    if [ -n "$backup" ]; then cp "$backup" "$SNIPPET"; fi
    die "nginx отверг конфигурацию — правило откачено, ничего не изменилось"
  fi
  if [ -n "$backup" ]; then rm -f "$backup"; fi
  return 0
}

if [ "$MODE" = "on" ]; then
  if [ ${#SUBNETS[@]} -eq 0 ]; then
    mapfile -t SUBNETS < <(detect_subnets)
  fi
  if [ ${#SUBNETS[@]} -eq 0 ]; then
    die "Сеть WireGuard не найдена. Поднимите туннель (wg-quick up wg0) или укажите её вручную: --subnet 10.8.0.0/24"
  fi

  step "Что будет разрешено"
  info "локальный сервер: 127.0.0.1, ::1"
  for net in "${SUBNETS[@]}"; do info "сеть VPN: $net"; done
  for net in "${EXTRA[@]}"; do info "дополнительно: $net"; done
  info "всем остальным — 403"

  if [ "$NONINTERACTIVE" != "1" ] && [ -r /dev/tty ]; then
    printf '\n    Панель перестанет открываться без VPN. SSH и WireGuard не затрагиваются.\n'
    printf '    Продолжить? [y/N]: '
    read -r answer </dev/tty || answer=""
    case "$answer" in [yYдД]*) ;; *) die "Отменено" ;; esac
  fi
fi

step "Правило nginx"
write_snippet
info "файл: $SNIPPET"

# Конфиг сайта мог быть создан прежней версией ssl.sh — без include правило
# не сработает, поэтому дописываем его сами.
ensure_include() {
  local site="" candidate port
  port=$(app_port)
  for candidate in "$NGINX_SITES"/* "$NGINX_CONFD"/*.conf; do
    [ -e "$candidate" ] || continue
    if grep -qs "proxy_pass http://127.0.0.1:$port" "$candidate"; then site=$candidate; break; fi
  done
  [ -n "$site" ] || { warn "Не нашёл конфиг сайта панели в $NGINX_SITES"; return 1; }

  local target backup
  target=$(readlink -f "$site")
  # Строка уже на месте (конфиг создан свежим ssl.sh или прошлым запуском)
  grep -qs "include $SNIPPET;" "$target" && return 0
  backup=$(mktemp)
  cp "$target" "$backup"
  # Строка идёт первой в каждый location / — проверка Let's Encrypt описана
  # отдельным location и ограничения не получает.
  awk -v snippet="$SNIPPET" '
    { print }
    /^[[:space:]]*location \/ \{[[:space:]]*$/ { print "        include " snippet ";" }
  ' "$backup" > "$target"

  if nginx -t >/dev/null 2>&1; then
    info "в $target добавлена строка include"
    rm -f "$backup"
    return 0
  fi
  nginx -t || true
  cp "$backup" "$target"
  rm -f "$backup"
  warn "Не удалось добавить include — конфиг сайта возвращён как был"
  return 1
}

if ! ensure_include; then
  warn "Правило записано, но конфиг сайта его не подключает: обновите панель"
  warn "и перезапустите deploy/ssl.sh — он добавит include сам."
fi

systemctl reload nginx 2>/dev/null || systemctl restart nginx || die "nginx не перезапустился"
info "nginx перечитал конфигурацию"

step "Проверка"
PORT=$(app_port)
if [ "$MODE" = "on" ]; then
  check_direct_port "$PORT"
  check_firewall
fi
if curl -fsS --max-time 5 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
  info "${GREEN}панель работает${OFF} (проверено на самом сервере)"
else
  warn "Панель не ответила на http://127.0.0.1:$PORT/healthz — проверьте службу ozon-pack"
fi

if [ "$MODE" = "off" ]; then
  cat <<SUMMARY

${GREEN}${BOLD}Готово: панель снова открыта со всех адресов.${OFF}
SUMMARY
else
  cat <<SUMMARY

${GREEN}${BOLD}Готово: панель доступна только через VPN.${OFF}
  Проверить снаружи (без VPN) — должно быть 403:
    curl -sI https://ВАШ-ДОМЕН | head -1
  Посмотреть состояние и адреса заходивших:
    sudo bash $APP_DIR/deploy/vpn-only.sh --status
  Открыть обратно:
    sudo bash $APP_DIR/deploy/vpn-only.sh --off

${YELLOW}Если панель не открывается и через VPN${OFF} — значит nginx видит не тот адрес.
Посмотрите, с какого адреса пришёл запрос, и разрешите именно его:
    sudo tail -5 /var/log/nginx/access.log
    sudo bash $APP_DIR/deploy/vpn-only.sh --allow ЭТОТ-АДРЕС
SUMMARY
fi
echo
