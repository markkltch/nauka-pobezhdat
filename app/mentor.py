"""The mentor: a разбор по Суворову.

1. Глазомер — clarifying questions (light model), then the plan (strong model) opens with the
   situation as it really is, its root cause and the goal.
2. Быстрота — one concrete action for the first 24 hours.
3. Натиск — steps with owners and dates, risks with counter-moves, the sign of victory.
After the plan: talk preparation for any step, a follow-up conversation and a разбор после дела
(what happened, the lesson). People and lessons are remembered and used in the next разборы.

Each разбор has a hard money budget: before every call the server estimates the call's maximum
cost and refuses it if the разбор would go over (settings.budget_per_situation_rub). A part of the
budget is kept for the разбор после дела.

The model returns JSON; the server checks it, maps the principle to a verified quote
and stores the plan. A quote the model writes on its own is never shown.
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
from .models import LEVELS, Lesson, Message, Person, Plan, Situation, Step, Usage, User, now

log = logging.getLogger("mentor")
METHOD = Path(__file__).resolve().parent.parent / "method"

TALK_CONTEXT_CHARS = 9000
USER_TEXT_CHARS = 3000
FILE_TEXT_CHARS = 6000
REVIEW_RESERVE_RUB = 0.4  # kept for the разбор после дела
STEP_KINDS = {"action", "talk", "check"}
OUTCOMES = {"win": "Победа", "partial": "Частично", "miss": "Не вышло"}


class BudgetSpent(llm.LLMError):
    """This разбор has used its model budget."""


@lru_cache
def principles() -> str:
    return (METHOD / "principles.md").read_text(encoding="utf-8")


@lru_cache
def quotes() -> dict[str, dict]:
    return {q["id"]: q for q in json.loads((METHOD / "quotes.json").read_text(encoding="utf-8"))}


# ---------- memory ----------

def memory(db: Session, user: User) -> str:
    """What the mentor knows about this manager's team and past разборы, kept short."""
    parts = []
    if (user.team_note or "").strip():
        parts.append("О команде и деле (со слов руководителя): " + user.team_note.strip()[:600])
    people = db.scalars(select(Person).where(Person.user_id == user.id)
                        .order_by(Person.mentions.desc(), Person.updated_at.desc()).limit(12)).all()
    if people:
        lines = []
        for p in people:
            line = p.name + (f" — {p.role}" if p.role else "") + (f": {p.note}" if p.note else "")
            lines.append("- " + line[:160])
        parts.append("Люди команды:\n" + "\n".join(lines))
    lessons = db.scalars(select(Lesson).where(Lesson.user_id == user.id).order_by(Lesson.id.desc()).limit(5)).all()
    if lessons:
        parts.append("Уроки прошлых разборов:\n" + "\n".join(
            f"- [{OUTCOMES.get(l.outcome, '')}] {l.text[:160]}" for l in lessons))
    recent = db.scalars(select(Plan).where(Plan.user_id == user.id).order_by(Plan.id.desc()).limit(3)).all()
    if recent:
        parts.append("Недавние планы: " + "; ".join(p.title[:80] for p in recent))
    return "\n\n".join(parts)


def remember_people(db: Session, user: User, people: list) -> None:
    for item in people[:8]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()[:120]
        if len(name) < 2 or name.lower() in {"вы", "я", "руководитель"}:
            continue
        p = db.scalars(select(Person).where(Person.user_id == user.id, func.lower(Person.name) == name.lower())).first()
        if not p:
            p = Person(user_id=user.id, name=name, mentions=0)
            db.add(p)
        p.mentions = (p.mentions or 0) + 1
        role = str(item.get("role", "")).strip()[:160]
        if role and not p.role:
            p.role = role
        p.updated_at = now()


# ---------- prompts ----------

def _who(user: User) -> str:
    level = LEVELS.get(user.level, "Руководитель")
    role = f", {user.role}" if user.role else ""
    return f"{level}{role}"


