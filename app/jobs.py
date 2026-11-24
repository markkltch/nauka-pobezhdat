"""Background jobs in the app process: deadline reminders and subscription renewals."""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from sqlalchemy import or_, select

from . import billing, mailer
from .config import get_settings
from .db import SessionLocal
from .models import Plan, Situation, Step, User, now

log = logging.getLogger("jobs")
INTERVAL_S = 600


def send_reminders(db) -> int:
    """One letter per step due tomorrow or overdue, at most once a day per step."""
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    steps = db.scalars(select(Step).where(
        Step.done.is_(False), Step.due.is_not(None), Step.due <= tomorrow,
        or_(Step.reminded_at.is_(None), Step.reminded_at < now() - dt.timedelta(hours=20)),
    )).all()
    base = get_settings().base_url
    sent = 0
    for step in steps:
        to_user = step.assignee or (db.get(User, step.created_by) if step.created_by else None)
        if not to_user:
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


def _loop() -> None:
    while True:
        try:
            with SessionLocal() as db:
                send_reminders(db)
                billing.renew_due(db)
        except Exception:  # noqa: BLE001
            log.exception("job round failed")
        time.sleep(INTERVAL_S)


def start() -> None:
    threading.Thread(target=_loop, name="jobs", daemon=True).start()
