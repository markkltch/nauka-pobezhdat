"""Voice messages: Yandex SpeechKit short-audio recognition (API v1, LPCM 16 kHz mono, up to 30 s).

The browser records raw 16-bit PCM itself (Web Audio), so no audio conversion is needed on the server.
SpeechKit bills by 15-second pieces; every recognition is written to Usage and counted per person per month.
"""
from __future__ import annotations

import math

import httpx
from sqlalchemy.orm import Session

from . import access
from .config import get_settings
from .models import Usage, User, now

URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
RATE = 16000


class VoiceError(RuntimeError):
    pass


def month_key() -> str:
    return now().strftime("%Y-%m")


def left(db: Session, user: User) -> int:
    s = get_settings()
    cap = s.voice_per_month if access.tier(db, user) == access.PRO else s.voice_free_per_month
    used = user.voice_used if user.voice_month == month_key() else 0
    return max(0, cap - used)


def seconds(pcm: bytes) -> float:
    return len(pcm) / (RATE * 2)


def recognize(db: Session, user: User, pcm: bytes) -> str:
    s = get_settings()
    if not pcm:
        raise VoiceError("Пустая запись")
    if seconds(pcm) > s.voice_max_seconds + 1:
        raise VoiceError(f"Запись длиннее {s.voice_max_seconds} секунд")
    if left(db, user) <= 0:
        raise VoiceError("Голосовые на этот месяц закончились")
    if s.llm_provider == "fake":
        text = "Тестовая расшифровка голосового сообщения."
    else:
        key = s.yandex_speech_api_key or s.yandex_api_key
        r = httpx.post(URL, params={"lang": "ru-RU", "format": "lpcm", "sampleRateHertz": RATE,
                                    "folderId": s.yandex_folder_id, "topic": "general"},
                       headers={"Authorization": f"Api-Key {key}"}, content=pcm, timeout=40)
        if r.status_code >= 400:
            raise VoiceError(f"SpeechKit {r.status_code}: {r.text[:200]}")
        text = (r.json().get("result") or "").strip()
    pieces = max(1, math.ceil(seconds(pcm) / 15))
    db.add(Usage(user_id=user.id, model="speechkit", purpose="voice", tokens=0,
                 cost_rub=round(pieces * s.price_voice_per_15s, 4)))
    if user.voice_month != month_key():
        user.voice_month, user.voice_used = month_key(), 0
    user.voice_used = (user.voice_used or 0) + 1
    db.commit()
    return text
