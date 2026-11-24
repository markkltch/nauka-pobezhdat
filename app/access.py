"""Tariffs: who gets how many разборов.

Free: a few разборов a month, plus the shtab, tasks and reminders (those cost us nothing).
Paid (1 500 ₽ a person): the full monthly allowance; extra packs on top.
A shtab's subscription covers as many members as it paid seats for, in the order they joined;
the rest of the members stay on the free plan until the next payment counts them.

Every разбор has a hard model budget (mentor.py checks it before each call), so the worst
case per person is allowance × budget — see docs/economics.md.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Member, Situation, Subscription, User, now

FREE, PRO = "free", "pro"
NAMES = {FREE: "Бесплатный", PRO: "Полный"}


def _active(sub: Subscription | None) -> bool:
    return bool(sub and sub.status == "active" and sub.paid_until and sub.paid_until > now())


def _covered_by_shtab(db: Session, user: User) -> bool:
    for member in db.scalars(select(Member).where(Member.user_id == user.id)).all():
        sub = db.scalars(select(Subscription).where(Subscription.shtab_id == member.shtab_id)
                         .order_by(Subscription.id.desc())).first()
        if not _active(sub):
            continue
        order = db.scalars(select(Member.user_id).where(Member.shtab_id == member.shtab_id)
                           .order_by(Member.joined_at, Member.id)).all()
        if user.id in order[: sub.seats]:
            return True
    return False


def tier(db: Session, user: User) -> str:
    if get_settings().free_beta:
        return PRO
    own = db.scalars(select(Subscription).where(Subscription.payer_id == user.id, Subscription.shtab_id.is_(None))
                     .order_by(Subscription.id.desc())).first()
    if _active(own) or _covered_by_shtab(db, user):
        return PRO
    return FREE


def allowance(db: Session, user: User) -> int:
    s = get_settings()
    return s.monthly_plan_limit if tier(db, user) == PRO else s.free_plans_per_month


def used_this_month(db: Session, user: User) -> int:
    start = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(select(func.count(Situation.id)).where(Situation.user_id == user.id,
                                                            Situation.created_at >= start)) or 0


def can_start(db: Session, user: User) -> bool:
    return used_this_month(db, user) < allowance(db, user) or (user.extra_plans or 0) > 0


def consume(db: Session, user: User) -> None:
    """Call right before a new разбор is created: past the monthly allowance it spends a bought one."""
    if used_this_month(db, user) >= allowance(db, user):
        user.extra_plans = max(0, (user.extra_plans or 0) - 1)


def summary(db: Session, user: User) -> dict:
    t = tier(db, user)
    return {"tier": t, "tier_name": NAMES[t], "used": used_this_month(db, user),
            "allowance": allowance(db, user), "extra": user.extra_plans or 0}
