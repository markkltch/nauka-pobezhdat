"""T-Bank's hosted checkout. The service never handles card details."""
from __future__ import annotations

import hashlib
import hmac
import re
import ssl
import uuid
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import certifi
import httpx

from .config import get_settings

API = "https://securepay.tinkoff.ru/v2"
PREFIX = "tbank:"
PAYMENT_HOSTS = {
    "securepayments.tinkoff.ru",
    "securepayments.tbank.ru",
    "securepay.tinkoff.ru",
    "securepay.tbank.ru",
    "pay.linksecurepayment.com",  # Hosted checkout URL returned by T-Bank's Init API.
}
CANCELED_STATUSES = {"CANCELED", "REJECTED", "AUTH_FAIL", "DEADLINE_EXPIRED", "REVERSED", "REFUNDED"}
FINAL_STATUSES = CANCELED_STATUSES | {"CONFIRMED"}


class PaymentError(Exception):
    """The bank did not return a usable payment or status."""


def token(payload: dict, password: str) -> str:
    values = {k: v for k, v in payload.items() if k != "Token" and type(v) in (str, int, bool)}
    values["Password"] = password
    joined = "".join(str(values[k]).lower() if isinstance(values[k], bool) else str(values[k])
                     for k in sorted(values))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=Path(__file__).with_name("certs") / "russian-trusted-ca.pem")
    return context


def _request(method: str, **data) -> dict:
    s = get_settings()
    if not s.tbank_credentials:
        raise PaymentError("T-Bank terminal is not configured")
    payload = {"TerminalKey": s.tbank_terminal_key, **data}
    payload["Token"] = token(payload, s.tbank_password)
    try:
        response = httpx.post(f"{API}/{method}", json=payload, timeout=15.0, verify=tls_context())
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PaymentError(f"T-Bank {method} request failed") from exc
    if not isinstance(result, dict) or result.get("Success") is not True:
        raise PaymentError(f"T-Bank rejected {method}")
    return result


def _provider_key(payment_id, order_id) -> str | None:
    if type(payment_id) not in (int, str) or not re.fullmatch(r"[0-9]{1,20}", str(payment_id)):
        return None
    if not isinstance(order_id, str) or not re.fullmatch(r"[0-9a-f]{32}", order_id):
        return None
    return f"{PREFIX}{payment_id}:{order_id}"


def init(amount: int, description: str, email: str, return_path: str) -> tuple[str, str]:
    s = get_settings()
    base = s.base_url.rstrip("/")
    order_id = uuid.uuid4().hex
    item = {"Name": description[:128], "Price": amount, "Quantity": 1, "Amount": amount,
            "Tax": s.tbank_vat, "PaymentMethod": "full_payment", "PaymentObject": "service"}
    if s.tbank_ffd == "1.2":
        item["MeasurementUnit"] = "шт"
    result = _request(
        "Init", OrderId=order_id, Amount=amount, Description=description[:128], PayType="O",
        SuccessURL=base + return_path, FailURL=base + return_path,
        NotificationURL=base + "/billing/tbank/webhook",
        Receipt={"Email": email, "Taxation": s.tbank_taxation, "Items": [item]},
    )
    key = _provider_key(result.get("PaymentId"), result.get("OrderId"))
    url = result.get("PaymentURL")
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
    except ValueError:
        parsed = None
    if (not key or result["OrderId"] != order_id or result.get("Amount") != amount
            or result.get("TerminalKey") != s.tbank_terminal_key
            or not parsed or parsed.scheme != "https" or parsed.netloc not in PAYMENT_HOSTS):
        raise PaymentError("T-Bank returned invalid checkout data")
    return key, url


def notification_key(payload: object) -> str | None:
    s = get_settings()
    if not isinstance(payload, dict) or not s.tbank_credentials or payload.get("TerminalKey") != s.tbank_terminal_key:
        return None
    signature = payload.get("Token")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        return None
    if not hmac.compare_digest(signature, token(payload, s.tbank_password)):
        return None
    return _provider_key(payload.get("PaymentId"), payload.get("OrderId"))


def state(provider_id: str, amount: int) -> str:
    _, bank_id, _ = provider_id.split(":")
    result = _request("GetState", PaymentId=bank_id)
    if (_provider_key(result.get("PaymentId"), result.get("OrderId")) != provider_id
            or result.get("TerminalKey") != get_settings().tbank_terminal_key):
        raise PaymentError("T-Bank returned a different order")
    status = result.get("Status")
    if status == "CONFIRMED":
        if result.get("Amount") != amount:
            raise PaymentError("T-Bank returned a different amount")
        return "succeeded"
    if status in CANCELED_STATUSES:
        return "canceled"
    return "pending"
