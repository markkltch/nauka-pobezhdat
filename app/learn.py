"""«Учение»: the learning section — course content, progress, streak, ranks, spaced repetition,
the skill map from the person's own plans, and «приказ на неделю» (a lesson applied to a real plan).

Everything here is rules and stored text: the only model call is the optional check of
an answer «своими словами» (words_feedback), on the full plan, inside a small daily cap.
The method is in the doc «Учение: методика развития руководителя».
"""
from __future__ import annotations

import datetime as dt
import json
import random
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import access, llm
from .models import (LearnAssign, LearnDay, LearnDone, LearnItem, LearnMark, LearnOrder, LearnState, Lesson, Member,
                     Message, Plan, Situation, Step, Usage, User, now)

CONTENT = Path(__file__).resolve().parent / "content" / "uchenie"
LADDER = [1, 3, 7, 21, 60]  # days until the next repeat, by box
XP = {"lesson": 10, "review": 5, "test": 20, "order": 50, "order_partial": 25, "task": 100}
PASS = 0.8
LEVEL_NAMES = ["Не начат", "Знает", "Помнит", "Применяет", "Владеет", "Учит"]
OUTCOME_NAMES = {"done": "Сделал", "partial": "Частично", "miss": "Не вышло"}
WORDS_PER_DAY = 10


def today() -> dt.date:
    return (now() + dt.timedelta(hours=3)).date()  # the service lives by Moscow time


# ---------- content ----------

@lru_cache
def course() -> dict:
    data = json.loads((CONTENT / "course.json").read_text(encoding="utf-8"))
    from .mentor import quotes

    q = quotes()
    lessons: dict[str, dict] = {}
    for st in data["stages"]:
        path = CONTENT / f"stage{st['n']}.json"
        full = {x["id"]: x for x in json.loads(path.read_text(encoding="utf-8"))} if path.exists() else {}
        st["ready"] = bool(full)
        st["lesson_list"] = []
        for i, (lid, title) in enumerate(st["lessons"]):
            les = dict(full.get(lid, {}), id=lid, title=title, stage=st["n"], no=i + 1, ready=lid in full)
            for k, it in enumerate(les.get("items", [])):
                it["id"] = f"{lid}.{k + 1}"
                it["lesson"] = lid
                it["stage"] = st["n"]
                if it["type"] == "quote":
                    it["text"] = q[it["quote"]]["quote"]
            if les.get("quote"):
                les["quote_text"] = q[les["quote"]]["quote"]
                les["quote_source"] = q[les["quote"]]["source"]
            lessons[lid] = les
            st["lesson_list"].append(les)
    data["lessons"] = lessons
    data["items"] = {it["id"]: it for les in lessons.values() for it in les.get("items", [])}
    data["diag"] = json.loads((CONTENT / "diag.json").read_text(encoding="utf-8"))
    data["total"] = len(lessons)
    return data


def stage(n: int) -> dict:
    return course()["stages"][n - 1]


def lesson(lid: str) -> dict | None:
    return course()["lessons"].get(lid)


def rank_name(i: int) -> str:
    ranks = course()["ranks"]
    return ranks[max(0, min(i, len(ranks) - 1))]


# ---------- state ----------

def state(db: Session, user: User, create: bool = True) -> LearnState | None:
    st = db.scalars(select(LearnState).where(LearnState.user_id == user.id)).first()
    if st is None and create:
        st = LearnState(user_id=user.id)
        db.add(st)
        db.flush()
    return st


def done_ids(db: Session, user: User) -> set[str]:
    return set(db.scalars(select(LearnDone.lesson_id).where(LearnDone.user_id == user.id)).all())


def done_count(db: Session, user: User) -> int:
    return db.scalar(select(func.count(func.distinct(LearnDone.lesson_id))).where(LearnDone.user_id == user.id)) or 0


def marks(db: Session, user: User, kind: str) -> dict[int, LearnMark]:
    """The best mark per stage."""
    best: dict[int, LearnMark] = {}
    for m in db.scalars(select(LearnMark).where(LearnMark.user_id == user.id, LearnMark.kind == kind)
                        .order_by(LearnMark.id)).all():
        if m.stage not in best or (m.passed and not best[m.stage].passed) or m.score > best[m.stage].score:
            best[m.stage] = m
    return best


def orders(db: Session, user: User) -> list[LearnOrder]:
    return db.scalars(select(LearnOrder).where(LearnOrder.user_id == user.id).order_by(LearnOrder.id.desc())).all()


