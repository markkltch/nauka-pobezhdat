#!/usr/bin/env bash
# Install or update on reg.ru shared hosting (ispmanager). Run in the hosting shell:
#   bash ~/nauka-app/deploy/hosting-install.sh
set -euo pipefail
SITE=${SITE:-nauka.ogiinst.ru}
PYBIN=${PYBIN:-/opt/python/python-3.12/bin/python}
APP=~/nauka-app
VENV=~/nauka-venv
WEB=~/www/$SITE

cd "$APP"
git pull --ff-only || true
[ -d "$VENV" ] || "$PYBIN" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r requirements-hosting.txt

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 32)|" .env
  sed -i "s|^DOMAIN=.*|DOMAIN=$SITE|; s|^BASE_URL=.*|BASE_URL=https://$SITE|" .env
  printf '\nDATABASE_URL=sqlite:///%s/nauka.db\nRUN_JOBS_IN_PROCESS=false\nMAIL_VIA_SENDMAIL=true\nSMTP_FROM=noreply@%s\n' "$APP" "$SITE" >> .env
  chmod 600 .env
  echo "Created $APP/.env — add ADMIN_EMAILS, SMTP_* and the LLM key there."
fi

mkdir -p "$WEB"
cp "$APP/passenger_wsgi.py" "$WEB/passenger_wsgi.py"
rm -f "$WEB/index.html" "$WEB/index.php"
mkdir -p "$WEB/tmp" && touch "$WEB/tmp/restart.txt" "$WEB/.restart-app"
echo "Done. Check https://$SITE/healthz"
