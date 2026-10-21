#!/usr/bin/env bash
# Installs or updates community-radar on Debian (LXC container), from the sources copied to /opt/community-radar.
set -euo pipefail
APP=/opt/community-radar
apt-get update -qq && apt-get install -y -qq python3-venv >/dev/null
id radar >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin radar
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q "$APP"
[ -f /etc/community-radar.env ] || install -m 0640 -g radar "$APP/deploy/community-radar.env.example" /etc/community-radar.env
install -m 0644 "$APP/deploy/community-radar.service" /etc/systemd/system/community-radar.service
systemctl daemon-reload
systemctl enable --now community-radar
systemctl restart community-radar
