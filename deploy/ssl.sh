#!/usr/bin/env bash
#
# HTTPS для панели Ozon Pack: nginx впереди, сертификат Let's Encrypt.
#
#   sudo bash ssl.sh --domain seller.example.com --email admin@example.com
#   sudo bash ssl.sh --ip                  # сертификат на IP, если домена нет
#   sudo bash ssl.sh --self-signed         # самоподписанный, для закрытой сети
#
# После настройки панель слушает только localhost и доступна снаружи
# исключительно через nginx по https.
#
set -Eeuo pipefail

APP_DIR=${APP_DIR:-/opt/ozon-pack}
SERVICE=${SERVICE:-ozon-pack}
SITE=${SITE:-ozon-pack}
DOMAIN=${DOMAIN:-}
EMAIL=${EMAIL:-}
IP_MODE=0
SELF_SIGNED=0
STAGING=0
HSTS=0
KEEP_OPEN=0
WEBROOT=/var/www/certbot

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$OFF"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%s[!] %s%s\n' "$YELLOW" "$*" "$OFF"; }
die()  { printf '%s[x] %s%s\n' "$RED" "$*" "$OFF" >&2; exit 1; }
# Ошибка внутри функции-обёртки указывала на строку её объявления, а не на
# место вызова — по такому сообщению непонятно, что сломалось. Показываем и
# саму команду, и строку, с которой всё пошло не так.
on_error() {
  local code=$? cmd=$BASH_COMMAND line=$1 i=1
  while [ "${FUNCNAME[$i]:-main}" != "main" ]; do line=${BASH_LINENO[$i]}; i=$((i + 1)); done
  printf '%s[x] %s прервана на строке %s.%s\n' "$RED" "Настройка" "$line" "$OFF" >&2
  printf '    Команда: %s\n' "$cmd" >&2
  printf '    Код возврата: %s. Причина — в выводе выше.\n' "$code" >&2
  exit 1
}
trap 'on_error $LINENO' ERR

usage() {
  cat <<'USAGE'
HTTPS для панели Ozon Pack.

  --domain NAME      выпустить сертификат Let's Encrypt на домен (90 дней)
  --email MAIL       почта для уведомлений Let's Encrypt об истечении
  --ip [ADDR]        сертификат Let's Encrypt на IP-адрес (160 часов, нужен
                     публичный IP; адрес определяется автоматически)
  --self-signed      самоподписанный сертификат (браузер будет предупреждать)
  --staging          тестовый сервер Let's Encrypt (без лимитов, но недоверенный)
  --hsts             включить HSTS (браузеры запомнят https на 180 дней)
  --keep-open        оставить прямой доступ к панели по http на её порту
  --dir PATH         каталог установки (по умолчанию /opt/ozon-pack)
  -h, --help         эта справка
USAGE
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN=$2; shift 2 ;;
    --email) EMAIL=$2; shift 2 ;;
    --ip)
      IP_MODE=1
      if [ "${2:-}" ] && [ "${2#-}" = "$2" ]; then DOMAIN=$2; shift 2; else shift; fi ;;
    --self-signed) SELF_SIGNED=1; shift ;;
    --staging) STAGING=1; shift ;;
    --hsts) HSTS=1; shift ;;
    --keep-open) KEEP_OPEN=1; shift ;;
    --dir) APP_DIR=$2; shift 2 ;;
    -h|--help) usage ;;
    *) die "Неизвестный аргумент: $1 (--help для справки)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "Запустите с правами root: sudo bash $0 ..."
[ -f "$APP_DIR/.env" ] || die "Не найден $APP_DIR/.env — сначала установите панель (deploy/install.sh)"

MODES=$((IP_MODE + SELF_SIGNED))
[ -n "$DOMAIN" ] && [ "$IP_MODE" = "0" ] && MODES=$((MODES + 1))
[ "$MODES" -eq 1 ] || die "Выберите ровно один режим: --domain NAME, --ip или --self-signed"

APP_PORT=$(awk -F= '/^PORT=/{print $2}' "$APP_DIR/.env" | tail -1)
APP_PORT=${APP_PORT:-8080}

