"""Штаб features: password and Yandex ID sign-in, settings, attachments, voice, the three-arts plan,
talk preparation, разбор после дела, memory, the printable order and the calendar."""
import datetime as dt
import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import mentor, yandex_id
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import Lesson, Person, Plan, Situation, Step, User
from tests.test_flow import sign_in


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _new_situation(client, text="Двое кладовщиков Иван и Олег опаздывают на смену", files=None):
    r = client.post("/app/new", data={"text": text}, files=files, follow_redirects=False)
    assert r.status_code == 303, r.text
    sid = int(re.search(r"/app/(\d+)", r.headers["location"]).group(1))
    client.post(f"/app/{sid}/say", data={"text": "Одни и те же, третью неделю"}, follow_redirects=False)
    return sid


def test_password_sign_in_and_lockout(client):
    sign_in(client, "pw@example.ru")
    r = client.post("/settings/password", data={"password": "short", "password2": "short"}, follow_redirects=False)
    assert "pw_" in r.headers["location"] and "pw_ok" not in r.headers["location"]
    r = client.post("/settings/password", data={"password": "Suvorov1799", "password2": "Suvorov1799"}, follow_redirects=False)
    assert r.headers["location"].endswith("pw_ok#password")
    client.cookies.clear()
    r = client.post("/login/password", data={"email": "PW@example.ru", "password": "Suvorov1799", "next": "/app"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    client.cookies.clear()
    for _ in range(5):
        r = client.post("/login/password", data={"email": "pw@example.ru", "password": "wrong-pass1"})
    r = client.post("/login/password", data={"email": "pw@example.ru", "password": "Suvorov1799"})
    assert "Слишком много попыток" in r.text


def test_settings_and_notifications(client):
    sign_in(client, "settings@example.ru")
    page = client.get("/settings").text
    assert "Утреннее донесение" in page and "Задать пароль" in page
    client.post("/settings/profile", data={"name": "Анна", "role": "Директор", "level": "top",
                                           "team_note": "Склад, 40 человек, две смены"})
    client.post("/settings/notify", data={"remind_steps": "1"})
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.email == "settings@example.ru")).one()
        assert u.level == "top" and u.team_note.startswith("Склад") and u.remind_steps and not u.morning_report


def test_plan_three_arts_prep_review_memory(client):
    sign_in(client, "plan@example.ru")
    sid = _new_situation(client)
    page = client.get(f"/app/{sid}").text
    for part in ("Глазомер", "Корень дела", "Быстрота", "Натиск", "Признак победы", "Подготовить разговор",
                 "Приказ для печати", "Сроки в календарь"):
        assert part in page, part
    with SessionLocal() as db:
        plan = db.scalars(select(Plan).where(Plan.situation_id == sid)).one()
        talk = next(s for s in plan.steps if s.kind == "talk")
        assert db.scalars(select(Person).where(Person.name == "Пётр Сидоров")).first()  # remembered
        assert len(db.scalars(select(Person).where(Person.name.in_(["Пётр Сидоров", "пётр  сидоров"]))).all()) == 1  # "пётр  сидоров" is the same person
        plan_id, talk_id = plan.id, talk.id
    client.post(f"/steps/{talk_id}/prep", follow_redirects=False)
    page = client.get(f"/app/{sid}").text
    assert "Подготовка разговора" in page and "Если скажут" in page

    order = client.get(f"/plans/{plan_id}/order").text
    assert "Приказываю" in order and "Признак победы" in order
    ics = client.get(f"/plans/{plan_id}/calendar.ics")
    assert ics.headers["content-type"].startswith("text/calendar") and ics.text.count("BEGIN:VEVENT") >= 3

    # after the deadlines: разбор после дела, the lesson goes to memory
    with SessionLocal() as db:
        for s in db.scalars(select(Step).where(Step.plan_id == plan_id)):
            s.due = dt.date.today() - dt.timedelta(days=1)
        db.commit()
    assert "Пора подвести итог" in client.get(f"/app/{sid}").text
    client.post(f"/app/{sid}/review", data={"outcome": "partial", "text": "Иван исправился, Олег нет"})
    page = client.get(f"/app/{sid}").text
    assert "Разбор после дела" in page and "Урок" in page
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.email == "plan@example.ru")).one()
        assert db.scalars(select(Lesson).where(Lesson.user_id == u.id)).first()
        assert "Уроки прошлых разборов" in mentor.memory(db, u)
    assert "Пётр Сидоров" in client.get("/people").text


