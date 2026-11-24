"""Attachments: the mentor reads the text of a document (txt, md, csv, docx, pdf).

Only the extracted text is kept (as a message of the разбор); the file itself is not stored.
"""
from __future__ import annotations

import io
import re
import zipfile

MAX_BYTES = 5 * 1024 * 1024
KINDS = ".txt .md .csv .docx .pdf"


class FileError(ValueError):
    pass


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _docx(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError):
        raise FileError("Не удалось открыть документ Word") from None
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S)
    lines = ["".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, flags=re.S)) for p in paras]
    text = "\n".join(l for l in lines if l.strip())
    return (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&apos;", "'"))


def _pdf(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise FileError("PDF пока не поддерживается на этом сервере — пришлите текст или .docx") from None
    try:
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:30])
    except Exception:  # noqa: BLE001 — broken PDFs raise many kinds of errors
        raise FileError("Не удалось прочитать PDF") from None


def extract(name: str, raw: bytes) -> str:
    if len(raw) > MAX_BYTES:
        raise FileError("Файл больше 5 МБ")
    low = name.lower()
    if low.endswith((".txt", ".md", ".csv")):
        text = _decode(raw)
    elif low.endswith(".docx"):
        text = _docx(raw)
    elif low.endswith(".pdf"):
        text = _pdf(raw)
    else:
        raise FileError(f"Этот формат не читается. Подойдут: {KINDS}")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise FileError("В файле нет текста (скан или картинка?)")
    return text
