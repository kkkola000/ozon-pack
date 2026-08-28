#!/usr/bin/env bash
# Установка Ozon Pack на чистый VPS (Ubuntu/Debian) без Docker.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/ozon-pack}
PORT=${PORT:-8080}

echo "==> Пакеты"
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-pip git ufw

echo "==> Пользователь и каталог $APP_DIR"
id -u ozon >/dev/null 2>&1 || sudo useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin ozon
sudo mkdir -p "$APP_DIR/data"

echo "==> Код"
sudo cp -r app requirements.txt "$APP_DIR/"
[ -f "$APP_DIR/.env" ] || sudo cp .env.example "$APP_DIR/.env"

echo "==> Окружение Python"
sudo python3 -m venv "$APP_DIR/.venv"
sudo "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
sudo chown -R ozon:ozon "$APP_DIR"

echo "==> Служба systemd"
sudo cp deploy/ozon-pack.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ozon-pack

echo "==> Файрвол: открываем $PORT"
sudo ufw allow "$PORT"/tcp || true

echo
echo "Готово. Впишите ключи Ozon в $APP_DIR/.env и выполните:"
echo "  sudo systemctl restart ozon-pack"
echo "Пароль администратора при первом запуске: sudo journalctl -u ozon-pack | grep 'Создан администратор'"
echo "Панель: http://$(hostname -I | awk '{print $1}'):$PORT"
