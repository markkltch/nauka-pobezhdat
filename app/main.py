"""Web routes."""
from __future__ import annotations

import datetime as dt
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from . import access, auth, billing, emails, files, jobs, learn, legal, llm, mentor, speech, tbank, yandex_id
from .config import get_settings
from .db import get_db, init_db
from .models import (LEVELS, LearnAssign, LearnDone, LearnMark, LearnNote, LearnOrder, Lesson, Member, Message, PackOrder, Payment,
                     Person, Plan, Shtab, Situation, Step, Subscription, Usage, User, now)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
HERE = Path(__file__).resolve().parent


init_db()  # also here: the WSGI wrapper on shared hosting may skip ASGI lifespan events


@asynccontextmanager
async def lifespan(_app):
    s = get_settings()
    if s.run_jobs_in_process and (s.llm_provider != "fake" or s.smtp_host):
        jobs.start()
    yield


app = FastAPI(title="Наука побеждать", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(LEVELS=LEVELS, settings=get_settings, yandex_id_enabled=yandex_id.enabled,
                             OUTCOMES=mentor.OUTCOMES, BRAND="Наука побеждать · Штаб")


def _ru_date(d: dt.date | None) -> str:
    if not d:
        return ""
    months = ["янв.", "февр.", "марта", "апр.", "мая", "июня", "июля", "авг.", "сент.", "окт.", "нояб.", "дек."]
    return f"{d.day} {months[d.month - 1]}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = int(n)
    k = n % 100
    word = many if 11 <= k <= 14 else one if n % 10 == 1 else few if n % 10 in (2, 3, 4) else many
    return f"{n} {word}"


templates.env.filters["ru_date"] = _ru_date
# times are stored in UTC; people read them in Moscow time
templates.env.filters["msk"] = lambda d: d.replace(tzinfo=dt.timezone.utc).astimezone(ZoneInfo("Europe/Moscow")) if d else d
templates.env.filters["plural"] = _plural
templates.env.filters["initials"] = lambda name: "".join(w[0] for w in (name or "?").split()[:2]).upper() or "?"
templates.env.filters["q"] = lambda t: t if str(t).startswith("«") else f"«{t}»"
templates.env.filters["rub"] = lambda n: f"{int(n):,}".replace(",", "\u00a0") + "\u00a0₽"


def _money(n) -> str:
    """1 234,50 ₽ — kopecks only when there are any."""
    n = round(float(n or 0), 2)
    whole = f"{abs(int(n)):,}".replace(",", "\u00a0")
    kop = round(abs(n) * 100) % 100
    return ("−" if n < 0 else "") + whole + (f",{kop:02d}" if kop else "") + "\u00a0₽"


templates.env.filters["money"] = _money


@app.exception_handler(auth.LoginRequired)
def _to_login(request: Request, exc: auth.LoginRequired):
    return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)


@app.exception_handler(auth.ConsentRequired)
def _to_consent(request: Request, exc: auth.ConsentRequired):
    return see_other("/privacy/confirm?next=" + quote(request.url.path, safe=""))


def page(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, {**legal.current(), **ctx})


def auth_token() -> str:
    import secrets

    return secrets.token_urlsafe(24)


def safe_next(url: str | None) -> str:
    return url if url and url.startswith("/") and not url.startswith(("//", "/\\")) else "/app"


def see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------- public ----------

@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return page(request, "landing.html")


@app.get("/payment", response_class=HTMLResponse)
def payment_terms(request: Request):
    return page(request, "legal_payment.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/offer", response_class=HTMLResponse)
def offer(request: Request):
    return page(request, "legal_offer.html")


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return page(request, "legal_privacy.html")


@app.get("/consent", response_class=HTMLResponse)
def consent(request: Request):
    return page(request, "legal_consent.html")


@app.get("/privacy/confirm", response_class=HTMLResponse)
def consent_form(request: Request, next: str = "/app", user: User = Depends(auth.require_session)):
    if user.ip_consent_at or get_settings().personal_data_operator != "ip":
        return see_other(safe_next(next))
    return page(request, "privacy_confirm.html", next=safe_next(next))


@app.post("/privacy/confirm")
def consent_save(request: Request, next: str = Form("/app"), consent: str = Form(""),
                 user: User = Depends(auth.require_session), db: Session = Depends(get_db)):
    if get_settings().personal_data_operator != "ip":
        return see_other(safe_next(next))
    if consent != "yes":
        return page(request, "privacy_confirm.html", next=safe_next(next), error=True)
    if not user.ip_consent_at:
        user.ip_consent_at = now()
        db.commit()
    return see_other(safe_next(next))


# ---------- sign-in ----------

LOGIN_ERRORS = {
    "consent": "Чтобы зарегистрироваться через Яндекс, отметьте согласие на обработку данных.",
    "failed": "Не получилось войти через Яндекс. Попробуйте ещё раз или войдите по почте.",
    "noacc": "Аккаунта с этим Яндекс ID ещё нет. Зарегистрируйтесь — это займёт минуту.",
}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/app", mode: str = "password", yerr: str = "", email: str = "",
               user: User | None = Depends(auth.current_user)):
    if user:
        return see_other(safe_next(next))
    return page(request, "login.html", next=safe_next(next), tab="login",
                step="email" if mode == "code" else "password", error=LOGIN_ERRORS.get(yerr, ""), email=email)


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request, next: str = "/app", yerr: str = "", email: str = "",
                  user: User | None = Depends(auth.current_user)):
    if user:
        return see_other(safe_next(next))
    return page(request, "login.html", next=safe_next(next), tab="register", step="register",
                error=LOGIN_ERRORS.get(yerr, ""), email=email)


@app.post("/register", response_class=HTMLResponse)
def register_send(request: Request, email: str = Form(...), name: str = Form(""), password: str = Form(""),
                  consent: str = Form(""), next: str = Form("/app"), db: Session = Depends(get_db)):
    email, name = auth.normalize_email(email), name.strip()
    again = dict(next=safe_next(next), tab="register", step="register", email=email, full_name=name)
    if "@" not in email or "." not in email.split("@")[-1]:
        return page(request, "login.html", error="Проверьте адрес почты", **again)
    if not name:
        return page(request, "login.html", error="Напишите, как к вам обращаться", **again)
    if problem := auth.password_problem(password):
        return page(request, "login.html", error=problem, **again)
    if not consent:
        return page(request, "login.html", error="Без согласия на обработку данных зарегистрироваться нельзя", **again)
    if auth.account_exists(db, email):
        return page(request, "login.html", exists=True, **again)
    auth.start_login(db, email, safe_next(next), name=name, password=password,
                     ip_consent=get_settings().personal_data_operator == "ip")
    return page(request, "login.html", next=safe_next(next), tab="register", step="code", email=email)


