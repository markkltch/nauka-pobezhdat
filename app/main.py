"""Web routes."""
from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import access, auth, billing, files, jobs, legal, llm, mentor, speech, yandex_id
from .config import get_settings
from .db import get_db, init_db
from .models import LEVELS, Lesson, Member, Person, Plan, Shtab, Situation, Step, Usage, User, now

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
                             OUTCOMES=mentor.OUTCOMES, BRAND="Наука побеждать · Штаб",
                             LEGAL=legal.OPERATOR, PROCESSORS=legal.PROCESSORS, EDITION=legal.EDITION)


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
templates.env.filters["plural"] = _plural
templates.env.filters["rub"] = lambda n: f"{int(n):,}".replace(",", "\u00a0") + "\u00a0₽"


@app.exception_handler(auth.LoginRequired)
def _to_login(request: Request, exc: auth.LoginRequired):
    return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)


def page(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, ctx)


def auth_token() -> str:
    import secrets

    return secrets.token_urlsafe(24)


def safe_next(url: str | None) -> str:
    return url if url and url.startswith("/") and not url.startswith("//") else "/app"


def see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------- public ----------

@app.get("/", response_class=HTMLResponse)
def landing():
    return FileResponse(HERE / "static" / "landing" / "index.html")


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


# ---------- sign-in ----------

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/app", mode: str = "code", yerr: str = "",
               user: User | None = Depends(auth.current_user)):
    if user:
        return see_other(safe_next(next))
    error = {"consent": "Чтобы войти через Яндекс, отметьте согласие на обработку данных.",
             "failed": "Не получилось войти через Яндекс. Попробуйте ещё раз или войдите по коду."}.get(yerr, "")
    return page(request, "login.html", next=safe_next(next), step="password" if mode == "password" else "email",
                error=error)


@app.post("/login", response_class=HTMLResponse)
def login_send(request: Request, email: str = Form(...), consent: str = Form(""), next: str = Form("/app"),
               db: Session = Depends(get_db)):
    email = auth.normalize_email(email)
    if "@" not in email or "." not in email.split("@")[-1]:
        return page(request, "login.html", next=safe_next(next), step="email", error="Проверьте адрес почты", email=email)
    if not consent:
        return page(request, "login.html", next=safe_next(next), step="email", email=email,
                    error="Без согласия на обработку данных войти нельзя")
    auth.start_login(db, email, safe_next(next))
    return page(request, "login.html", next=safe_next(next), step="code", email=email)


def _signed_in(db: Session, user: User, next_url: str) -> RedirectResponse:
    if not user.consent_at:
        user.consent_at = now()
        db.commit()
    sess = auth.open_session(db, user)
    target = "/onboarding?next=" + quote(next_url) if not user.level else next_url
    resp = see_other(target)
    secure = get_settings().base_url.startswith("https://")
    resp.set_cookie(auth.COOKIE, sess.id, max_age=int(auth.SESSION_TTL.total_seconds()), httponly=True,
                    samesite="lax", secure=secure)
    return resp


@app.post("/login/code", response_class=HTMLResponse)
def login_code(request: Request, email: str = Form(...), code: str = Form(...), next: str = Form("/app"),
               db: Session = Depends(get_db)):
    user = auth.check_code(db, email, code)
    if not user:
        return page(request, "login.html", next=safe_next(next), step="code", email=email,
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
        return page(request, "login.html", next=safe_next(next), step="password", email=email, error=error)
    return _signed_in(db, user, safe_next(next))


@app.get("/auth/yandex")
def yandex_start(next: str = "/app", consent: str = "", link: str = "",
                 user: User | None = Depends(auth.current_user)):
    if not yandex_id.enabled():
        return see_other("/login")
    if not user and not consent:
        return see_other(f"/login?next={quote(safe_next(next))}&yerr=consent")
    state = auth_token()
    mode = "link" if (link and user) else "login"
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
            found = User(email=prof["email"], name=prof["name"][:120])
            db.add(found)
        found.yandex_id = prof["id"]
        db.commit()
        resp = _signed_in(db, found, safe_next(next_url))
    resp.delete_cookie("ya_state")
    return resp


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
    situations = db.scalars(select(Situation).where(Situation.user_id == user.id).order_by(Situation.updated_at.desc())).all()
    shtabs = my_shtabs(db, user)
    return {"situations": situations, "shtabs": shtabs, "plan": access.summary(db, user)}


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
    if not sit or sit.user_id != user.id:
        raise HTTPException(404)
    return sit


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
    return page(request, "app.html", user=user, current=sit, err=err, **_sidebar(db, user),
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
def billing_page(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    from .models import Subscription

    owned = db.scalars(select(Shtab).where(Shtab.owner_id == user.id)).all()
    subs = db.scalars(select(Subscription).where(Subscription.payer_id == user.id, Subscription.status == "active",
                                                Subscription.paid_until > now())).all()
    return page(request, "billing.html", user=user, owned=owned, plan=access.summary(db, user), subs=subs,
                shtab_names={sh.id: sh.name for sh in owned}, enabled=get_settings().billing_enabled)


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
    if not get_settings().billing_enabled or access.tier(db, user) != access.PRO:
        return see_other("/billing")
    return see_other(billing.start_pack(db, user))


@app.get("/billing/pack/{order_id}")
def billing_pack_return(order_id: int, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    from .models import PackOrder

    order = db.get(PackOrder, order_id)
    if order and order.user_id == user.id and order.provider_id:
        billing.sync_payment(db, order.provider_id)
    return see_other("/app")


@app.post("/billing/start")
def billing_start(shtab_id: int = Form(0), user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if not get_settings().billing_enabled:
        return see_other("/billing")
    shtab = None
    if shtab_id:
        shtab = db.get(Shtab, shtab_id)
        if not shtab or shtab.owner_id != user.id:
            raise HTTPException(404)
    return see_other(billing.start_payment(db, user, shtab))


@app.get("/billing/return/{sub_id}")
def billing_return(sub_id: int, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    from .models import Payment

    for pay in db.scalars(select(Payment).where(Payment.subscription_id == sub_id)).all():
        billing.sync_payment(db, pay.provider_id)
    return see_other("/app")


@app.post("/billing/webhook")
async def billing_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    pid = (body.get("object") or {}).get("id")
    if pid:
        billing.sync_payment(db, pid)  # re-reads the payment from YooKassa; the body itself is not trusted
    return JSONResponse({"ok": True})


# ---------- owner's page ----------

@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, user: User = Depends(auth.require_admin), db: Session = Depends(get_db)):
    month = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(
        select(User.email, func.count(Usage.id), func.coalesce(func.sum(Usage.cost_rub), 0))
        .join(Usage, Usage.user_id == User.id).where(Usage.created_at >= month)
        .group_by(User.email).order_by(func.sum(Usage.cost_rub).desc())
    ).all()
    stats = {
        "users": db.scalar(select(func.count(User.id))),
        "situations": db.scalar(select(func.count(Situation.id))),
        "plans_month": db.scalar(select(func.count(Plan.id)).where(Plan.created_at >= month)),
        "cost_month": round(db.scalar(select(func.coalesce(func.sum(Usage.cost_rub), 0)).where(Usage.created_at >= month)), 2),
        "shtabs": db.scalar(select(func.count(Shtab.id))),
    }
    return page(request, "admin.html", user=user, stats=stats, rows=rows)
