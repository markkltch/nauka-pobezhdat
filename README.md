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
| Оплата и доступ | `app/billing.py`, `app/tbank.py` |
| Напоминания и продления | `app/jobs.py` (раз в 10 минут, внутри приложения) |
| Экраны | `app/templates/`, `app/static/app.css`; лендинг — `app/templates/landing.html` |
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

### Т‑Банк

1. На сервере укажите `PAYMENT_PROVIDER=tbank`, `PERSONAL_DATA_OPERATOR=ip`, `BASE_URL=https://nauka.ogiinst.ru` и `FREE_BETA=true`.
   Оферта, политика, согласие и страница оплаты станут доступны для проверки. В существующих аккаунтах может понадобиться согласие новому оператору.
2. В Т‑Бизнесе создайте интернет-магазин для этого домена. Для платёжной формы банка включите карты, СБП и T-Pay, настройте одностадийную оплату и
   уведомления HTTP(S) на `https://nauka.ogiinst.ru/billing/tbank/webhook`.
3. Подключите кассу ИП к терминалу, выберите её формат `TBANK_FFD`, систему налогообложения `TBANK_TAXATION` и ставку `TBANK_VAT`.
   Проверьте электронный чек, в том числе адрес сайта и признак расчёта в интернете. Код передаёт банку email плательщика, наименование услуги и суммы в копейках.
4. Проверьте тестовый терминал в отдельном окружении. После проверки магазина запишите ключ и пароль рабочего терминала в серверный `.env`
   как `TBANK_TERMINAL_KEY` и `TBANK_PASSWORD`. Для начала продаж установите `FREE_BETA=false` и перезапустите приложение.

Новые оплаты Т‑Банка разовые. Доступ и пакеты активируются только после подтверждённого платежа. При возврате
оформите операцию и чек возврата в кабинете банка и кассы, затем скорректируйте остаток доступа или пакета.

Платежи ЮKassa продолжают проверяться по старому адресу `/billing/webhook`, пока заданы её ключи. При переключении
на Т‑Банк автоматические списания через ЮKassa прекращаются, ранее оплаченный доступ действует до конца периода.

Документация [Т‑Банка по подключению магазина](https://www.tbank.ru/business/help/business-payments/internet-acquiring/how-involve/connect/),
[API интернет-эквайринга](https://developer.tbank.ru/eacq/api) и [фискализации](https://developer.tbank.ru/eacq/scenarios/fiscalization).

## Владельцу

`/admin` (адреса из `ADMIN_EMAILS`): пользователи, планы за месяц, расход на ИИ всего и по людям.

## Почта на хостинге reg.ru

Письма уходят через sendmail хостинга (MAIL_VIA_SENDMAIL=true) с адреса noreply@nauka.ogiinst.ru.
Для поддомена в ispmanager создан почтовый домен nauka.ogiinst.ru с DKIM (селектор dkim) и DMARC;
панель сама добавила записи SPF, DKIM и DMARC в DNS. Без этого Яндекс кладёт письма в «Спам».