def _docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="w"><w:body><w:p><w:r><w:t>' + text +
                   '</w:t></w:r></w:p></w:body></w:document>')
    return buf.getvalue()


def test_attachments(client):
    sign_in(client, "file@example.ru")
    sid = _new_situation(client, text="Посмотрите регламент",
                         files={"file": ("регламент.docx", _docx("Смена начинается в 8:00"), "application/octet-stream")})
    page = client.get(f"/app/{sid}").text
    assert "регламент.docx" in page
    with SessionLocal() as db:
        sit = db.get(Situation, sid)
        assert any(m.kind == "file" and "8:00" in m.text for m in sit.messages)
    r = client.post("/app/new", data={"text": "x"}, files={"file": ("photo.jpg", b"\xff\xd8", "image/jpeg")},
                    follow_redirects=False)
    assert "ferr=" in r.headers["location"]


def test_voice(client):
    sign_in(client, "voice@example.ru")
    pcm = b"\x00\x00" * 16000 * 3  # 3 seconds of silence at 16 kHz
    r = client.post("/voice", content=pcm)
    assert r.status_code == 200 and r.json()["text"]
    too_long = b"\x00\x00" * 16000 * (get_settings().voice_max_seconds + 5)
    assert client.post("/voice", content=too_long).status_code == 400


def test_mobile_enter_and_menu_markup(client):
    sign_in(client, "mobile@example.ru")
    page = client.get("/app").text
    assert 'data-menu' in page and 'enctype="multipart/form-data"' in page and "data-voice" in page
    js = client.get("/static/app.js").text
    assert "pointer: coarse" in js  # Enter is a new line on a phone


def test_yandex_id(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "yandex_oauth_client_id", "cid")
    monkeypatch.setattr(s, "yandex_oauth_client_secret", "secret")
    client.cookies.clear()
    assert "Войти с Яндекс ID" in client.get("/login").text
    # signing in with an unknown Yandex account leads to sign-up, not to a silent new account
    r = client.get("/auth/yandex?next=/app", follow_redirects=False)
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    monkeypatch.setattr(yandex_id, "profile", lambda code: {"id": "778", "email": "new@yandex.ru", "name": "Анна"})
    r = client.get(f"/auth/yandex/callback?code=abc&state={state}", follow_redirects=False)
    assert r.headers["location"].startswith("/register?yerr=noacc")
    with SessionLocal() as db:
        assert db.scalars(select(User).where(User.email == "new@yandex.ru")).first() is None
    r = client.get("/auth/yandex?next=/app&consent=1", follow_redirects=False)
    assert r.headers["location"].startswith("https://oauth.yandex.ru/authorize")
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    monkeypatch.setattr(yandex_id, "profile", lambda code: {"id": "777", "email": "ya@yandex.ru", "name": "Пётр"})
    r = client.get(f"/auth/yandex/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/onboarding")
    with SessionLocal() as db:
        assert db.scalars(select(User).where(User.yandex_id == "777")).one().email == "ya@yandex.ru"
    # a forged state is refused
    client.cookies.clear()
    r = client.get("/auth/yandex/callback?code=abc&state=forged", follow_redirects=False)
    assert "yerr=failed" in r.headers["location"]


def test_worst_case_with_voice_keeps_1200():
    s = get_settings()
    ai = 30 * s.budget_per_situation_rub
    voice = s.voice_per_month * 2 * s.price_voice_per_15s  # 30 s = two billing pieces
    net = s.price_per_seat_rub * (1 - 0.035 * 1.22 - 0.06) - ai - voice
    assert net >= 1200, net
    free_year = 12 * (s.free_plans_per_month * s.budget_per_situation_rub
                      + s.voice_free_per_month * 2 * s.price_voice_per_15s)
    assert free_year <= 200, free_year