def touch(db: Session, user: User, seconds: int = 0) -> None:
    d = today()
    row = db.scalars(select(LearnDay).where(LearnDay.user_id == user.id, LearnDay.day == d)).first()
    if row is None:
        db.add(LearnDay(user_id=user.id, day=d, seconds=max(0, seconds)))
    else:
        row.seconds = (row.seconds or 0) + max(0, seconds)
    db.flush()


def add_xp(db: Session, user: User, n: int) -> None:
    st = state(db, user)
    st.xp = (st.xp or 0) + n


def _active_days(db: Session, user: User, since: dt.date) -> set[dt.date]:
    days = set(db.scalars(select(LearnDay.day).where(LearnDay.user_id == user.id, LearnDay.day >= since)).all())
    for t in db.scalars(select(Step.done_at).where(Step.done.is_(True), Step.done_at.is_not(None),
                                                   (Step.assignee_id == user.id) | ((Step.created_by == user.id) & Step.assignee_id.is_(None)),
                                                   Step.done_at >= dt.datetime.combine(since, dt.time()))).all():
        days.add((t + dt.timedelta(hours=3)).date())
    return days


def streak(db: Session, user: User) -> dict:
    """Days in a row with learning or a closed step. Weekends do not break it when the person counts
    workdays only; two missed days a month are forgiven automatically (привал)."""
    st = state(db, user, create=False)
    workdays = st.workdays if st else True
    d0 = today()
    days = _active_days(db, user, d0 - dt.timedelta(days=400))
    n, rests, pending, d = 0, {}, {}, d0
    if d0 not in days:
        d -= dt.timedelta(days=1)  # today is not over yet
    while (d0 - d).days <= 400:
        if workdays and d.weekday() >= 5:
            d -= dt.timedelta(days=1)
            continue
        if d in days:
            n += 1
            for k, v in pending.items():  # a rest counts only when it bridges two active days
                rests[k] = rests.get(k, 0) + v
            pending = {}
        else:
            key = (d.year, d.month)
            if n == 0 or rests.get(key, 0) + pending.get(key, 0) >= 2:
                break
            pending[key] = pending.get(key, 0) + 1
        d -= dt.timedelta(days=1)
    week_start = d0 - dt.timedelta(days=d0.weekday())
    week = []
    for i in range(7):
        day = week_start + dt.timedelta(days=i)
        week.append({"label": "Пн Вт Ср Чт Пт Сб Вс".split()[i], "on": day in days, "today": day == d0,
                     "rest": workdays and i >= 5, "future": day > d0})
    used = rests.get((d0.year, d0.month), 0)
    return {"days": n, "today": d0 in days, "week": week, "rests_left": max(0, 2 - used)}


def minutes_today(db: Session, user: User) -> int:
    row = db.scalars(select(LearnDay).where(LearnDay.user_id == user.id, LearnDay.day == today())).first()
    return round((row.seconds or 0) / 60) if row else 0


# ---------- access ----------

def tier(db: Session, user: User) -> str:
    return access.tier(db, user)


def stage_open(db: Session, user: User, n: int) -> bool:
    return n <= course()["free_stages"] or tier(db, user) == access.PRO


def stage_reached(n: int, done: set[str], tests: dict[int, LearnMark]) -> bool:
    if n == 1:
        return True
    prev = stage(n - 1)
    return all(les["id"] in done for les in prev["lesson_list"]) or (tests.get(n - 1) and tests[n - 1].passed)


def lesson_open(db: Session, user: User, les: dict, done: set[str] | None = None,
                tests: dict | None = None, recommended: str | None = None) -> bool:
    if not les.get("ready") or not stage_open(db, user, les["stage"]):
        return False
    done = done_ids(db, user) if done is None else done
    tests = marks(db, user, "test") if tests is None else tests
    if les["id"] in done or les["id"] == recommended:
        return True
    st = stage(les["stage"])
    if les["no"] == 1:
        return stage_reached(les["stage"], done, tests)
    return st["lesson_list"][les["no"] - 2]["id"] in done


# ---------- skill signals from the plans (no AI) ----------

