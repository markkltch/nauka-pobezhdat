"""The letters the service sends: each one is a plain-text part and an HTML part in the brand's look.

The HTML is built for mail clients, not browsers: tables for layout, inline styles, no images and no web
fonts (they are blocked by default in most mailboxes, and a letter of pictures looks like spam), a
hidden preheader for the inbox preview, and a light colour scheme. Every letter has the same frame:
the brand line, a white card, a footer that says why the letter came and, for letters a person can turn
off, a one-click way to do it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from dataclasses import dataclass, field
from html import escape

from .config import get_settings
from . import legal

INK, MUTED, CARMINE, SAND, CARD, LINE, GOLD = "#12201a", "#5b5a50", "#a8142f", "#f4eee2", "#fffdf8", "#e6dcc8", "#e9cf97"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"
MONO = "'SFMono-Regular',Menlo,Consolas,'Liberation Mono',monospace"
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")


@dataclass
class Letter:
    subject: str
    text: str
    html: str
    unsubscribe: str | None = None  # one-click link for letters a person can turn off (List-Unsubscribe)
    headers: dict = field(default_factory=dict)


def ru_date(d: dt.date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}"


# ---------- one-click unsubscribe ----------

KINDS = {"steps": "напоминания о сроках", "morning": "утреннее донесение"}


def unsub_token(user_id: int, kind: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, f"unsub:{user_id}:{kind}".encode(), hashlib.sha256).hexdigest()[:32]


def unsub_url(user_id: int, kind: str) -> str:
    return f"{get_settings().base_url.rstrip('/')}/unsubscribe/{user_id}/{kind}/{unsub_token(user_id, kind)}"


# ---------- building blocks (HTML) ----------

def _p(text: str, *, size: int = 16, color: str = "#2b352f", top: int = 0, bottom: int = 16) -> str:
    return (f'<p style="margin:{top}px 0 {bottom}px;font:400 {size}px/1.55 {FONT};color:{color};">{text}</p>')


def _h1(text: str) -> str:
    return (f'<h1 style="margin:0 0 12px;font:400 28px/1.2 {FONT};letter-spacing:-0.4px;color:{INK};">{escape(text)}</h1>')


def _kicker(text: str) -> str:
    return (f'<p style="margin:0 0 10px;font:600 12px/1 {FONT};letter-spacing:1.6px;text-transform:uppercase;color:{CARMINE};">'
            f'{escape(text)}</p>')


def _button(label: str, url: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 20px;">'
        f'<tr><td bgcolor="{CARMINE}" style="border-radius:12px;">'
        f'<a href="{escape(url)}" target="_blank" style="display:inline-block;padding:15px 28px;font:600 16px/1 {FONT};'
        f'color:{CARD};text-decoration:none;border-radius:12px;">{escape(label)}&nbsp;&rarr;</a></td></tr></table>'
    )


def _code(code: str) -> str:
    spaced = "&#8202;".join(escape(c) for c in code)
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0 22px;">'
        f'<tr><td align="center" bgcolor="{SAND}" style="border-radius:16px;padding:22px 12px;border:1px solid {LINE};">'
        f'<div style="font:600 38px/1 {MONO};letter-spacing:10px;color:{INK};">{spaced}</div>'
        f'<div style="margin-top:10px;font:400 13px/1.4 {FONT};color:{MUTED};">Код действует 15 минут</div>'
        '</td></tr></table>'
    )


def _rows(rows: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<tr><td style="padding:12px 0;border-top:1px solid {LINE};font:400 15px/1.4 {FONT};color:{MUTED};">{escape(k)}</td>'
        f'<td align="right" style="padding:12px 0 12px 16px;border-top:1px solid {LINE};font:600 15px/1.4 {FONT};color:{INK};">{escape(v)}</td></tr>'
        for k, v in rows)
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0 22px;">{cells}</table>'


def _items(title: str, items: list[tuple[str, str, str]], *, accent: str = INK) -> str:
    """A titled list of tasks: (text, note, url)."""
    out = [f'<p style="margin:22px 0 8px;font:600 13px/1 {FONT};letter-spacing:1.2px;text-transform:uppercase;color:{accent};">{escape(title)}</p>']
    for text, note, url in items:
        out.append(
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 8px;">'
            f'<tr><td style="padding:14px 16px;border:1px solid {LINE};border-radius:12px;background:{CARD};">'
            f'<a href="{escape(url)}" target="_blank" style="font:500 15px/1.4 {FONT};color:{INK};text-decoration:none;">{escape(text)}</a>'
            + (f'<div style="margin-top:4px;font:400 13px/1.4 {FONT};color:{MUTED};">{escape(note)}</div>' if note else "")
            + '</td></tr></table>')
    return "".join(out)


def _steps(items: list[tuple[str, str]]) -> str:
    rows = "".join(
        f'<tr><td valign="top" style="padding:0 14px 16px 0;width:28px;">'
        f'<div style="width:28px;height:28px;border-radius:14px;background:{CARMINE};color:{CARD};font:600 14px/28px {FONT};text-align:center;">{i}</div></td>'
        f'<td valign="top" style="padding:3px 0 16px;font:400 15px/1.5 {FONT};color:#2b352f;"><b style="color:{INK};font-weight:600;">{escape(t)}.</b> {escape(d)}</td></tr>'
        for i, (t, d) in enumerate(items, 1))
    return f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:6px 0 10px;">{rows}</table>'


def _merchant() -> dict:
    return legal.current()["MERCHANT"]


def _frame(subject: str, preheader: str, body: str, why: str, unsubscribe: tuple[str, str] | None = None) -> str:
    s = get_settings()
    m = _merchant()
    base = s.base_url.rstrip("/")
    unsub = (f'<br><a href="{escape(unsubscribe[1])}" target="_blank" style="color:{MUTED};text-decoration:underline;">{escape(unsubscribe[0])}</a>'
             if unsubscribe else "")
    return f"""<!doctype html>
