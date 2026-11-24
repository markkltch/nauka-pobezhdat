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
    kind: Mapped[str] = mapped_column(String(12), default="text")  # text | questions | plan
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


class Usage(Base):
    """One call to a language model: tokens and cost, for the unit economics."""

    __tablename__ = "usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    situation_id: Mapped[int | None] = mapped_column(ForeignKey("situations.id"))
    model: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(24))
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_rub: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)