def _system(user: User, mode: str, mem: str = "") -> str:
    today = dt.date.today().isoformat()
    head = (
        "Ты — наставник руководителей «Наука побеждать · Штаб». Помогаешь решать управленческие задачи по "
        "методу А. В. Суворова. Пиши по-русски, на «вы», коротко и конкретно.\n\n"
        f"Собеседник: {_who(user)}. Сегодня {today}.\n\n"
        f"МЕТОДИЧКА:\n{principles()}\n\n"
        f"Допустимые id цитат: {', '.join(quotes())}.\n\n"
    )
    if mem:
        head += f"ПАМЯТЬ (используй, если относится к делу; не пересказывай):\n{mem}\n\n"
    if mode == "questions":
        return head + (
            "РЕЖИМ: ГЛАЗОМЕР. Прочитай ситуацию и вложения. Если для хорошего плана не хватает важного (цель, "
            "кто решает, сроки, ставки, что уже пробовали, причина), задай 2–3 коротких вопроса — самые важные. "
            "Не спрашивай то, что уже известно из памяти. Если всего хватает — ready=true. "
            "Ответь ТОЛЬКО JSON:\n"
            '{"title": "суть ситуации в 3–6 словах", "ready": false, "questions": ["...", "..."]}'
        )
    if mode == "plan":
        return head + (
            "РЕЖИМ: ПЛАН. Сделай разбор по трём искусствам. Ответь ТОЛЬКО JSON:\n"
            "{"
            '"title": "План: ... (до 60 знаков)", '
            '"summary": "главная мысль плана в одном предложении", '
            '"scene": "глазомер: что происходит на самом деле, 1–2 предложения", '
            '"cause": "корень дела: почему это происходит (если не ясно — самая вероятная причина и как проверить)", '
            '"goal": "что будет победой и к какому сроку", '
            '"first_day": "быстрота: одно конкретное действие на ближайшие 24 часа", '
            '"steps": [{"text": "действие", "owner": "кто (имя, роль или Вы)", "due_in_days": 1, '
            '"kind": "action | talk | check"}], '
            '"risks": [{"risk": "что может пойти не так", "counter": "что делаем заранее"}], '
            '"victory": "по каким признакам поймём, что победили (измеримо)", '
            '"principle_id": "id цитаты из списка", '
            '"principle_why": "одно предложение: как принцип применяется здесь", '
            '"warning": "пусто или предупреждение о юридических и медицинских рисках", '
            '"people": [{"name": "имя конкретного человека из рассказа", "role": "его роль"}]'
            "}\n"
            "Шагов 3–5, последний — контрольная точка (kind=check). kind=talk — шаг-разговор. "
            "first_day — не повтор первого шага, а то, что можно сделать уже сегодня, чтобы шаги пошли "
            "(договориться о встрече, собрать факты, предупредить человека). Каждое поле — с заглавной буквы. "
            "Рисков 1–3. due_in_days — целое число дней от сегодня. people — только люди, названные по имени."
        )
    if mode == "prep":
        return head + (
            "РЕЖИМ: ПОДГОТОВКА РАЗГОВОРА. Подготовь руководителя к разговору из плана. Учитывай уровень, людей "
            "из памяти и методичку (сначала понять причину, твёрдо и справедливо, каждый понимает свой манёвр). "
            "Ответь ТОЛЬКО JSON:\n"
            '{"aim": "чего добиться разговором", "opening": "первая фраза дословно", '
            '"points": ["что сказать — 2–4 пункта"], '
            '"objections": [{"they": "возможное возражение", "you": "как ответить"}], '
            '"close": "как закончить: о чём договориться и к какому сроку"}'
        )
    if mode == "review":
        return head + (
            "РЕЖИМ: РАЗБОР ПОСЛЕ ДЕЛА. Руководитель рассказал, что вышло по плану. Оцени честно, без похвалы "
            "ради похвалы. Ответь ТОЛЬКО JSON:\n"
            '{"verdict": "win | partial | miss", "summary": "что вышло, 1–2 предложения", '
            '"lesson": "урок на будущее — одно предложение, применимое к следующим делам", '
            '"next": "что сделать дальше (пусто, если победа полная)"}'
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
        elif m.kind == "file":
            content = f"Вложение «{(m.data or {}).get('name', 'файл')}»:\n{m.text}"
        else:
            content = m.text
        out.append({"role": "user" if m.role == "user" else "assistant", "content": content})
    return out


def _trimmed(transcript: list[dict], limit: int) -> list[dict]:
    """Keep the latest messages within the limit; the plan itself always stays."""
    out, total = [], 0
    for m in reversed(transcript):
        if total + len(m["content"]) > limit and out:
            break
        out.append(m)
        total += len(m["content"])
    out.reverse()
    plan = next((m for m in transcript if m["content"].startswith("План: ")), None)
    if plan and plan not in out:
        out.insert(0, plan)
    return out


# ---------- budget ----------

def _record(db: Session, user: User, situation: Situation, reply: llm.Reply, purpose: str) -> None:
    db.add(Usage(user_id=user.id, situation_id=situation.id, model=reply.model, purpose=purpose,
                 tokens=reply.tokens, tokens_in=reply.tokens_in, tokens_out=reply.tokens_out,
                 cost_rub=llm.cost_rub(reply)))
    db.flush()


def spent(db: Session, situation: Situation) -> float:
    return float(db.scalar(select(func.coalesce(func.sum(Usage.cost_rub), 0))
                           .where(Usage.situation_id == situation.id)) or 0)


def _limit(situation: Situation, purpose: str) -> float:
    budget = get_settings().budget_per_situation_rub
    # the разбор после дела always has room; chat and preparation leave it some
    if purpose in {"talk", "prep"} and situation.review != "done":
        return budget - REVIEW_RESERVE_RUB
    return budget


def _fits(db: Session, situation: Situation, messages: list[dict], tier: str, max_tokens: int, purpose: str) -> bool:
    return spent(db, situation) + llm.max_cost_rub(messages, tier, max_tokens) <= _limit(situation, purpose)


def _call(db, user, situation, messages, tier, max_tokens, purpose, temperature=0.3) -> llm.Reply:
    """One model call inside the разбор budget. A plan that no longer fits the strong model
    is written by the light one; anything else over budget is refused before it costs money."""
    if not _fits(db, situation, messages, tier, max_tokens, purpose):
        if tier == "strong" and _fits(db, situation, messages, "light", max_tokens, purpose):
            tier = "light"
        else:
            raise BudgetSpent("Бюджет разбора исчерпан")
    reply = llm.chat(messages, tier=tier, temperature=temperature, max_tokens=max_tokens)
    _record(db, user, situation, reply, purpose)
    return reply


def _probe(user: User, mode: str) -> list[dict]:
    return [{"role": "system", "content": _system(user, mode, "x" * 2500)},
            {"role": "user", "content": "x" * (TALK_CONTEXT_CHARS + USER_TEXT_CHARS)}]


def can_talk(db: Session, user: User, situation: Situation) -> bool:
    """Is there room for one more follow-up answer in this разбор (at its largest)?"""
    s = get_settings()
    return (spent(db, situation) + llm.max_cost_rub(_probe(user, "talk"), "light", s.max_out_talk)
            <= _limit(situation, "talk"))


def can_prep(db: Session, user: User, situation: Situation) -> bool:
    return (spent(db, situation) + llm.max_cost_rub(_probe(user, "prep"), "light", get_settings().max_out_talk)
            <= _limit(situation, "prep"))


def _ask_json(db, user, situation, mode, tier, purpose, max_tokens, extra: list[dict] | None = None) -> dict:
    mem = memory(db, user)
    history = _transcript(situation) if mode in {"questions", "plan"} else _trimmed(_transcript(situation), TALK_CONTEXT_CHARS)
    messages = [{"role": "system", "content": _system(user, mode, mem)}, *history, *(extra or [])]
    for attempt in range(2):
        # a second try only fixes the format, so the cheap model does it
        reply = _call(db, user, situation, messages, tier if attempt == 0 else "light", max_tokens, purpose)
        try:
            return llm.parse_json(reply.text)
        except (ValueError, json.JSONDecodeError):
            log.warning("bad JSON from model (attempt %s): %s", attempt + 1, reply.text[:300])
            messages.append({"role": "assistant", "content": reply.text})
            messages.append({"role": "user", "content": "Ответь строго одним JSON-объектом по схеме, без текста вокруг."})
    raise llm.LLMError("Модель вернула ответ не в том виде")


# ---------- the разбор ----------

def _s(data: dict, key: str, limit: int) -> str:
    return str(data.get(key) or "").strip()[:limit]


def _make_plan(db: Session, user: User, situation: Situation) -> Plan:
    data = _ask_json(db, user, situation, "plan", "strong", "plan", get_settings().max_out_plan)
    steps = [s for s in data.get("steps", []) if isinstance(s, dict) and str(s.get("text", "")).strip()][:6]
    if not steps:
        raise llm.LLMError("В плане нет шагов")
    q = quotes().get(str(data.get("principle_id", "")))
    principle = {"quote": q["quote"], "source": q["source"], "why": _s(data, "principle_why", 300)} if q else None
    risks = [{"risk": str(r.get("risk", ""))[:300], "counter": str(r.get("counter", ""))[:300]}
             for r in data.get("risks", []) if isinstance(r, dict) and r.get("risk")][:3]
    plan = Plan(
        situation_id=situation.id, user_id=user.id,
        title=_s(data, "title", 200) or "План действий",
        summary=_s(data, "summary", 1000), goal=_s(data, "goal", 500),
        scene=_s(data, "scene", 800), cause=_s(data, "cause", 800), first_day=_s(data, "first_day", 500),
        risks=risks, victory=_s(data, "victory", 500),
        principle=principle, warning=_s(data, "warning", 500),
    )
    today = dt.date.today()
    for i, s in enumerate(steps):
        try:
            days = max(0, min(365, int(s.get("due_in_days", 0))))
        except (TypeError, ValueError):
            days = 0
        kind = str(s.get("kind", "action")).strip()
        plan.steps.append(Step(position=i, text=str(s["text"])[:500], owner_name=str(s.get("owner", ""))[:120],
                               kind=kind if kind in STEP_KINDS else "action",
                               due=today + dt.timedelta(days=days) if days else today, created_by=user.id))
    db.add(plan)
    db.flush()
    remember_people(db, user, data.get("people") or [])
    shown = {"title": plan.title, "summary": plan.summary, "scene": plan.scene, "cause": plan.cause,
             "goal": plan.goal, "first_day": plan.first_day, "victory": plan.victory,
             "steps": [{"text": s.text, "owner": s.owner_name, "due": s.due.isoformat()} for s in plan.steps]}
    situation.messages.append(Message(role="mentor", kind="plan", text=plan.summary, data=shown))
    situation.stage = "follow"
    return plan


def add_file(situation: Situation, name: str, text: str) -> None:
    situation.messages.append(Message(role="user", kind="file", text=text[:FILE_TEXT_CHARS],
                                      data={"name": name[:120], "chars": len(text)}))


def handle(db: Session, user: User, situation: Situation, text: str) -> None:
    """Take the person's message and add the mentor's answer (questions, a plan or a reply)."""
    if text.strip():
        situation.messages.append(Message(role="user", text=text.strip()[:USER_TEXT_CHARS]))
    situation.updated_at = now()
    db.flush()

    if situation.stage == "clarify":
        data = _ask_json(db, user, situation, "questions", "light", "questions", get_settings().max_out_questions)
        title = str(data.get("title", "")).strip()
        if title and situation.title == "Новая ситуация":
            situation.title = title[:160]
        questions = [str(q).strip() for q in data.get("questions", []) if str(q).strip()][:3]
        if questions and not data.get("ready"):
            situation.messages.append(Message(role="mentor", kind="questions",
                                              text="Глазомер: чтобы план был точным, уточню.", data={"questions": questions}))
            situation.stage = "plan"
        else:
            _make_plan(db, user, situation)
    elif situation.stage == "plan":
        _make_plan(db, user, situation)
    else:
        messages = [{"role": "system", "content": _system(user, "talk", memory(db, user))},
                    *_trimmed(_transcript(situation), TALK_CONTEXT_CHARS)]
        reply = _call(db, user, situation, messages, "light", get_settings().max_out_talk, "talk", temperature=0.5)
        situation.messages.append(Message(role="mentor", text=reply.text.strip()[:4000]))
    db.commit()


def prepare_talk(db: Session, user: User, step: Step) -> dict:
    """A script for a conversation step: aim, first phrase, points, objections, how to close."""
    situation = db.get(Situation, step.plan.situation_id)
    ask = [{"role": "user", "content": f"Подготовь меня к шагу плана: «{step.text}» (с кем: {step.owner_name or 'не указано'})."}]
    data = _ask_json(db, user, situation, "prep", "light", "prep", get_settings().max_out_talk, extra=ask)
    prep = {
        "aim": _s(data, "aim", 300), "opening": _s(data, "opening", 400),
        "points": [str(p)[:300] for p in data.get("points", []) if str(p).strip()][:4],
        "objections": [{"they": str(o.get("they", ""))[:300], "you": str(o.get("you", ""))[:400]}
                       for o in data.get("objections", []) if isinstance(o, dict) and o.get("they")][:3],
        "close": _s(data, "close", 400),
    }
    step.prep = prep
    db.commit()
    return prep


def review_due(situation: Situation) -> bool:
    """Time for a разбор после дела: all steps are done, or the last date has passed."""
    plan = situation.plan
    if not plan or situation.review == "done" or not plan.steps:
        return False
    if all(s.done for s in plan.steps):
        return True
    last = max((s.due for s in plan.steps if s.due), default=None)
    return bool(last and last < dt.date.today())


def review(db: Session, user: User, situation: Situation, outcome: str, text: str) -> Lesson:
    told = {"win": "Получилось.", "partial": "Получилось частично.", "miss": "Не получилось."}.get(outcome, "")
    note = (told + " " + text.strip()[:USER_TEXT_CHARS]).strip()
    situation.messages.append(Message(role="user", text="Разбор после дела. " + note))
    db.flush()
    data = _ask_json(db, user, situation, "review", "light", "review", get_settings().max_out_talk)
    verdict = str(data.get("verdict", outcome or "partial"))
    verdict = verdict if verdict in OUTCOMES else (outcome if outcome in OUTCOMES else "partial")
    lesson_text = _s(data, "lesson", 400) or _s(data, "summary", 400) or note[:400]
    lesson = Lesson(user_id=user.id, situation_id=situation.id, outcome=verdict, text=lesson_text)
    db.add(lesson)
    situation.messages.append(Message(role="mentor", kind="review", text=_s(data, "summary", 800),
                                      data={"verdict": verdict, "lesson": lesson_text, "next": _s(data, "next", 600)}))
    situation.review = "done"
    situation.updated_at = now()
    db.commit()
    return lesson
