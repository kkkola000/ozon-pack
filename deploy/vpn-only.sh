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
# Сколько раз и с каким интервалом перечитывать, что слушает порт после правки.
VERIFY_TRIES=${VERIFY_TRIES:-5}
VERIFY_DELAY=${VERIFY_DELAY:-2}
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
  --subnet СЕТЬ    указать сеть VPN вручную вместо поиска по wg0
  --allow АДРЕС    дополнительно разрешить адрес или сеть
  --yes            не задавать вопросов
  -h, --help       эта справка

У --subnet и --allow можно перечислить несколько значений через запятую
или повторить флаг. Принимается и сеть (10.8.0.0/24), и один адрес
(10.8.0.5). Адрес хоста приводится к его сети: 10.8.0.1/24 -> 10.8.0.0/24.

Примеры
  сеть вручную            sudo bash vpn-only.sh --subnet 10.8.0.0/24
  две сети сразу          sudo bash vpn-only.sh --subnet 10.8.0.0/24,10.9.0.0/24
  сеть VPN плюс офис      sudo bash vpn-only.sh --subnet 10.8.0.0/24 --allow 203.0.113.10
  только свой адрес       sudo bash vpn-only.sh --subnet 10.8.0.5

Подсмотреть свою сеть: ip -o addr show wg0    (или: sudo wg show)
USAGE
  exit 0
}

# Проверяем то, что набрали руками: «10.8.0.1/24, 10.9.0.0/24» -> две
# нормализованные строки. Если python3 почему-то нет, пропускаем значение
# как есть — ошибку тогда поймает nginx.
normalize_targets() {
  if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "$1"
    return 0
  fi
  python3 - "$1" <<'ADDR'
import ipaddress, sys

good, bad = [], []
for raw in sys.argv[1].replace(";", ",").split(","):
    item = raw.strip()
    if not item:
        continue
    try:
        if "/" in item:
            good.append(str(ipaddress.ip_network(item, strict=False)))
        else:
            good.append(str(ipaddress.ip_address(item)))
    except ValueError:
        bad.append(item)

if bad or not good:
    sys.exit(1)
print("\n".join(good))
ADDR
}

while [ $# -gt 0 ]; do
  case "$1" in
    --off) MODE=off; shift ;;
    --status) MODE=status; shift ;;
    --subnet|--allow)
      [ $# -ge 2 ] || die "У флага $1 не указано значение, например: $1 10.8.0.0/24"
      if ! PARSED=$(normalize_targets "$2" 2>/dev/null); then
        die "Не понимаю «$2» у флага $1. Нужен адрес или сеть, например: 10.8.0.0/24"
      fi
      while read -r value; do
        if [ -n "$value" ]; then
          if [ "$1" = "--subnet" ]; then SUBNETS+=("$value"); else EXTRA+=("$value"); fi
        fi
      done <<< "$PARSED"
      shift 2 ;;
    --yes|-y) NONINTERACTIVE=1; shift ;;
    -h|--help) usage ;;
    *) die "Неизвестный аргумент: $1 (--help для справки)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "Запустите с правами root: sudo bash $0"
# Без nginx скрипт раньше просто умирал. Но ограничение по адресу живёт и в
# самой панели, а она работает и без прокси, — значит закрыть вход можно всё
# равно. Отказываться тут значит оставлять панель открытой всему интернету,
# хотя починить это в одну строку.
HAVE_NGINX=1
command -v nginx >/dev/null 2>&1 || HAVE_NGINX=0

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

# Адреса, на которых порт панели слушает не localhost. Пустой вывод — снаружи
# к панели напрямую не подключиться. Код 2 — проверить нечем (нет ss).
listening_outside() {
  local port=$1
  command -v ss >/dev/null 2>&1 || return 2
  ss -ltnH 2>/dev/null | awk '{print $4}' |
    grep -E "[:.]${port}\$" | grep -Ev '^(127\.|\[::1\])' || true
  return 0
}

