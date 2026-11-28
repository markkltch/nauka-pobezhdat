import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import LoginCode, Step, Usage


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def sign_in(client, email, level="mid", name="Иван Петров"):
    client.cookies.clear()
    r = client.post("/login", data={"email": email, "consent": "1", "next": "/app"})
    assert "Код из письма" in r.text
    with SessionLocal() as db:
        code = db.scalars(select(LoginCode).where(LoginCode.email == email).order_by(LoginCode.id.desc())).first().code
    r = client.post("/login/code", data={"email": email, "code": code, "next": "/app"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/onboarding", data={"name": name, "role": "Начальник отдела", "level": level, "next": "/app"},
                    follow_redirects=False)
    assert r.headers["location"] == "/app"


def test_public_pages(client):
    assert client.get("/").status_code == 200
    for p in ("/offer", "/privacy", "/consent", "/healthz"):
        assert client.get(p).status_code == 200
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_consent_required(client):
    client.cookies.clear()
    r = client.post("/register", data={"name": "Икс", "email": "x@example.ru", "password": "secret123", "next": "/app"})
    assert "согласия" in r.text


def test_wrong_code(client):
    client.post("/login", data={"email": "w@example.ru", "consent": "1"})
    r = client.post("/login/code", data={"email": "w@example.ru", "code": "000000"})
    assert "Код не подошёл" in r.text or r.status_code == 303


def test_full_flow(client):
    sign_in(client, "boss@example.ru")
    r = client.post("/app/new", data={"text": "Двое в смене опаздывают"}, follow_redirects=False)
    assert r.status_code == 303
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/app/{sid}").text
    assert "чтобы план был точным" in page

    client.post(f"/app/{sid}/say", data={"text": "Одни и те же, говорил один раз"})
    page = client.get(f"/app/{sid}").text
    assert "План на две недели" in page
    assert "Каждый воин должен понимать свой маневр" in page  # verified quote mapped by id
    assert "Соберите штаб" in page

    # follow-up question after the plan
    client.post(f"/app/{sid}/say", data={"text": "А если они уволятся?"})
    assert "Хороший вопрос" in client.get(f"/app/{sid}").text

    # shtab: create, send plan, invite a second person
    r = client.post("/shtab/new", data={"name": "Линия 2"}, follow_redirects=False)
    shtab_url = r.headers["location"]
    shtab_id = int(shtab_url.rsplit("/", 1)[1])
    page = client.get(f"/app/{sid}").text
    pid = int(re.search(r"/plans/(\d+)/send", page).group(1))
    client.post(f"/plans/{pid}/send", data={"shtab_id": shtab_id})
    page = client.get(shtab_url).text
    assert "Поговорите с каждым один на один" in page
    invite = re.search(r"/join/([\w-]+)", page).group(1)

    with SessionLocal() as db:
        step = db.scalars(select(Step).where(Step.shtab_id == shtab_id)).first()
        assert step.assignee_id is not None  # "Вы" -> the author
        assert db.scalars(select(Usage)).first() is not None  # cost ledger written

    sign_in(client, "deputy@example.ru", level="line", name="Анна Ковалёва")
    client.get(f"/join/{invite}")
    page = client.get(shtab_url).text
    assert "Анна Ковалёва" in page and "2 человека" in page
    r = client.post(f"/steps/{step.id}/toggle", follow_redirects=False)
    assert r.status_code == 303

    # a stranger cannot see the shtab or the situation
    sign_in(client, "stranger@example.ru")
    assert client.get(shtab_url).status_code == 404
    assert client.get(f"/app/{sid}").status_code == 404


def test_limit_and_admin(client):
    sign_in(client, "boss@example.ru")
    # limit is 2 plans a month in tests; one plan exists, make a second
    r = client.post("/app/new", data={"text": "Конфликт двух мастеров"}, follow_redirects=False)
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    client.post(f"/app/{sid}/say", data={"text": "Делят станок"})
    r = client.post("/app/new", data={"text": "Ещё одна"}, follow_redirects=False)
    assert r.headers["location"] == "/app?limit=1"
    assert "Разборы на этот месяц закончились" in client.get("/app").text
    page = client.get("/admin").text
    assert "Расход на ИИ" in page and "Покупок пока нет" in page
    # purchases show up in the owner's summary: a paid subscription and a pending pack
    from app.models import PackOrder, Payment, Subscription, User
    with SessionLocal() as db:
        boss = db.scalars(select(User).where(User.email == "boss@example.ru")).one()
        sub = Subscription(payer_id=boss.id, seats=1, status="active")
        db.add(sub); db.flush()
        db.add(Payment(subscription_id=sub.id, provider_id="t-admin-1", amount_rub=1500, status="succeeded"))
        db.add(PackOrder(user_id=boss.id, plans=5, amount_rub=990, status="pending"))
        db.commit()
    page = client.get("/admin").text
    assert "Подписка · 1 место" in page and "Пакет разборов · 5 шт." in page
    assert "1\u00a0500\u00a0₽" in page and "Оплачено" in page and "Ждёт оплаты" in page
    sign_in(client, "deputy@example.ru", level="line")
    assert client.get("/admin").status_code == 404


def test_reminders(client, monkeypatch):
    from app import jobs
    from app.jobs import send_reminders
    monkeypatch.setattr(jobs, "daytime", lambda: True)  # reminders go out only 8:00–21:00 MSK
    with SessionLocal() as db:
        assert send_reminders(db) >= 1


def test_legal_pages_name_operator(client):
    for p in ("/offer", "/privacy", "/consent"):
        t = client.get(p).text
        assert "9710113703" in t and "Открытый гуманитарный институт" in t
        assert "[ИНН]" not in t and "[ПОЛНОЕ" not in t and "Черновик" not in t
    assert "ЯНДЕКС.ОБЛАКО" in client.get("/consent").text
    client.cookies.clear()
    login = client.get("/login").text
    reg = client.get("/register").text
    assert "/consent" in reg and "оферты" in reg and "/register" in login


def test_cancel_autorenew(client):
    import datetime as dt

    from app.models import Subscription, User

    sign_in(client, "renew@example.ru")
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.email == "renew@example.ru")).one()
        sub = Subscription(payer_id=u.id, seats=1, status="active", payment_method_id="pm_1",
                           paid_until=dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(days=20))
        db.add(sub)
        db.commit()
        sid = sub.id
    assert "Отключить автопродление" in client.get("/billing").text
    r = client.post(f"/billing/cancel/{sid}", follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        s = db.get(Subscription, sid)
        assert s.payment_method_id == "" and s.status == "active"
    assert "Автопродление отключено" in client.get("/billing").text


def test_register_then_password_login(client):
    from app.models import User

    client.cookies.clear()
    page = client.get("/register").text
    assert "Создать аккаунт" in page and "Регистрация" in page
    # weak password and missing consent are refused before any letter goes out
    r = client.post("/register", data={"name": "Ольга", "email": "olga@example.ru", "password": "12345678", "consent": "1"})
    assert "буквы" in r.text
    r = client.post("/register", data={"name": "Ольга", "email": "olga@example.ru", "password": "secret123"})
    assert "согласия" in r.text
    r = client.post("/register", data={"name": "Ольга", "email": "olga@example.ru", "password": "secret123", "consent": "1"})
    assert "Подтвердите почту" in r.text
    with SessionLocal() as db:  # nothing is stored on the account until the address is confirmed
        assert db.scalars(select(User).where(User.email == "olga@example.ru")).first() is None
        code = db.scalars(select(LoginCode).where(LoginCode.email == "olga@example.ru")).first().code
    r = client.post("/login/code", data={"email": "olga@example.ru", "code": code, "tab": "register"}, follow_redirects=False)
    assert r.headers["location"].startswith("/onboarding")
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.email == "olga@example.ru")).one()
        assert u.name == "Ольга" and u.password_hash and u.consent_at
    client.cookies.clear()
    r = client.post("/register", data={"name": "X", "email": "olga@example.ru", "password": "secret123", "consent": "1"})
    assert "уже зарегистрирована" in r.text
    r = client.post("/login/password", data={"email": "olga@example.ru", "password": "secret123"}, follow_redirects=False)
    assert r.status_code == 303


