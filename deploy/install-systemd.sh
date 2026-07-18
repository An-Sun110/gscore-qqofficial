#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root." >&2
  exit 1
fi

APP_DIR=/opt/gscore-qqofficial
id gscore >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin gscore
mkdir -p "$APP_DIR"
cp -R gscore_qq pyproject.toml README.md "$APP_DIR/"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --no-cache-dir "$APP_DIR"
chown -R gscore:gscore "$APP_DIR"
mkdir -p /var/lib/gscore-qqofficial
chown gscore:gscore /var/lib/gscore-qqofficial
install -m 0644 deploy/gscore-qq.service /etc/systemd/system/gscore-qq.service
if [ ! -f /etc/gscore-qqofficial.env ]; then
  install -m 0600 -o root -g root .env.example /etc/gscore-qqofficial.env
  echo "Edit /etc/gscore-qqofficial.env, then start the service." >&2
fi
grep -q '^STATE_PATH=' /etc/gscore-qqofficial.env || echo 'STATE_PATH=/var/lib/gscore-qqofficial/state.db' >> /etc/gscore-qqofficial.env
systemctl daemon-reload
systemctl enable gscore-qq.service
echo "Run: systemctl restart gscore-qq"
