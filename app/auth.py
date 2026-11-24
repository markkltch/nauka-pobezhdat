"""Sign-in by a one-time code (or link) sent to email; sessions in a cookie."""
from __future__ import annotations

import datetime as dt
import secrets

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import mailer
from .config import get_settings
from .db import get_db
from .models import AuthSession, LoginCode, User, now

COOKIE = "np_session"
CODE_TTL = dt.timedelta(minutes=15)
SESSION_TTL = dt.timedelta(days=30)
MAX_ATTEMPTS = 5


class LoginRequired(Exception):
    """Raised for pages that need a signed-in user; main.py turns it into a redirect."""


def normalize_email(email: str) -> str:
    return email.strip().lower()


def start_login(db: Session, email: str, next_url: str = "/app") -> None:
    email = normalize_email(email)
    recent = db.scalars(
        select(LoginCode).where(LoginCode.email == email, LoginCode.used.is_(False),
                                LoginCode.created_at > now() - dt.timedelta(minutes=1))
    ).first()
    if recent:  # one letter a minute per address; the code already sent still works
        return
    code = f"{secrets.randbelow(10**6):06d}"
    row = LoginCode(email=email, code=code, expires_at=now() + CODE_TTL)
    db.add(row)
    db.commit()
    link = f"{get_settings().base_url}/login/link/{row.link_token}?next={next_url}"
    mailer.send(
        email,
        f"Код входа: {code}",
        f"Ваш код для входа в «Наука побеждать»: {code}\n\n"
        f"Или откройте ссылку: {link}\n\n"
        "Код действует 15 минут. Если вы не запрашивали вход, просто удалите это письмо.",
    )


def _user_for(db: Session, email: str) -> User:
    user = db.scalars(select(User).where(User.email == email)).first()
    if not user:
        user = User(email=email)
        db.add(user)
        db.commit()
    return user


def check_code(db: Session, email: str, code: str) -> User | None:
    email = normalize_email(email)
    row = db.scalars(
        select(LoginCode)
        .where(LoginCode.email == email, LoginCode.used.is_(False), LoginCode.expires_at > now())
        .order_by(LoginCode.id.desc())
    ).first()
    if not row:
        return None
    row.attempts += 1
    if row.attempts > MAX_ATTEMPTS or not secrets.compare_digest(row.code, code.strip()):
        db.commit()
        return None
    row.used = True
    db.commit()
    return _user_for(db, email)


def check_link(db: Session, link_token: str) -> User | None:
    row = db.scalars(
        select(LoginCode).where(LoginCode.link_token == link_token, LoginCode.used.is_(False), LoginCode.expires_at > now())
    ).first()
    if not row:
        return None
    row.used = True
    db.commit()
    return _user_for(db, row.email)


def open_session(db: Session, user: User) -> AuthSession:
    sess = AuthSession(user_id=user.id, expires_at=now() + SESSION_TTL)
    db.add(sess)
    db.commit()
    return sess


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    sid = request.cookies.get(COOKIE)
    if not sid:
        return None
    sess = db.get(AuthSession, sid)
    if not sess or sess.expires_at < now():
        return None
    return db.get(User, sess.user_id)


def require_user(user: User | None = Depends(current_user)) -> User:
    if not user:
        raise LoginRequired()
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.email not in get_settings().admins:
        raise HTTPException(status_code=404)
    return user
