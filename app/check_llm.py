"""Smoke test of the configured model provider: one tiny call per tier, prints model, tokens and cost.
   cd ~/nauka-app && ~/nauka-venv/bin/python -m app.check_llm"""
from . import llm
from .config import get_settings


def main() -> None:
    s = get_settings()
    print(f"provider: {s.llm_provider}")
    for tier in ("light", "strong"):
        try:
            r = llm.chat([{"role": "system", "content": "Отвечай одним словом."},
                          {"role": "user", "content": "Столица России?"}], tier=tier, max_tokens=20)
            print(f"{tier}: OK {r.model} — «{r.text.strip()[:40]}», {r.tokens_in}+{r.tokens_out} токенов, "
                  f"{llm.cost_rub(r):.4f} ₽")
        except llm.LLMError as e:
            print(f"{tier}: ОШИБКА {e}")
    if s.llm_provider != "fake":
        import httpx

        from .speech import RATE, URL
        key = s.yandex_speech_api_key or s.yandex_api_key
        r = httpx.post(URL, params={"lang": "ru-RU", "format": "lpcm", "sampleRateHertz": RATE, "folderId": s.yandex_folder_id},
                       headers={"Authorization": f"Api-Key {key}"}, content=b"\x00\x00" * RATE, timeout=30)
        print("speech: OK SpeechKit отвечает" if r.status_code == 200 else f"speech: ОШИБКА {r.status_code} {r.text[:160]}")


if __name__ == "__main__":
    main()