<html lang="ru" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<meta name="format-detection" content="telephone=no, date=no, address=no, email=no">
<title>{escape(subject)}</title>
</head>
<body style="margin:0;padding:0;background:{SAND};-webkit-text-size-adjust:100%;">
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;line-height:1px;color:{SAND};">{escape(preheader)}&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{SAND}">
<tr><td align="center" style="padding:28px 12px 36px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;">
<tr><td style="padding:0 6px 18px;">
<a href="{base}/" target="_blank" style="text-decoration:none;">
<span style="display:inline-block;width:12px;height:15px;border:5px solid {CARMINE};border-bottom:0;border-radius:11px 11px 0 0;vertical-align:-3px;"></span>
<span style="font:600 17px/1 {FONT};color:{INK};letter-spacing:-0.2px;">&nbsp;Наука побеждать</span>
<span style="font:600 11px/1 {FONT};color:{CARMINE};letter-spacing:1.6px;">&nbsp;ШТАБ</span>
</a>
</td></tr>
<tr><td bgcolor="{CARD}" style="background:{CARD};border:1px solid {LINE};border-radius:20px;padding:34px 30px 18px;">
{body}
</td></tr>
<tr><td style="padding:20px 8px 0;font:400 12px/1.6 {FONT};color:{MUTED};">
{escape(why)}{unsub}<br><br>
{escape(m['short'])}, ИНН {escape(m['inn'])} · <a href="mailto:{escape(m['email'])}" style="color:{MUTED};">{escape(m['email'])}</a> · <a href="{base}/" target="_blank" style="color:{MUTED};">{escape(base.split('//')[-1])}</a>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def _footer_text(why: str, unsubscribe: tuple[str, str] | None = None) -> str:
    s = get_settings()
    lines = ["", "—", why]
    if unsubscribe:
        lines.append(f"{unsubscribe[0]}: {unsubscribe[1]}")
    m = _merchant()
    lines.append(f"{m['short']} · {m['email']} · {s.base_url.rstrip('/')}")
    return "\n".join(lines)


# ---------- the letters ----------

def login_code(code: str, link: str, register: bool) -> Letter:
    if register:
        subject, title, lead, btn = f"Подтвердите почту: {code}", "Подтвердите почту", \
            "Введите этот код на странице регистрации, чтобы закончить её:", "Подтвердить почту"
        why = "Вы получили это письмо, потому что на ваш адрес зарегистрировали аккаунт в «Наука побеждать · Штаб». Если это были не вы, просто удалите письмо."
    else:
        subject, title, lead, btn = f"Код входа: {code}", "Код для входа", \
            "Введите этот код на странице входа:", "Войти в Штаб"
        why = "Вы получили это письмо, потому что кто-то запросил вход в «Наука побеждать · Штаб» с вашим адресом. Если это были не вы, просто удалите письмо — без кода войти нельзя."
    body = (_h1(title) + _p(escape(lead)) + _code(code)
            + _p("Или нажмите кнопку — код вводить не придётся:", bottom=4) + _button(btn, link)
            + _p(f'Кнопка не открывается? Скопируйте ссылку в браузер:<br><a href="{escape(link)}" style="color:{MUTED};word-break:break-all;">{escape(link)}</a>',
                 size=13, color=MUTED))
    text = (f"{title}\n\n{lead}\n\n    {code}\n\nКод действует 15 минут.\n\n"
            f"Или откройте ссылку, код вводить не придётся:\n{link}\n" + _footer_text(why))
    return Letter(subject, text, _frame(subject, f"{code} — код действует 15 минут", body, why))


