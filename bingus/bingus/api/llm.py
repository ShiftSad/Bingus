"""Sumários e respostas sobre páginas por uma API compatível com a da OpenAI: o OpenRouter ou o
llama.cpp local do compose. Sem chave, tudo devolve None."""

import logging
import os
import re
import time

import httpx

log = logging.getLogger("bingus.llm")

MODEL = os.environ.get("BINGUS_LLM_MODEL", "inclusionai/ling-3.0-flash")
KEY = os.environ.get("OPENROUTER_KEY")
BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# Da página; o resto raramente muda um sumário. O modelo local foi medido com 4000.
MAX_CHARS = int(os.environ.get("BINGUS_LLM_CHARS", 12_000))
TTL = 12 * 3600  # respostas com prompt vivem só na RAM
CACHE_MAX = 1000

SUMMARY = (
    "Resuma o texto desta página web em até três frases, em português, de forma objetiva."
    " Sem introdução, sem opinião, só o que a página diz."
)
ANSWER = (
    "Responda em português à pergunta do usuário usando apenas o texto da página abaixo."
    " Se a página não responde, diga isso em uma frase."
)

client = httpx.AsyncClient(
    base_url=BASE_URL, headers={"Authorization": f"Bearer {KEY}"}, timeout=30
)
# (content_hash, prompt) -> (expira em, resposta)
answers: dict[tuple[bytes, str], tuple[float, str]] = {}


async def complete(system: str, user: str, max_tokens: int) -> str | None:
    if not KEY:
        return None
    r = await client.post(
        "/chat/completions",
        json={
            "model": MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,  # modelo pequeno que não para sozinho não segura a fila
            "temperature": 0,
        },
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


async def summarize(text: str) -> str | None:
    summary = await complete(SUMMARY, text[:MAX_CHARS], 200)
    # o resultado da busca mostra o texto cru: sai o markdown que o modelo põe em termo e código
    return re.sub(r"[*`]", "", summary) if summary else summary


async def ask(content_hash: bytes, text: str, prompt: str) -> str | None:
    key = (content_hash, prompt)
    hit = answers.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    answer = await complete(ANSWER, f"Pergunta: {prompt}\n\nPágina:\n{text[:MAX_CHARS]}", 500)
    if answer:
        if len(answers) >= CACHE_MAX:
            answers.clear()
        answers[key] = (time.monotonic() + TTL, answer)
    return answer
