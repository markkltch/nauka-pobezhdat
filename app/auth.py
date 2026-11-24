"""Sign-in by a one-time code (or link) sent to email; sessions in a cookie."""
from __future__ import annotations

import datetime as dt
import hashlib
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


def start_login(db: Session, email: str, next_url: str = "/app", name: str = "", password: str = "") -> None:
    """Mails a one-time code. With name/password it is a sign-up: they are kept with the code and
    reach the account only once the code proves the address."""
    email = normalize_email(email)
    pending_pw = hash_password(password) if password else ""
    recent = db.scalars(
        select(LoginCode).where(LoginCode.email == email, LoginCode.used.is_(False),
                                LoginCode.created_at > now() - dt.timedelta(minutes=1))
    ).first()
    if recent:  # one letter a minute per address; the code already sent still works
        if name or pending_pw:
            recent.pending_name, recent.pending_password = name[:120], pending_pw
            db.commit()
        return
    code = f"{secrets.randbelow(10**6):06d}"
    row = LoginCode(email=email, code=code, expires_at=now() + CODE_TTL, pending_name=name[:120],
                    pending_password=pending_pw)
    db.add(row)
    db.commit()
    link = f"{get_settings().base_url}/login/link/{row.link_token}?next={next_url}"
    if name or pending_pw:
        subject, lead = f"Подтвердите почту: {code}", "Код для подтверждения регистрации"
        tail = "Если вы не регистрировались, просто удалите это письмо."
    else:
        subject, lead = f"Код входа: {code}", "Ваш код для входа"
        tail = "Если вы не запрашивали вход, просто удалите это письмо."
    mailer.send(
        email,
        subject,
        f"{lead} в «Наука побеждать · Штаб»: {code}\n\n"
        f"Или откройте ссылку: {link}\n\n"
        f"Код действует 15 минут. {tail}",
    )


def _apply_pending(db: Session, user: User, row: LoginCode) -> User:
    if row.pending_name and not user.name:
        user.name = row.pending_name
    if row.pending_password and not user.password_hash:
        user.password_hash = row.pending_password
        user.failed_logins, user.locked_until = 0, None
    db.commit()
    return user


def account_exists(db: Session, email: str) -> bool:
    return db.scalars(select(User.id).where(User.email == normalize_email(email))).first() is not None


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
    return _apply_pending(db, _user_for(db, email), row)


def check_link(db: Session, link_token: str) -> User | None:
    row = db.scalars(
        select(LoginCode).where(LoginCode.link_token == link_token, LoginCode.used.is_(False), LoginCode.expires_at > now())
    ).first()
    if not row:
        return None
    row.used = True
    db.commit()
    return _apply_pending(db, _user_for(db, row.email), row)


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


# ---------- passwords (scrypt from the standard library; no extra packages on the hosting) ----------

PW_MIN = 8
LOCK_AFTER = 5
LOCK_FOR = dt.timedelta(minutes=15)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return secrets.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str:
    if len(password) < PW_MIN:
        return f"Пароль короче {PW_MIN} символов"
    if password.isdigit() or password.isalpha():
        return "Добавьте в пароль и буквы, и цифры"
    return ""


def check_password(db: Session, email: str, password: str) -> tuple[User | None, str]:
    """Returns (user, error). Five wrong passwords lock the account for 15 minutes."""
    user = db.scalars(select(User).where(User.email == normalize_email(email))).first()
    if not user or not user.password_hash:
        return None, "Неверная почта или пароль. Если пароль не задан — войдите по коду и задайте его в настройках."
    if user.locked_until and user.locked_until > now():
        return None, "Слишком много попыток. Подождите 15 минут или войдите по коду из письма."
    if not _verify(password, user.password_hash):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= LOCK_AFTER:
            user.locked_until, user.failed_logins = now() + LOCK_FOR, 0
        db.commit()
        return None, "Неверная почта или пароль."
    user.failed_logins, user.locked_until = 0, None
    db.commit()
    return user, ""


def end_other_sessions(db: Session, user: User, keep: str | None) -> int:
    rows = db.scalars(select(AuthSession).where(AuthSession.user_id == user.id)).all()
    n = 0
    for r in rows:
        if r.id != keep:
            db.delete(r)
            n += 1
    db.commit()
    return n