# ------------------------------------------------------------------ адрес панели
public_ip() {
  local ip
  for url in https://api.ipify.org https://ifconfig.me/ip https://icanhazip.com; do
    ip=$(curl -fsS --max-time 8 "$url" 2>/dev/null | tr -d '[:space:]') || continue
    [ -n "$ip" ] && { echo "$ip"; return; }
  done
  hostname -I 2>/dev/null | awk '{print $1}'
}

if [ "$IP_MODE" = "1" ] && [ -z "$DOMAIN" ]; then
  DOMAIN=$(public_ip)
  [ -n "$DOMAIN" ] || die "Не удалось определить публичный IP — задайте его: --ip 1.2.3.4"
fi
if [ "$SELF_SIGNED" = "1" ] && [ -z "$DOMAIN" ]; then
  DOMAIN=$(public_ip)
fi
[ -n "$DOMAIN" ] || die "Не задано имя или адрес панели"

case "$DOMAIN" in
  10.*|192.168.*|127.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*)
    [ "$SELF_SIGNED" = "1" ] || die "Let's Encrypt не выдаёт сертификаты на приватные адреса ($DOMAIN). Используйте --self-signed или домен." ;;
esac

step "Панель: $APP_DIR, порт $APP_PORT; адрес: $DOMAIN"

# На серверах с отключённым IPv6 строка listen [::] роняет весь nginx,
# поэтому добавляем её только когда стек действительно есть.
if [ -f /proc/net/if_inet6 ]; then HAS_IPV6=1; else HAS_IPV6=0; info "IPv6 не обнаружен — nginx будет слушать только IPv4"; fi

# ------------------------------------------------------------------ пакеты
step "nginx"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx curl openssl >/dev/null
mkdir -p "$WEBROOT"

# Отдельная директива http2 появилась в nginx 1.25.1; в более старых версиях
# (в Ubuntu 24.04 идёт 1.24) http2 включается параметром listen.
NGINX_VERSION=$(nginx -v 2>&1 | sed 's#.*nginx/##; s# .*##')
if [ "$(printf '%s\n1.25.1\n' "$NGINX_VERSION" | sort -V | head -1)" = "1.25.1" ] && [ "$NGINX_VERSION" != "1.25.0" ]; then
  HTTP2_STYLE=directive
else
  HTTP2_STYLE=listen
fi
info "nginx $NGINX_VERSION"

CERT_DIR=/etc/letsencrypt/live/$DOMAIN
if [ "$SELF_SIGNED" = "1" ]; then
  CERT_DIR=/etc/ssl/ozon-pack
fi

# ------------------------------------------------------------------ конфиг nginx
ACCESS_SNIPPET=/etc/nginx/snippets/ozon-pack-access.conf

ensure_access_snippet() {
  # Правило доступа к панели живёт отдельным файлом: его меняет vpn-only.sh,
  # а конфиг сайта только подключает — поэтому ограничение переживает
  # повторный запуск этого скрипта.
  mkdir -p "$(dirname "$ACCESS_SNIPPET")"
  [ -f "$ACCESS_SNIPPET" ] && return

  # Панель уже настроена на список адресов (установка с --vpn-subnet): нельзя
  # создавать правило разрешающим — nginx открыл бы то, что панель закрывает,
  # и оператор увидел бы 403 из панели вместо честного отказа на входе.
  local app_list=""
  if [ -f "$APP_DIR/.env" ]; then
    app_list=$(awk -F= '/^IP_ALLOWLIST=/{sub(/^IP_ALLOWLIST=/, ""); print}' "$APP_DIR/.env" | tail -1 | tr -d ' ')
  fi
  if [ -n "$app_list" ]; then
    {
      echo "# Создано ssl.sh по списку IP_ALLOWLIST из $APP_DIR/.env."
      echo "# Открыть панель всем: sudo bash $APP_DIR/deploy/vpn-only.sh --off"
      echo "allow 127.0.0.1;"
      echo "allow ::1;"
      printf '%s\n' "$app_list" | tr ',' '\n' | while read -r entry; do
        entry=$(printf '%s' "$entry" | tr -d ' ')
        case "$entry" in ""|127.0.0.1|::1) continue ;; esac
        echo "allow $entry;"
      done
      echo "deny all;"
    } > "$ACCESS_SNIPPET"
    info "правило доступа создано по IP_ALLOWLIST: $app_list"
    return
  fi

  cat > "$ACCESS_SNIPPET" <<'CONF'
