"""Access and payments: 1 500 ₽ a month per person; a shtab pays for its members.

YooKassa: the first payment saves the card (or SBP) for monthly auto-payments. A webhook only
says "something happened"; the server always re-reads the payment from YooKassa before trusting it.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Member, Payment, Shtab, Subscription, User, now

log = logging.getLogger("billing")
API = "https://api.yookassa.ru/v3"
PERIOD = dt.timedelta(days=30)


def _active(sub: Subscription | None) -> bool:
    return bool(sub and sub.status == "active" and sub.paid_until and sub.paid_until > now())


def has_access(db: Session, user: User) -> bool:
    if get_settings().free_beta:
        return True
    own = db.scalars(select(Subscription).where(Subscription.payer_id == user.id, Subscription.shtab_id.is_(None))
                     .order_by(Subscription.id.desc())).first()
    if _active(own):
        return True
    shtab_ids = db.scalars(select(Member.shtab_id).where(Member.user_id == user.id)).all()
    for sid in shtab_ids:
        sub = db.scalars(select(Subscription).where(Subscription.shtab_id == sid).order_by(Subscription.id.desc())).first()
        if _active(sub):
            return True
    return False


def seats_for(shtab: Shtab) -> int:
    return max(1, len(shtab.members))


def _auth():
    s = get_settings()
    return (s.yookassa_shop_id, s.yookassa_secret_key)


def start_payment(db: Session, payer: User, shtab: Shtab | None) -> str:
    """Create a subscription row and a YooKassa payment; return the URL to send the person to."""
    s = get_settings()
    seats = seats_for(shtab) if shtab else 1
    amount = seats * s.price_per_seat_rub
    sub = Subscription(payer_id=payer.id, shtab_id=shtab.id if shtab else None, seats=seats)
    db.add(sub)
    db.flush()
    what = f"штаб «{shtab.name}», {seats} чел." if shtab else "личный доступ"
    body = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "save_payment_method": True,
        "confirmation": {"type": "redirect", "return_url": f"{s.base_url}/billing/return/{sub.id}"},
        "description": f"Наука побеждать: {what}, 1 месяц",
        "metadata": {"subscription_id": str(sub.id)},
        "receipt": {
            "customer": {"email": payer.email},
            "items": [{
                "description": f"Доступ к сервису «Наука побеждать», {what}, 1 месяц"[:128],
                "quantity": "1.00",
                "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                "vat_code": s.yookassa_vat_code,
                "payment_subject": "service",
                "payment_mode": "full_payment",
            }],
        },
    }
    r = httpx.post(f"{API}/payments", json=body, auth=_auth(), headers={"Idempotence-Key": str(uuid.uuid4())}, timeout=30)
    r.raise_for_status()
    p = r.json()
    db.add(Payment(subscription_id=sub.id, provider_id=p["id"], amount_rub=amount, status=p["status"]))
    db.commit()
    return p["confirmation"]["confirmation_url"]


def sync_payment(db: Session, provider_id: str) -> None:
    """Re-read one payment from YooKassa and apply it."""
    pay = db.scalars(select(Payment).where(Payment.provider_id == provider_id)).first()
    if not pay:
        return
    r = httpx.get(f"{API}/payments/{provider_id}", auth=_auth(), timeout=30)
    r.raise_for_status()
    p = r.json()
    if pay.status == p["status"]:
        return
    pay.status = p["status"]
    sub = db.get(Subscription, pay.subscription_id)
    if p["status"] == "succeeded" and sub:
        start = sub.paid_until if _active(sub) else now()
        sub.paid_until = start + PERIOD
        sub.status = "active"
        method = p.get("payment_method") or {}
        if method.get("saved"):
            sub.payment_method_id = method.get("id", "")
    db.commit()


def renew_due(db: Session) -> None:
    """Charge saved methods for subscriptions that end within a day (run by the scheduler)."""
    s = get_settings()
    if not s.billing_enabled:
        return
    soon = now() + dt.timedelta(days=1)
    subs = db.scalars(select(Subscription).where(Subscription.status == "active", Subscription.paid_until < soon,
                                                 Subscription.payment_method_id != "")).all()
    for sub in subs:
        recent = db.scalars(select(Payment).where(Payment.subscription_id == sub.id,
                                                  Payment.created_at > now() - dt.timedelta(days=2))).first()
        if recent:
            continue
        seats = sub.seats
        if sub.shtab_id:
            shtab = db.get(Shtab, sub.shtab_id)
            seats = seats_for(shtab) if shtab else seats
            sub.seats = seats
        amount = seats * s.price_per_seat_rub
        payer = db.get(User, sub.payer_id)
        body = {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"}, "capture": True,
            "payment_method_id": sub.payment_method_id,
            "description": "Наука побеждать: продление на 1 месяц",
            "metadata": {"subscription_id": str(sub.id)},
            "receipt": {"customer": {"email": payer.email}, "items": [{
                "description": "Доступ к сервису «Наука побеждать», 1 месяц", "quantity": "1.00",
                "amount": {"value": f"{amount:.2f}", "currency": "RUB"}, "vat_code": s.yookassa_vat_code,
                "payment_subject": "service", "payment_mode": "full_payment"}]},
        }
        try:
            r = httpx.post(f"{API}/payments", json=body, auth=_auth(),
                           headers={"Idempotence-Key": f"renew-{sub.id}-{sub.paid_until:%Y%m%d}"}, timeout=30)
            r.raise_for_status()
            p = r.json()
            db.add(Payment(subscription_id=sub.id, provider_id=p["id"], amount_rub=amount, status=p["status"]))
            db.commit()
            if p["status"] == "succeeded":
                sync_payment(db, p["id"])
        except httpx.HTTPError:
            log.exception("renewal failed for subscription %s", sub.id)
