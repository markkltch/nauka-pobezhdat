#!/usr/bin/env bash
# Connect Yandex AI Studio. The key is typed by the owner; it is not shown and goes only into ~/nauka-app/.env.
#   bash ~/nauka-app/deploy/set-yandex-key.sh
set -euo pipefail
ENV=~/nauka-app/.env
read -r -p "ID каталога Yandex Cloud (b1g...): " FOLDER
read -r -s -p "API-ключ сервисного аккаунта (ввод не отображается): " KEY; echo
[ -n "$FOLDER" ] && [ -n "$KEY" ] || { echo "Пусто — ничего не изменено."; exit 1; }
sed -i '/^LLM_PROVIDER=/d; /^YANDEX_API_KEY=/d; /^YANDEX_FOLDER_ID=/d' "$ENV"
printf 'LLM_PROVIDER=yandex\nYANDEX_FOLDER_ID=%s\nYANDEX_API_KEY=%s\n' "$FOLDER" "$KEY" >> "$ENV"
chmod 600 "$ENV"
unset KEY
touch ~/www/nauka.ogiinst.ru/tmp/restart.txt
cd ~/nauka-app && ~/nauka-venv/bin/python -m app.check_llm
