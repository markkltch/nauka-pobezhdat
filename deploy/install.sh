#!/usr/bin/env bash
# First install on a fresh Ubuntu VPS (reg.ru). Run as root:
#   curl -fsSL https://raw.githubusercontent.com/markkltch/nauka-pobezhdat/main/deploy/install.sh | bash
# (the repository is private: clone it with a deploy key instead, see README)
set -euo pipefail
APP_DIR=/opt/nauka

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
apt-get install -y git >/dev/null

if [ ! -d "$APP_DIR/.git" ]; then
  echo "Clone the repository into $APP_DIR first (see README, 'Deploy key')." >&2
  exit 1
fi
cd "$APP_DIR"
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/" .env
  sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 16)/" .env
  echo "Created .env — fill DOMAIN, BASE_URL, ADMIN_EMAILS, SMTP_*, LLM keys, then run this script again."
  exit 0
fi
docker compose up -d --build
# nightly database backup at 03:30, keep 14 days
( crontab -l 2>/dev/null | grep -v nauka-backup ; echo "30 3 * * * $APP_DIR/deploy/backup.sh >> /var/log/nauka-backup.log 2>&1 # nauka-backup" ) | crontab -
echo "Started. Check: docker compose ps; open https://\$(grep ^DOMAIN .env | cut -d= -f2)"