def signals(db: Session, user: User) -> dict[int, str]:
    out: dict[int, str] = {}
    plans = db.scalars(select(Plan).where(Plan.user_id == user.id).order_by(Plan.id.desc()).limit(5)).all()
    d0 = today()
    # 4. Постановка задач: steps without an owner or a date
    if plans:
        weak = sum(1 for p in plans if p.steps and sum(1 for s in p.steps if not (s.owner_name or s.assignee_id) or not s.due)
                   / len(p.steps) > 0.3)
        if weak >= 2:
            out[4] = f"В {weak} из {len(plans)} последних планов у шагов нет исполнителя или срока"
    # 6. Натиск: two or more overdue at once
    mine = (Step.assignee_id == user.id) | ((Step.created_by == user.id) & Step.assignee_id.is_(None))
    late = db.scalar(select(func.count(Step.id)).where(mine, Step.done.is_(False), Step.due < d0)) or 0
    if late >= 2:
        out[6] = f"Просрочено шагов сейчас: {late}"
    # 5. Быстрота: the first step closed later than 3 days after the plan
    slow = 0
    for p in plans:
        first = min((s.done_at for s in p.steps if s.done and s.done_at), default=None)
        age = (now() - p.created_at).days
        if (first and (first - p.created_at).days > 3) or (not first and age > 3):
            slow += 1
    if len(plans) >= 2 and slow * 2 > len(plans):
        out[5] = f"В {slow} из {len(plans)} планов первый шаг закрыт позже трёх дней"
    # 7. Полная победа: a plan without movement for 14 days
    stalled = 0
    for p in plans:
        open_steps = [s for s in p.steps if not s.done]
        last = max((s.done_at for s in p.steps if s.done_at), default=p.created_at)
        if open_steps and (now() - last).days > 14:
            stalled += 1
    if stalled:
        out[7] = f"Планов без движения больше двух недель: {stalled}"
    # 8. Обратная связь: under half of the due plans got a разбор после дела
    from .mentor import review_due

    sits = db.scalars(select(Situation).where(Situation.user_id == user.id, Situation.stage == "follow")).all()
    due = [s for s in sits if s.review == "done" or review_due(s)]
    if len(due) >= 2:
        reviewed = sum(1 for s in due if s.review == "done")
        if reviewed * 2 < len(due):
            out[8] = f"Итог подведён в {reviewed} из {len(due)} планов, где пора"
    # 2. Ясная цель: «частично» in 2 of the last 3 разборов после дела
    outs = db.scalars(select(Lesson.outcome).where(Lesson.user_id == user.id).order_by(Lesson.id.desc()).limit(3)).all()
    if len(outs) >= 2 and sum(1 for o in outs if o == "partial") >= 2:
        out[2] = f"Итог «частично» в {sum(1 for o in outs if o == 'partial')} из {len(outs)} последних разборов после дела"
    # 3. Глазомер: short answers to the mentor's questions
    texts = db.scalars(select(Message.text).join(Situation, Situation.id == Message.situation_id)
                       .where(Situation.user_id == user.id, Message.role == "user", Message.kind == "text")
                       .order_by(Message.id.desc()).limit(8)).all()
    if len(texts) >= 4 and sum(len(t) for t in texts) / len(texts) < 60:
        out[3] = "Ответы на вопросы наставника в среднем короче 60 знаков"
    # 1. Учение: plans but no итог for a month
    old = [p for p in plans if (now() - p.created_at).days > 14]
    recent_lessons = db.scalar(select(func.count(Lesson.id)).where(Lesson.user_id == user.id,
                                                                    Lesson.created_at > now() - dt.timedelta(days=30))) or 0
    if old and not recent_lessons:
        out[1] = "За месяц ни одного итога в «Людях и уроках»"
    return out


# ---------- levels, rank ----------

def orders_by_stage(ords: list[LearnOrder]) -> dict[int, int]:
    n: dict[int, int] = {}
    for o in ords:
        if o.outcome in ("done", "partial"):
            s = int(o.lesson_id.split(".")[0])
            n[s] = n.get(s, 0) + 1
    return n


def levels(db: Session, user: User, done: set[str], tests: dict, sigs: dict, ords: list[LearnOrder]) -> dict[int, int]:
    per = orders_by_stage(ords)
    out = {}
    for st in course()["stages"]:
        n = st["n"]
        lv = 0
        if st["lesson_list"] and all(les["id"] in done for les in st["lesson_list"]):
            lv = 1
        if tests.get(n) and tests[n].passed:
            lv = max(lv, 2)
        if per.get(n, 0) >= 1:
            lv = max(lv, 3)
        if per.get(n, 0) >= 3 and n not in sigs:
            lv = 4
        if per.get(n, 0) >= 5:
            lv = 5
        out[n] = lv
    return out


