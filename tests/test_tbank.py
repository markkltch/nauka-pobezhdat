import datetime as dt

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import access, auth, tbank
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import PackOrder, Payment, Subscription, User, now


@pytest.fixture
def terminal(monkeypatch):
    settings = get_settings()
    for key, value in {
        "base_url": "https://testserver", "payment_provider": "tbank", "free_beta": False,
        "personal_data_operator": "ip", "tbank_terminal_key": "exampleDEMO",
        "tbank_password": "example-password", "tbank_taxation": "usn_income", "tbank_vat": "none",
    }.items():
        monkeypatch.setattr(settings, key, value)
    return settings


def client_for(email, consent=True):
    client = TestClient(app, base_url="https://testserver")
    with SessionLocal() as db:
        user = User(email=email, level="line", consent_at=now(), ip_consent_at=now() if consent else None)
        db.add(user)
        db.flush()
        client.cookies.set(auth.COOKIE, auth.open_session(db, user).id)
        return client, user.id


def test_bank_notification_token_example():
    payload = {"TerminalKey": "1234567890DEMO", "OrderId": "000000", "Success": True, "Status": "AUTHORIZED",
               "PaymentId": "0000000", "ErrorCode": "0", "Amount": "1111", "CardId": "000000",
               "Pan": "200000******0000", "ExpDate": "1111", "RebillId": "000000",
               "Data": {"ignored": "value"}, "Token": "ignored"}
    assert tbank.token(payload, "11111111111") == "1c0964277d0213349243065a0d5b838b8e90d2d25f740d0f2767836e710e80c8"


def test_tbank_checkout_webhook_pack_and_return(monkeypatch, terminal):
    buyer, buyer_id = client_for("tbank-buyer@example.ru")
    outsider, _ = client_for("tbank-outsider@example.ru")
    payments = {}
    inits = []

    def fake_bank(url, json, **_kwargs):
        assert json["Token"] == tbank.token(json, terminal.tbank_password)
        assert json["TerminalKey"] == terminal.tbank_terminal_key
        if url.endswith("/Init"):
            payment_id = str(100 + len(payments))
            payload = {"Success": True, "OrderId": json["OrderId"], "PaymentId": payment_id,
                       "TerminalKey": json["TerminalKey"], "Amount": json["Amount"], "Status": "NEW"}
            payments[payment_id] = payload
            inits.append(json)
            payload = {**payload, "PaymentURL": "https://securepayments.tinkoff.ru/" + payment_id}
        else:
            assert url.endswith("/GetState")
            payload = payments[json["PaymentId"]]
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(tbank.httpx, "post", fake_bank)

    def notify(payment_id, status):
        payload = payments[payment_id]
        payload["Status"] = status
        return buyer.post("/billing/tbank/webhook", json={**payload, "Token": tbank.token(payload, terminal.tbank_password)})

    started = buyer.post("/billing/start", data={"shtab_id": "0"}, follow_redirects=False)
    assert started.headers["location"] == "https://securepayments.tinkoff.ru/100"
    first = inits[0]
    assert first["Amount"] == terminal.price_per_seat_rub * 100
    assert first["PayType"] == "O" and "Recurrent" not in first
    assert first["Receipt"]["Email"] == "tbank-buyer@example.ru"
    assert first["Receipt"]["Items"][0]["PaymentObject"] == "service"
    assert first["NotificationURL"] == "https://testserver/billing/tbank/webhook"
    return_path = first["SuccessURL"].removeprefix("https://testserver")
    assert outsider.get(return_path).status_code == 404
    assert "Проверяем оплату" in buyer.get(return_path).text
    forged = buyer.post("/billing/tbank/webhook", json={**payments["100"], "Status": "CONFIRMED", "Token": "0" * 64})
    assert forged.status_code == 403
    assert notify("100", "AUTHORIZED").text == "OK"
    with SessionLocal() as db:
        assert access.tier(db, db.get(User, buyer_id)) == access.FREE
    payments["100"]["Amount"] += 100
    assert notify("100", "CONFIRMED").status_code == 503
    payments["100"]["Amount"] -= 100
    assert notify("100", "CONFIRMED").text == "OK"
    assert notify("100", "CONFIRMED").text == "OK"
    assert "Оплата подтверждена" in buyer.get(return_path).text
    with SessionLocal() as db:
        first_end = db.scalar(select(Subscription.paid_until).where(Subscription.payer_id == buyer_id))
        assert access.tier(db, db.get(User, buyer_id)) == access.PRO

    buyer.post("/billing/start", data={"shtab_id": "0"}, follow_redirects=False)
    with SessionLocal() as db:
        assert access.tier(db, db.get(User, buyer_id)) == access.PRO
    assert notify("101", "CONFIRMED").text == "OK"
    with SessionLocal() as db:
        assert db.scalar(select(Subscription.paid_until).where(Subscription.payer_id == buyer_id)
                         .order_by(Subscription.paid_until.desc())) == first_end + dt.timedelta(days=30)

    buyer.post("/billing/pack", follow_redirects=False)
    assert inits[-1]["Amount"] == terminal.pack_price_rub * 100
    assert notify("102", "CONFIRMED").text == "OK"
    assert notify("102", "CONFIRMED").text == "OK"
    with SessionLocal() as db:
        assert db.get(User, buyer_id).extra_plans == terminal.pack_size
        assert db.scalar(select(PackOrder).where(PackOrder.user_id == buyer_id)).credited
        assert db.scalar(select(Payment.status).where(Payment.provider_id.like("tbank:100:%"))) == "succeeded"

    buyer.post("/billing/pack", follow_redirects=False)
    assert notify("103", "REJECTED").text == "OK"
    with SessionLocal() as db:
        assert db.get(User, buyer_id).extra_plans == terminal.pack_size


def test_merchant_pages_and_operator_consent(terminal):
    client, user_id = client_for("tbank-legacy@example.ru", consent=False)
    for path in ("/", "/offer", "/privacy", "/consent", "/payment"):
        response = client.get(path)
        assert response.status_code == 200
        assert "ИП Стариков Матвей" in response.text
        assert "ОГРНИП 326508100589159" in response.text
        assert "info@oginst.ru" in response.text
    for name in ("mir", "visa", "mastercard", "unionpay", "sbp", "tpay", "tbank"):
        assert client.get(f"/static/payments/{name}.svg").headers["content-type"] == "image/svg+xml"
    response = client.get("/billing", follow_redirects=False)
    assert response.headers["location"] == "/privacy/confirm?next=%2Fbilling"
    assert "Отметьте согласие" in client.post("/privacy/confirm", data={"next": "/billing"}).text
    assert client.post("/privacy/confirm", data={"consent": "yes", "next": "/billing"},
                       follow_redirects=False).headers["location"] == "/billing"
    with SessionLocal() as db:
        user = db.get(User, user_id)
        assert user.consent_at < user.ip_consent_at
    assert client.get("/billing").status_code == 200
