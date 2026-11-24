"""Web routes."""
from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth, billing, jobs, llm, mentor
from .config import get_settings
from .db import get_db, init_db
from .models import LEVELS, Member, Plan, Shtab, Situation, Step, Usage, User, now

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
templates.env.globals.update(LEVELS=LEVELS, settings=get_settings)


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
def login_form(request: Request, next: str = "/app", user: User | None = Depends(auth.current_user)):
    if user:
        return see_other(safe_next(next))
    return page(request, "login.html", next=safe_next(next), step="email")


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
def logout():
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


# ---------- the mentor ----------

def _sidebar(db: Session, user: User) -> dict:
    situations = db.scalars(select(Situation).where(Situation.user_id == user.id).order_by(Situation.updated_at.desc())).all()
    shtabs = my_shtabs(db, user)
    return {"situations": situations, "shtabs": shtabs, "plans_used": mentor.plans_this_month(db, user),
            "plan_limit": get_settings().monthly_plan_limit}


def my_shtabs(db: Session, user: User) -> list[Shtab]:
    ids = db.scalars(select(Member.shtab_id).where(Member.user_id == user.id)).all()
    return db.scalars(select(Shtab).where(Shtab.id.in_(ids)).order_by(Shtab.id)).all() if ids else []


def _gate(db: Session, user: User) -> RedirectResponse | None:
    if not user.level:
        return see_other("/onboarding")
    if not billing.has_access(db, user):
        return see_other("/billing")
    return None


@app.get("/app", response_class=HTMLResponse)
def app_home(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    return page(request, "app.html", user=user, current=None, **_sidebar(db, user),
                limit=mentor.limit_reached(db, user))


def _own_situation(db: Session, user: User, sid: int) -> Situation:
    sit = db.get(Situation, sid)
    if not sit or sit.user_id != user.id:
        raise HTTPException(404)
    return sit


@app.post("/app/new")
def app_new(text: str = Form(...), user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    if not text.strip():
        return see_other("/app")
    if mentor.limit_reached(db, user):
        return see_other("/app?limit=1")
    sit = Situation(user_id=user.id)
    db.add(sit)
    db.flush()
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
                limit=mentor.limit_reached(db, user) and sit.stage != "follow")


@app.post("/app/{sid}/say")
def app_say(sid: int, text: str = Form(...), user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
    if r := _gate(db, user):
        return r
    sit = _own_situation(db, user, sid)
    if not text.strip():
        return see_other(f"/app/{sid}")
    if sit.stage != "follow" and mentor.limit_reached(db, user):
        return see_other(f"/app/{sid}")
    if len(sit.messages) >= get_settings().messages_per_situation:
        return see_other(f"/app/{sid}?err=2")
    try:
        mentor.handle(db, user, sit, text)
    except llm.LLMError:
        db.rollback()
        return see_other(f"/app/{sid}?err=1")
    return see_other(f"/app/{sid}#end")


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
    owned = db.scalars(select(Shtab).where(Shtab.owner_id == user.id)).all()
    return page(request, "billing.html", user=user, owned=owned, has_access=billing.has_access(db, user),
                enabled=get_settings().billing_enabled)


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