def rank(tests: dict, ords: list[LearnOrder]) -> int:
    """A rank for each stage in a row with its смотр passed and at least one of its orders carried out."""
    per = orders_by_stage(ords)
    r = 0
    for st in course()["stages"]:
        n = st["n"]
        if tests.get(n) and tests[n].passed and per.get(n, 0) >= 1:
            r += 1
        else:
            break
    return r


def rank_progress(tests: dict, ords: list[LearnOrder], done: set[str], r: int) -> dict:
    """What is left for the next rank: the lessons, the смотр and an order of stage r+1."""
    if r >= len(course()["stages"]):
        return {"next": None, "have": 3, "need": 3, "parts": []}
    st = stage(r + 1)
    per = orders_by_stage(ords)
    parts = [("Уроки этапа", all(les["id"] in done for les in st["lesson_list"])),
             ("Смотр этапа", bool(tests.get(st["n"]) and tests[st["n"]].passed)),
             ("Исполненный приказ этапа", per.get(st["n"], 0) >= 1)]
    return {"next": rank_name(r + 1), "stage": st, "have": sum(1 for _, ok in parts if ok), "need": 3, "parts": parts}


# ---------- what to do next ----------

def due_items(db: Session, user: User) -> list[LearnItem]:
    return db.scalars(select(LearnItem).where(LearnItem.user_id == user.id, LearnItem.due <= today())
                      .order_by(LearnItem.due, LearnItem.box)).all()


def assigned_stage(db: Session, user: User) -> LearnAssign | None:
    ids = db.scalars(select(Member.shtab_id).where(Member.user_id == user.id)).all()
    if not ids:
        return None
    return db.scalars(select(LearnAssign).where(LearnAssign.shtab_id.in_(ids)).order_by(LearnAssign.id.desc())).first()


def next_lesson_in_path(db: Session, user: User, done: set[str], tests: dict) -> dict | None:
    for st in course()["stages"]:
        for les in st["lesson_list"]:
            if les["id"] not in done:
                return les if les.get("ready") else None
    return None


def recommend(db: Session, user: User, done: set[str], tests: dict, sigs: dict, ords: list[LearnOrder]) -> dict:
    """The one next step, by rules (methodology, «Подбор и адаптация»)."""
    d0 = today()
    for o in ords:
        if not o.outcome and o.due <= d0:
            return {"kind": "report", "order": o, "why": "Срок приказа подошёл: отметьте, как прошло."}
    due = due_items(db, user)
    if len(due) >= 5:
        return {"kind": "review", "count": len(due), "why": f"Пора повторить: {len(due)} упражнений."}
    lv = levels(db, user, done, tests, sigs, ords)
    for n in sorted(sigs, key=lambda k: (lv.get(k, 0), k)):
        st = stage(n)
        les = next((x for x in st["lesson_list"] if x["id"] not in done and x.get("ready")), None)
        if les and stage_open(db, user, n):
            return {"kind": "lesson", "lesson": les, "why": f"Подобран по вашим разборам: {sigs[n][0].lower()}{sigs[n][1:]}."}
    asg = assigned_stage(db, user)
    if asg:
        st = stage(asg.stage)
        les = next((x for x in st["lesson_list"] if x["id"] not in done and x.get("ready")), None)
        if les and stage_open(db, user, asg.stage):
            return {"kind": "lesson", "lesson": les, "why": f"Этап «{st['name']}» назначил глава штаба."}
    les = next_lesson_in_path(db, user, done, tests)
    if les and stage_open(db, user, les["stage"]):
        return {"kind": "lesson", "lesson": les, "why": ""}
    for st in course()["stages"]:
        if all(x["id"] in done for x in st["lesson_list"]) and not (tests.get(st["n"]) and tests[st["n"]].passed):
            return {"kind": "test", "stage": st, "why": f"Уроки этапа «{st['name']}» пройдены: сдайте смотр."}
    if due:
        return {"kind": "review", "count": len(due), "why": f"Пора повторить: {len(due)} упражнений."}
    if les and not stage_open(db, user, les["stage"]):
        return {"kind": "locked", "lesson": les, "why": "Этап открыт на полном доступе."}
    if all(st["ready"] for st in course()["stages"]):
        return {"kind": "wait", "done": True, "why": "Все девять этапов пройдены. Повторяйте, исполняйте приказы и доделайте задания этапов."}
    return {"kind": "wait", "why": "Следующие этапы готовятся. Пока повторяйте и исполняйте приказы."}