def welcome(name: str) -> Letter:
    base = get_settings().base_url.rstrip("/")
    first = (name or "").split()[0] if name else ""
    hello = f"{first}, добро пожаловать в Штаб" if first else "Добро пожаловать в Штаб"
    subject = "Добро пожаловать в «Наука побеждать · Штаб»"
    steps = [("Опишите ситуацию", "Текстом или голосом, как рассказали бы опытному коллеге."),
             ("Ответьте на 2–3 вопроса", "Наставник уточнит главное — глазомер прежде действия."),
             ("Получите план", "Шаги с исполнителями и сроками; наставник напомнит, когда срок подойдёт.")]
    body = (_kicker("Наставник по методу Суворова") + _h1(hello)
            + _p("Здесь разбирают рабочие ситуации руководителя и превращают их в план: что сделать, кому и к какому сроку.")
            + _steps(steps) + _button("Разобрать первую ситуацию", f"{base}/app")
            + _p(f'А пять минут в день — <a href="{base}/learn" style="color:{CARMINE};">«Учение»</a>: короткие уроки по «Науке побеждать» '
                 "с приказом на неделю в вашем деле.", size=15))
    why = "Вы получили это письмо, потому что только что создали аккаунт в «Наука побеждать · Штаб»."
    text = (f"{hello}!\n\nЗдесь разбирают рабочие ситуации руководителя и превращают их в план: что сделать, кому и к какому сроку.\n\n"
            + "\n".join(f"{i}. {t}. {d}" for i, (t, d) in enumerate(steps, 1))
            + f"\n\nРазобрать первую ситуацию: {base}/app\n«Учение» — пять минут в день: {base}/learn\n" + _footer_text(why))
    return Letter(subject, text, _frame(subject, "Опишите ситуацию — наставник задаст 2–3 вопроса и даст план.", body, why))


def reminder(user_id: int, task: str, when: str, url: str, overdue: bool) -> Letter:
    subject = f"Срок {when}: {task[:60]}"
    kick = "Срок прошёл" if overdue else f"Срок {when}"
    unsub = ("Не присылать напоминания о сроках", unsub_url(user_id, "steps"))
    body = (_kicker(kick) + _h1("Напоминание от наставника")
            + _items("Задача", [(task, f"Срок: {when}", url)], accent=CARMINE if overdue else INK)
            + _p("Сделано — отметьте шаг в плане. Не успеваете — перенесите срок или передайте задачу, пока не поздно.", size=15, top=12)
            + _button("Открыть задачу", url))
    why = "Вы получили это письмо, потому что у вас есть задача со сроком в «Наука побеждать · Штаб»."
    text = (f"Напоминание от наставника.\n\nЗадача: {task}\nСрок: {when}\n\nОткрыть: {url}\n" + _footer_text(why, unsub))
    return Letter(subject, text, _frame(subject, f"{task[:80]} — срок {when}", body, why, unsub), unsubscribe=unsub[1])


def nudge(user_id: int, from_name: str, shtab_name: str, task: str, when: str, url: str, overdue: bool) -> Letter:
    """The head of a shtab asks about one task right now (a button on the shtab's board)."""
    subject = f"{from_name} напоминает: {task[:60]}"
    kick = f"Штаб «{shtab_name}»"
    unsub = ("Не присылать напоминания о задачах", unsub_url(user_id, "steps"))
    body = (_kicker(kick) + _h1(f"{from_name} напоминает о задаче")
            + _items("Задача", [(task, f"Срок: {when}" if when else "Без срока", url)], accent=CARMINE if overdue else INK)
            + _p("Сделано — отметьте задачу в штабе, глава увидит сразу. Не успеваете — скажите об этом сейчас, а не в день срока.",
                 size=15, top=12)
            + _button("Открыть штаб", url))
    why = f"Вы участник штаба «{shtab_name}» в «Наука побеждать · Штаб», и задача назначена вам."
    text = (f"{from_name} напоминает о задаче.\n\nЗадача: {task}\nСрок: {when or 'без срока'}\n\nОткрыть: {url}\n" + _footer_text(why, unsub))
    return Letter(subject, text, _frame(subject, f"{task[:80]}", body, why, unsub), unsubscribe=unsub[1])


