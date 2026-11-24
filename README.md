# Наука побеждать

ИИ-наставник для руководителей по методу А. В. Суворова: разбор ситуации → уточняющие вопросы →
план действий с исполнителями и сроками → штаб команды с общими задачами.

## Что внутри

| Часть | Где |
| --- | --- |
| Сервер (FastAPI) | `app/main.py` — маршруты; `app/models.py` — таблицы |
| Вход по коду на почту | `app/auth.py`, `app/mailer.py` |
| Наставник | `app/mentor.py` — вопросы и план; `app/llm.py` — GigaChat, YandexGPT или заглушка |
| Методичка | `method/principles.md` — принципы; `method/quotes.json` — проверенные цитаты |
| Оплата ЮKassa и доступ | `app/billing.py` |
| Напоминания и продления | `app/jobs.py` (раз в 10 минут, внутри приложения) |
| Экраны | `app/templates/`, `app/static/app.css`; лендинг — `app/static/landing/` |
| Выкладка | `docker-compose.yml` (PostgreSQL, приложение, Caddy с HTTPS), `deploy/` |

Наставник показывает цитату Суворова, только если её id есть в `method/quotes.json`: модель выбирает
id, текст и источник берутся из файла. Чтобы поменять тон или правила, правьте `method/principles.md` —
код трогать не нужно.

## Запуск на своём компьютере

```bash
pip install -r requirements-dev.txt
LLM_PROVIDER=fake uvicorn app.main:app --reload
# коды входа печатаются в консоль, пока не задан SMTP
pytest -q
```

## Выкладка на сервер reg.ru

1. VPS на Ubuntu 22.04/24.04: 2 ядра, 4 ГБ памяти. В DNS домена A-записи `@` и `www` — на IP сервера.
2. **Deploy key** (репозиторий приватный): на сервере `ssh-keygen -t ed25519 -f ~/.ssh/nauka -N ""`,
   публичный ключ `~/.ssh/nauka.pub` добавить в GitHub → Settings → Deploy keys (только чтение).
   Затем `GIT_SSH_COMMAND="ssh -i ~/.ssh/nauka" git clone git@github.com:markkltch/nauka-pobezhdat.git /opt/nauka`.
3. `bash /opt/nauka/deploy/install.sh` — поставит Docker и создаст `.env`. Заполните `.env` и
   запустите скрипт ещё раз: поднимутся база, приложение и HTTPS, включится ночная копия базы.
4. Для GigaChat положите корневой сертификат НУЦ Минцифры `russian_trusted_root_ca.cer` в
   `/opt/nauka/deploy/certs/` (скачивается на Госуслугах, раздел «Сертификаты Минцифры»).
5. Обновление: `/opt/nauka/deploy/update.sh`.

Восстановление базы из копии:
`docker compose exec -T db pg_restore -U nauka -d nauka --clean < backups/nauka-ГГГГ-ММ-ДД.dump`

## Оплата

Пока `YOOKASSA_SHOP_ID` пуст или `FREE_BETA=true`, доступ бесплатный. После одобрения магазина:
впишите ключи, `FREE_BETA=false`, в кабинете ЮKassa укажите адрес уведомлений
`https://ДОМЕН/billing/webhook` (события `payment.succeeded`, `payment.canceled`). Сервер не верит
телу уведомления и перечитывает платёж из ЮKassa.

## Владельцу

`/admin` (адреса из `ADMIN_EMAILS`): пользователи, планы за месяц, расход на ИИ всего и по людям.

## Почта на хостинге reg.ru

Письма уходят через sendmail хостинга (MAIL_VIA_SENDMAIL=true) с адреса noreply@nauka.ogiinst.ru.
Для поддомена в ispmanager создан почтовый домен nauka.ogiinst.ru с DKIM (селектор dkim) и DMARC;
панель сама добавила записи SPF, DKIM и DMARC в DNS. Без этого Яндекс кладёт письма в «Спам».
