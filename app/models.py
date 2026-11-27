"""Tables."""
from __future__ import annotations

import datetime as dt
import secrets

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def token(n: int = 24) -> str:
    return secrets.token_urlsafe(n)


LEVELS = {"line": "Линейный руководитель", "mid": "Руководитель среднего звена", "top": "Топ-менеджер"}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(160), default="")  # должность, своими словами
    level: Mapped[str] = mapped_column(String(8), default="")  # line | mid | top
    consent_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    ip_consent_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    extra_plans: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # bought разборы left
    # sign-in
    password_hash: Mapped[str] = mapped_column(String(200), default="", server_default="")
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime)
    yandex_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # settings
    team_note: Mapped[str] = mapped_column(Text, default="", server_default="")  # о команде и деле, для наставника
    remind_steps: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    morning_report: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    report_sent_on: Mapped[dt.date | None] = mapped_column(Date)
    voice_month: Mapped[str] = mapped_column(String(7), default="", server_default="")
    voice_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    @property
    def initials(self) -> str:
        parts = [p for p in (self.name or self.email).replace(".", " ").split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    @property
    def display_name(self) -> str:
        return self.name or self.email.split("@")[0]


class LoginCode(Base):
    __tablename__ = "login_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), index=True)
    code: Mapped[str] = mapped_column(String(6))
    link_token: Mapped[str] = mapped_column(String(64), default=token, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    # registration: applied to the account only after the address is confirmed
    pending_name: Mapped[str] = mapped_column(String(120), default="", server_default="")
    pending_password: Mapped[str] = mapped_column(String(255), default="", server_default="")
    pending_ip_consent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class AuthSession(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: token(32))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)


class Situation(Base):
    """One conversation with the mentor (a 'разбор')."""

    __tablename__ = "situations"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(160), default="Новая ситуация")
    stage: Mapped[str] = mapped_column(String(16), default="clarify")  # clarify | plan | follow
    review: Mapped[str] = mapped_column(String(12), default="", server_default="")  # "" | due | done
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    messages: Mapped[list[Message]] = relationship(order_by="Message.id", cascade="all, delete-orphan")
    plans: Mapped[list[Plan]] = relationship(order_by="Plan.id", cascade="all, delete-orphan")

    @property
    def plan(self) -> Plan | None:
        return self.plans[-1] if self.plans else None


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    situation_id: Mapped[int] = mapped_column(ForeignKey("situations.id"), index=True)
    role: Mapped[str] = mapped_column(String(12))  # user | mentor
    kind: Mapped[str] = mapped_column(String(12), default="text")  # text | questions | plan | file | review
    text: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    situation_id: Mapped[int] = mapped_column(ForeignKey("situations.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    goal: Mapped[str] = mapped_column(Text, default="")
    # Суворовский разбор: глазомер (обстановка), быстрота (первые сутки), натиск (шаги, риски, победа)
    scene: Mapped[str] = mapped_column(Text, default="", server_default="")  # обстановка
    cause: Mapped[str] = mapped_column(Text, default="", server_default="")  # корень дела
    first_day: Mapped[str] = mapped_column(Text, default="", server_default="")  # что сделать в первые 24 часа
    risks: Mapped[list | None] = mapped_column(JSON)  # [{"risk", "counter"}]
    victory: Mapped[str] = mapped_column(Text, default="", server_default="")  # по чему поймём, что победили
    principle: Mapped[dict | None] = mapped_column(JSON)  # {"quote", "source", "why"}
    warning: Mapped[str] = mapped_column(Text, default="")
    sent_to_shtab_id: Mapped[int | None] = mapped_column(ForeignKey("shtabs.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    steps: Mapped[list[Step]] = relationship(back_populates="plan", order_by="Step.position", cascade="all, delete-orphan")


class Step(Base):
    """A step of a plan; the same row is the task once the plan is sent to a shtab."""

    __tablename__ = "steps"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), index=True)
    shtab_id: Mapped[int | None] = mapped_column(ForeignKey("shtabs.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    owner_name: Mapped[str] = mapped_column(String(120), default="")
    kind: Mapped[str] = mapped_column(String(12), default="action", server_default="action")  # action | talk | check
    prep: Mapped[dict | None] = mapped_column(JSON)  # подготовка разговора
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    due: Mapped[dt.date | None] = mapped_column(Date)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    done_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    reminded_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    plan: Mapped[Plan | None] = relationship(back_populates="steps")
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])

    @property
    def late(self) -> bool:
        return bool(self.due and not self.done and self.due < dt.date.today())


class Person(Base):
    """Someone from the manager's team: the mentor remembers them across разборы."""

    __tablename__ = "people"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(160), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    mentions: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Lesson(Base):
    """What a разбор после дела taught: the mentor uses it next time."""

    __tablename__ = "lessons"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    situation_id: Mapped[int | None] = mapped_column(ForeignKey("situations.id"))
    outcome: Mapped[str] = mapped_column(String(12), default="")  # win | partial | miss
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Shtab(Base):
    __tablename__ = "shtabs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    about: Mapped[str] = mapped_column(String(240), default="")
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    invite_token: Mapped[str] = mapped_column(String(64), default=token, unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    members: Mapped[list[Member]] = relationship(cascade="all, delete-orphan", order_by="Member.id")
    tasks: Mapped[list[Step]] = relationship(foreign_keys=[Step.shtab_id], order_by="Step.due")


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("shtab_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shtab_id: Mapped[int] = mapped_column(ForeignKey("shtabs.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(120), default="")  # роль в штабе
    joined_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    user: Mapped[User] = relationship()


class Subscription(Base):
    """A paid period for a person (seats=1) or a whole shtab (seats=members)."""

    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    payer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    shtab_id: Mapped[int | None] = mapped_column(ForeignKey("shtabs.id"), index=True)
    seats: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | active | canceled
    paid_until: Mapped[dt.datetime | None] = mapped_column(DateTime)
    renew_reminded_at: Mapped[dt.datetime | None] = mapped_column(DateTime)  # the «доступ заканчивается» letter
    payment_method_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String(64), unique=True)
    amount_rub: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class PackOrder(Base):
    """A one-off purchase of extra разборов."""

    __tablename__ = "pack_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plans: Mapped[int] = mapped_column(Integer)
    amount_rub: Mapped[float] = mapped_column(Float)
    provider_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    credited: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Usage(Base):
    """One call to a language model: tokens and cost, for the unit economics."""

    __tablename__ = "usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    situation_id: Mapped[int | None] = mapped_column(ForeignKey("situations.id"))
    model: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(24))
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_rub: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)