def morning(user_id: int, name: str, day: dt.date, sections: list[tuple[str, list[tuple[str, str, str]]]],
            review: list[tuple[str, str]], learn_line: tuple[str, str] | None, subject: str) -> Letter:
    base = get_settings().base_url.rstrip("/")
    first = (name or "").split()[0] if name else ""
    hello = f"Доброе утро, {first}" if first else "Доброе утро"
    unsub = ("Отключить утреннее донесение", unsub_url(user_id, "morning"))
    parts = [_kicker(f"Донесение на {ru_date(day)}"), _h1(hello)]
    lines = [f"{hello}. Донесение на {ru_date(day)}.", ""]
    for title, items in sections:
        if items:
            parts.append(_items(title, items, accent=CARMINE if title == "Срок прошёл" else INK))
            lines.append(f"{title}:")
            lines += [f"— {t} ({n}) {u}" for t, n, u in items]
            lines.append("")
    if review:
        parts.append(_items("Пора подвести итог", [(t, "Разбор после дела: что вышло и чему научились", u) for t, u in review]))
        lines.append("Пора подвести итог (разбор после дела):")
        lines += [f"— {t} {u}" for t, u in review]
        lines.append("")
    if learn_line:
        parts.append(_items("Учение", [(learn_line[0], "", learn_line[1])], accent=CARMINE))
        lines += [f"Учение: {learn_line[0]} — {learn_line[1]}", ""]
    parts.append('<div style="height:10px;"></div>' + _button("Открыть Штаб", f"{base}/app"))
    why = "Вы получили это письмо, потому что включили утреннее донесение в «Наука побеждать · Штаб»."
    return Letter(subject, "\n".join(lines) + _footer_text(why, unsub),
                  _frame(subject, subject, "".join(parts), why, unsub), unsubscribe=unsub[1])


def paid(what: str, amount_rub: float, until: dt.datetime | None) -> Letter:
    base = get_settings().base_url.rstrip("/")
    amount = f"{int(amount_rub):,}".replace(",", " ") + " ₽"
    rows = [("Что оплачено", what), ("Сумма", amount)]
    if until:
        rows.append(("Доступ до", until.strftime("%d.%m.%Y")))
    subject = "Оплата прошла"
    body = (_kicker("Спасибо") + _h1("Оплата прошла") + _p("Всё готово — полный доступ уже открыт.")
            + _rows(rows) + _button("Открыть Штаб", f"{base}/app")
            + _p(f'Кассовый чек придёт отдельным письмом. Условия — в <a href="{base}/payment" style="color:{MUTED};">правилах оплаты и возврата</a>.',
                 size=13, color=MUTED))
    why = "Вы получили это письмо, потому что оплатили доступ к «Наука побеждать · Штаб»."
    text = ("Оплата прошла. Полный доступ уже открыт.\n\n" + "\n".join(f"{k}: {v}" for k, v in rows)
            + f"\n\nОткрыть Штаб: {base}/app\nКассовый чек придёт отдельным письмом.\n" + _footer_text(why))
    return Letter(subject, text, _frame(subject, f"{what} — {amount}", body, why))


def renew_soon(what: str, until: dt.datetime) -> Letter:
    base = get_settings().base_url.rstrip("/")
    days = max(0, (until.date() - dt.date.today()).days)
    left = {0: "сегодня", 1: "завтра"}.get(days, f"через {days} дня" if days < 5 else f"через {days} дней")
    subject = f"Доступ заканчивается {left}"
    body = (_kicker("Полный доступ") + _h1(f"Доступ заканчивается {left}")
            + _p(f"{escape(what)}: оплачено до {until.strftime('%d.%m.%Y')}. Чтобы разборы, голосовые и «Учение» не прервались, "
                 "продлите доступ — это минута.")
            + _button("Продлить доступ", f"{base}/billing")
            + _p("Не продлите — аккаунт перейдёт на бесплатный тариф, планы и штаб сохранятся.", size=13, color=MUTED))
    why = "Вы получили это письмо, потому что у вас оплачен доступ к «Наука побеждать · Штаб» и он скоро закончится."
    text = (f"Доступ заканчивается {left}.\n\n{what}: оплачено до {until:%d.%m.%Y}.\n"
            f"Продлить: {base}/billing\n\nНе продлите — аккаунт перейдёт на бесплатный тариф, планы и штаб сохранятся.\n" + _footer_text(why))
    return Letter(subject, text, _frame(subject, f"Оплачено до {until:%d.%m.%Y}", body, why))
