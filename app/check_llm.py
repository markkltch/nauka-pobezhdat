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


if __name__ == "__main__":
    main()
