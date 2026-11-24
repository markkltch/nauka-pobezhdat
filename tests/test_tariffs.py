import datetime as dt

import httpx
import pytest
from sqlalchemy import select

from app import access, llm, mentor
from app.config import get_settings
from app.db import SessionLocal
from app.models import Member, Shtab, Situation, Subscription, User, now


@pytest.fixture
def settings():
    s = get_settings()
    saved = s.model_dump()
    yield s
    for k, v in saved.items():
        setattr(s, k, v)


def _user(db, email):
    u = db.scalars(select(User).where(User.email == email)).first()
    if not u:
        u = User(email=email, level="mid")
        db.add(u)
        db.flush()
    return u


def test_free_tier_and_packs(settings):
    settings.free_beta = False
    with SessionLocal() as db:
        u = _user(db, "free@example.ru")
        assert access.tier(db, u) == access.FREE
        assert access.allowance(db, u) == settings.free_plans_per_month == 1
        assert access.can_start(db, u)
        access.consume(db, u)
        db.add(Situation(user_id=u.id))
        db.flush()
        assert not access.can_start(db, u)  # free month used up

        u.extra_plans = 1  # a bought pack
        assert access.can_start(db, u)
        access.consume(db, u)
        assert u.extra_plans == 0
        db.rollback()


def test_shtab_seats_cover_members_in_join_order(settings):
    settings.free_beta = False
    with SessionLocal() as db:
        owner, first, late = (_user(db, f"seat{i}@example.ru") for i in range(3))
        sh = Shtab(name="Seats", owner_id=owner.id)
        db.add(sh)
        db.flush()
        for i, u in enumerate((owner, first, late)):
            db.add(Member(shtab_id=sh.id, user_id=u.id, joined_at=now() + dt.timedelta(seconds=i)))
        db.add(Subscription(payer_id=owner.id, shtab_id=sh.id, seats=2, status="active",
                            paid_until=now() + dt.timedelta(days=10)))
        db.flush()
        assert access.tier(db, owner) == access.PRO
        assert access.tier(db, first) == access.PRO
        assert access.tier(db, late) == access.FREE  # joined after payment, not paid for yet
        db.rollback()


def test_budget_stops_a_long_talk(settings):
    settings.budget_per_situation_rub = 3.0
    with SessionLocal() as db:
        u = _user(db, "budget@example.ru")
        sit = Situation(user_id=u.id)
        db.add(sit)
        db.flush()
        mentor.handle(db, u, sit, "Двое в смене опаздывают")
        mentor.handle(db, u, sit, "Одни и те же")
        assert sit.stage == "follow"
        with pytest.raises(mentor.BudgetSpent):
            for _ in range(30):
                mentor.handle(db, u, sit, "А если так? " * 50)
        assert mentor.spent(db, sit) <= settings.budget_per_situation_rub
        assert not mentor.can_talk(db, u, sit)


def test_worst_case_per_seat_keeps_1200_net():
    """30 разборов at the hard budget, acquiring 3.5 % + VAT on it, УСН 6 %."""
    s = get_settings()
    worst_ai = 30 * s.budget_per_situation_rub
    net = s.price_per_seat_rub * (1 - 0.035 * 1.22 - 0.06) - worst_ai
    assert net >= 1200, net


def test_yandex_ai_studio_request(settings, monkeypatch):
    settings.llm_provider, settings.yandex_api_key, settings.yandex_folder_id = "yandex", "KEY", "b1gfolder"
    seen = {}

    def fake_post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, body=json)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    reply = llm.chat([{"role": "user", "content": "hi"}], tier="strong", max_tokens=700)
    assert seen["url"] == "https://ai.api.cloud.yandex.net/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Api-Key KEY"
    assert seen["body"]["model"] == "gpt://b1gfolder/aliceai-llm/latest"
    assert seen["body"]["max_tokens"] == 700
    assert llm.cost_rub(reply) == pytest.approx(1000 * 0.5 / 1000 + 500 * 1.2 / 1000)
