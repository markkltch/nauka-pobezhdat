"""Payments: 1 500 ₽ a month per person, a shtab pays for its members, packs of extra разборов.

YooKassa: the first payment saves the card (or SBP) for monthly auto-payments. A webhook only
says "something happened"; the server always re-reads the payment from YooKassa before trusting it.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import tbank
from .config import get_settings
from .models import PackOrder, Payment, Shtab, Subscription, User, now

log = logging.getLogger("billing")
API = "https://api.yookassa.ru/v3"
PERIOD = dt.timedelta(days=30)


def _active(sub: Subscription | None) -> bool:
    return bool(sub and sub.status == "active" and sub.paid_until and sub.paid_until > now())


def seats_for(shtab: Shtab) -> int:
    return max(1, len(shtab.members))


def _auth():
    s = get_settings()
    return (s.yookassa_shop_id, s.yookassa_secret_key)


def start_payment(db: Session, payer: User, shtab: Shtab | None) -> str:
    """Create a paid period and return the payment form URL."""
    s = get_settings()
    seats = seats_for(shtab) if shtab else 1
    amount = seats * s.price_per_seat_rub
    sub = Subscription(payer_id=payer.id, shtab_id=shtab.id if shtab else None, seats=seats)
    db.add(sub)
    db.flush()
    if s.payment_provider == "tbank":
        description = f"Наука побеждать: доступ для штаба на 30 дней, мест: {seats}" if shtab else (
            "Наука побеждать: личный доступ на 30 дней"
        )
        key, url = tbank.init(amount * 100, description, payer.email, f"/billing/return/{sub.id}")
        db.add(Payment(subscription_id=sub.id, provider_id=key, amount_rub=amount))
        db.commit()
        return url
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


def _receipt(email: str, text: str, amount: float) -> dict:
    return {"customer": {"email": email}, "items": [{
        "description": text[:128], "quantity": "1.00",
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"}, "vat_code": get_settings().yookassa_vat_code,
        "payment_subject": "service", "payment_mode": "full_payment"}]}


def start_pack(db: Session, user: User) -> str:
    """One-off payment for extra разборов; no card is saved."""
    s = get_settings()
    order = PackOrder(user_id=user.id, plans=s.pack_size, amount_rub=s.pack_price_rub)
    db.add(order)
    db.flush()
    what = f"Наука побеждать: {s.pack_size} дополнительных разборов"
    if s.payment_provider == "tbank":
        order.provider_id, url = tbank.init(s.pack_price_rub * 100, what, user.email,
                                          f"/billing/pack/{order.id}")
        db.commit()
        return url
    body = {
        "amount": {"value": f"{order.amount_rub:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": f"{s.base_url}/billing/pack/{order.id}"},
        "description": what,
        "metadata": {"pack_order_id": str(order.id)},
        "receipt": _receipt(user.email, what, order.amount_rub),
    }
    r = httpx.post(f"{API}/payments", json=body, auth=_auth(), headers={"Idempotence-Key": str(uuid.uuid4())}, timeout=30)
    r.raise_for_status()
    p = r.json()
    order.provider_id = p["id"]
    order.status = p["status"]
    db.commit()
    return p["confirmation"]["confirmation_url"]


def _sync_pack(db: Session, order: PackOrder) -> None:
    r = httpx.get(f"{API}/payments/{order.provider_id}", auth=_auth(), timeout=30)
    r.raise_for_status()
    order.status = r.json()["status"]
    if order.status == "succeeded" and not order.credited:
        user = db.get(User, order.user_id)
        user.extra_plans = (user.extra_plans or 0) + order.plans
        order.credited = True
    db.commit()


def _sync_tbank(db: Session, order: Payment | PackOrder) -> str:
    if order.status != "pending":
        return order.status
    status = tbank.state(order.provider_id, round(order.amount_rub * 100))
    if status == "pending":
        return status
    table = type(order)
    claimed = db.execute(
        update(table).where(table.id == order.id, table.status == "pending").values(status=status)
    )
    if not claimed.rowcount:
        db.refresh(order)
        return order.status
    if status == "succeeded":
        if isinstance(order, PackOrder):
            db.execute(update(User).where(User.id == order.user_id)
                       .values(extra_plans=func.coalesce(User.extra_plans, 0) + order.plans))
            order.credited = True
        else:
            sub = db.get(Subscription, order.subscription_id)
            db.scalar(select(User.id).where(User.id == sub.payer_id).with_for_update())
            previous_end = db.scalar(select(func.max(Subscription.paid_until)).where(
                Subscription.payer_id == sub.payer_id, Subscription.shtab_id == sub.shtab_id,
                Subscription.status == "active",
            ))
            start = now()
            sub.paid_until = max(start, previous_end or start) + PERIOD
            sub.status = "active"
    db.commit()
    return status


def sync_payment(db: Session, provider_id: str) -> str | None:
    """Check the payment with its original provider and apply the result."""
    pay = db.scalars(select(Payment).where(Payment.provider_id == provider_id)).first()
    order = pay or db.scalars(select(PackOrder).where(PackOrder.provider_id == provider_id)).first()
    if not order:
        return None
    if provider_id.startswith(tbank.PREFIX):
        return _sync_tbank(db, order)
    if not pay:
        if get_settings().yookassa_enabled:
            _sync_pack(db, order)
        return order.status
    if not get_settings().yookassa_enabled:
        return pay.status
    r = httpx.get(f"{API}/payments/{provider_id}", auth=_auth(), timeout=30)
    r.raise_for_status()
    p = r.json()
    if pay.status == p["status"]:
        return pay.status
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
    return pay.status


def renew_due(db: Session) -> None:
    """Charge saved methods for subscriptions that end within a day (run by the scheduler)."""
    s = get_settings()
    if s.payment_provider != "yookassa" or not s.yookassa_enabled:
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
