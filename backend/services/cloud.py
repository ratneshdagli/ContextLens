"""Cloud LLM chain for ContextLens: `llm.chat` / `llm.stream_chat` try these providers FIRST and fall back to LM Studio only if all fail.

OpenAI-compatible endpoints only; keys come from backend/.env (a provider with no key is skipped). Each provider has a
circuit breaker: a failure parks it for a while (429 → rate-limit cool-down, auth/404 → long, 5xx/timeout → short) so the
next call goes straight to the next one. Everything sent (story text, Ask/voice questions) leaves the machine — CLOUD_FALLBACK=0 keeps it all local.
Embeddings never use this chain (vectors must stay in one space).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx

log = logging.getLogger("cloud")

# name: (base_url, key env, model, extra body, min max_tokens)  — models verified live against these keys
_P = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-20b", {"reasoning_effort": "low"}, 3000),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY", "open-mistral-nemo", {}, 0),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "gemini-2.5-flash", {}, 1500),
    "cloudflare": ("https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1", "CLOUDFLARE_API_TOKEN",
                   "@cf/meta/llama-3.3-70b-instruct-fp8-fast", {}, 0),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY", "nvidia/nemotron-3-super-120b-a12b", {}, 1500),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "nvidia/nemotron-3-super-120b-a12b:free", {}, 1500),
}
# the editor is an independent judge: lead with different model families than the writer
_ORDER = {
    "writer": ["groq", "gemini", "mistral", "nvidia", "cloudflare", "openrouter"],
    "editor": ["mistral", "cloudflare", "gemini", "groq", "nvidia", "openrouter"],
}
_down: dict[str, float] = {}


def enabled() -> bool:
    return os.getenv("CLOUD_FALLBACK", "1") != "0"


def configured() -> list[str]:
    ok = [n for n, (b, k, *_r) in _P.items() if os.getenv(k) and ("{CLOUDFLARE_ACCOUNT_ID}" not in b or os.getenv("CLOUDFLARE_ACCOUNT_ID"))]
    return ok if enabled() else []


def status() -> dict:
    now = time.time()
    return {n: ("cooling" if _down.get(n, 0) > now else "ready") for n in configured()}


def _park(name: str, code: int | None) -> None:
    secs = 60 if code == 429 else 600 if code in (401, 403, 404, 410) else 90
    _down[name] = time.time() + secs
    log.warning("cloud/%s failed (%s) — parked %ss", name, code or "timeout/network", secs)


async def chat(role: str, messages: list[dict], *, temperature: float, max_tokens: int, timeout: float = 90) -> tuple[str, str]:
    """Try each ready provider in the role's order. Returns (text, "provider/model"). Raises RuntimeError when all fail."""
    now, errs = time.time(), []
    for name in _ORDER.get(role, _ORDER["writer"]):
        if name not in configured() or _down.get(name, 0) > now:
            continue
        base, keyenv, model, extra, min_tok = _P[name]
        base = base.replace("{CLOUDFLARE_ACCOUNT_ID}", os.getenv("CLOUDFLARE_ACCOUNT_ID", ""))
        payload = {"model": model, "messages": messages, "temperature": temperature,
                   "max_tokens": max(max_tokens, min_tok), **extra}
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {os.getenv(keyenv)}"}, json=payload)
            if r.status_code != 200:
                _park(name, r.status_code)
                errs.append(f"{name}:{r.status_code}")
                continue
            text = (r.json()["choices"][0]["message"].get("content") or "").strip()
            if not text:  # reasoning ate the budget, or a blank answer: try the next one
                _park(name, 0)
                errs.append(f"{name}:empty")
                continue
            return text, f"{name}/{model}"
        except Exception as e:
            _park(name, None)
            errs.append(f"{name}:{type(e).__name__}")
    raise RuntimeError("no cloud provider answered (" + ", ".join(errs or ["none configured/ready"]) + ")")


def clear() -> None:
    _down.clear()
