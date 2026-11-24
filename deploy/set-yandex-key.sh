#!/usr/bin/env bash
# Connect Yandex AI Studio (models + SpeechKit). The key is typed by the owner; it is not shown and goes only into ~/nauka-app/.env.
#   bash ~/nauka-app/deploy/set-yandex-key.sh
set -euo pipefail
ENV=~/nauka-app/.env
current=$(grep -E '^YANDEX_FOLDER_ID=' "$ENV" | cut -d= -f2- || true)
read -r -p "ID каталога Yandex Cloud [${current:-b1g...}]: " FOLDER
FOLDER=${FOLDER:-$current}
read -r -s -p "API-ключ сервисного аккаунта (ввод не отображается): " KEY; echo
KEY=$(printf '%s' "$KEY" | tr -d '[:space:]')
# a paste that landed several times: keep one copy
for p in $(seq 20 $(( ${#KEY} / 2 ))); do
  if (( ${#KEY} % p == 0 )) && [ "$(printf "${KEY:0:p}%.0s" $(seq 1 $(( ${#KEY} / p ))))" = "$KEY" ]; then KEY=${KEY:0:p}; break; fi
done
[ -n "$FOLDER" ] && [ -n "$KEY" ] || { echo "Пусто — ничего не изменено."; exit 1; }
sed -i '/^LLM_PROVIDER=/d; /^YANDEX_API_KEY=/d; /^YANDEX_FOLDER_ID=/d; /^YANDEX_SPEECH_API_KEY=/d' "$ENV"
printf 'LLM_PROVIDER=yandex\nYANDEX_FOLDER_ID=%s\nYANDEX_API_KEY=%s\n' "$FOLDER" "$KEY" >> "$ENV"
chmod 600 "$ENV"
echo "Ключ записан (${#KEY} символов)."
unset KEY
touch ~/www/nauka.ogiinst.ru/tmp/restart.txt
cd ~/nauka-app && ~/nauka-venv/bin/python -m app.check_llm
