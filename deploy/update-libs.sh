#!/usr/bin/env bash
#
# Обновление библиотек сервера, на которых работает панель.
#
#   sudo /opt/ozon-pack/deploy/update-libs.sh --check   посмотреть, что устарело
#   sudo /opt/ozon-pack/deploy/update-libs.sh           обновить и перезапустить панель
#
# Обновляет то, от чего панель зависит: python3, sqlite3, git, curl, шрифт для
# PDF, а также nginx и certbot, если они стоят. Плюс приводит в порядок
# окружение Python самой панели.
#
# Код панели этот скрипт не трогает — для него есть установщик (deploy/install.sh).
set -Eeuo pipefail

APP_DIR=${APP_DIR:-/opt/ozon-pack}
SERVICE=${SERVICE:-ozon-pack}
CHECK_ONLY=0
ALL=0
QUIET=0
ASSUME_YES=0
HEALTH_TRIES=${HEALTH_TRIES:-30}

# Библиотеки, без которых панель не работает. Список закрытый: обновлять весь
# сервер молча — не дело скрипта панели, для этого есть отдельный флаг --all.
#
#   python3, python3-venv, python3-pip — то, на чём панель запускается
#   sqlite3                            — база панели, файл в data/
#   git, curl, ca-certificates         — обновление кода и запросы к площадкам
#   fonts-dejavu-core                  — кириллица в листе возвратов PDF
PACKAGES="python3 python3-venv python3-pip sqlite3 git curl ca-certificates fonts-dejavu-core"
# Эти обновляем, только если они уже стоят: панель работает и без них.
OPTIONAL="nginx certbot python3-certbot-nginx ufw"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
step() { [ "$QUIET" = "1" ] || printf '\n%s==> %s%s\n' "$BOLD" "$*" "$OFF"; }
info() { [ "$QUIET" = "1" ] || printf '    %s\n' "$*"; }
warn() { printf '%s[!] %s%s\n' "$YELLOW" "$*" "$OFF" >&2; }
die()  { printf '%s[x] %s%s\n' "$RED" "$*" "$OFF" >&2; exit 1; }

on_error() {
  local code=$? cmd=$BASH_COMMAND line=$1 i=1
  while [ "${FUNCNAME[$i]:-main}" != "main" ]; do line=${BASH_LINENO[$i]}; i=$((i + 1)); done
  printf '%s[x] %s прервано на строке %s.%s\n' "$RED" "Обновление библиотек" "$line" "$OFF" >&2
  printf '    Команда: %s\n' "$cmd" >&2
  printf '    Код возврата: %s. Причина — в выводе выше.\n' "$code" >&2
  exit 1
}
trap 'on_error $LINENO' ERR

usage() {
  [ -r "$0" ] && sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'USAGE'

Флаги:
  --check        только показать, что устарело; ничего не ставить
  --all          обновить все пакеты сервера (apt upgrade), а не только нужные панели
  --yes          не спрашивать подтверждения
  --quiet        молчать, когда обновлять нечего, — для cron
  --dir PATH     каталог установки (по умолчанию /opt/ozon-pack)
  --service NAME служба systemd (по умолчанию ozon-pack)
  -h, --help     эта справка

Коды возврата:
  0   обновлять нечего либо обновление прошло успешно
  10  (только с --check) есть что обновить
  1   ошибка: нет прав, нет apt, панель не поднялась после перезапуска

Библиотеки Python самой панели закреплены по версиям в requirements.txt:
скрипт приводит окружение к этому списку, но выше закреплённых версий не
поднимает — их меняют в коде, а не на сервере.

Проверять раз в неделю строкой в cron от root:
  23 4 * * 1  /opt/ozon-pack/deploy/update-libs.sh --yes --quiet
USAGE
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    --all) ALL=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --quiet) QUIET=1; ASSUME_YES=1; shift ;;
    --dir) APP_DIR=$2; shift 2 ;;
    --service) SERVICE=$2; shift 2 ;;
    -h|--help) usage ;;
    *) die "Неизвестный флаг: $1 (справка: $0 --help)" ;;
  esac
done

command -v apt-get >/dev/null || die "Скрипт рассчитан на Ubuntu/Debian (нет apt-get)"

installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "ok installed"; }

# Список: обязательные плюс те необязательные, что уже стоят.
WANTED=""
for package in $PACKAGES; do
  WANTED="$WANTED $package"
done
for package in $OPTIONAL; do
  installed "$package" && WANTED="$WANTED $package"