# ---------- Учение: the learning section ----------

class LearnState(Base):
    """One per person who opened «Учение»: experience, the daily goal, the entrance review."""

    __tablename__ = "learn_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    xp: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    goal_min: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    workdays: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    in_report: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    diag: Mapped[dict | None] = mapped_column(JSON)  # {"1": 0..2, ...} per stage
    diag_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class LearnDay(Base):
    """A day with learning in it (lesson, review, test, report): the streak is counted from these."""

    __tablename__ = "learn_days"
    __table_args__ = (UniqueConstraint("user_id", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    day: Mapped[dt.date] = mapped_column(Date)
    seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class LearnDone(Base):
    """A finished lesson (it can be repeated: the latest row counts)."""

    __tablename__ = "learn_done"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    lesson_id: Mapped[str] = mapped_column(String(8), index=True)
    right: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    seconds: Mapped[int] = mapped_column(Integer, default=0)
    xp: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class LearnItem(Base):
    """Spaced repetition of one exercise for one person: box 0..5 on the 1-3-7-21-60 ladder."""

    __tablename__ = "learn_items"
    __table_args__ = (UniqueConstraint("user_id", "item_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(String(16))
    box: Mapped[int] = mapped_column(Integer, default=0)
    due: Mapped[dt.date] = mapped_column(Date)
    right: Mapped[int] = mapped_column(Integer, default=0)
    wrong: Mapped[int] = mapped_column(Integer, default=0)
    last_pick: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class LearnOrder(Base):
    """«Приказ на неделю»: a lesson applied to a real plan, as an if-then step with a due date."""

    __tablename__ = "learn_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    lesson_id: Mapped[str] = mapped_column(String(8))
    step_id: Mapped[int | None] = mapped_column(ForeignKey("steps.id"))
    title: Mapped[str] = mapped_column(String(200))
    if_text: Mapped[str] = mapped_column(Text, default="")
    then_text: Mapped[str] = mapped_column(Text, default="")
    due: Mapped[dt.date] = mapped_column(Date)
    outcome: Mapped[str] = mapped_column(String(8), default="", server_default="")  # "" | done | partial | miss
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    reported_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class LearnMark(Base):
    """A stage's review (смотр: score of total) or its big task (задание этапа)."""

    __tablename__ = "learn_marks"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # test | task
    stage: Mapped[int] = mapped_column(Integer)
    score: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class LearnAssign(Base):
    """A shtab head gives the team a stage, with a deadline."""

    __tablename__ = "learn_assign"
    id: Mapped[int] = mapped_column(primary_key=True)
    shtab_id: Mapped[int] = mapped_column(ForeignKey("shtabs.id"), index=True)
    stage: Mapped[int] = mapped_column(Integer)
    due: Mapped[dt.date | None] = mapped_column(Date)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class LearnNote(Base):
    """Praise or a nudge from the shtab head, shown once in the member's «Учение»."""

    __tablename__ = "learn_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    from_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    to_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    text: Mapped[str] = mapped_column(String(300))
    seen: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    sender: Mapped[User] = relationship(foreign_keys=[from_id])