# Кто может открывать панель. Закрыть доступ всем, кроме VPN:
#   sudo bash /opt/ozon-pack/deploy/vpn-only.sh
allow all;
CONF
}

write_nginx() {
  local with_tls=$1
  ensure_access_snippet
  {
    cat <<CONF
# Создано deploy/ssl.sh для Ozon Pack. Правки перезапишутся при повторном запуске.
server {
    listen 80;
$( [ "$HAS_IPV6" = "1" ] && echo "    listen [::]:80;" )
    server_name $DOMAIN;

    # Проверка Let's Encrypt при выпуске и продлении
    location /.well-known/acme-challenge/ {
        root $WEBROOT;
    }
CONF
    if [ "$with_tls" = "1" ]; then
      cat <<CONF

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl$( [ "$HTTP2_STYLE" = "listen" ] && echo " http2" );
$( [ "$HAS_IPV6" = "1" ] && echo "    listen [::]:443 ssl$( [ "$HTTP2_STYLE" = "listen" ] && echo " http2" );" )
$( [ "$HTTP2_STYLE" = "directive" ] && echo "    http2 on;" )
    server_name $DOMAIN;

    ssl_certificate     $CERT_DIR/fullchain.pem;
    ssl_certificate_key $CERT_DIR/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;
CONF
      [ "$HSTS" = "1" ] && echo '    add_header Strict-Transport-Security "max-age=15552000" always;'
      cat <<CONF
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options SAMEORIGIN always;
    add_header Referrer-Policy same-origin always;

    # Стикеры и листы возвратов бывают крупными
    client_max_body_size 20m;
    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;

    location /.well-known/acme-challenge/ {
        root $WEBROOT;
    }

    location / {
        include $ACCESS_SNIPPET;
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        # По этому заголовку панель понимает, что соединение защищено,
        # и помечает куку сессии как Secure
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
CONF
    else
      cat <<CONF

    location / {
        include $ACCESS_SNIPPET;
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
CONF
    fi
  } > "/etc/nginx/sites-available/$SITE"

  ln -sf "/etc/nginx/sites-available/$SITE" "/etc/nginx/sites-enabled/$SITE"
  rm -f /etc/nginx/sites-enabled/default
  nginx -t >/dev/null 2>&1 || { nginx -t; die "Ошибка в конфигурации nginx"; }
  reload_nginx
}

reload_nginx() {
  # На systemd-хосте достаточно reload; если nginx не запущен, его надо поднять.
  if systemctl reload nginx 2>/dev/null || systemctl restart nginx 2>/dev/null; then
    return 0
  fi
  if pgrep -x nginx >/dev/null 2>&1; then
    nginx -s reload 2>/dev/null && return 0
  fi
  nginx 2>/dev/null || warn "Не удалось запустить nginx — проверьте: nginx -t"
}

step "Конфигурация nginx (пока http, для проверки Let's Encrypt)"
write_nginx 0
info "сайт /etc/nginx/sites-available/$SITE"

# ------------------------------------------------------------------ сертификат
if [ "$SELF_SIGNED" = "1" ]; then
  step "Самоподписанный сертификат"
  mkdir -p "$CERT_DIR"
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
    -keyout "$CERT_DIR/privkey.pem" -out "$CERT_DIR/fullchain.pem" \
    -subj "/CN=$DOMAIN" -addext "subjectAltName=$( case "$DOMAIN" in *[a-zA-Z]*) echo "DNS:$DOMAIN";; *) echo "IP:$DOMAIN";; esac )" \
    >/dev/null 2>&1
  chmod 600 "$CERT_DIR/privkey.pem"
  info "выпущен на 10 лет: $CERT_DIR"
  warn "Браузер будет предупреждать о недоверенном сертификате — это нормально для"
  warn "самоподписанного. Для «зелёного замка» нужен домен и Let's Encrypt."
else
  # Домены и IP требуют разных версий certbot: в apt лежит старая, IP-сертификаты
  # появились в certbot 5.3+, поэтому для них ставим свежий из pip.
  if [ "$IP_MODE" = "1" ]; then
    step "certbot 5.x (нужен для сертификатов на IP)"
    apt-get install -y -qq python3-venv >/dev/null
    [ -x /opt/certbot/bin/certbot ] || python3 -m venv /opt/certbot
    /opt/certbot/bin/pip install --quiet --upgrade pip certbot
    ln -sf /opt/certbot/bin/certbot /usr/local/bin/certbot
    CERTBOT=/opt/certbot/bin/certbot
    info "$($CERTBOT --version 2>&1)"
  else
    step "certbot"
    apt-get install -y -qq certbot >/dev/null
    CERTBOT=$(command -v certbot)
    info "$($CERTBOT --version 2>&1)"
  fi

  # Предполётная проверка: адрес должен вести на этот сервер, иначе выпуск не пройдёт
  MY_IP=$(public_ip)
  if [ "$IP_MODE" = "1" ]; then
    [ "$DOMAIN" = "$MY_IP" ] || warn "Указан IP $DOMAIN, а внешний адрес сервера — $MY_IP. Проверьте, что это тот же сервер."
  else
    RESOLVED=$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1)
    if [ -z "$RESOLVED" ]; then
      die "Домен $DOMAIN не разрешается в IP. Добавьте A-запись на $MY_IP и подождите обновления DNS."
    elif [ "$RESOLVED" != "$MY_IP" ]; then
      warn "$DOMAIN ведёт на $RESOLVED, а внешний адрес сервера — $MY_IP."
      warn "Если между ними прокси (например, Cloudflare), выпуск может не пройти:"
      warn "на время выпуска отключите проксирование (серое облако)."
    else
      info "DNS в порядке: $DOMAIN -> $RESOLVED"
    fi
  fi

  step "Выпуск сертификата Let's Encrypt"
  ARGS=(certonly --webroot --webroot-path "$WEBROOT" --non-interactive --agree-tos --keep-until-expiring)
  if [ -n "$EMAIL" ]; then
    ARGS+=(--email "$EMAIL")
  else
    ARGS+=(--register-unsafely-without-email)
    warn "Почта не указана — Let's Encrypt не сможет предупредить об истечении (--email)"
  fi
  [ "$STAGING" = "1" ] && ARGS+=(--staging)
  if [ "$IP_MODE" = "1" ]; then
    # Сертификаты на IP выдаются только по профилю shortlived: 160 часов
    ARGS+=(--preferred-profile shortlived --ip-address "$DOMAIN" --cert-name "$DOMAIN")
  else
    ARGS+=(-d "$DOMAIN")
  fi
  "$CERTBOT" "${ARGS[@]}"
  [ -f "$CERT_DIR/fullchain.pem" ] || die "Сертификат не появился в $CERT_DIR"
  info "сертификат: $CERT_DIR"

  # ---------------------------------------------------------------- продление
  step "Автопродление"
  mkdir -p /etc/letsencrypt/renewal-hooks/deploy
  cat > /etc/letsencrypt/renewal-hooks/deploy/10-reload-nginx.sh <<'HOOK'
#!/bin/sh
# После обновления сертификата nginx должен его перечитать
systemctl reload nginx 2>/dev/null || nginx -s reload 2>/dev/null || true
HOOK
  chmod +x /etc/letsencrypt/renewal-hooks/deploy/10-reload-nginx.sh

  if systemctl list-unit-files 2>/dev/null | grep -q '^certbot.timer'; then
    systemctl enable --now certbot.timer >/dev/null 2>&1 || true
    info "продление по таймеру certbot.timer (дважды в сутки)"
  else
    # certbot из pip своего таймера не приносит — заводим собственный.
    # Для 160-часовых сертификатов на IP проверка нужна часто.
    cat > /etc/systemd/system/certbot-renew.service <<UNIT
[Unit]
Description=Продление сертификатов Let's Encrypt

[Service]
Type=oneshot
ExecStart=$CERTBOT renew --quiet
UNIT
    cat > /etc/systemd/system/certbot-renew.timer <<'UNIT'
[Unit]
Description=Продление сертификатов Let's Encrypt (каждые 6 часов)

[Timer]
OnCalendar=*-*-* 0,6,12,18:17:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
UNIT
    systemctl daemon-reload
    systemctl enable --now certbot-renew.timer >/dev/null 2>&1 || warn "Не удалось включить таймер продления"
    info "продление по таймеру certbot-renew.timer (каждые 6 часов)"
  fi
fi

# ------------------------------------------------------------------ включаем TLS
step "Включение https"
write_nginx 1
info "nginx слушает 443"

# ------------------------------------------------------------------ закрываем прямой доступ
# Адреса, на которых порт панели слушает не localhost. Пусто — снаружи напрямую
# не подключиться. Код 2 — проверить нечем (нет ss).
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

DIRECT_OPEN=0
if [ "$KEEP_OPEN" = "0" ]; then
  step "Панель закрывается от прямого доступа"
  if systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE.service"; then
    set_env_var "$APP_DIR/.env" HOST 127.0.0.1
    # nginx стучится с самого сервера, поэтому доверять X-Forwarded-For можно
    # только ему. «*» тут означало бы, что адрес посетителя подделает кто угодно.
    set_env_var "$APP_DIR/.env" FORWARDED_ALLOW_IPS 127.0.0.1
    info "в $APP_DIR/.env записано HOST=127.0.0.1, перезапускаю $SERVICE"
    systemctl restart "$SERVICE"
  elif command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ozon-pack$'; then
    # В контейнере HOST=127.0.0.1 сделал бы панель недоступной и для nginx —
    # прямой доступ там закрывается адресом, на котором опубликован порт.
    set_env_var "$APP_DIR/.env" BIND_ADDR 127.0.0.1
    # С хоста nginx приходит в контейнер с адреса шлюза docker-сети, а не с
    # 127.0.0.1: иначе в журнале окажется адрес моста вместо адреса сборщика.
    set_env_var "$APP_DIR/.env" FORWARDED_ALLOW_IPS 172.16.0.0/12
    info "BIND_ADDR=127.0.0.1 — порт контейнера публикуется только на localhost"
    if ! (cd "$APP_DIR" && docker compose up -d >/dev/null 2>&1); then
      warn "docker compose up -d не отработал — выполните вручную в $APP_DIR"
    fi
  else
    warn "Служба $SERVICE не найдена и контейнер не запущен."
    warn "Закройте прямой доступ вручную: HOST=127.0.0.1 в $APP_DIR/.env"
    warn "(в Docker — BIND_ADDR=127.0.0.1) и перезапустите панель."
    DIRECT_OPEN=1
  fi

  # Проверяем, а не рапортуем: правка .env бесполезна, если панель её не читает.
  if [ "$DIRECT_OPEN" = "0" ]; then
    OUTSIDE=""
    for _ in 1 2 3 4 5; do
      sleep 2
      OUTSIDE=$(listening_outside "$APP_PORT") || OUTSIDE="?"
      [ -n "$OUTSIDE" ] && [ "$OUTSIDE" != "?" ] || break
    done
    if [ "$OUTSIDE" = "?" ]; then
      warn "Не нашёл команду ss — проверить прямой доступ нечем, сверьте вручную:"
      warn "  sudo ss -ltnp | grep :$APP_PORT"
    elif [ -n "$OUTSIDE" ]; then
      DIRECT_OPEN=1
      warn "${RED}Порт $APP_PORT по-прежнему слушает $(echo "$OUTSIDE" | tr '\n' ' ').${OFF}"
      warn "Панель осталась доступна по адресу сервера в обход nginx и https."
      warn "Посмотрите, кто держит порт:  sudo ss -ltnp | grep :$APP_PORT"
    else
      info "${GREEN}прямой доступ закрыт${OFF} — порт $APP_PORT слушает только localhost"
    fi
  fi
fi

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  step "Файрвол"
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  info "открыты 80 и 443"
  if [ "$KEEP_OPEN" = "0" ]; then
    ufw delete allow "$APP_PORT"/tcp >/dev/null 2>&1 && info "закрыт прямой порт $APP_PORT" || true
  fi
fi

# ------------------------------------------------------------------ проверка
step "Проверка"
sleep 2
CURL_OPTS=(-fsS --max-time 10)
[ "$SELF_SIGNED" = "1" ] && CURL_OPTS+=(-k)
case "$DOMAIN" in *:*) URL_HOST="[$DOMAIN]" ;; *) URL_HOST="$DOMAIN" ;; esac
HEALTHY=0
if curl "${CURL_OPTS[@]}" "https://$URL_HOST/healthz" >/dev/null 2>&1; then
  HEALTHY=1
  info "${GREEN}https работает${OFF}"