done
WANTED=${WANTED# }

versions() {
  # Версии пакетов одной строкой — по ним видно, что реально поменялось.
  dpkg-query -W -f='${Package} ${Version}\n' $WANTED 2>/dev/null | sort
}

step "Что стоит сейчас"
info "каталог панели: $APP_DIR"
if [ "$(id -u)" = "0" ]; then
  apt-get update -qq || warn "Не удалось обновить список пакетов — проверьте сеть: curl -sI https://deb.debian.org"
else
  warn "Запущено не от root: список пакетов может быть устаревшим, точная проверка — sudo $0 --check"
fi

BEFORE=$(versions)
[ "$QUIET" = "1" ] || printf '%s\n' "$BEFORE" | sed 's/^/    /'

# apt сам знает, что можно поднять: спрашиваем только про интересующие пакеты.
UPGRADABLE=$(apt-get install --only-upgrade --simulate $WANTED 2>/dev/null |
             awk '/^Inst /{print $2}' | sort -u || true)
PIP="$APP_DIR/.venv/bin/pip"

step "Что можно обновить"
if [ -n "$UPGRADABLE" ]; then
  printf '%s\n' "$UPGRADABLE" | sed 's/^/    /'
else
  info "системные пакеты свежие"
fi

if [ "$CHECK_ONLY" = "1" ]; then
  if [ -n "$UPGRADABLE" ]; then
    printf '\n%s%sЕсть что обновить.%s Обновиться: sudo %s --yes\n\n' "$BOLD" "$YELLOW" "$OFF" "$0"
    exit 10
  fi
  [ "$QUIET" = "1" ] || printf '\n%sВсё свежее — обновлять нечего.%s\n\n' "$GREEN" "$OFF"
  exit 0
fi

if [ -z "$UPGRADABLE" ] && [ "$ALL" != "1" ]; then
  # Окружение панели всё равно проверяем: библиотека могла отвалиться сама,
  # а узнать об этом по падению службы — худший способ.
  step "Окружение панели"
  if [ -x "$PIP" ]; then
    "$PIP" install --quiet -r "$APP_DIR/requirements.txt" && info "библиотеки на месте"
  else
    warn "Окружение Python не найдено ($APP_DIR/.venv)"
  fi
  [ "$QUIET" = "1" ] || printf '\n%sВсё свежее — обновлять нечего.%s\n\n' "$GREEN" "$OFF"
  exit 0
fi

[ "$(id -u)" = "0" ] || die "Установка пакетов требует прав root: sudo $0"

if [ "$ASSUME_YES" != "1" ]; then
  printf '\n%s' "Обновить перечисленное и перезапустить панель? [y/N] "
  read -r answer
  case "$answer" in [yY]*) ;; *) die "Отменено" ;; esac
fi

export DEBIAN_FRONTEND=noninteractive

if [ "$ALL" = "1" ]; then
  step "Обновление всех пакетов сервера"
  warn "apt upgrade трогает весь сервер, а не только панель: возможны перезапуски служб"
  apt-get upgrade -y -qq || die "apt upgrade не прошёл — смотрите вывод выше"
else
  step "Обновление библиотек панели"
  # --only-upgrade: доустанавливать отсутствующее — дело установщика, здесь
  # обновляется то, что уже стоит, и неожиданных новых пакетов не появляется.
  apt-get install -y -qq --only-upgrade $WANTED || die "apt не смог обновить пакеты — смотрите вывод выше"
fi

step "Окружение панели"
if [ -x "$PIP" ]; then
  "$PIP" install --quiet --upgrade pip || warn "pip обновить не удалось"
  # Версии библиотек закреплены в requirements.txt: ставим ровно их. Поднимать
  # выше на сервере нельзя — панель проверена именно на этом наборе.
  "$PIP" install --quiet -r "$APP_DIR/requirements.txt" || die "Не удалось поставить библиотеки панели"
  info "библиотеки приведены к requirements.txt"
else
  warn "Окружение Python не найдено ($APP_DIR/.venv) — пропускаю"
fi

AFTER=$(versions)
step "Что изменилось"
CHANGES=$(diff <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$AFTER") | grep '^>' | sed 's/^> //' || true)
if [ -n "$CHANGES" ]; then
  printf '%s\n' "$CHANGES" | sed 's/^/    /'
else
  info "версии пакетов прежние"
fi

# Перезапуск нужен даже когда обновился только python3: служба держит в памяти
# старый интерпретатор и библиотеки, пока её не перезапустят.
step "Перезапуск панели"
if ! command -v systemctl >/dev/null 2>&1; then
  warn "systemctl не найден — перезапустите панель сами"
else
  systemctl restart "$SERVICE"
  # Порт — из .env установки. Файла может не быть вовсе (панель ставили иначе),
  # и на этом скрипт спотыкаться не должен: берём значение по умолчанию.
  PORT=""
  if [ -f "$APP_DIR/.env" ]; then
    PORT=$(awk -F= '/^PORT=/{print $2}' "$APP_DIR/.env" | tail -1 | tr -d ' ' || true)
  fi
  PORT=${PORT:-8080}
  HEALTHY=0
  for _ in $(seq 1 "$HEALTH_TRIES"); do
    if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
      HEALTHY=1; break
    fi
    sleep 1
  done
  if [ "$HEALTHY" != "1" ]; then
    warn "Панель не ответила на http://127.0.0.1:$PORT/healthz"
    warn "Журнал: journalctl -u $SERVICE -n 50 --no-pager"
    warn "Вернуть прежний код панели: sudo bash $APP_DIR/deploy/install.sh --yes"
    exit 1
  fi
  info "${GREEN}панель отвечает${OFF}"
fi

if [ -f /var/run/reboot-required ]; then
  printf '\n%s[!] Системе нужна перезагрузка — обновилось ядро или системная библиотека.%s\n' "$YELLOW" "$OFF"
  printf '    Панель это переживёт, но перезагрузить сервер стоит в нерабочее время: sudo reboot\n'
fi

printf '\n%s%sГотово.%s\n\n' "$GREEN" "$BOLD" "$OFF"
