"""«Учение» end to end on the deployed code, in a throwaway database (never the real one):
   cd ~/nauka-app && DATABASE_URL=sqlite:////tmp/learn-selftest.db LLM_PROVIDER=fake SMTP_HOST= \\
       ~/nauka-venv/bin/python -m app.learn_selftest
Signs a test person in, takes the entrance review, a lesson with its order, the report, the rest of
stage 1, the смотр (a rank), review, the stage task and the shtab page, and checks every page renders."""
from __future__ import annotations

import os
import sys

if "learn-selftest" not in os.environ.get("DATABASE_URL", ""):
    sys.exit("Run with DATABASE_URL=sqlite:////tmp/learn-selftest.db — never on the real database.")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from . import auth, learn  # noqa: E402
from .db import SessionLocal, init_db  # noqa: E402
from .main import app  # noqa: E402
from .models import LearnOrder, Member, Message, Plan, Shtab, Situation, Step, User, now  # noqa: E402


def check(ok: bool, what: str) -> None:
    print(("OK   " if ok else "FAIL ") + what)
    if not ok:
        check.failed = True


check.failed = False


def main() -> None:
    init_db()
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.email == "learn@selftest.invalid")).first()
        if not u:
            u = User(email="learn@selftest.invalid", name="Иван Самотест", role="Начальник смены", level="mid",
                     consent_at=now(), ip_consent_at=now())
            db.add(u)
            db.flush()
        sit = Situation(user_id=u.id, title="Опоздания во второй смене", stage="follow")
        db.add(sit)
        db.flush()
        plan = Plan(situation_id=sit.id, user_id=u.id, title="План на две недели", goal="Порядок за 14 дней")
        db.add(plan)
        db.flush()
        db.add(Step(plan_id=plan.id, position=1, text="Поговорить с каждым один на один", owner_name="Вы",
                    due=learn.today(), created_by=u.id))
        sh = Shtab(name="Самотест", owner_id=u.id)
        db.add(sh)
        db.flush()
        db.add(Member(shtab_id=sh.id, user_id=u.id, title="Глава штаба"))
        db.commit()
        uid, sid, plan_id, shid = u.id, sit.id, plan.id, sh.id
        token = auth.open_session(db, u).id

    c = TestClient(app)
    c.cookies.set(auth.COOKIE, token)
    for path in ("/learn", "/learn/start", "/learn/lesson/1.1", "/learn/task/1", f"/shtab/{shid}/learn", f"/shtab/{shid}"):
        r = c.get(path, follow_redirects=False)
        check(r.status_code == 200, f"GET {path} → {r.status_code}")
    r = c.post("/learn/start", json={"picks": {f"d{i}": 1 for i in range(9)}})
    check(r.json().get("next") == "/learn/map", "вступительный смотр сохранён")
    check("Ваша карта навыков" in c.get("/learn/map").text, "карта навыков")

    def results(lid):
        return {it["id"]: {"ok": True, "pick": it.get("answer")} for it in learn.lesson(lid)["items"]}

    r = c.post("/learn/lesson/1.1/finish", json={"results": results("1.1"), "seconds": 200})
    check(r.status_code == 200 and r.json()["right"] == len(learn.lesson("1.1")["items"]), "урок 1.1 пройден")
    fb = c.post("/learn/words", json={"lesson": "1.1", "text": "Короткие правила дела, которые все помнят"}).json()
    check(bool(fb.get("model")), "своими словами: эталон")
    r = c.post("/learn/lesson/1.1/order", data={"plan_id": plan_id, "if_text": "на планёрке", "then_text": "предложу три правила",
                                                "due": learn.today().isoformat(), "who": "Я"}, follow_redirects=False)
    check(r.headers.get("location") == "/learn/lesson/1.1/done", "приказ поставлен")
    check("Урок пройден" in c.get("/learn/lesson/1.1/done").text, "итог урока")
    page = c.get(f"/app/{sid}").text
    check("Срок приказа сегодня" in page, "«Как прошло?» в разборе")
    with SessionLocal() as db:
        oid = db.scalars(select(LearnOrder).where(LearnOrder.user_id == uid)).first().id
    c.post(f"/learn/order/{oid}/report", data={"outcome": "done", "note": "Приняли три правила", "next": f"/app/{sid}"})
    check("Отчёт по приказу" in c.get(f"/app/{sid}").text, "отчёт принят, ответ наставника в разборе")
    for lid in ("1.2", "1.3"):
        c.post(f"/learn/lesson/{lid}/finish", json={"results": results(lid), "seconds": 120})
    check(c.get("/learn/test/1").status_code == 200, "смотр открывается")
    items = learn.test_items(None, type("U", (), {"id": uid})(), 1)
    nxt = c.post("/learn/test/1/finish", json={"results": {it["id"]: {"ok": True} for it in items}, "seconds": 300}).json()["next"]
    check("rank=1" in nxt and "чин рядового" in c.get(nxt).text, "смотр сдан, новый чин")
    c.post("/learn/task/1", data={"note": "Пять правил"})
    home = c.get("/learn").text
    check("Рядовой" in home and "Пройден" in home, "главная после этапа")
    with SessionLocal() as db:
        from .models import LearnItem
        for row in db.scalars(select(LearnItem).where(LearnItem.user_id == uid)).all():
            row.due = learn.today()
        db.commit()
    check(c.get("/learn/review").status_code == 200, "повторение")
    check("Команда" in c.get(f"/shtab/{shid}/learn").text, "штаб: учение")
    c.post(f"/shtab/{shid}/learn/assign", data={"stage": 2})
    check("Назначен этап 2" in c.get(f"/shtab/{shid}/learn").text, "штаб: этап назначен")
    with SessionLocal() as db:
        msgs = db.scalars(select(Message).where(Message.situation_id == sid)).all()
        check(any((m.data or {}).get("learn_order") for m in msgs), "сообщение наставника про приказ")
    print("ГОТОВО" if not check.failed else "ЕСТЬ ОШИБКИ")
    sys.exit(1 if check.failed else 0)


if __name__ == "__main__":
    main()
