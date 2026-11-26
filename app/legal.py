"""Who runs the service, in one place: the legal pages, the footer and the order printout read from here."""

from .config import get_settings

EDITION = "24 ноября 2026 г."
IP_EDITION = "26 ноября 2026 г."

OPERATOR = {
    "full": "Автономная некоммерческая организация высшего образования «Открытый гуманитарный институт»",
    "short": "АНО ВО «ОГИ»",
    "ogrn": "1237700304126",
    "registration_label": "ОГРН",
    "inn": "9710113703",
    "kpp": "771001001",
    "address": "125375, г. Москва, вн. тер. г. муниципальный округ Тверской, Страстной бульвар, д. 6, стр. 2, помещ. 1/4",
    "head": "ректор Русецкая Маргарита Николаевна",
    "phone": "+7 (495) 132-62-23",
    "email": "info@oginst.ru",
    "site": "https://ogiinst.ru",
    "rkn": "77-25-362816",
    "bank": "р/с 40703810800000734982 в АО «ТБанк», БИК 044525974, к/с 30101810145250000974",
}

# Those who handle data on the operator's instruction (ст. 6 ч. 3, ст. 9 ч. 4 п. 7 152-ФЗ).
PROCESSORS = (
    {"name": "ООО «ЯНДЕКС.ОБЛАКО»", "inn": "7704458262",
     "address": "119021, г. Москва, ул. Льва Толстого, д. 16, помещ. 528",
     "what": "языковая модель и распознавание речи (Yandex AI Studio, SpeechKit)"},
    {"name": "ООО «РЕГ.РУ»", "inn": "7733568767",
     "address": "г. Москва, Ленинградский проспект, д. 72, корп. 3",
     "what": "хостинг сервера и почты в России"},
)
YOOKASSA = {"name": "ООО НКО «ЮМани» (ЮKassa)", "inn": "7750005725",
            "address": "115035, г. Москва, ул. Садовническая, д. 82, стр. 2",
            "what": "приём оплаты и отправка кассовых чеков"}


def entrepreneur() -> dict:
    s = get_settings()
    return {
        "full": "Индивидуальный предприниматель Стариков Матвей",
        "short": "ИП Стариков Матвей",
        "ogrn": "326508100589159",
        "registration_label": "ОГРНИП",
        "inn": "504325366979",
        "registration_date": "30.09.2026",
        "registrar": "Межрайонная ИФНС России № 23 по Московской области",
        "address": s.merchant_postal_address or OPERATOR["address"],
        "phone": s.merchant_phone or OPERATOR["phone"],
        "email": s.merchant_email or OPERATOR["email"],
        "rkn": s.merchant_rkn_number,
    }


def current() -> dict:
    s = get_settings()
    tbank = s.payment_provider == "tbank"
    ip_operator = s.personal_data_operator == "ip"
    return {
        "MERCHANT": entrepreneur() if tbank else OPERATOR,
        "LEGAL": entrepreneur() if ip_operator else OPERATOR,
        "PROCESSORS": PROCESSORS if tbank else (*PROCESSORS, YOOKASSA),
        "OFFER_EDITION": IP_EDITION if tbank else EDITION,
        "EDITION": IP_EDITION if ip_operator else EDITION,
        "TBANK": tbank,
    }