def test_code_login_unknown_address_points_to_signup(client):
    client.cookies.clear()
    r = client.post("/login", data={"email": "nobody@example.ru"})
    assert "Зарегистрироваться" in r.text and "Аккаунта с почтой" in r.text


def test_letters_and_unsubscribe(client, monkeypatch):
    from app import emails, mailer
    from app.config import get_settings
    from app.models import User

    sent = []
    monkeypatch.setattr(mailer, "_deliver", lambda msg: sent.append(msg))
    # a new account gets the code letter (text + HTML) and, after sign-in, the welcome letter
    client.cookies.clear()
    client.post("/login", data={"email": "letters@example.ru", "consent": "1", "next": "/app"})
    code_msg = sent[-1]
    assert code_msg["Subject"].startswith("Код входа") and code_msg["Message-ID"] and code_msg["Date"]
    html = code_msg.get_body(("html",)).get_content()
    with SessionLocal() as db:
        row = db.scalars(select(LoginCode).where(LoginCode.email == "letters@example.ru")).first()
        code = row.code
    assert code in html and "/login/link/" in html
    client.post("/login/code", data={"email": "letters@example.ru", "code": code, "next": "/app"})
    assert any("Добро пожаловать" in m["Subject"] for m in sent)
    # one-click unsubscribe works without signing in and only with the right token
    with SessionLocal() as db:
        uid = db.scalars(select(User.id).where(User.email == "letters@example.ru")).one()
    letter = emails.reminder(uid, "Сделать отчёт", "завтра", "http://testserver/app", False)
    msg = mailer.build("letters@example.ru", letter.subject, letter.text, letter.html, letter.unsubscribe)
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    client.cookies.clear()
    assert client.post(f"/unsubscribe/{uid}/steps/{'0' * 32}").status_code == 404
    assert "Отписаться" in client.get(letter.unsubscribe.replace(get_settings().base_url.rstrip("/"), "")).text
    assert "Готово" in client.post(letter.unsubscribe.replace(get_settings().base_url.rstrip("/"), "")).text
    with SessionLocal() as db:
        assert db.get(User, uid).remind_steps is False


