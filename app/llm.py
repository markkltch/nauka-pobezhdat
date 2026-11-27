"""One door to the language models: Yandex AI Studio, GigaChat, or a fake for development and tests.

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
    tokens_in: int
    tokens_out: int
    tier: str = "light"

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


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

    def chat(self, messages: list[dict], tier: str, temperature: float, max_tokens: int) -> Reply:
        s = get_settings()
        model = s.gigachat_strong_model if tier == "strong" else s.gigachat_light_model
        r = httpx.post(
            self.chat_url,
            headers={"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"},
            json={"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
            verify=self._verify(),
            timeout=s.llm_timeout_s,
        )
        r.raise_for_status()
        body = r.json()
        return _openai_reply(body, model, tier)


def _openai_reply(body: dict, model: str, tier: str) -> Reply:
    usage = body.get("usage") or {}
    return Reply(text=body["choices"][0]["message"]["content"] or "", model=model, tier=tier,
                 tokens_in=int(usage.get("prompt_tokens", 0)), tokens_out=int(usage.get("completion_tokens", 0)))


class YandexAIStudio:
    """Yandex AI Studio through its OpenAI-compatible Chat Completions API (API key of a service account)."""

    def chat(self, messages: list[dict], tier: str, temperature: float, max_tokens: int) -> Reply:
        s = get_settings()
        model = s.yandex_strong_model if tier == "strong" else s.yandex_light_model
        r = httpx.post(
            f"{s.yandex_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Api-Key {s.yandex_api_key}", "OpenAI-Project": s.yandex_folder_id},
            json={"model": f"gpt://{s.yandex_folder_id}/{model}", "messages": messages,
                  "temperature": temperature, "max_tokens": max_tokens},
            timeout=s.llm_timeout_s,
        )
        if r.status_code >= 400:
            log.error("Yandex AI Studio %s: %s", r.status_code, r.text[:500])
        r.raise_for_status()
        return _openai_reply(r.json(), model, tier)


class Fake:
    """Deterministic answers in the real shapes, so the whole product works without a key."""

    def chat(self, messages: list[dict], tier: str, temperature: float, max_tokens: int) -> Reply:
        system = messages[0]["content"]
        if "РЕЖИМ: ГЛАЗОМЕР" in system:
            text = json.dumps({"title": "Ситуация в команде", "ready": False, "questions": [
                "Кто ещё участвует и кто может решить вопрос своей властью?",
                "Сколько у вас времени и что будет, если не успеть?",
            ]}, ensure_ascii=False)
        elif "РЕЖИМ: ПЛАН" in system:
            text = json.dumps({
                "title": "План на две недели",
                "summary": "Сначала разберитесь в причинах один на один, потом задайте одно правило для всех.",
                "scene": "Опоздания стали нормой для двоих, остальные видят, что за это ничего не бывает.",
                "cause": "Скорее всего, правило не названо вслух и не проверяется; проверьте в разговоре один на один.",
                "goal": "Порядок в команде за 14 дней без потери людей.",
                "first_day": "Сегодня до конца смены договоритесь о разговоре с каждым из двоих на завтра.",
                "steps": [
                    {"text": "Поговорите с каждым один на один и узнайте причину", "owner": "Вы", "due_in_days": 1, "kind": "talk"},
                    {"text": "Объявите одно правило для всех и что будет при нарушении", "owner": "Вы", "due_in_days": 2, "kind": "talk"},
                    {"text": "Отмечайте приход смены каждый день", "owner": "Старший смены", "due_in_days": 3, "kind": "action"},
                    {"text": "Контрольная точка: опоздания за неделю", "owner": "Вы", "due_in_days": 7, "kind": "check"},
                ],
                "risks": [{"risk": "Люди воспримут правило как наказание", "counter": "Объясните, зачем оно и что оно защищает"}],
                "victory": "Ни одного опоздания за 5 смен подряд.",
                "principle_id": "maneuver",
                "principle_why": "Каждый должен понимать не только что делать, но и зачем.",
                "warning": "",
                "people": [{"name": "Пётр Сидоров", "role": "кладовщик"}, {"name": "пётр  сидоров", "role": ""}],
            }, ensure_ascii=False)
        elif "РЕЖИМ: ПОДГОТОВКА" in system:
            text = json.dumps({
                "aim": "Понять причину и договориться о приходе вовремя.",
                "opening": "Иван, хочу поговорить о начале смены — мне важно понять, что мешает.",
                "points": ["Факты: три опоздания за неделю", "Как это бьёт по отгрузке", "Что нужно от него"],
                "objections": [{"they": "Автобус опаздывает", "you": "Давайте посмотрим, какой рейс раньше, или сдвинем начало на 10 минут на месяц"}],
                "close": "Договоритесь о конкретном времени прихода и проверке через неделю.",
            }, ensure_ascii=False)
        elif "РЕЖИМ: РАЗБОР ПОСЛЕ ДЕЛА" in system:
            text = json.dumps({"verdict": "partial", "summary": "Опоздания сократились, но не исчезли.",
                               "lesson": "Правило работает, когда его проверяют каждый день, а не раз в неделю.",
                               "next": "Ещё неделю отмечайте приход ежедневно."}, ensure_ascii=False)
        elif "РЕЖИМ: УЧЕНИЕ" in system:
            text = json.dumps({"ok": True, "good": "Суть схвачена: есть цель и проверка понимания.",
                               "add": "Добавьте срок — без него задача остаётся пожеланием."}, ensure_ascii=False)
        else:
            text = "Хороший вопрос. Начните с первого шага плана сегодня и вернитесь ко мне с результатом."
        return Reply(text=text, model=f"fake-{tier}", tier=tier,
                     tokens_in=estimate_tokens(messages), tokens_out=min(max_tokens, len(text) // 3))


_PROVIDERS = {"gigachat": GigaChat, "yandex": YandexAIStudio, "fake": Fake}
_instance = None


def provider():
    global _instance
    name = get_settings().llm_provider
    if _instance is None or type(_instance) is not _PROVIDERS[name]:
        _instance = _PROVIDERS[name]()
    return _instance


def chat(messages: list[dict], tier: str = "light", temperature: float = 0.4, max_tokens: int = 600) -> Reply:
    try:
        return provider().chat(messages, tier, temperature, max_tokens)
    except httpx.HTTPError as e:
        log.exception("LLM call failed")
        raise LLMError("Модель не ответила") from e


def _prices(tier: str) -> tuple[float, float]:
    s = get_settings()
    if tier == "strong":
        return s.price_strong_in_per_1k, s.price_strong_out_per_1k
    return s.price_light_in_per_1k, s.price_light_out_per_1k


def cost_rub(reply: Reply) -> float:
    p_in, p_out = _prices(reply.tier)
    return round((reply.tokens_in * p_in + reply.tokens_out * p_out) / 1000, 4)


def estimate_tokens(messages: list[dict]) -> int:
    chars = sum(len(m["content"]) for m in messages)
    return int(chars / get_settings().chars_per_token_estimate) + 10 * len(messages)


def max_cost_rub(messages: list[dict], tier: str, max_tokens: int) -> float:
    """An upper estimate of one call, checked against the разбор budget before the call is made."""
    p_in, p_out = _prices(tier)
    return (estimate_tokens(messages) * p_in + max_tokens * p_out) / 1000


_JSON = re.compile(r"\{.*\}", re.S)


def parse_json(text: str) -> dict:
    """Models sometimes wrap JSON in prose or ``` fences; take the outermost object."""
    m = _JSON.search(text)
    if not m:
        raise ValueError("no JSON object in reply")
    return json.loads(m.group(0))