@app.post("/login", response_class=HTMLResponse)
def login_send(request: Request, email: str = Form(...), consent: str = Form(""), next: str = Form("/app"),
               db: Session = Depends(get_db)):
    """Code by mail. An existing account just gets a code; a new address needs the consent box
    (sign-up without a password)."""
    email = auth.normalize_email(email)
    again = dict(next=safe_next(next), tab="login", step="email", email=email)
    if "@" not in email or "." not in email.split("@")[-1]:
        return page(request, "login.html", error="Проверьте адрес почты", **again)
    exists = auth.account_exists(db, email)
    if not exists and not consent:
        return page(request, "login.html", noacc=True, **again)
    auth.start_login(db, email, safe_next(next),
                     ip_consent=bool(not exists and consent and get_settings().personal_data_operator == "ip"))
    return page(request, "login.html", next=safe_next(next), tab="login", step="code", email=email)


def _signed_in(db: Session, user: User, next_url: str) -> RedirectResponse:
    if not user.consent_at and get_settings().personal_data_operator == "ogi":
        user.consent_at = now()
        db.commit()
    sess = auth.open_session(db, user)
    target = "/onboarding?next=" + quote(next_url) if not user.level else next_url
    if get_settings().personal_data_operator == "ip" and not user.ip_consent_at:
        target = "/privacy/confirm?next=" + quote(target, safe="")
    resp = see_other(target)
    secure = get_settings().base_url.startswith("https://")
    resp.set_cookie(auth.COOKIE, sess.id, max_age=int(auth.SESSION_TTL.total_seconds()), httponly=True,
                    samesite="lax", secure=secure)
    return resp


@app.post("/login/code", response_class=HTMLResponse)
def login_code(request: Request, email: str = Form(...), code: str = Form(...), next: str = Form("/app"),
               tab: str = Form("login"), db: Session = Depends(get_db)):
    user = auth.check_code(db, email, code)
    if not user:
        return page(request, "login.html", next=safe_next(next), step="code", email=email, tab=tab,
                    error="Код не подошёл или устарел. Проверьте письмо или запросите новый.")
    return _signed_in(db, user, safe_next(next))


@app.get("/login/link/{link_token}")
def login_link(link_token: str, next: str = "/app", db: Session = Depends(get_db)):
    user = auth.check_link(db, link_token)
    if not user:
        return see_other("/login?expired=1")
    return _signed_in(db, user, safe_next(next))


@app.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    sid = request.cookies.get(auth.COOKIE)
    if sid and (row := db.get(auth.AuthSession, sid)):
        db.delete(row)
        db.commit()
    resp = see_other("/")
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/onboarding", response_class=HTMLResponse)
def onboarding_form(request: Request, next: str = "/app", user: User = Depends(auth.require_user)):
    return page(request, "onboarding.html", user=user, next=safe_next(next))


@app.post("/onboarding")
def onboarding_save(name: str = Form(""), role: str = Form(""), level: str = Form(...), next: str = Form("/app"),
                    user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if level not in LEVELS:
        raise HTTPException(400)
    user = db.merge(user)
    user.name, user.role, user.level = name.strip()[:120], role.strip()[:160], level
    db.commit()
    return see_other(safe_next(next))


@app.post("/login/password", response_class=HTMLResponse)
def login_password(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/app"),
                   db: Session = Depends(get_db)):
    user, error = auth.check_password(db, email, password)
    if not user:
        return page(request, "login.html", next=safe_next(next), tab="login", step="password", email=email, error=error)
    return _signed_in(db, user, safe_next(next))


@app.get("/auth/yandex")
def yandex_start(next: str = "/app", consent: str = "", link: str = "",
                 user: User | None = Depends(auth.current_user)):
    if not yandex_id.enabled():
        return see_other("/login")
    state = auth_token()
    mode = "link" if (link and user) else ("register-" + get_settings().personal_data_operator if consent else "login")
    resp = see_other(yandex_id.authorize_url(state))
    resp.set_cookie("ya_state", f"{state}|{mode}|{safe_next(next)}", max_age=600, httponly=True, samesite="lax",
                    secure=get_settings().base_url.startswith("https://"))
    return resp


@app.get("/auth/yandex/callback")
def yandex_callback(request: Request, code: str = "", state: str = "", error: str = "",
                    user: User | None = Depends(auth.current_user), db: Session = Depends(get_db)):
    saved = (request.cookies.get("ya_state") or "").split("|", 2)
    if error or len(saved) != 3 or not code or saved[0] != state:
        return see_other("/login?yerr=failed")
    _, mode, next_url = saved
    try:
        prof = yandex_id.profile(code)
    except yandex_id.YandexIdError:
        logging.getLogger("yandex_id").exception("Yandex ID sign-in failed")
        return see_other("/login?yerr=failed")
    if mode == "link" and user:
        if get_settings().personal_data_operator == "ip" and not user.ip_consent_at:
            return see_other("/privacy/confirm?next=/settings")
        other = db.scalars(select(User).where(User.yandex_id == prof["id"], User.id != user.id)).first()
        if other:
            return see_other("/settings?msg=ya_taken")
        me = db.merge(user)
        me.yandex_id = prof["id"]
        db.commit()
        resp = see_other("/settings?msg=ya_linked")
    else:
        found = db.scalars(select(User).where(User.yandex_id == prof["id"])).first()
        if not found:
            found = db.scalars(select(User).where(User.email == prof["email"])).first()
        if not found:
            if mode != "register-" + get_settings().personal_data_operator:
                resp = see_other(f"/register?yerr=noacc&email={quote(prof['email'])}&next={quote(safe_next(next_url))}")
                resp.delete_cookie("ya_state")
                return resp
            found = User(email=prof["email"], name=prof["name"][:120],
                         ip_consent_at=now() if mode == "register-ip" else None)
            db.add(found)
            found.yandex_id = prof["id"]
            db.commit()
            auth.send_welcome(found)
        found.yandex_id = prof["id"]
        db.commit()
        resp = _signed_in(db, found, safe_next(next_url))
    resp.delete_cookie("ya_state")
    return resp


# ---------- one-click unsubscribe (List-Unsubscribe) ----------

def _unsub_user(db: Session, uid: int, kind: str, token: str) -> User:
    if kind not in emails.KINDS or not hmac.compare_digest(token, emails.unsub_token(uid, kind)):
        raise HTTPException(404)
    user = db.get(User, uid)
    if not user:
        raise HTTPException(404)
    return user


@app.get("/unsubscribe/{uid}/{kind}/{token}", response_class=HTMLResponse)
def unsubscribe_page(request: Request, uid: int, kind: str, token: str, db: Session = Depends(get_db)):
    _unsub_user(db, uid, kind, token)
    return page(request, "unsubscribe.html", kind=emails.KINDS[kind], done=False)


@app.post("/unsubscribe/{uid}/{kind}/{token}", response_class=HTMLResponse)
def unsubscribe_do(request: Request, uid: int, kind: str, token: str, db: Session = Depends(get_db)):
    user = _unsub_user(db, uid, kind, token)
    if kind == "steps":
        user.remind_steps = False
    else:
        user.morning_report = False
    db.commit()
    return page(request, "unsubscribe.html", kind=emails.KINDS[kind], done=True)


# ---------- settings ----------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = "", user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    return page(request, "settings.html", user=user, msg=msg, **_sidebar(db, user),
                voice_left=speech.left(db, user), sessions=db.scalar(
                    select(func.count()).select_from(auth.AuthSession).where(auth.AuthSession.user_id == user.id)))