def test_lifetime_access(monkeypatch):
    from app import access
    from app.config import get_settings
    from app.models import User

    s = get_settings()
    monkeypatch.setattr(s, "free_beta", False)
    monkeypatch.setattr(s, "lifetime_emails", "Friend@Example.ru")
    with SessionLocal() as db:
        assert access.tier(db, User(email="friend@example.ru")) == access.PRO
        assert access.tier(db, User(email="boss@example.ru")) == access.PRO  # owners too
        assert access.tier(db, User(email="someone@example.ru")) == access.FREE


def test_delete_and_rename_situation(client):
    from app import access
    from app.models import Message, Plan, Situation, User
    sign_in(client, "deleter@example.ru")
    r = client.post("/app/new", data={"text": "Двое в смене опаздывают"}, follow_redirects=False)
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    client.post(f"/app/{sid}/say", data={"text": "Одни и те же, говорил один раз"})
    r = client.post("/shtab/new", data={"name": "Склад"}, follow_redirects=False)
    shtab_id = int(r.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/app/{sid}").text
    assert 'data-sit-menu="%d"' % sid in page and 'data-shtab="1"' not in page
    pid = int(re.search(r"/plans/(\d+)/send", page).group(1))
    client.post(f"/plans/{pid}/send", data={"shtab_id": shtab_id})
    assert 'data-shtab="1"' in client.get(f"/app/{sid}").text

    # rename: trimmed, empty ignored, back where it was asked
    r = client.post(f"/app/{sid}/rename", data={"title": "  Опоздания   на складе ", "next": f"/app/{sid}"}, follow_redirects=False)
    assert r.headers["location"] == f"/app/{sid}"
    assert "Опоздания на складе" in client.get(f"/app/{sid}").text
    client.post(f"/app/{sid}/rename", data={"title": "   "})
    assert "Опоздания на складе" in client.get(f"/app/{sid}").text

    with SessionLocal() as db:
        user = db.scalars(select(User).where(User.email == "deleter@example.ru")).one()
        used = access.used_this_month(db, user)

    # a stranger can neither rename nor delete it
    sign_in(client, "outsider@example.ru")
    assert client.post(f"/app/{sid}/delete").status_code == 404
    assert client.post(f"/app/{sid}/rename", data={"title": "x"}).status_code == 404

    sign_in(client, "deleter@example.ru")
    r = client.post(f"/app/{sid}/delete", data={"next": f"/app/{sid}"}, follow_redirects=False)
    assert r.headers["location"] == "/app?deleted=1"
    assert "Разбор удалён" in client.get("/app?deleted=1").text
    assert client.get(f"/app/{sid}").status_code == 404
    assert "Опоздания на складе" not in client.get("/app").text
    with SessionLocal() as db:
        sit = db.get(Situation, sid)
        assert sit.deleted_at and sit.title == "Удалённый разбор"
        assert not db.scalars(select(Message).where(Message.situation_id == sid)).all()
        assert not db.scalars(select(Plan).where(Plan.situation_id == sid)).all()
        kept = db.scalars(select(Step).where(Step.shtab_id == shtab_id)).all()
        assert kept and all(s.plan_id is None for s in kept)  # the team keeps its tasks
        user = db.scalars(select(User).where(User.email == "deleter@example.ru")).one()
        assert access.used_this_month(db, user) == used  # deleting does not give a разбор back
    assert "Поговорите с каждым один на один" in client.get(f"/shtab/{shtab_id}").text
    # deleting from another page returns there
    r = client.post("/app/new", data={"text": "Сотрудник просит повышения"}, follow_redirects=False)
    sid2 = int(r.headers["location"].rsplit("/", 1)[1])
    r = client.post(f"/app/{sid2}/delete", data={"next": "/people"}, follow_redirects=False)
    assert r.headers["location"] == "/people"


def test_staff_panel(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "staff_emails", "helper@example.ru")
    sign_in(client, "helper@example.ru")
    page = client.get("/app").text
    assert 'href="/admin"' in page and "Управление" in page and "Владелец" not in page
    assert client.get("/admin").status_code == 200
    assert "Полный · навсегда" in page  # the staff have the full plan, like the owners
    sign_in(client, "boss@example.ru")
    page = client.get("/app").text
    assert "Владелец" in page and ">Управление<" not in page