def overview(db: Session, user: User) -> dict:
    """Everything the main page needs, in one place."""
    st = state(db, user)
    done = done_ids(db, user)
    tests = marks(db, user, "test")
    tasks = marks(db, user, "task")
    ords = orders(db, user)
    sigs = signals(db, user)
    lv = levels(db, user, done, tests, sigs, ords)
    r = rank(tests, ords)
    rec = recommend(db, user, done, tests, sigs, ords)
    rec_id = rec["lesson"]["id"] if rec.get("lesson") else None
    stages = []
    for s in course()["stages"]:
        steps = []
        for les in s["lesson_list"]:
            steps.append({"lesson": les, "done": les["id"] in done, "now": les["id"] == rec_id,
                          "open": lesson_open(db, user, les, done, tests, rec_id)})
        lessons_done = all(x["done"] for x in steps)
        t = tests.get(s["n"])
        status = "done" if (t and t.passed and lessons_done) else ("now" if any(x["now"] for x in steps) or (
            rec.get("stage") and rec["stage"]["n"] == s["n"]) else "")
        stages.append({"stage": s, "steps": steps, "test": t, "task": tasks.get(s["n"]), "status": status,
                       "open": stage_open(db, user, s["n"]), "reached": stage_reached(s["n"], done, tests),
                       "lessons_done": lessons_done})
    return {"state": st, "done": done, "tests": tests, "orders": ords, "signals": sigs, "levels": lv, "rank": r,
            "rank_name": rank_name(r), "rank_progress": rank_progress(tests, ords, done, r), "rec": rec,
            "stages": stages, "streak": streak(db, user), "minutes": minutes_today(db, user),
            "open_orders": [o for o in ords if not o.outcome], "due": len(due_items(db, user)),
            "done_count": len(done), "total": course()["total"], "xp_week": xp_week(db, user)}


def xp_week(db: Session, user: User) -> int:
    since = now() - dt.timedelta(days=7)
    n = sum(db.scalars(select(LearnDone.xp).where(LearnDone.user_id == user.id, LearnDone.created_at >= since)).all())
    n += sum(XP["order"] if o.outcome == "done" else XP["order_partial"]
             for o in db.scalars(select(LearnOrder).where(LearnOrder.user_id == user.id, LearnOrder.reported_at >= since,
                                                          LearnOrder.outcome.in_(("done", "partial")))).all())
    n += sum(XP["test"] for _ in db.scalars(select(LearnMark.id).where(LearnMark.user_id == user.id, LearnMark.kind == "test",
                                                                      LearnMark.passed.is_(True), LearnMark.created_at >= since)).all())
    return n


# ---------- exercises: player payload and grading ----------

def public_item(it: dict) -> dict:
    """What the browser gets: the answers too (grading is instant on the page; the server re-checks)."""
    out = {k: v for k, v in it.items() if k not in ("stage",)}
    if it["type"] in ("quote", "order", "match"):
        rnd = random.Random(it["id"])
        if it["type"] == "quote":
            chunks = list(it["chunks"])
            while True:
                rnd.shuffle(chunks)
                if chunks != it["chunks"] or len(chunks) < 2:
                    break
            out["shuffled"] = chunks
        elif it["type"] == "order":
            steps = list(it["steps"])
            while True:
                rnd.shuffle(steps)
                if steps != it["steps"] or len(steps) < 2:
                    break
            out["shuffled"] = steps
        else:
            rights = [p[1] for p in it["pairs"]]
            rnd.shuffle(rights)
            out["rights"] = rights
    return out


def schedule(db: Session, user: User, results: dict) -> tuple[int, int]:
    """Record first-try results {item_id: {"ok": bool, "pick": int|None}} on the repetition ladder."""
    right = 0
    items = course()["items"]
    for iid, res in results.items():
        if iid not in items:
            continue
        ok = bool(res.get("ok"))
        right += ok
        row = db.scalars(select(LearnItem).where(LearnItem.user_id == user.id, LearnItem.item_id == iid)).first()
        if row is None:
            row = LearnItem(user_id=user.id, item_id=iid, box=0, due=today())
            db.add(row)
        row.box = min(row.box + 1, len(LADDER) - 1) if ok else 0
        row.due = today() + dt.timedelta(days=LADDER[row.box] if ok else 1)
        row.right = (row.right or 0) + ok
        row.wrong = (row.wrong or 0) + (not ok)
        if isinstance(res.get("pick"), int):
            row.last_pick = res["pick"]
        row.updated_at = now()
    db.flush()
    return right, len([i for i in results if i in items])