@app.post("/settings/profile")
def settings_profile(name: str = Form(""), role: str = Form(""), level: str = Form(...), team_note: str = Form(""),
                     user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if level not in LEVELS:
        raise HTTPException(400)
    me = db.merge(user)
    me.name, me.role, me.level = name.strip()[:120], role.strip()[:160], level
    me.team_note = team_note.strip()[:1500]
    db.commit()
    return see_other("/settings?msg=saved")


@app.post("/settings/notify")
def settings_notify(remind_steps: str = Form(""), morning_report: str = Form(""),
                    user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    me = db.merge(user)
    me.remind_steps, me.morning_report = bool(remind_steps), bool(morning_report)
    db.commit()
    return see_other("/settings?msg=saved#notify")


@app.post("/settings/password", response_class=HTMLResponse)
def settings_password(request: Request, password: str = Form(...), password2: str = Form(...), current: str = Form(""),
                      user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    me = db.merge(user)
    problem = auth.password_problem(password)
    if not problem and password != password2:
        problem = "Пароли не совпадают"
    if not problem and me.password_hash and not auth.check_password(db, me.email, current)[0]:
        problem = "Текущий пароль указан неверно"
    if problem:
        return see_other(f"/settings?msg=pw_{quote(problem)}#password")
    me.password_hash = auth.hash_password(password)
    db.commit()
    auth.end_other_sessions(db, me, request.cookies.get(auth.COOKIE))
    return see_other("/settings?msg=pw_ok#password")


@app.post("/settings/sessions")
def settings_sessions(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    auth.end_other_sessions(db, db.merge(user), request.cookies.get(auth.COOKIE))
    return see_other("/settings?msg=sessions#security")


@app.post("/settings/yandex/unlink")
def settings_yandex_unlink(user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    me = db.merge(user)
    me.yandex_id = None
    db.commit()
    return see_other("/settings?msg=ya_unlinked#security")


# ---------- people (the mentor's memory) ----------

@app.get("/people", response_class=HTMLResponse)
def people_page(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    people = db.scalars(select(Person).where(Person.user_id == user.id).order_by(Person.name)).all()
    lessons = db.scalars(select(Lesson).where(Lesson.user_id == user.id).order_by(Lesson.id.desc()).limit(20)).all()
    return page(request, "people.html", user=user, people=people, lessons=lessons, **_sidebar(db, user))


@app.post("/people")
def people_add(name: str = Form(...), role: str = Form(""), note: str = Form(""),
               user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if name.strip():
        db.add(Person(user_id=user.id, name=name.strip()[:120], role=role.strip()[:160], note=note.strip()[:500]))
        db.commit()
    return see_other("/people")


@app.post("/people/{pid}")
def people_edit(pid: int, name: str = Form(""), role: str = Form(""), note: str = Form(""), delete: str = Form(""),
                user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    p = db.get(Person, pid)
    if not p or p.user_id != user.id:
        raise HTTPException(404)
    if delete:
        db.delete(p)
    else:
        p.name = name.strip()[:120] or p.name
        p.role, p.note, p.updated_at = role.strip()[:160], note.strip()[:500], now()
    db.commit()
    return see_other("/people")


@app.post("/lessons/{lid}/delete")
def lesson_delete(lid: int, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    lesson = db.get(Lesson, lid)
    if lesson and lesson.user_id == user.id:
        db.delete(lesson)
        db.commit()
    return see_other("/people#lessons")


# ---------- the mentor ----------

def _sidebar(db: Session, user: User) -> dict:
    situations = db.scalars(select(Situation).where(Situation.user_id == user.id, Situation.deleted_at.is_(None))
                            .order_by(Situation.updated_at.desc())).all()
    shtabs = my_shtabs(db, user)
    return {"situations": situations, "shtabs": shtabs, "plan": access.summary(db, user),
            "learn_count": learn.done_count(db, user), "learn_total": learn.course()["total"]}


def my_shtabs(db: Session, user: User) -> list[Shtab]:
    ids = db.scalars(select(Member.shtab_id).where(Member.user_id == user.id)).all()
    return db.scalars(select(Shtab).where(Shtab.id.in_(ids)).order_by(Shtab.id)).all() if ids else []


def _gate(db: Session, user: User) -> RedirectResponse | None:
    if not user.level:
        return see_other("/onboarding")
    return None


@app.get("/app", response_class=HTMLResponse)
def app_home(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    return page(request, "app.html", user=user, current=None, **_sidebar(db, user),
                limit=not access.can_start(db, user))


def _own_situation(db: Session, user: User, sid: int) -> Situation:
    sit = db.get(Situation, sid)
    if not sit or sit.user_id != user.id or sit.deleted_at:
        raise HTTPException(404)
    return sit


def _forget(db: Session, sit: Situation) -> None:
    """Delete a разбор for its author: the messages, the plan and its steps go. Steps already sent to a shtab stay
    with the team as plain tasks; lessons stay in «Люди и уроки». The row itself stays, emptied, so the month's
    count of разборы does not go down."""
    quiet = {"synchronize_session": False}
    plan_ids = db.scalars(select(Plan.id).where(Plan.situation_id == sit.id)).all()
    if plan_ids:
        db.execute(update(Step).where(Step.plan_id.in_(plan_ids), Step.shtab_id.is_not(None)).values(plan_id=None)
                   .execution_options(**quiet))
        step_ids = db.scalars(select(Step.id).where(Step.plan_id.in_(plan_ids))).all()
        if step_ids:
            db.execute(update(LearnOrder).where(LearnOrder.step_id.in_(step_ids)).values(step_id=None)
                       .execution_options(**quiet))
            db.execute(delete(Step).where(Step.id.in_(step_ids)).execution_options(**quiet))
        db.execute(delete(Plan).where(Plan.id.in_(plan_ids)).execution_options(**quiet))
    db.execute(delete(Message).where(Message.situation_id == sit.id).execution_options(**quiet))
    db.execute(update(Lesson).where(Lesson.situation_id == sit.id).values(situation_id=None).execution_options(**quiet))
    db.execute(update(Situation).where(Situation.id == sit.id)
               .values(title="Удалённый разбор", stage="deleted", review="done", deleted_at=now())
               .execution_options(**quiet))
    db.commit()
    db.expire_all()


def _back(next_url: str, sid: int, flash: str) -> RedirectResponse:
    """Where to go after renaming or deleting from the menu: back to the page it was opened on,
    or to a new situation when that page was the deleted разбор itself."""
    dest = safe_next(next_url) if next_url else f"/app/{sid}"
    path = dest.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if flash == "deleted" and path in (f"/app/{sid}", "/app"):
        return see_other("/app?deleted=1")
    return see_other(dest)


@app.post("/app/{sid}/rename")
def app_rename(sid: int, title: str = Form(""), next: str = Form(""),
               user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    sit = _own_situation(db, user, sid)
    clean = " ".join(title.split())[:160]
    if clean:
        sit.title = clean
        db.commit()
    return _back(next, sid, "renamed")


@app.post("/app/{sid}/delete")
def app_delete(sid: int, next: str = Form(""), user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    sit = _own_situation(db, user, sid)
    _forget(db, sit)
    return _back(next, sid, "deleted")


async def _read_upload(upload: UploadFile | None) -> tuple[str, str] | None:
    if not upload or not upload.filename:
        return None
    raw = await upload.read(files.MAX_BYTES + 1)
    return upload.filename, files.extract(upload.filename, raw)


def _file_error(url: str, e: Exception) -> RedirectResponse:
    return see_other(f"{url}?ferr={quote(str(e))}")


@app.post("/app/new")
async def app_new(text: str = Form(""), file: UploadFile | None = File(None),
                  user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    try:
        attached = await _read_upload(file)
    except files.FileError as e:
        return _file_error("/app", e)
    if not text.strip() and not attached:
        return see_other("/app")
    if not access.can_start(db, user):
        return see_other("/app?limit=1")
    access.consume(db, user)
    sit = Situation(user_id=user.id)
    db.add(sit)
    db.flush()
    if attached:
        mentor.add_file(sit, *attached)
    try:
        mentor.handle(db, user, sit, text)
    except llm.LLMError:
        db.rollback()
        return see_other("/app?err=1")
    return see_other(f"/app/{sit.id}")


@app.get("/app/{sid}", response_class=HTMLResponse)
def app_situation(request: Request, sid: int, err: int = 0, user: User = Depends(auth.require_user),
                  db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    sit = _own_situation(db, user, sid)
    learn_report = _learn_report(db, user, sit)
    return page(request, "app.html", user=user, current=sit, err=err, **_sidebar(db, user), learn_report=learn_report, L=learn,
                limit=False, room=sit.stage != "follow" or mentor.can_talk(db, user, sit),
                review_due=mentor.review_due(sit), can_prep=mentor.can_prep(db, user, sit),
                ferr=request.query_params.get("ferr", ""))


@app.post("/app/{sid}/say")
async def app_say(sid: int, text: str = Form(""), file: UploadFile | None = File(None),
                  user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    sit = _own_situation(db, user, sid)
    try:
        attached = await _read_upload(file)
    except files.FileError as e:
        return _file_error(f"/app/{sid}", e)
    if not text.strip() and not attached:
        return see_other(f"/app/{sid}")
    if len(sit.messages) >= get_settings().messages_per_situation:
        return see_other(f"/app/{sid}?err=2")
    if attached:
        mentor.add_file(sit, *attached)
    try:
        mentor.handle(db, user, sit, text or "Посмотрите вложение.")
    except mentor.BudgetSpent:
        db.rollback()
        return see_other(f"/app/{sid}?err=2")
    except llm.LLMError:
        db.rollback()
        return see_other(f"/app/{sid}?err=1")
    return see_other(f"/app/{sid}#end")


@app.post("/app/{sid}/review")
def app_review(sid: int, outcome: str = Form(""), text: str = Form(""),
               user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    sit = _own_situation(db, user, sid)
    if sit.review == "done" or not sit.plan:
        return see_other(f"/app/{sid}")
    try:
        mentor.review(db, user, sit, outcome, text)
    except llm.LLMError:
        db.rollback()
        return see_other(f"/app/{sid}?err=1#review")
    return see_other(f"/app/{sid}#end")


def _own_step(db: Session, user: User, step_id: int) -> Step:
    step = db.get(Step, step_id)
    if not step or not step.plan or step.plan.user_id != user.id:
        raise HTTPException(404)
    return step


@app.post("/steps/{step_id}/prep")
def step_prep(step_id: int, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    step = _own_step(db, user, step_id)
    sid = step.plan.situation_id
    if not step.prep:
        try:
            mentor.prepare_talk(db, user, step)
        except mentor.BudgetSpent:
            db.rollback()
            return see_other(f"/app/{sid}?err=2")
        except llm.LLMError:
            db.rollback()
            return see_other(f"/app/{sid}?err=1")
    return see_other(f"/app/{sid}#step-{step.id}")


def _own_plan(db: Session, user: User, pid: int) -> Plan:
    plan = db.get(Plan, pid)
    if not plan or plan.user_id != user.id:
        raise HTTPException(404)
    return plan


@app.get("/plans/{pid}/order", response_class=HTMLResponse)
def plan_order(request: Request, pid: int, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    """The plan as a printable order (приказ): print it or save as PDF from the browser."""
    plan = _own_plan(db, user, pid)
    return page(request, "order.html", user=user, plan=plan, today=dt.date.today())


def _ics_text(t: str) -> str:
    return t.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


@app.get("/plans/{pid}/calendar.ics")
def plan_ics(pid: int, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    """Step dates as all-day calendar events (Яндекс Календарь, Google, Outlook, Apple)."""
    plan = _own_plan(db, user, pid)
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//nauka.ogiinst.ru//shtab//RU", "CALSCALE:GREGORIAN"]
    for s in plan.steps:
        if not s.due:
            continue
        d = s.due.strftime("%Y%m%d")
        nxt = (s.due + dt.timedelta(days=1)).strftime("%Y%m%d")
        lines += ["BEGIN:VEVENT", f"UID:step-{s.id}@nauka.ogiinst.ru", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{d}", f"DTEND;VALUE=DATE:{nxt}",
                  f"SUMMARY:{_ics_text(s.text[:120])}",
                  f"DESCRIPTION:{_ics_text(plan.title + (' · ' + s.owner_name if s.owner_name else ''))}",
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines) + "\r\n", media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="plan-{plan.id}.ics"'})


@app.post("/voice")
async def voice(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    """Raw 16 kHz 16-bit mono PCM from the browser → text for the message field."""
    pcm = await request.body()
    try:
        text = speech.recognize(db, user, pcm)
    except speech.VoiceError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:  # noqa: BLE001 — network and API errors
        logging.getLogger("voice").exception("SpeechKit failed")
        return JSONResponse({"error": "Не удалось распознать запись. Попробуйте ещё раз."}, status_code=502)
    return {"text": text, "left": speech.left(db, user)}


@app.post("/plans/{pid}/send")
def plan_send(pid: int, shtab_id: int = Form(...), user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    plan = db.get(Plan, pid)
    if not plan or plan.user_id != user.id:
        raise HTTPException(404)
    shtab = _member_shtab(db, user, shtab_id)
    plan.sent_to_shtab_id = shtab.id
    for step in plan.steps:
        step.shtab_id = shtab.id
        if step.owner_name.strip().lower() in {"вы", "я", user.display_name.lower()}:
            step.assignee_id = user.id
    db.commit()
    return see_other(f"/shtab/{shtab.id}")


@app.post("/steps/{step_id}/toggle")
def step_toggle(request: Request, step_id: int, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    step = db.get(Step, step_id)
    if not step:
        raise HTTPException(404)
    allowed = step.created_by == user.id or (step.shtab_id and _is_member(db, user, step.shtab_id))
    if not allowed:
        raise HTTPException(404)
    step.done = not step.done
    step.done_at = now() if step.done else None
    db.commit()
    return see_other(request.headers.get("referer") or "/app")


# ---------- shtab ----------

def _is_member(db: Session, user: User, shtab_id: int) -> bool:
    return db.scalar(select(func.count(Member.id)).where(Member.shtab_id == shtab_id, Member.user_id == user.id)) > 0


def _member_shtab(db: Session, user: User, shtab_id: int) -> Shtab:
    shtab = db.get(Shtab, shtab_id)
    if not shtab or not _is_member(db, user, shtab_id):
        raise HTTPException(404)
    return shtab


@app.get("/shtab", response_class=HTMLResponse)
def shtab_home(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    shtabs = my_shtabs(db, user)
    if len(shtabs) == 1:
        return see_other(f"/shtab/{shtabs[0].id}")
    return page(request, "shtab_list.html", user=user, shtabs=shtabs)


@app.post("/shtab/new")
def shtab_new(name: str = Form(...), about: str = Form(""), user: User = Depends(auth.require_user),
              db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    shtab = Shtab(name=name.strip()[:120] or "Мой штаб", about=about.strip()[:240], owner_id=user.id)
    db.add(shtab)
    db.flush()
    db.add(Member(shtab_id=shtab.id, user_id=user.id, title="Глава штаба"))
    db.commit()
    return see_other(f"/shtab/{shtab.id}")


@app.get("/shtab/{shtab_id}", response_class=HTMLResponse)
def shtab_view(request: Request, shtab_id: int, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    shtab = _member_shtab(db, user, shtab_id)
    tasks = db.scalars(select(Step).where(Step.shtab_id == shtab.id).order_by(Step.done, Step.due, Step.id)).all()
    late = [t for t in tasks if t.late]
    return page(request, "shtab.html", user=user, shtab=shtab, tasks=tasks, late=late,
                invite_url=f"{get_settings().base_url}/join/{shtab.invite_token}",
                is_owner=shtab.owner_id == user.id, shtabs=my_shtabs(db, user))


@app.post("/shtab/{shtab_id}/tasks")
def shtab_add_task(shtab_id: int, text: str = Form(...), assignee_id: int = Form(0), due: str = Form(""),
                   user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    shtab = _member_shtab(db, user, shtab_id)
    due_date = None
    if due:
        try:
            due_date = dt.date.fromisoformat(due)
        except ValueError:
            due_date = None
    assignee = assignee_id if assignee_id and _is_member(db, db.get(User, assignee_id), shtab.id) else None
    db.add(Step(shtab_id=shtab.id, text=text.strip()[:500], assignee_id=assignee, due=due_date, created_by=user.id))
    db.commit()
    return see_other(f"/shtab/{shtab.id}")


@app.post("/steps/{step_id}/assign")
def step_assign(step_id: int, assignee_id: int = Form(0), user: User = Depends(auth.require_user),
                db: Session = Depends(get_db)):
    step = db.get(Step, step_id)
    if not step or not step.shtab_id or not _is_member(db, user, step.shtab_id):
        raise HTTPException(404)
    target = db.get(User, assignee_id) if assignee_id else None
    step.assignee_id = target.id if target and _is_member(db, target, step.shtab_id) else None
    db.commit()
    return see_other(f"/shtab/{step.shtab_id}")


@app.get("/join/{invite_token}")
def shtab_join(invite_token: str, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    shtab = db.scalars(select(Shtab).where(Shtab.invite_token == invite_token)).first()
    if not shtab:
        raise HTTPException(404)
    if not _is_member(db, user, shtab.id):
        db.add(Member(shtab_id=shtab.id, user_id=user.id))
        db.commit()
    return see_other(f"/shtab/{shtab.id}")


# ---------- billing ----------

@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request, error: str = "", user: User = Depends(auth.require_user),
                 db: Session = Depends(get_db)):
    from .models import Subscription

    owned = db.scalars(select(Shtab).where(Shtab.owner_id == user.id)).all()
    paid = db.scalars(select(Subscription).where(Subscription.payer_id == user.id, Subscription.status == "active",
                                                Subscription.paid_until > now())
                      .order_by(Subscription.paid_until.desc())).all()
    subs = list({sub.shtab_id: sub for sub in reversed(paid)}.values()) if get_settings().payment_provider == "tbank" else paid
    return page(request, "billing.html", user=user, owned=owned, plan=access.summary(db, user), subs=subs,
                shtab_names={sh.id: sh.name for sh in owned}, enabled=get_settings().checkout_enabled,
                personal_sub=next((sub for sub in subs if sub.shtab_id is None), None),
                payment_error=error == "unavailable")


@app.post("/billing/cancel/{sub_id}")
def billing_cancel(sub_id: int, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    """Stop auto-renewal; the paid period stays."""
    from .models import Subscription

    sub = db.get(Subscription, sub_id)
    if not sub or sub.payer_id != user.id:
        raise HTTPException(404)
    sub.payment_method_id = ""
    db.commit()
    return see_other("/billing")


@app.post("/billing/pack")
def billing_pack(user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if not get_settings().checkout_enabled or access.tier(db, user) != access.PRO:
        return see_other("/billing")
    try:
        return see_other(billing.start_pack(db, user))
    except (tbank.PaymentError, httpx.HTTPError):
        logging.getLogger("billing").warning("Could not start pack checkout")
        return see_other("/billing?error=unavailable")


def _payment_result(request: Request, db: Session, provider_id: str):
    try:
        status = billing.sync_payment(db, provider_id)
    except (tbank.PaymentError, httpx.HTTPError):
        status = "pending"
    return page(request, "billing_result.html", status=status)


@app.get("/billing/pack/{order_id}")
def billing_pack_return(request: Request, order_id: int, user: User = Depends(auth.require_user),
                        db: Session = Depends(get_db)):
    from .models import PackOrder

    order = db.get(PackOrder, order_id)
    if not order or order.user_id != user.id or not order.provider_id:
        raise HTTPException(404)
    return _payment_result(request, db, order.provider_id)


@app.post("/billing/start")
def billing_start(shtab_id: int = Form(0), user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if not get_settings().checkout_enabled:
        return see_other("/billing")
    shtab = None
    if shtab_id:
        shtab = db.get(Shtab, shtab_id)
        if not shtab or shtab.owner_id != user.id:
            raise HTTPException(404)
    try:
        return see_other(billing.start_payment(db, user, shtab))
    except (tbank.PaymentError, httpx.HTTPError):
        logging.getLogger("billing").warning("Could not start access checkout")
        return see_other("/billing?error=unavailable")


@app.get("/billing/return/{sub_id}")
def billing_return(request: Request, sub_id: int, user: User = Depends(auth.require_user),
                   db: Session = Depends(get_db)):
    from .models import Payment, Subscription

    sub = db.get(Subscription, sub_id)
    if not sub or sub.payer_id != user.id:
        raise HTTPException(404)
    pay = db.scalars(select(Payment).where(Payment.subscription_id == sub_id).order_by(Payment.id.desc())).first()
    return _payment_result(request, db, pay.provider_id) if pay else see_other("/billing")


@app.post("/billing/webhook")
async def billing_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    pid = (body.get("object") or {}).get("id")
    if isinstance(pid, str) and not pid.startswith(tbank.PREFIX):
        billing.sync_payment(db, pid)  # re-reads the payment from YooKassa; the body itself is not trusted
    return JSONResponse({"ok": True})


@app.post("/billing/tbank/webhook", response_class=PlainTextResponse)
def tbank_webhook(payload: dict, db: Session = Depends(get_db)):
    key = tbank.notification_key(payload)
    if not key:
        raise HTTPException(403)
    try:
        if payload.get("Status") in tbank.FINAL_STATUSES and billing.sync_payment(db, key) == "pending":
            raise tbank.PaymentError("T-Bank has not finalized the payment yet")
    except tbank.PaymentError as exc:
        raise HTTPException(503, "Проверка платежа временно недоступна") from exc
    return "OK"


# ---------- owner's page ----------

@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, user: User = Depends(auth.require_admin), db: Session = Depends(get_db)):
    month = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(
        select(User.email, func.count(Usage.id), func.coalesce(func.sum(Usage.cost_rub), 0))
        .join(Usage, Usage.user_id == User.id).where(Usage.created_at >= month)
        .group_by(User.email).order_by(func.sum(Usage.cost_rub).desc())
    ).all()
    # every purchase, newest first: subscription payments (a person or a whole shtab) and packs of разборов
    buys = []
    for pay, sub, email, shtab in db.execute(
        select(Payment, Subscription, User.email, Shtab.name)
        .join(Subscription, Payment.subscription_id == Subscription.id)
        .join(User, Subscription.payer_id == User.id)
        .outerjoin(Shtab, Subscription.shtab_id == Shtab.id)
        .order_by(Payment.created_at.desc()).limit(200)
    ).all():
        what = f"Подписка, штаб «{shtab}» · мест: {sub.seats}" if shtab else "Подписка · 1 место"
        buys.append({"at": pay.created_at, "email": email, "what": what, "rub": pay.amount_rub, "status": pay.status})
    for order, email in db.execute(
        select(PackOrder, User.email).join(User, PackOrder.user_id == User.id)
        .order_by(PackOrder.created_at.desc()).limit(200)
    ).all():
        buys.append({"at": order.created_at, "email": email, "what": f"Пакет разборов · {order.plans} шт.",
                     "rub": order.amount_rub, "status": order.status})
    buys.sort(key=lambda b: b["at"], reverse=True)
    buys = buys[:200]

    def paid(since=None):
        total = 0.0
        for model in (Payment, PackOrder):
            q = select(func.coalesce(func.sum(model.amount_rub), 0)).where(model.status == "succeeded")
            if since is not None:
                q = q.where(model.created_at >= since)
            total += db.scalar(q) or 0
        return round(total, 2)

    active = db.execute(select(func.count(Subscription.id), func.coalesce(func.sum(Subscription.seats), 0))
                        .where(Subscription.status == "active", Subscription.paid_until > now())).one()
    cost_month = round(db.scalar(select(func.coalesce(func.sum(Usage.cost_rub), 0)).where(Usage.created_at >= month)), 2)
    revenue_month = paid(month)
    stats = {
        "users": db.scalar(select(func.count(User.id))),
        "situations": db.scalar(select(func.count(Situation.id))),
        "plans_month": db.scalar(select(func.count(Plan.id)).where(Plan.created_at >= month)),
        "cost_month": cost_month,
        "shtabs": db.scalar(select(func.count(Shtab.id))),
        "revenue_month": revenue_month,
        "revenue_total": paid(),
        "subs_active": active[0],
        "seats_active": active[1],
        "buys_month": sum(1 for b in buys if b["status"] == "succeeded" and b["at"] >= month),
        "net_month": round(revenue_month - cost_month, 2),
    }
    return page(request, "admin.html", user=user, stats=stats, rows=rows, buys=buys)


# ---------- Учение ----------

def _learn_report(db: Session, user: User, sit: Situation) -> LearnOrder | None:
    """An order from «Учение» whose step sits in this plan and whose due date has come."""
    plan_ = sit.plan
    if not plan_:
        return None
    ids = [s.id for s in plan_.steps]
    if not ids:
        return None
    return db.scalars(select(LearnOrder).where(LearnOrder.user_id == user.id, LearnOrder.step_id.in_(ids),
                                               LearnOrder.outcome == "", LearnOrder.due <= learn.today())
                      .order_by(LearnOrder.due)).first()


def _lesson_or_404(lid: str) -> dict:
    les = learn.lesson(lid)
    if not les or not les.get("ready"):
        raise HTTPException(404)
    return les


@app.get("/learn", response_class=HTMLResponse)
def learn_home(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    ov = learn.overview(db, user)
    notes = db.scalars(select(LearnNote).where(LearnNote.to_id == user.id, LearnNote.seen.is_(False))
                       .order_by(LearnNote.id.desc())).all()
    for n in notes:
        n.seen = True
    db.commit()
    return page(request, "learn.html", user=user, ov=ov, notes=notes, course=learn.course(), L=learn,
                **_sidebar(db, user))


@app.get("/learn/start", response_class=HTMLResponse)
def learn_start(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    items = [{"id": f"d{i}", "type": "diag", "scene": q["scene"], "q": q["q"], "options": [o[0] for o in q["options"]]}
             for i, q in enumerate(learn.course()["diag"])]
    return page(request, "learn_player.html", user=user, mode="diag", title="Вступительный смотр",
                payload={"mode": "diag", "items": items, "finish": "/learn/start", "close": "/learn"})


@app.post("/learn/start")
def learn_start_save(payload: dict, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    picks = {str(k).lstrip("d"): v for k, v in (payload.get("picks") or {}).items()}
    learn.save_diag(db, user, picks)
    db.commit()
    return JSONResponse({"next": "/learn/map"})


@app.get("/learn/map", response_class=HTMLResponse)
def learn_map(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    st = learn.state(db, user)
    rows = learn.diag_map(db, user)
    ov = learn.overview(db, user)
    db.commit()
    return page(request, "learn_map.html", user=user, st=st, rows=rows, ov=ov)


@app.post("/learn/settings")
def learn_settings(goal_min: int = Form(5), workdays: str = Form(""), in_report: str = Form(""), next: str = Form("/learn"),
                   user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    st = learn.state(db, user)
    st.goal_min = goal_min if goal_min in (2, 5, 10) else 5
    st.workdays = bool(workdays)
    st.in_report = bool(in_report)
    db.commit()
    return see_other(safe_next(next))


@app.get("/learn/lesson/{lid}", response_class=HTMLResponse)
def learn_lesson(request: Request, lid: str, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    les = _lesson_or_404(lid)
    ov = learn.overview(db, user)
    rec_id = ov["rec"]["lesson"]["id"] if ov["rec"].get("lesson") else None
    if not learn.lesson_open(db, user, les, ov["done"], ov["tests"], rec_id):
        return see_other("/learn#path")
    plans = learn.candidate_plans(db, user)
    payload = {"mode": "lesson", "lesson": {k: les[k] for k in ("id", "title", "quote_text", "quote_source", "idea", "source", "example", "words")},
               "items": [learn.public_item(it) for it in les["items"]], "finish": f"/learn/lesson/{lid}/finish",
               "words_url": "/learn/words", "close": "/learn"}
    st = learn.stage(les["stage"])
    db.commit()
    return page(request, "learn_player.html", user=user, mode="lesson", les=les, stage=st, plans=plans, L=learn,
                title=les["title"], payload=payload, today=learn.today(), dt=dt,
                due=learn.today() + dt.timedelta(days=les.get("order", {}).get("days", 5)))


@app.post("/learn/lesson/{lid}/finish")
def learn_lesson_finish(lid: str, payload: dict, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    les = _lesson_or_404(lid)
    right, total = learn.schedule(db, user, payload.get("results") or {})
    first = lid not in learn.done_ids(db, user)
    xp = learn.XP["lesson"] if first else learn.XP["review"]
    seconds = max(0, min(int(payload.get("seconds") or 0), 3600))
    db.add(LearnDone(user_id=user.id, lesson_id=les["id"], right=right, total=total, seconds=seconds, xp=xp))
    learn.add_xp(db, user, xp)
    learn.touch(db, user, seconds)
    db.commit()
    return JSONResponse({"ok": True, "right": right, "total": total, "xp": xp})


@app.post("/learn/words")
def learn_words(payload: dict, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    les = _lesson_or_404(str(payload.get("lesson", "")))
    fb = learn.words_feedback(db, user, les, str(payload.get("text", "")))
    db.commit()
    return JSONResponse(fb)


@app.post("/learn/lesson/{lid}/order")
def learn_order(lid: str, plan_id: int = Form(0), if_text: str = Form(""), then_text: str = Form(""), due: str = Form(""),
                who: str = Form("Я"), skip: str = Form(""), user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    les = _lesson_or_404(lid)
    if not skip and if_text.strip() and then_text.strip():
        try:
            d = dt.date.fromisoformat(due) if due else learn.today() + dt.timedelta(days=5)
        except ValueError:
            d = learn.today() + dt.timedelta(days=5)
        d = max(d, learn.today())
        plan_ = None
        if plan_id:
            plan_ = db.get(Plan, plan_id)
            if not plan_ or plan_.user_id != user.id:
                raise HTTPException(404)
        learn.make_order(db, user, les, plan_, if_text, then_text, d, who)
        db.commit()
    return see_other(f"/learn/lesson/{lid}/done")


@app.get("/learn/lesson/{lid}/done", response_class=HTMLResponse)
def learn_lesson_done(request: Request, lid: str, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    les = _lesson_or_404(lid)
    last = db.scalars(select(LearnDone).where(LearnDone.user_id == user.id, LearnDone.lesson_id == lid)
                      .order_by(LearnDone.id.desc())).first()
    if not last:
        return see_other(f"/learn/lesson/{lid}")
    order = db.scalars(select(LearnOrder).where(LearnOrder.user_id == user.id, LearnOrder.lesson_id == lid)
                       .order_by(LearnOrder.id.desc())).first()
    if order and order.created_at < last.created_at:
        order = None
    ov = learn.overview(db, user)
    db.commit()
    return page(request, "learn_done.html", user=user, kind="lesson", les=les, last=last, order=order, ov=ov, L=learn,
                plan_title=(learn.order_situation(db, order).plan.title if order and learn.order_situation(db, order) else ""))


@app.get("/learn/review", response_class=HTMLResponse)
def learn_review(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    items = learn.review_items(db, user)
    if not items:
        return see_other("/learn?review=none")
    return page(request, "learn_player.html", user=user, mode="review", title="Повторение",
                payload={"mode": "review", "items": [learn.public_item(it) for it in items],
                         "finish": "/learn/review/finish", "close": "/learn"})


@app.post("/learn/review/finish")
def learn_review_finish(payload: dict, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    right, total = learn.schedule(db, user, payload.get("results") or {})
    if total:
        learn.add_xp(db, user, learn.XP["review"])
        learn.touch(db, user, max(0, min(int(payload.get("seconds") or 0), 3600)))
    db.commit()
    return JSONResponse({"next": f"/learn?reviewed={right}-{total}"})


@app.get("/learn/test/{n}", response_class=HTMLResponse)
def learn_test(request: Request, n: int, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    if not 1 <= n <= len(learn.course()["stages"]) or not learn.stage(n)["ready"] or not learn.stage_open(db, user, n):
        return see_other("/learn#path")
    items = learn.test_items(db, user, n)
    return page(request, "learn_player.html", user=user, mode="test", title=f"Смотр этапа «{learn.stage(n)['name']}»",
                payload={"mode": "test", "items": [learn.public_item(it) for it in items],
                         "finish": f"/learn/test/{n}/finish", "close": "/learn"})


@app.post("/learn/test/{n}/finish")
def learn_test_finish(n: int, payload: dict, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if not 1 <= n <= len(learn.course()["stages"]):
        raise HTTPException(404)
    before = learn.rank(learn.marks(db, user, "test"), learn.orders(db, user))
    results = payload.get("results") or {}
    right, total = learn.schedule(db, user, results)
    passed = bool(total) and right / total >= learn.PASS
    db.add(LearnMark(user_id=user.id, kind="test", stage=n, score=right, total=total, passed=passed))
    if passed:
        learn.add_xp(db, user, learn.XP["test"])
    learn.touch(db, user, max(0, min(int(payload.get("seconds") or 0), 3600)))
    db.flush()
    after = learn.rank(learn.marks(db, user, "test"), learn.orders(db, user))
    db.commit()
    return JSONResponse({"next": f"/learn/test/{n}/result" + (f"?rank={after}" if after > before else "")})


@app.get("/learn/test/{n}/result", response_class=HTMLResponse)
def learn_test_result(request: Request, n: int, rank: int = 0, user: User = Depends(auth.require_user),
                      db: Session = Depends(get_db)):
    mark = db.scalars(select(LearnMark).where(LearnMark.user_id == user.id, LearnMark.kind == "test", LearnMark.stage == n)
                      .order_by(LearnMark.id.desc())).first()
    if not mark:
        return see_other("/learn")
    ov = learn.overview(db, user)
    new_rank = rank if rank and ov["rank"] >= rank else 0
    db.commit()
    return page(request, "learn_done.html", user=user, kind="test", stage=learn.stage(n), mark=mark, ov=ov, L=learn,
                new_rank=new_rank)


@app.get("/learn/task/{n}", response_class=HTMLResponse)
def learn_task(request: Request, n: int, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    if not 1 <= n <= len(learn.course()["stages"]) or not learn.stage_open(db, user, n):
        return see_other("/learn#path")
    done = learn.marks(db, user, "task").get(n)
    return page(request, "learn_task.html", user=user, stage=learn.stage(n), done=done)


@app.post("/learn/task/{n}")
def learn_task_save(n: int, note: str = Form(""), user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if not 1 <= n <= len(learn.course()["stages"]):
        raise HTTPException(404)
    if not learn.marks(db, user, "task").get(n):
        db.add(LearnMark(user_id=user.id, kind="task", stage=n, passed=True, note=note.strip()[:4000]))
        learn.add_xp(db, user, learn.XP["task"])
        learn.touch(db, user)
        db.commit()
    return see_other("/learn?task=1#path")


@app.get("/learn/order/{oid}", response_class=HTMLResponse)
def learn_order_view(request: Request, oid: int, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    o = db.get(LearnOrder, oid)
    if not o or o.user_id != user.id:
        raise HTTPException(404)
    sit = learn.order_situation(db, o)
    if sit and not o.outcome:
        return see_other(f"/app/{sit.id}#learn-report")
    return page(request, "learn_order.html", user=user, o=o, les=learn.lesson(o.lesson_id), OUT=learn.OUTCOME_NAMES, L=learn)


@app.post("/learn/order/{oid}/report")
def learn_order_report(oid: int, outcome: str = Form(...), note: str = Form(""), next: str = Form("/learn"),
                       user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    o = db.get(LearnOrder, oid)
    if not o or o.user_id != user.id:
        raise HTTPException(404)
    before = learn.rank(learn.marks(db, user, "test"), learn.orders(db, user))
    gained = learn.report(db, user, o, outcome, note)
    db.flush()
    after = learn.rank(learn.marks(db, user, "test"), learn.orders(db, user))
    db.commit()
    if after > before:
        stage_n = after
        return see_other(f"/learn/test/{stage_n}/result?rank={after}")
    url = safe_next(next)
    sep = "&" if "?" in url else "?"
    return see_other(f"{url.split('#')[0]}{sep}learned={gained}#learn-done" if url.startswith("/app/") else f"{url}{sep}learned={gained}")


# ---------- Учение штаба ----------

def _team_rows(db: Session, shtab: Shtab) -> list[dict]:
    rows = []
    for m in shtab.members:
        u = m.user
        done = learn.done_ids(db, u)
        tests = learn.marks(db, u, "test")
        ords = learn.orders(db, u)
        r = learn.rank(tests, ords)
        cur = next((s["n"] for s in learn.course()["stages"] if not all(x["id"] in done for x in s["lesson_list"])),
                   len(learn.course()["stages"]))
        last = max([d for d in [db.scalar(select(func.max(LearnDone.created_at)).where(LearnDone.user_id == u.id))] if d],
                   default=None)
        rows.append({"user": u, "member": m, "stage": cur, "done": len(done), "rank": learn.rank_name(r),
                     "orders": sum(1 for o in ords if o.outcome in ("done", "partial")), "streak": learn.streak(db, u)["days"],
                     "idle": (now() - last).days if last else None, "is_owner": u.id == shtab.owner_id})
    return rows


@app.get("/shtab/{shtab_id}/learn", response_class=HTMLResponse)
def shtab_learn(request: Request, shtab_id: int, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    shtab = _member_shtab(db, user, shtab_id)
    rows = _team_rows(db, shtab)
    assign = db.scalars(select(LearnAssign).where(LearnAssign.shtab_id == shtab.id).order_by(LearnAssign.id.desc())).first()
    week_start = learn.today() - dt.timedelta(days=learn.today().weekday())
    member_ids = [m.user_id for m in shtab.members]
    week_orders = db.scalar(select(func.count(LearnOrder.id)).where(
        LearnOrder.user_id.in_(member_ids), LearnOrder.outcome.in_(("done", "partial")),
        LearnOrder.reported_at >= dt.datetime.combine(week_start, dt.time()) - dt.timedelta(hours=3))) or 0
    goal = max(5, 5 * len(member_ids))
    dist = None
    if assign:
        st = learn.stage(assign.stage)
        first = next((it for les in st["lesson_list"] for it in les.get("items", []) if it["type"] == "situation"), None)
        if first:
            from .models import LearnItem
            picks = db.scalars(select(LearnItem.last_pick).where(LearnItem.item_id == first["id"],
                                                                 LearnItem.user_id.in_(member_ids))).all()
            counts = [sum(1 for p in picks if p == i) for i in range(len(first["options"]))]
            dist = {"item": first, "counts": counts, "n": sum(counts), "max": max(counts) if counts and max(counts) else 1}
    nudges = []
    if shtab.owner_id == user.id:
        since = now() - dt.timedelta(days=7)
        for row in rows:
            u = row["user"]
            if u.id == user.id:
                continue
            o = db.scalars(select(LearnOrder).where(LearnOrder.user_id == u.id, LearnOrder.outcome == "done",
                                                    LearnOrder.reported_at >= since).order_by(LearnOrder.id.desc())).first()
            t = db.scalars(select(LearnMark).where(LearnMark.user_id == u.id, LearnMark.kind == "test", LearnMark.passed.is_(True),
                                                   LearnMark.created_at >= since).order_by(LearnMark.id.desc())).first()
            if o:
                nudges.append({"user": u, "text": f"{u.display_name.split()[0]} исполнил(а) приказ «{o.title}»", "kind": "praise",
                               "note": f"Видел, что вы исполнили приказ «{o.title}». Так держать!"})
            elif t:
                nudges.append({"user": u, "text": f"{u.display_name.split()[0]} сдал(а) смотр этапа {t.stage}", "kind": "praise",
                               "note": f"Поздравляю со смотром этапа {t.stage}!"})
            elif row["idle"] is None or row["idle"] >= 5:
                nudges.append({"user": u, "text": f"{u.display_name.split()[0]} давно не заходил(а) в «Учение»", "kind": "nudge",
                               "note": "Жду вас в «Учении»: пять минут сегодня."})
    return page(request, "shtab_learn.html", user=user, shtab=shtab, rows=rows, assign=assign, week_orders=week_orders,
                goal=goal, dist=dist, nudges=nudges, is_owner=shtab.owner_id == user.id, course=learn.course(),
                sent=request.query_params.get("sent", ""))


@app.post("/shtab/{shtab_id}/learn/assign")
def shtab_learn_assign(shtab_id: int, stage: int = Form(...), due: str = Form(""), user: User = Depends(auth.require_user),
                       db: Session = Depends(get_db)):
    shtab = _member_shtab(db, user, shtab_id)
    if shtab.owner_id != user.id or not 1 <= stage <= len(learn.course()["stages"]):
        raise HTTPException(403)
    try:
        d = dt.date.fromisoformat(due) if due else None
    except ValueError:
        d = None
    db.add(LearnAssign(shtab_id=shtab.id, stage=stage, due=d))
    db.commit()
    return see_other(f"/shtab/{shtab.id}/learn?sent=assign")


@app.post("/shtab/{shtab_id}/learn/note")
def shtab_learn_note(shtab_id: int, to_id: int = Form(...), text: str = Form(...), user: User = Depends(auth.require_user),
                     db: Session = Depends(get_db)):
    shtab = _member_shtab(db, user, shtab_id)
    if shtab.owner_id != user.id or to_id not in [m.user_id for m in shtab.members]:
        raise HTTPException(403)
    db.add(LearnNote(from_id=user.id, to_id=to_id, text=text.strip()[:300] or "Так держать!"))
    db.commit()
    return see_other(f"/shtab/{shtab.id}/learn?sent=note")
