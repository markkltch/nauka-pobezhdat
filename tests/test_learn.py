"""«Учение»: content, the entrance review, a lesson with its order, the report in the разбор,
the смотр and the rank, review, the shtab view."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import learn
from app.db import SessionLocal
from app.main import app
from app.models import LearnItem, LearnMark, LearnOrder, LearnState, Lesson, Member, Message, Plan, Shtab, Step, User
from tests.test_features import _new_situation
from tests.test_flow import sign_in


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_content_is_consistent():
    c = learn.course()
    from app.mentor import quotes

    q = quotes()
    assert len(c["stages"]) == 9 and c["total"] == 27
    for s in c["stages"]:
        assert s["ready"]
        for les in s["lesson_list"]:
            assert les["ready"] and les["quote"] in q and les["words"]["model"] and les["order"]["title"]
            assert len(les["items"]) >= 5
            for it in les["items"]:
                if it["type"] in ("situation", "better"):
                    assert 0 <= it["answer"] < len(it["options"]) and len(it["why"]) == len(it["options"])
                elif it["type"] == "fix":
                    assert any(p.get("err") for p in it["parts"])
                elif it["type"] == "quote":
                    assert " ".join(it["chunks"]) == it["text"], it["id"]
                elif it["type"] == "match":
                    assert len(it["pairs"]) >= 3
                elif it["type"] == "order":
                    assert len(it["steps"]) >= 3
                else:
                    raise AssertionError(it["type"])
    assert len(c["diag"]) == 9 and {d["stage"] for d in c["diag"]} == set(range(1, 10))


def _results(lid, ok=True):
    return {it["id"]: {"ok": ok, "pick": it.get("answer")} for it in learn.lesson(lid)["items"]}


def test_learn_flow(client):
    sign_in(client, "learner@example.ru")
    page = client.get("/learn").text
    assert "Учение" in page and "вступительного смотра" in page and "Постановка задач" in page and "Перспектива" in page and "Готовятся" not in page
    assert client.get("/learn/start").status_code == 200
    r = client.post("/learn/start", json={"picks": {f"d{i}": 2 for i in range(9)} | {"d3": 0}})
    assert r.json()["next"] == "/learn/map"
    m = client.get("/learn/map").text
    assert "Зона роста" in m and "Постановка задач" in m

    sid = _new_situation(client)
    assert client.get("/learn/lesson/1.1").status_code == 200
    assert client.get("/learn/lesson/1.2", follow_redirects=False).status_code == 303  # not open yet
    r = client.post("/learn/lesson/1.1/finish", json={"results": _results("1.1"), "seconds": 200})
    assert r.json()["xp"] == learn.XP["lesson"] and r.json()["right"] == 5
    fb = client.post("/learn/words", json={"lesson": "1.1", "text": "Короткие правила дела, которые каждый помнит"}).json()
    assert fb["model"] and fb["ai"] and fb["add"]  # FREE_BETA is on in tests: the full plan

    with SessionLocal() as db:
        plan = db.scalars(select(Plan).where(Plan.situation_id == sid)).one()
        plan_id, steps_before = plan.id, len(plan.steps)
    due = learn.today().isoformat()
    r = client.post("/learn/lesson/1.1/order", data={"plan_id": plan_id, "if_text": "на планёрке", "then_text": "предложу три правила",
                                                     "due": due, "who": "Я"}, follow_redirects=False)
    assert r.headers["location"] == "/learn/lesson/1.1/done"
    done = client.get("/learn/lesson/1.1/done").text
    assert "Урок пройден" in done and "Приказ стоит" in done
    with SessionLocal() as db:
        plan = db.get(Plan, plan_id)
        assert len(plan.steps) == steps_before + 1
        o = db.scalars(select(LearnOrder).where(LearnOrder.lesson_id == "1.1")).one()
        oid = o.id
    # the report waits in the разбор on its due date
    page = client.get(f"/app/{sid}").text
    assert "Срок приказа сегодня" in page and f"/learn/order/{oid}/report" in page
    r = client.post(f"/learn/order/{oid}/report", data={"outcome": "done", "note": "Команда приняла три правила", "next": f"/app/{sid}"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/app/{sid}").text
    assert "Отчёт по приказу" in page and "Срок приказа сегодня" not in page
    with SessionLocal() as db:
        o = db.get(LearnOrder, oid)
        assert o.outcome == "done" and db.get(Step, o.step_id).done
        assert db.scalars(select(Lesson).where(Lesson.text.like("Приказ «%"))).first()
        assert db.scalars(select(Message).where(Message.situation_id == sid, Message.role == "mentor")).all()[-1].data["learn_order"] == oid

    # the rest of stage 1, then the смотр: a rank (lessons + смотр + an order of the stage)
    for lid in ("1.2", "1.3"):
        assert client.get(f"/learn/lesson/{lid}").status_code == 200
        client.post(f"/learn/lesson/{lid}/finish", json={"results": _results(lid), "seconds": 120})
    tp = client.get("/learn/test/1").text
    assert "uc-data" in tp
    items = learn.test_items(None, type("U", (), {"id": 0})(), 1)
    assert len(items) == 10 or len(items) == len([i for les in learn.stage(1)["lesson_list"] for i in les["items"]])
    res = {it["id"]: {"ok": True} for it in items}
    nxt = client.post("/learn/test/1/finish", json={"results": res, "seconds": 300}).json()["next"]
    assert "rank=1" in nxt
    page = client.get(nxt).text
    assert "Вам присвоен чин рядового" in page
    home = client.get("/learn").text
    assert "Рядовой" in home and "Пройден · смотр" in home
    # stage task
    client.post("/learn/task/1", data={"note": "Пять правил приняты"})
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.email == "learner@example.ru")).one()
        assert db.scalars(select(LearnMark).where(LearnMark.user_id == u.id, LearnMark.kind == "task")).one()
        st = db.scalars(select(LearnState).where(LearnState.user_id == u.id)).one()
        assert st.xp >= 3 * learn.XP["lesson"] + learn.XP["order"] + learn.XP["test"] + learn.XP["task"]
        # make every repeat due today: review works
        for row in db.scalars(select(LearnItem).where(LearnItem.user_id == u.id)).all():
            row.due = learn.today() - dt.timedelta(days=1)
        db.commit()
    assert "uc-data" in client.get("/learn/review").text
    r = client.post("/learn/review/finish", json={"results": {"1.1.1": {"ok": True, "pick": 1}}, "seconds": 60})
    assert "reviewed=1-1" in r.json()["next"]
    assert "Повторение пройдено" in client.get(r.json()["next"]).text


def test_free_stages_and_shtab(client, monkeypatch):
    sign_in(client, "head@example.ru", name="Глава Штаба")
    client.post("/shtab/new", data={"name": "Учебный", "about": ""})
    with SessionLocal() as db:
        sh = db.scalars(select(Shtab).where(Shtab.name == "Учебный")).one()
        head = db.scalars(select(User).where(User.email == "head@example.ru")).one()
        sid = sh.id
        learner = db.scalars(select(User).where(User.email == "learner@example.ru")).one()
        db.add(Member(shtab_id=sid, user_id=learner.id, title="Начальник смены"))
        db.commit()
        learner_id = learner.id
    page = client.get(f"/shtab/{sid}/learn").text
    assert "Команда" in page and "Назначить этап" in page and "Ответы в ситуации урока" not in page
    client.post(f"/shtab/{sid}/learn/assign", data={"stage": 1, "due": ""})
    page = client.get(f"/shtab/{sid}/learn").text
    assert "Ответы в ситуации урока" in page and "Похвалить" in page
    client.post(f"/shtab/{sid}/learn/note", data={"to_id": learner_id, "text": "Так держать!"})
    assert "Учение" in client.get(f"/shtab/{sid}").text

    sign_in(client, "learner@example.ru")
    home = client.get("/learn").text
    assert "Так держать!" in home
    assert "Так держать!" not in client.get("/learn").text  # shown once

    # on the free plan, stage 3+ is locked (and stage 3 has no lessons yet anyway)
    from app import access
    monkeypatch.setattr(access, "tier", lambda db, user: access.FREE)
    assert learn.stage_open(None, None, 1) and learn.stage_open(None, None, 2) and not learn.stage_open(None, None, 3)
    assert client.get("/learn/test/3", follow_redirects=False).status_code == 303


def test_streak_counts_workdays_and_rests():
    with SessionLocal() as db:
        u = User(email="streak@example.ru", name="Стрик", level="mid")
        db.add(u)
        db.flush()
        from app.models import LearnDay
        d = learn.today()
        days, k = [], 0
        while len(days) < 6:
            day = d - dt.timedelta(days=k)
            if day.weekday() < 5:
                days.append(day)
            k += 1
        for day in days[:2] + days[3:]:  # one workday missed: a rest covers it
            db.add(LearnDay(user_id=u.id, day=day))
        db.flush()
        s = learn.streak(db, u)
        assert s["days"] == 5 and s["today"] == (d.weekday() < 5)
        db.rollback()