def test_items(db: Session, user: User, n: int) -> list[dict]:
    """A смотр: up to 7 exercises of the stage and 3 of the stages before it, mixed."""
    rnd = random.Random(f"{user.id}-{n}-{today()}")
    own = [it for les in stage(n)["lesson_list"] for it in les.get("items", [])]
    prev = [it for s in course()["stages"][: n - 1] for les in s["lesson_list"] for it in les.get("items", [])]
    pick = rnd.sample(own, min(7, len(own))) + (rnd.sample(prev, min(3, len(prev))) if prev else [])
    if len(pick) < 10 and len(own) > 7:
        pick += rnd.sample([x for x in own if x not in pick], min(10 - len(pick), len(own) - 7))
    rnd.shuffle(pick)
    return pick


def review_items(db: Session, user: User, limit: int = 7) -> list[dict]:
    items = course()["items"]
    return [items[r.item_id] for r in due_items(db, user) if r.item_id in items][:limit]


# ---------- приказ ----------

def candidate_plans(db: Session, user: User) -> list[Plan]:
    sits = db.scalars(select(Situation).where(Situation.user_id == user.id, Situation.stage == "follow",
                                              Situation.review != "done").order_by(Situation.updated_at.desc())).all()
    return [s.plan for s in sits if s.plan][:6]


def plan_hint(p: Plan) -> str:
    no_owner = sum(1 for s in p.steps if not (s.owner_name or s.assignee_id))
    late = sum(1 for s in p.steps if s.late)
    if no_owner:
        return f"{no_owner} без исполнителя"
    if late:
        return f"{late} просрочено"
    return ""


def make_order(db: Session, user: User, les: dict, plan: Plan | None, if_text: str, then_text: str,
               due: dt.date, who: str) -> LearnOrder:
    title = les.get("order", {}).get("title") or les["title"]
    step_id = None
    if plan is not None:
        pos = max((s.position for s in plan.steps), default=0) + 1
        step = Step(plan_id=plan.id, position=pos, text=f"{title}: если {if_text.strip().rstrip(',')}, то {then_text.strip().rstrip('.')}.",
                    owner_name=who.strip()[:120] or "Я", kind="action", due=due, created_by=user.id)
        db.add(step)
        db.flush()
        step_id = step.id
    o = LearnOrder(user_id=user.id, lesson_id=les["id"], step_id=step_id, title=title[:200], if_text=if_text.strip()[:600],
                   then_text=then_text.strip()[:600], due=due)
    db.add(o)
    db.flush()
    return o


def order_situation(db: Session, o: LearnOrder) -> Situation | None:
    if not o.step_id:
        return None
    step = db.get(Step, o.step_id)
    if step and step.plan_id:
        plan = db.get(Plan, step.plan_id)
        return db.get(Situation, plan.situation_id) if plan else None
    return None


MENTOR_REPLY = {
    "done": "Приказ исполнен — это и есть суворовское правило в деле. Закрепите: в следующий раз сделайте так же без напоминания, а через неделю проверьте, держится ли.",
    "partial": "Частично — тоже результат: правило начало работать. Подумайте, что помешало сделать до конца, и поставьте на это один шаг.",
    "miss": "Не вышло — честный итог, с него и начинается учёба. Что помешало: не было случая, забыли или сделали иначе? Напишите — разберём.",
}


