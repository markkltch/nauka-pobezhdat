"""Background jobs: deadline reminders, the morning report (утреннее донесение), subscription renewals.

Letters go out only between 8:00 and 21:00 Moscow time."""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from . import billing, mailer
from .config import get_settings
from .db import SessionLocal
from .models import Plan, Situation, Step, User, now

MSK = ZoneInfo("Europe/Moscow")

log = logging.getLogger("jobs")
INTERVAL_S = 600


def msk_now() -> dt.datetime:
    return dt.datetime.now(MSK)


def daytime() -> bool:
    return 8 <= msk_now().hour < 21


def send_reminders(db) -> int:
    """One letter per step due tomorrow or overdue, at most once a day per step."""
    if not daytime():
        return 0
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    steps = db.scalars(select(Step).where(
        Step.done.is_(False), Step.due.is_not(None), Step.due <= tomorrow,
        or_(Step.reminded_at.is_(None), Step.reminded_at < now() - dt.timedelta(hours=20)),
    )).all()
    base = get_settings().base_url
    sent = 0
    for step in steps:
        to_user = step.assignee or (db.get(User, step.created_by) if step.created_by else None)
        if not to_user or not to_user.remind_steps:
            continue
        where = f"{base}/shtab/{step.shtab_id}" if step.shtab_id else f"{base}/app"
        if step.plan_id:
            plan = db.get(Plan, step.plan_id)
            sit = db.get(Situation, plan.situation_id) if plan else None
            if sit and not step.shtab_id:
                where = f"{base}/app/{sit.id}"
        when = "сегодня" if step.due <= dt.date.today() else "завтра"
        if step.due < dt.date.today():
            when = f"был {step.due:%d.%m} — срок прошёл"
        try:
            mailer.send(to_user.email, f"Срок {when}: {step.text[:60]}",
                        f"Напоминание от наставника.\n\nЗадача: {step.text}\nСрок: {when}\n\nОткрыть: {where}")
            step.reminded_at = now()
            sent += 1
        except Exception:  # noqa: BLE001 — one bad address must not stop the rest
            log.exception("reminder failed for step %s", step.id)
    db.commit()
    return sent


def _situation_link(db, step: Step, base: str) -> str:
    if step.plan_id and (plan := db.get(Plan, step.plan_id)):
        return f"{base}/app/{plan.situation_id}"
    return f"{base}/shtab/{step.shtab_id}" if step.shtab_id else f"{base}/app"


def send_morning_reports(db) -> int:
    """Утреннее донесение: after 8:00 MSK, once a day, only when there is something to report."""
    from .mentor import review_due

    today = msk_now().date()
    if msk_now().hour < 8 or msk_now().hour >= 21:
        return 0
    base = get_settings().base_url
    sent = 0
    users = db.scalars(select(User).where(User.morning_report.is_(True),
                                          or_(User.report_sent_on.is_(None), User.report_sent_on < today))).all()
    for user in users:
        mine = or_(Step.assignee_id == user.id, (Step.created_by == user.id) & Step.assignee_id.is_(None))
        open_steps = db.scalars(select(Step).where(mine, Step.done.is_(False), Step.due.is_not(None),
                                                   Step.due <= today + dt.timedelta(days=1)).order_by(Step.due)).all()
        overdue = [s for s in open_steps if s.due < today]
        due_today = [s for s in open_steps if s.due == today]
        tomorrow = [s for s in open_steps if s.due > today]
        to_review = [s for s in db.scalars(select(Situation).where(Situation.user_id == user.id, Situation.stage == "follow",
                                                                   Situation.review != "done")).all() if review_due(s)]
        user.report_sent_on = today
        if not (overdue or due_today or to_review):
            continue
        lines = [f"Доброе утро, {user.name.split()[0] if user.name else 'командир'}. Донесение на {today:%d.%m}.", ""]
        for title, items in (("Срок прошёл", overdue), ("На сегодня", due_today), ("На завтра", tomorrow)):
            if items:
                lines.append(f"{title}:")
                lines += [f"— {s.text[:120]} ({s.owner_name or 'вы'}) {_situation_link(db, s, base)}" for s in items[:8]]
                lines.append("")
        if to_review:
            lines.append("Пора подвести итог (разбор после дела):")
            lines += [f"— {s.title} {base}/app/{s.id}#aar" for s in to_review[:5]]
            lines.append("")
        learn_line = _learn_line(db, user, base)
        if learn_line:
            lines += [learn_line, ""]
        lines.append(f"Отключить донесение: {base}/settings#notify")
        subject = f"Донесение: {len(overdue)} просрочено, {len(due_today)} на сегодня" if (overdue or due_today) \
            else "Донесение: пора подвести итог"
        try:
            mailer.send(user.email, subject, "\n".join(lines))
            sent += 1
        except Exception:  # noqa: BLE001
            log.exception("morning report failed for user %s", user.id)
    db.commit()
    return sent


def _learn_line(db, user: User, base: str) -> str:
    """«Урок дня» in the morning report, for people who keep it on in «Учение»."""
    from . import learn

    st = learn.state(db, user, create=False)
    if not st or not st.in_report:
        return ""
    ov_done = learn.done_ids(db, user)
    tests = learn.marks(db, user, "test")
    rec = learn.recommend(db, user, ov_done, tests, learn.signals(db, user), learn.orders(db, user))
    if rec["kind"] == "lesson":
        return f"Учение: урок дня «{rec['lesson']['title']}», {rec['lesson'].get('minutes') or 5} минут — {base}/learn/lesson/{rec['lesson']['id']}"
    if rec["kind"] == "review":
        return f"Учение: повторение на 2 минуты — {base}/learn/review"
    if rec["kind"] == "report":
        return f"Учение: срок приказа «{rec['order'].title}» — отметьте, как прошло: {base}/learn/order/{rec['order'].id}"
    return ""


def _loop() -> None:
    while True:
        try:
            with SessionLocal() as db:
                send_reminders(db)
                send_morning_reports(db)
                billing.renew_due(db)
        except Exception:  # noqa: BLE001
            log.exception("job round failed")
        time.sleep(INTERVAL_S)


def start() -> None:
    threading.Thread(target=_loop, name="jobs", daemon=True).start()
