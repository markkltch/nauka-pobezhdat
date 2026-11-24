"""The mentor: clarify the situation (light model), then write a plan (strong model).

The model returns JSON; the server checks it, maps the principle to a verified quote
and stores the plan with its steps. A quote the model writes on its own is never shown.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import llm
from .config import get_settings
from .models import LEVELS, Message, Plan, Situation, Step, Usage, User, now

log = logging.getLogger("mentor")
METHOD = Path(__file__).resolve().parent.parent / "method"


@lru_cache
def principles() -> str:
    return (METHOD / "principles.md").read_text(encoding="utf-8")


@lru_cache
def quotes() -> dict[str, dict]:
    return {q["id"]: q for q in json.loads((METHOD / "quotes.json").read_text(encoding="utf-8"))}


def _who(user: User) -> str:
    level = LEVELS.get(user.level, "Руководитель")
    role = f", {user.role}" if user.role else ""
    return f"{level}{role}"


def _system(user: User, mode: str) -> str:
    today = dt.date.today().isoformat()
    head = (
        "Ты — наставник руководителей «Наука побеждать». Помогаешь решать управленческие задачи по методу "
        "А. В. Суворова. Пиши по-русски.\n\n"
        f"Собеседник: {_who(user)}. Сегодня {today}.\n\n"
        f"МЕТОДИЧКА:\n{principles()}\n\n"
        f"Допустимые id цитат: {', '.join(quotes())}.\n\n"
    )
    if mode == "questions":
        return head + (
            "РЕЖИМ: ВОПРОСЫ. Прочитай ситуацию. Если для хорошего плана не хватает важного (цель, кто решает, "
            "сроки, ставки, что уже пробовали), задай 2–3 коротких уточняющих вопроса. Если всего хватает — "
            "ready=true и вопросов нет. Ответь ТОЛЬКО JSON без пояснений:\n"
            '{"title": "суть ситуации в 3–6 словах", "ready": false, "questions": ["...", "..."]}'
        )
    if mode == "plan":
        return head + (
            "РЕЖИМ: ПЛАН. Составь план действий по методичке. Ответь ТОЛЬКО JSON без пояснений:\n"
            '{"title": "План: ... (до 60 знаков)", "summary": "1–2 предложения: главная мысль и почему так", '
            '"goal": "что будет победой и к какому сроку", '
            '"steps": [{"text": "действие", "owner": "кто (роль или Вы)", "due_in_days": 1}], '
            '"principle_id": "id цитаты из списка", "principle_why": "одно предложение, как принцип применяется здесь", '
            '"warning": "пусто или предупреждение о юридических, медицинских рисках"}\n'
            "Шагов 2–5. due_in_days — целое число дней от сегодня."
        )
    return head + (
        "РЕЖИМ: РАЗГОВОР. План уже составлен (он ниже в переписке). Отвечай на вопросы руководителя коротко, "
        "3–6 предложений, по методичке. Если просят изменить план — опиши изменения словами. Без цитат."
    )


def _transcript(situation: Situation) -> list[dict]:
    out = []
    for m in situation.messages:
        if m.kind == "plan" and m.data:
            content = "План: " + json.dumps(m.data, ensure_ascii=False)
        else:
            content = m.text
        out.append({"role": "user" if m.role == "user" else "assistant", "content": content})
    return out


def _record(db: Session, user: User, situation: Situation, reply: llm.Reply, tier: str, purpose: str) -> None:
    db.add(Usage(user_id=user.id, situation_id=situation.id, model=reply.model, purpose=purpose,
                 tokens=reply.tokens, cost_rub=llm.cost_rub(reply, tier)))


def plans_this_month(db: Session, user: User) -> int:
    start = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(select(func.count(Plan.id)).where(Plan.user_id == user.id, Plan.created_at >= start)) or 0


def limit_reached(db: Session, user: User) -> bool:
    return plans_this_month(db, user) >= get_settings().monthly_plan_limit


def _ask_json(db, user, situation, mode, tier, purpose) -> dict:
    messages = [{"role": "system", "content": _system(user, mode)}, *_transcript(situation)]
    for attempt in range(2):
        reply = llm.chat(messages, tier=tier, temperature=0.3)
        _record(db, user, situation, reply, tier, purpose)
        try:
            return llm.parse_json(reply.text)
        except (ValueError, json.JSONDecodeError):
            log.warning("bad JSON from model (attempt %s): %s", attempt + 1, reply.text[:300])
            messages.append({"role": "assistant", "content": reply.text})
            messages.append({"role": "user", "content": "Ответь строго одним JSON-объектом по схеме, без текста вокруг."})
    raise llm.LLMError("Модель вернула ответ не в том виде")


def _make_plan(db: Session, user: User, situation: Situation) -> Plan:
    data = _ask_json(db, user, situation, "plan", "strong", "plan")
    steps = [s for s in data.get("steps", []) if isinstance(s, dict) and str(s.get("text", "")).strip()][:6]
    if not steps:
        raise llm.LLMError("В плане нет шагов")
    q = quotes().get(str(data.get("principle_id", "")))
    principle = {"quote": q["quote"], "source": q["source"], "why": str(data.get("principle_why", ""))[:300]} if q else None
    plan = Plan(
        situation_id=situation.id, user_id=user.id,
        title=str(data.get("title") or "План действий")[:200],
        summary=str(data.get("summary", ""))[:1000], goal=str(data.get("goal", ""))[:500],
        principle=principle, warning=str(data.get("warning", ""))[:500],
    )
    today = dt.date.today()
    for i, s in enumerate(steps):
        try:
            days = max(0, min(365, int(s.get("due_in_days", 0))))
        except (TypeError, ValueError):
            days = 0
        plan.steps.append(Step(position=i, text=str(s["text"])[:500], owner_name=str(s.get("owner", ""))[:120],
                               due=today + dt.timedelta(days=days) if days else today, created_by=user.id))
    db.add(plan)
    db.flush()
    shown = {"title": plan.title, "summary": plan.summary, "goal": plan.goal,
             "steps": [{"text": s.text, "owner": s.owner_name, "due": s.due.isoformat()} for s in plan.steps]}
    situation.messages.append(Message(role="mentor", kind="plan", text=plan.summary, data=shown))
    situation.stage = "follow"
    return plan


def handle(db: Session, user: User, situation: Situation, text: str) -> None:
    """Take the person's message and add the mentor's answer (questions, a plan or a reply)."""
    situation.messages.append(Message(role="user", text=text.strip()[:6000]))
    situation.updated_at = now()
    db.flush()

    if situation.stage == "clarify":
        data = _ask_json(db, user, situation, "questions", "light", "questions")
        title = str(data.get("title", "")).strip()
        if title and situation.title == "Новая ситуация":
            situation.title = title[:160]
        questions = [str(q).strip() for q in data.get("questions", []) if str(q).strip()][:3]
        if questions and not data.get("ready"):
            situation.messages.append(Message(role="mentor", kind="questions",
                                              text="Чтобы план был точным, уточню:", data={"questions": questions}))
            situation.stage = "plan"
        else:
            _make_plan(db, user, situation)
    elif situation.stage == "plan":
        _make_plan(db, user, situation)
    else:
        messages = [{"role": "system", "content": _system(user, "talk")}, *_transcript(situation)]
        reply = llm.chat(messages, tier="strong", temperature=0.5)
        _record(db, user, situation, reply, "strong", "talk")
        situation.messages.append(Message(role="mentor", text=reply.text.strip()[:4000]))
    db.commit()
