"""Sign-in with Yandex ID (OAuth 2.0, authorization code).

The app is registered at https://oauth.yandex.ru with the callback
{base_url}/auth/yandex/callback and the permission «Доступ к адресу электронной почты».
Until YANDEX_OAUTH_CLIENT_ID is set, the button is not shown.
"""
from __future__ import annotations

from urllib.parse import urlencode

import httpx

from .config import get_settings

AUTHORIZE = "https://oauth.yandex.ru/authorize"
TOKEN = "https://oauth.yandex.ru/token"
INFO = "https://login.yandex.ru/info"


class YandexIdError(RuntimeError):
    pass


def enabled() -> bool:
    s = get_settings()
    return bool(s.yandex_oauth_client_id and s.yandex_oauth_client_secret)


def callback_url() -> str:
    return f"{get_settings().base_url}/auth/yandex/callback"


def authorize_url(state: str) -> str:
    s = get_settings()
    return AUTHORIZE + "?" + urlencode({
        "response_type": "code", "client_id": s.yandex_oauth_client_id,
        "redirect_uri": callback_url(), "state": state, "force_confirm": "no",
    })


def profile(code: str) -> dict:
    """Exchange the code for a token and read the person's id, email and name."""
    s = get_settings()
    r = httpx.post(TOKEN, data={"grant_type": "authorization_code", "code": code,
                                "client_id": s.yandex_oauth_client_id,
                                "client_secret": s.yandex_oauth_client_secret}, timeout=20)
    if r.status_code != 200:
        raise YandexIdError(f"token {r.status_code}")
    access = r.json().get("access_token")
    r = httpx.get(INFO, params={"format": "json"}, headers={"Authorization": f"OAuth {access}"}, timeout=20)
    if r.status_code != 200:
        raise YandexIdError(f"info {r.status_code}")
    info = r.json()
    email = (info.get("default_email") or "").strip().lower()
    if not info.get("id") or "@" not in email:
        raise YandexIdError("Яндекс не передал почту")
    return {"id": str(info["id"]), "email": email,
            "name": (info.get("real_name") or info.get("display_name") or "").strip()}