elif curl "${CURL_OPTS[@]}" -H "Host: $DOMAIN" "https://127.0.0.1/healthz" >/dev/null 2>&1; then
  # Снаружи адрес может быть недоступен с самого сервера (NAT, закрытый файрвол),
  # но локально всё поднято — это не ошибка настройки.
  HEALTHY=1
  info "${GREEN}https работает локально${OFF}"
  warn "Снаружи по https://$URL_HOST сервер сам себя не видит — проверьте с рабочего места"
else
  warn "Панель не ответила по https://$URL_HOST/healthz"
  warn "Проверьте: systemctl status nginx; systemctl status $SERVICE; journalctl -u $SERVICE -n 30"
fi

cat <<SUMMARY

${GREEN}${BOLD}Готово.${OFF}
  Адрес панели:  ${BOLD}https://$URL_HOST${OFF}
  Сертификат:    $CERT_DIR
  Конфиг nginx:  /etc/nginx/sites-available/$SITE
  Кто пускается: $ACCESS_SNIPPET
SUMMARY

if [ "$SELF_SIGNED" = "1" ]; then
  cat <<SUMMARY
  Тип:           самоподписанный (браузер предупреждает)
SUMMARY
elif [ "$IP_MODE" = "1" ]; then
  cat <<SUMMARY
  Тип:           Let's Encrypt на IP, срок 160 часов (продлевается автоматически)
