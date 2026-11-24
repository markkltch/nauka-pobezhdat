#!/usr/bin/env bash
# Sign-in with Yandex ID: the app's ClientID and Client secret from oauth.yandex.ru.
# The secret is typed by the owner and is not shown.
#   bash ~/nauka-app/deploy/set-yandex-oauth.sh
set -euo pipefail
ENV=~/nauka-app/.env
read -r -p "ClientID приложения: " CID
read -r -s -p "Client secret (ввод не отображается): " SECRET; echo
CID=$(printf '%s' "$CID" | tr -d '[:space:]'); SECRET=$(printf '%s' "$SECRET" | tr -d '[:space:]')
for p in $(seq 16 $(( ${#SECRET} / 2 ))); do
  if (( ${#SECRET} % p == 0 )) && [ "$(printf "${SECRET:0:p}%.0s" $(seq 1 $(( ${#SECRET} / p ))))" = "$SECRET" ]; then SECRET=${SECRET:0:p}; break; fi
done
[ -n "$CID" ] && [ -n "$SECRET" ] || { echo "Пусто — ничего не изменено."; exit 1; }
sed -i '/^YANDEX_OAUTH_CLIENT_ID=/d; /^YANDEX_OAUTH_CLIENT_SECRET=/d' "$ENV"
printf 'YANDEX_OAUTH_CLIENT_ID=%s\nYANDEX_OAUTH_CLIENT_SECRET=%s\n' "$CID" "$SECRET" >> "$ENV"
chmod 600 "$ENV"
echo "Записано: ClientID ${#CID} символов, секрет ${#SECRET} символов."
unset SECRET
touch ~/www/nauka.ogiinst.ru/tmp/restart.txt
sleep 3
curl -s -m 20 https://nauka.ogiinst.ru/login | grep -q "Яндекс ID" && echo "Кнопка «Войти с Яндекс ID» на месте." || echo "Кнопки нет — проверьте значения."
