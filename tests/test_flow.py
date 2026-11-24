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
    r = client.post("/login", data={"email": "x@example.ru", "next": "/app"})
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
    assert "Чтобы план был точным" in page

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
    assert "Поговорите с каждым участником" in page
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
    assert "Лимит разборов" in client.get("/app").text
    page = client.get("/admin").text
    assert "Расход на ИИ" in page
    sign_in(client, "deputy@example.ru", level="line")
    assert client.get("/admin").status_code == 404


def test_reminders(client):
    from app.jobs import send_reminders
    with SessionLocal() as db:
        assert send_reminders(db) >= 1