SUMMARY
else
  cat <<SUMMARY
  Тип:           Let's Encrypt, срок 90 дней (продлевается автоматически)
  Проверка продления: sudo certbot renew --dry-run
SUMMARY
fi
if [ -f "$ACCESS_SNIPPET" ] && grep -q '^ *deny all;' "$ACCESS_SNIPPET"; then
  cat <<SUMMARY
  Доступ:        только из сети VPN (правило сохранено)
SUMMARY
else
  cat <<SUMMARY
  Закрыть панель снаружи (нужен WireGuard):
    sudo bash $APP_DIR/deploy/vpn-only.sh
SUMMARY
fi

if [ "$DIRECT_OPEN" = "1" ]; then
  cat <<SUMMARY

${RED}${BOLD}Внимание: панель осталась доступна в обход https.${OFF}
  Порт $APP_PORT слушает внешний адрес, поэтому в панель можно зайти по
  http://IP-сервера:$APP_PORT — без сертификата и мимо правил nginx, пароль
  при этом уходит по открытому каналу.

  Закрыть:
    служба systemd   HOST=127.0.0.1 в $APP_DIR/.env, затем
                     sudo systemctl restart $SERVICE
    Docker           BIND_ADDR=127.0.0.1 в $APP_DIR/.env, затем
                     cd $APP_DIR && sudo docker compose up -d
  Проверить:
    sudo ss -ltnp | grep :$APP_PORT      # адрес должен быть только 127.0.0.1
SUMMARY
fi
echo
[ "$HEALTHY" = "1" ] || exit 1