# Записать KEY=value в .env, не плодя повторяющихся строк.
set_env_var() {
  local file=$1 key=$2 value=$3
  [ -f "$file" ] || return 1
  if grep -q "^$key=" "$file"; then
    sed -i "s#^$key=.*#$key=$value#" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

panel_in_docker() {
  command -v docker >/dev/null 2>&1 || return 1
  docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ozon-pack$'
}

panel_in_systemd() {
  systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE.service"
}

# Панель, поднятая не на localhost, открыта мимо nginx — правило её не закроет.
# Возвращает 1, если прямой доступ остался: вызывающий обязан сказать об этом
# вслух, а не отрапортовать об успехе.
check_direct_port() {
  local port=$1 outside="" probed=1

  if ! outside=$(listening_outside "$port"); then
    # Без ss не видно, какой адрес слушает панель. Закрываем вслепую: за nginx
    # ей и положено быть на localhost, а промолчать здесь означало бы повторить
    # прежнюю ошибку — считать непроверенное сделанным.
    probed=0
    warn "Не нашёл команду ss — не вижу, какой адрес слушает панель."
    warn "Закрываю прямой доступ вслепую, результат придётся сверить руками."
  elif [ -z "$outside" ]; then
    return 0
  else
    warn "Панель слушает не только localhost: $(echo "$outside" | tr '\n' ' ')"
    warn "В неё можно зайти мимо nginx — по адресу сервера с портом $port."
  fi

  if [ -f "$APP_DIR/.env" ] && panel_in_systemd; then
    set_env_var "$APP_DIR/.env" HOST 127.0.0.1
    info "в $APP_DIR/.env записано HOST=127.0.0.1, перезапускаю $SERVICE"
    systemctl restart "$SERVICE" 2>/dev/null || true
  elif [ -f "$APP_DIR/.env" ] && panel_in_docker; then
    # Внутри контейнера панель всегда слушает 0.0.0.0, иначе опубликованный порт
    # до неё не достучится. Снаружи её закрывает адрес публикации порта.
    set_env_var "$APP_DIR/.env" BIND_ADDR 127.0.0.1
    # nginx с хоста приходит в контейнер с адреса шлюза docker-сети, а не с
    # 127.0.0.1: без этого X-Forwarded-For перестанет учитываться и в журнале
    # окажется адрес моста вместо адреса сборщика.
    set_env_var "$APP_DIR/.env" FORWARDED_ALLOW_IPS 172.16.0.0/12
    info "в $APP_DIR/.env записано BIND_ADDR=127.0.0.1, пересоздаю контейнер"
    if ! (cd "$APP_DIR" && docker compose up -d >/dev/null 2>&1); then
      warn "docker compose up -d не отработал — выполните вручную в $APP_DIR"
    fi
  else
    warn "Ни служба $SERVICE, ни контейнер ozon-pack не найдены."
    warn "Закройте прямой доступ вручную: HOST=127.0.0.1 в $APP_DIR/.env"
    warn "(в Docker — BIND_ADDR=127.0.0.1) и перезапустите панель."
    return 1
  fi

  if [ "$probed" = "0" ]; then
    warn "Проверить результат нечем. Сверьте сами:  sudo ss -ltnp | grep :$port"
    warn "В выводе должен остаться только адрес 127.0.0.1."
    return 0
  fi

  # На слово не верим: панель могла не перезапуститься, .env — оказаться не тем,
  # а юнит — держать адрес захардкоженным. Перечитываем, что слушает порт.
  local attempt=0
  while [ "$attempt" -lt "$VERIFY_TRIES" ]; do
    attempt=$((attempt + 1))
    sleep "$VERIFY_DELAY"
    outside=$(listening_outside "$port") || outside=""
    [ -n "$outside" ] || break
  done
  if [ -n "$outside" ]; then
    warn "${RED}Порт $port по-прежнему слушает $(echo "$outside" | tr '\n' ' ') — прямой доступ НЕ закрыт.${OFF}"
    warn "Панель осталась доступна по адресу сервера в обход VPN."
    warn "Посмотрите, кто держит порт:  sudo ss -ltnp | grep :$port"
    return 1
  fi
  info "${GREEN}прямой доступ закрыт${OFF} — порт $port слушает только localhost"
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

# Конфиг сайта панели: тот, где nginx проксирует на её порт.
site_config() {
  local candidate port
  port=$(app_port)
  for candidate in "$NGINX_SITES"/* "$NGINX_CONFD"/*.conf; do
    [ -e "$candidate" ] || continue
    if grep -qs "proxy_pass http://127.0.0.1:$port" "$candidate"; then
      readlink -f "$candidate"
      return 0
    fi
  done
  return 1
}

# В режиме --status ничего не меняем, только сообщаем.
check_direct_port_note() {
  local port=$1 outside
  if ! outside=$(listening_outside "$port"); then
    warn "нет команды ss — проверить прямой доступ к порту $port нечем"
    return 0
  fi
  if [ -n "$outside" ] && [ "$HAVE_NGINX" = "0" ]; then
    # Без прокси панель и должна слушать адрес сервера — иначе до неё не дойти
    # даже из туннеля. Посторонних отсекает список адресов в самой панели.
    info "панель слушает $(echo "$outside" | tr '\n' ' ') — так и надо без nginx"
    info "посторонних отсекает IP_ALLOWLIST, проверьте его выше"
  elif [ -n "$outside" ]; then
    warn "${RED}панель слушает мимо nginx: $(echo "$outside" | tr '\n' ' ')${OFF}"
    warn "в неё можно зайти по адресу сервера без VPN — запустите скрипт без --status"
  else
    info "панель слушает только localhost — снаружи только через nginx"
  fi
  return 0
}

# ------------------------------------------------------------------ состояние
show_status() {
  step "Ограничение доступа"
  local site="" connected=0
  site=$(site_config) || site=""
  if [ -n "$site" ] && grep -qs "include $SNIPPET;" "$site"; then connected=1; fi

  if [ "$HAVE_NGINX" = "0" ]; then
    info "nginx не установлен — правилу прокси взяться неоткуда"
    info "вход ограничивает сама панель, см. следующий раздел"
  elif [ ! -f "$SNIPPET" ]; then
    warn "выключено — файл $SNIPPET не создан, панель открыта со всех адресов"
  elif ! grep -q '^ *deny all;' "$SNIPPET"; then
    warn "выключено — панель открыта со всех адресов"
  elif [ "$connected" = "1" ]; then
    info "${GREEN}включено${OFF} — панель отвечает только этим адресам:"
    grep '^ *allow' "$SNIPPET" | sed 's/^ *allow /      /; s/;$//'
  else
    warn "${RED}правило записано, но nginx его не применяет — панель открыта!${OFF}"
    if [ -n "$site" ]; then
      warn "в $site нет строки: include $SNIPPET;"
    else
      warn "не нашёл конфиг сайта панели в $NGINX_SITES (порт $(app_port))"
    fi
    warn "исправить: sudo bash $0 --subnet ВАША-СЕТЬ"
  fi

  if [ -n "$site" ]; then
    info "конфиг сайта: $site"
  fi

  step "Ограничение в самой панели"
  # Второй уровень, не зависящий от nginx: панель сверяет адрес посетителя сама.
  local app_list=""
  if [ -f "$APP_DIR/.env" ]; then
    app_list=$(awk -F= '/^IP_ALLOWLIST=/{sub(/^IP_ALLOWLIST=/, ""); print}' "$APP_DIR/.env" | tail -1 | tr -d ' ')
  fi
  if [ -z "$app_list" ]; then
    warn "IP_ALLOWLIST пуст — панель адрес не проверяет, всё держится на nginx"
  else
    info "${GREEN}включено${OFF} — IP_ALLOWLIST=$app_list"
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

# Ограничение только в nginx — одна точка отказа: конфиг сайта может потерять
# строку include, и панель молча откроется всем. Поэтому тот же список адресов
# кладём в .env, откуда его читает сама панель (IP_ALLOWLIST).
apply_app_allowlist() {
  local value=$1 restart=${2:-1}
  [ -f "$APP_DIR/.env" ] || { warn "Нет $APP_DIR/.env — панель адрес проверять не будет"; return 1; }
  set_env_var "$APP_DIR/.env" IP_ALLOWLIST "$value" || return 1
  if [ -n "$value" ]; then
    info "в $APP_DIR/.env записано IP_ALLOWLIST=$value — панель проверяет адрес сама"
  else
    info "в $APP_DIR/.env очищен IP_ALLOWLIST — панель больше не ограничивает по адресу"
  fi
  [ "$restart" = "1" ] || return 0
  if panel_in_systemd; then
    systemctl restart "$SERVICE" 2>/dev/null || true
  elif panel_in_docker; then
    (cd "$APP_DIR" && docker compose up -d >/dev/null 2>&1) ||
      warn "docker compose up -d не отработал — перезапустите панель вручную в $APP_DIR"
  fi
  return 0
}

if [ "$HAVE_NGINX" = "1" ]; then
  step "Правило nginx"
  write_snippet
  info "файл: $SNIPPET"
else
  step "nginx не установлен"
  warn "Правило для прокси не пишем — писать его некуда."
  warn "Вход закроем на уровне самой панели: она сверяет адрес посетителя сама."
  if [ "$MODE" = "on" ]; then
    warn "Панель при этом отвечает по http, без сертификата. Внутри туннеля"
    warn "WireGuard трафик всё равно шифруется, но в браузере будет http://"
    warn "Поставить https: sudo bash $APP_DIR/deploy/ssl.sh --domain ВАШ-ДОМЕН --email ПОЧТА"
  fi
fi

step "Ограничение в самой панели"
if [ "$MODE" = "off" ]; then
  # Снимаем с обоих уровней сразу: иначе nginx открыт, а панель закрыта, и это
  # выглядит как поломка, причину которой ищут в nginx.
  apply_app_allowlist "" || true
else
  ALLOW_LIST="127.0.0.1"
  for net in "${SUBNETS[@]}"; do ALLOW_LIST="$ALLOW_LIST,$net"; done
  for net in "${EXTRA[@]}"; do ALLOW_LIST="$ALLOW_LIST,$net"; done
  apply_app_allowlist "$ALLOW_LIST" || true
fi

# Конфиг сайта мог быть создан прежней версией ssl.sh — без include правило
# не сработает, поэтому дописываем его сами.
ensure_include() {
  local target backup
  target=$(site_config) || {
    warn "Не нашёл конфиг сайта панели в $NGINX_SITES (порт $(app_port))"
    return 1
  }
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

if [ "$HAVE_NGINX" = "1" ]; then
  if ! ensure_include; then
    warn "Правило записано, но конфиг сайта его не подключает: обновите панель"
    warn "и перезапустите deploy/ssl.sh — он добавит include сам."
  fi
  systemctl reload nginx 2>/dev/null || systemctl restart nginx || die "nginx не перезапустился"
  info "nginx перечитал конфигурацию"
fi

step "Проверка"
PORT=$(app_port)
DIRECT_OPEN=0
if [ "$MODE" = "on" ] && [ "$HAVE_NGINX" = "1" ]; then
  # Порт закрываем только когда перед панелью есть прокси. Без него панель
  # слушает адрес сервера сама — загнав её на localhost, мы отрезали бы вход
  # вообще, вместе с сотрудниками в туннеле.
  check_direct_port "$PORT" || DIRECT_OPEN=1
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
elif [ "$HAVE_NGINX" = "0" ]; then
  cat <<SUMMARY

${GREEN}${BOLD}Готово: вход в панель только из сети VPN.${OFF}
  Панель сверяет адрес посетителя сама — постороннему она отвечает 403 ещё до
  формы входа. Проверьте снаружи, без VPN: страница открываться не должна.

${YELLOW}nginx не установлен${OFF}, поэтому панель отвечает по http и на своём порту
  $PORT. Внутри туннеля WireGuard трафик шифруется, так что пароль по открытой
  сети не идёт, но в браузере будет http:// и адрес с портом.

  Поставить https и убрать порт из адреса:
    sudo bash $APP_DIR/deploy/ssl.sh --domain ВАШ-ДОМЕН --email ПОЧТА
    sudo bash $APP_DIR/deploy/vpn-only.sh --subnet ВАША-СЕТЬ
SUMMARY
elif [ "$DIRECT_OPEN" = "1" ]; then
  cat <<SUMMARY

${RED}${BOLD}Правило nginx включено, но прямой доступ к панели остался открыт.${OFF}
  Через nginx (порты 80 и 443) пускаются только адреса из сети VPN, однако
  сама панель по-прежнему слушает внешний адрес на порту $PORT — зайти в неё
  можно в обход VPN, минуя это правило и https.

  Что сделать:
    служба systemd   HOST=127.0.0.1 в $APP_DIR/.env, затем
                     sudo systemctl restart $SERVICE
    Docker           BIND_ADDR=127.0.0.1 в $APP_DIR/.env, затем
                     cd $APP_DIR && sudo docker compose up -d
  Проверить:
    sudo ss -ltnp | grep :$PORT      # адрес должен быть только 127.0.0.1
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
# Ненулевой код, пока прямой доступ открыт: правило nginx стоит, но панель
# всё ещё пускает мимо VPN, и молча считать это успехом нельзя.
[ "$DIRECT_OPEN" = "0" ] || exit 1