def report(db: Session, user: User, o: LearnOrder, outcome: str, note: str) -> int:
    """«Как прошло?»: the order's outcome, the step, a lesson in «Люди и уроки», experience."""
    if outcome not in OUTCOME_NAMES or o.outcome:
        return 0
    o.outcome = outcome
    o.note = note.strip()[:1000]
    o.reported_at = now()
    step = db.get(Step, o.step_id) if o.step_id else None
    if step and outcome == "done" and not step.done:
        step.done, step.done_at = True, now()
    sit = order_situation(db, o)
    les = lesson(o.lesson_id)
    text = f"Приказ «{o.title}»: {OUTCOME_NAMES[outcome].lower()}."
    if o.note:
        text += f" {o.note}"
    db.add(Lesson(user_id=user.id, situation_id=sit.id if sit else None,
                  outcome={"done": "win", "partial": "partial", "miss": "miss"}[outcome], text=text[:2000]))
    if sit:
        if o.note:
            db.add(Message(situation_id=sit.id, role="user", kind="text", text=f"{OUTCOME_NAMES[outcome]}. {o.note}"))
        db.add(Message(situation_id=sit.id, role="mentor", kind="text", text=MENTOR_REPLY[outcome],
                       data={"learn_order": o.id, "lesson": les["title"] if les else ""}))
        sit.updated_at = now()
    gained = XP["order"] if outcome == "done" else XP["order_partial"] if outcome == "partial" else 0
    add_xp(db, user, gained)
    touch(db, user)
    db.flush()
    return gained


# ---------- «своими словами» ----------

def words_feedback(db: Session, user: User, les: dict, text: str) -> dict:
    """On the full plan the light model compares the answer with the model answer (≈0.2 ₽, at most
    WORDS_PER_DAY a day); otherwise, or if the model fails, the person compares it themselves."""
    model_answer = les.get("words", {}).get("model", "")
    base = {"model": model_answer, "ai": False, "good": "", "add": ""}
    text = (text or "").strip()
    if len(text) < 10 or tier(db, user) != access.PRO:
        return base
    start = dt.datetime.combine(today(), dt.time()) - dt.timedelta(hours=3)
    used = db.scalar(select(func.count(Usage.id)).where(Usage.user_id == user.id, Usage.purpose == "learn",
                                                         Usage.created_at >= start)) or 0
    if used >= WORDS_PER_DAY:
        return base
    system = ("РЕЖИМ: УЧЕНИЕ. Ты наставник руководителя по «Науке побеждать» Суворова. Руководитель объяснил правило "
              "своими словами. Сравни с эталоном по смыслу, а не по словам. Пиши по-русски, коротко, на «вы», без оценок "
              "личности. Верни только JSON: {\"ok\": true|false, \"good\": \"что схвачено, одна фраза\", "
              "\"add\": \"чего не хватает, одна фраза или пусто\"}.")
    user_msg = (f"Урок: {les['title']}\nВопрос: {les['words']['prompt']}\nЭталон: {model_answer}\n"
                f"Ответ руководителя: {text[:600]}")
    try:
        reply = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
                         tier="light", temperature=0.2, max_tokens=160)
        db.add(Usage(user_id=user.id, model=reply.model, purpose="learn", tokens=reply.tokens,
                     tokens_in=reply.tokens_in, tokens_out=reply.tokens_out, cost_rub=llm.cost_rub(reply)))
        data = llm.parse_json(reply.text)
        base.update(ai=True, ok=bool(data.get("ok", True)), good=str(data.get("good", ""))[:300],
                    add=str(data.get("add", ""))[:300])
    except (llm.LLMError, ValueError, json.JSONDecodeError):
        pass
    return base


# ---------- diag ----------

def save_diag(db: Session, user: User, picks: dict[str, int]) -> dict[int, int]:
    scores: dict[int, int] = {}
    for i, q in enumerate(course()["diag"]):
        p = picks.get(str(i))
        if isinstance(p, int) and 0 <= p < len(q["options"]):
            scores[q["stage"]] = q["options"][p][1]
    st = state(db, user)
    st.diag = {str(k): v for k, v in scores.items()}
    st.diag_at = now()
    touch(db, user)
    return scores


def diag_map(db: Session, user: User) -> list[dict]:
    st = state(db, user)
    sigs = signals(db, user)
    scores = {int(k): v for k, v in (st.diag or {}).items()}
    rows = []
    for s in course()["stages"]:
        n = s["n"]
        sc = scores.get(n)
        sig = sigs.get(n)
        if sig or sc == 0:
            kind = "grow"
        elif sc == 2:
            kind = "strong"
        else:
            kind = "even"
        basis = []
        if sc is not None:
            basis.append({0: "в ситуации смотра выбран слабый ход", 1: "в ситуации смотра — ровный ответ",
                          2: "в ситуации смотра — сильный ход"}[sc])
        if sig:
            basis.append(sig[0].lower() + sig[1:])
        rows.append({"stage": s, "kind": kind, "basis": "; ".join(basis).capitalize() if basis else "Пока нет данных"})
    return rows
