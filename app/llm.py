"""One door to the language models: GigaChat, YandexGPT, or a fake for development and tests.

Two tiers: "light" asks the clarifying questions, "strong" writes the plan. Every call is
written to the Usage ledger with its tokens and cost, so the unit economics can be checked.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass

import httpx

from .config import get_settings

log = logging.getLogger("llm")


@dataclass
class Reply:
    text: str
    model: str
    tokens: int


class LLMError(RuntimeError):
    pass


# ---------- providers ----------

class GigaChat:
    oauth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    chat_url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

    def __init__(self) -> None:
        self._token = ""
        self._token_exp = 0.0
        self._lock = threading.Lock()

    def _verify(self):
        return get_settings().gigachat_ca_bundle or True

    def _access_token(self) -> str:
        s = get_settings()
        with self._lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            r = httpx.post(
                self.oauth_url,
                headers={"Authorization": f"Basic {s.gigachat_auth_key}", "RqUID": str(uuid.uuid4()),
                         "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                data={"scope": s.gigachat_scope},
                verify=self._verify(),
                timeout=20,
            )
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            self._token_exp = body.get("expires_at", 0) / 1000 or time.time() + 25 * 60
            return self._token

    def chat(self, messages: list[dict], tier: str, temperature: float) -> Reply:
        s = get_settings()
        model = s.gigachat_strong_model if tier == "strong" else s.gigachat_light_model
        r = httpx.post(
            self.chat_url,
            headers={"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"},
            json={"model": model, "messages": messages, "temperature": temperature},
            verify=self._verify(),
            timeout=s.llm_timeout_s,
        )
        r.raise_for_status()
        body = r.json()
        text = body["choices"][0]["message"]["content"]
        return Reply(text=text, model=model, tokens=int(body.get("usage", {}).get("total_tokens", 0)))


class YandexGPT:
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    def chat(self, messages: list[dict], tier: str, temperature: float) -> Reply:
        s = get_settings()
        model = s.yandex_strong_model if tier == "strong" else s.yandex_light_model
        r = httpx.post(
            self.url,
            headers={"Authorization": f"Api-Key {s.yandex_api_key}", "x-folder-id": s.yandex_folder_id},
            json={
                "modelUri": f"gpt://{s.yandex_folder_id}/{model}",
                "completionOptions": {"stream": False, "temperature": temperature, "maxTokens": 2000},
                "messages": [{"role": m["role"], "text": m["content"]} for m in messages],
            },
            timeout=s.llm_timeout_s,
        )
        r.raise_for_status()
        result = r.json()["result"]
        text = result["alternatives"][0]["message"]["text"]
        return Reply(text=text, model=model, tokens=int(result.get("usage", {}).get("totalTokens", 0)))


class Fake:
    """Deterministic answers in the real shapes, so the whole product works without a key."""

    def chat(self, messages: list[dict], tier: str, temperature: float) -> Reply:
        system = messages[0]["content"]
        if "РЕЖИМ: ВОПРОСЫ" in system:
            text = json.dumps({"title": "Ситуация в команде", "ready": False, "questions": [
                "Кто ещё участвует и кто может решить вопрос своей властью?",
                "Сколько у вас времени и что будет, если не успеть?",
            ]}, ensure_ascii=False)
        elif "РЕЖИМ: ПЛАН" in system:
            text = json.dumps({
                "title": "План на две недели",
                "summary": "Сначала разберитесь в причинах один на один, потом задайте одно правило для всех.",
                "goal": "Порядок в команде за 14 дней без потери людей.",
                "steps": [
                    {"text": "Поговорите с каждым участником один на один и узнайте причину", "owner": "Вы", "due_in_days": 1},
                    {"text": "Объявите одно правило для всех и что будет при нарушении", "owner": "Вы", "due_in_days": 2},
                    {"text": "Проведите короткий разбор: что изменилось", "owner": "Вы", "due_in_days": 7},
                ],
                "principle_id": "maneuver",
                "principle_why": "Каждый должен понимать не только что делать, но и зачем.",
                "warning": "",
            }, ensure_ascii=False)
        else:
            text = "Хороший вопрос. Начните с первого шага плана сегодня и вернитесь ко мне с результатом."
        return Reply(text=text, model=f"fake-{tier}", tokens=400 if tier == "light" else 900)


_PROVIDERS = {"gigachat": GigaChat, "yandex": YandexGPT, "fake": Fake}
_instance = None


def provider():
    global _instance
    name = get_settings().llm_provider
    if _instance is None or type(_instance) is not _PROVIDERS[name]:
        _instance = _PROVIDERS[name]()
    return _instance


def chat(messages: list[dict], tier: str = "light", temperature: float = 0.4) -> Reply:
    try:
        return provider().chat(messages, tier, temperature)
    except httpx.HTTPError as e:
        log.exception("LLM call failed")
        raise LLMError("Модель не ответила") from e


def cost_rub(reply: Reply, tier: str) -> float:
    s = get_settings()
    price = s.price_strong_per_1k if tier == "strong" else s.price_light_per_1k
    return round(reply.tokens / 1000 * price, 4)


_JSON = re.compile(r"\{.*\}", re.S)


def parse_json(text: str) -> dict:
    """Models sometimes wrap JSON in prose or ``` fences; take the outermost object."""
    m = _JSON.search(text)
    if not m:
        raise ValueError("no JSON object in reply")
    return json.loads(m.group(0))
