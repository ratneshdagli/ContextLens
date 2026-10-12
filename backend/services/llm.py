"""ContextLens v3 — LLM + embedding client (OpenAI-compatible; built for LM Studio).

Rules that fix the v2 failure modes:
  * Never returns error strings as if they were model output — failures raise `LLMError`.
  * Structured calls use `response_format: json_schema` (grammar-constrained decoding) and are
    validated; one retry with the validation error fed back.
  * Reasoning/thinking output is stripped (<think> tags, `reasoning_content`, "Here's a thinking process").
  * A global semaphore serialises chat calls so a small GPU is never overloaded, and callers
    are expected to batch work per model role to minimise model swaps.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any, Callable, Optional

import httpx
import numpy as np

from backend.config import LLM_API_KEY, LLM_BASE_URL
from backend.services import cloud, lms
from backend.settings_store import get_settings

log = logging.getLogger("llm")

import contextvars
import heapq
import itertools

_prio: contextvars.ContextVar[int] = contextvars.ContextVar("llm_priority", default=1)


def interactive() -> None:
    """Mark the current request/task as user-facing: its LLM calls jump ahead of background work."""
    _prio.set(0)


def background() -> None:
    """Mark the current task as background pipeline work (yields to readers)."""
    _prio.set(1)


class _Gate:
    """One chat call at a time (a small GPU can only serve one), granted in priority order:
    0 = a person is waiting, 1 = background pipeline work."""

    def __init__(self) -> None:
        self._busy = False
        self._heap: list = []
        self._seq = itertools.count()

    async def __aenter__(self):
        if not self._busy and not self._heap:
            self._busy = True
            return self
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (_prio.get(), next(self._seq), fut))
        try:
            await fut
        except BaseException:
            if fut.done() and not fut.cancelled():
                self._release()
            raise
        return self

    def _release(self) -> None:
        while self._heap:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.done():
                fut.set_result(None)
                return
        self._busy = False

    async def __aexit__(self, *exc):
        self._release()


_sem = _Gate()


def gate_busy() -> bool:
    """True while some chat call already holds the (single) GPU slot — informational only, for status messages."""
    return _sem._busy
_models_cache: tuple[float, list[str]] | None = None
_embed_cache: dict[str, np.ndarray] = {}
_schema_unsupported = False

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_PREAMBLE_RE = re.compile(
    r"^\s*(?:here[’']?s a thinking process|thinking process|let me think|okay, let'?s|first, i need to)",
    re.IGNORECASE,
)


class LLMError(Exception):
    """Raised for any LLM/embedding failure. Callers decide whether to fail closed."""


def _headers() -> dict:
    return {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}


def strip_thinking(text: str) -> str:
    text = _THINK_RE.sub("", text or "")
    if "</think>" in text:  # unterminated opening tag
        text = text.split("</think>", 1)[1]
    return text.strip()


def looks_like_garbage(text: str) -> bool:
    """True for leaked reasoning or server-side error strings that must never be stored/shown."""
    t = (text or "").strip()
    if not t:
        return True
    low = t.lower()
    return bool(
        _PREAMBLE_RE.match(t)
        or low.startswith(("[lm studio", "[ai error", "[stream error", "error:"))
        or "thinking process" in low[:120]
        or "analyze user input" in low
        or "**constraint" in low
    )


# ── models ───────────────────────────────────────────────────────────────────

async def list_models(force: bool = False) -> list[str]:
    global _models_cache
    import time
    if not force and _models_cache and time.time() - _models_cache[0] < 30:
        return _models_cache[1]
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(f"{LLM_BASE_URL}/models", headers=_headers())
            r.raise_for_status()
            ids = [m["id"] for m in r.json().get("data", [])]
    except Exception as e:
        raise LLMError(f"LLM server unreachable at {LLM_BASE_URL}: {e}") from e
    _models_cache = (time.time(), ids)
    return ids


async def health() -> dict:
    fb = cloud.configured()
    try:
        ids = await list_models(force=True)
        return {"available": True, "local": True, "models": ids, "base_url": LLM_BASE_URL, "fallback": cloud.status()}
    except LLMError as e:
        # LM Studio is down, but chat still works while any cloud provider is configured
        return {"available": bool(fb), "local": False, "models": [], "base_url": LLM_BASE_URL, "error": str(e), "fallback": cloud.status()}


def _match_model(wanted: str, available: list[str]) -> Optional[str]:
    if wanted in available:
        return wanted
    w = wanted.lower()
    for m in available:
        if m.split(":")[0].lower() == w:
            return m
    for m in available:
        if w in m.lower():
            return m
    return None


async def resolve_model(role: str) -> str:
    """role: writer | editor | embed"""
    s = await get_settings()
    key = {"writer": "model_writer", "editor": "model_editor", "embed": "model_embed"}[role]
    wanted = s[key]
    if role == "editor" and s.get("single_model_mode"):
        wanted = s["model_writer"]
    available = await list_models()
    found = _match_model(wanted, available)
    if not found:
        raise LLMError(f"Model '{wanted}' (role={role}) is not available. Loaded/available: {available}")
    return found


async def _ensure_resident(model: str) -> None:
    """Unload any other chat model and load this one (see lms.py). Runs inside the gate, so swaps never overlap calls."""
    st = await get_settings()
    if not st.get("manage_models", True):
        return
    idle = int(st.get("idle_unload_minutes", 0) or 0)
    ok = await lms.ensure(model, ttl_seconds=idle * 60)
    if not ok:
        raise LLMError(lms.status().get("error") or f"could not load {model} in LM Studio")


# ── chat ─────────────────────────────────────────────────────────────────────

async def _chat_local(
    role: str,
    messages: list[dict],
    *,
    schema: Optional[dict] = None,
    schema_name: str = "output",
    temperature: float = 0.2,
    max_tokens: int = 700,
    timeout: float = 240,
) -> str:
    global _schema_unsupported
    model = await resolve_model(role)
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if schema and not _schema_unsupported:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        }
    async with _sem:
        try:
            await _ensure_resident(model)
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(f"{LLM_BASE_URL}/chat/completions", headers=_headers(), json=payload)
                if r.status_code == 400 and schema and "response_format" in payload:
                    body = r.text.lower()
                    if "response_format" in body or "json_schema" in body or "grammar" in body:
                        # The server really lacks (or rejects) json_schema: retry this call with a plain prompt.
                        # Only remember it server-wide when the server says it does not support the feature at all;
                        # a single schema that fails to compile must not switch off constrained decoding for everything.
                        log.warning("json_schema rejected (%s); retrying with prompt-only JSON", r.text[:160])
                        if "not support" in body or "unsupported" in body:
                            _schema_unsupported = True
                        payload.pop("response_format")
                        payload["messages"] = payload["messages"] + [
                            {"role": "user", "content": "Respond with ONLY a JSON object matching this schema: " + json.dumps(schema)}]
                        r = await c.post(f"{LLM_BASE_URL}/chat/completions", headers=_headers(), json=payload)
                if r.status_code == 400 and "unloaded" in r.text.lower():
                    # someone (the user, LM Studio's idle timer, another tool) unloaded the model while we held it
                    log.warning("model was unloaded under us (%s); reloading and retrying once", r.text[:80])
                    lms.invalidate()
                    await _ensure_resident(model)
                    r = await c.post(f"{LLM_BASE_URL}/chat/completions", headers=_headers(), json=payload)
                if r.status_code >= 400:
                    raise LLMError(f"chat call failed ({role}/{model}): HTTP {r.status_code}: {r.text[:300]}")
                data = r.json()
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"chat call failed ({role}/{model}): {e}") from e
    try:
        msg = data["choices"][0]["message"]
    except Exception as e:
        raise LLMError(f"malformed chat response: {str(data)[:200]}") from e
    content = strip_thinking(msg.get("content") or "")
    if not content:
        raise LLMError(f"empty response from {model} (finish_reason={data['choices'][0].get('finish_reason')})")
    return content


async def _chat_cloud(role: str, messages: list[dict], schema: Optional[dict], temperature: float, max_tokens: int) -> str:
    """Walk the cloud chain (cloud.py). Same contract as chat(): text or LLMError."""
    if schema:  # cloud providers get the schema in the prompt; chat_json validates it
        messages = messages + [{"role": "user", "content": "Respond with ONLY a JSON object matching this schema: " + json.dumps(schema)}]
    try:
        text, who = await cloud.chat(role, messages, temperature=temperature, max_tokens=max_tokens)
    except RuntimeError as e:
        raise LLMError(f"cloud: {e}") from e
    text = strip_thinking(text)
    if looks_like_garbage(text):
        raise LLMError(f"cloud ({who}) returned unusable output")
    log.info("answered by cloud %s", who)
    return text


async def chat(role: str, messages: list[dict], *, schema: Optional[dict] = None, schema_name: str = "output",
               temperature: float = 0.2, max_tokens: int = 700, timeout: float = 240) -> str:
    """Cloud providers first (when keys are configured), LM Studio as the last resort."""
    cloud_err = None
    if cloud.configured():
        try:
            return await _chat_cloud(role, messages, schema, temperature, max_tokens)
        except LLMError as e:
            cloud_err = e
            log.warning("cloud chain failed, using LM Studio: %s", str(e)[:160])
    try:
        return await _chat_local(role, messages, schema=schema, schema_name=schema_name,
                                 temperature=temperature, max_tokens=max_tokens, timeout=timeout)
    except LLMError as e:
        if not cloud_err:
            raise
        raise LLMError(f"{e} | {cloud_err}") from e


def _parse_json(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).rstrip("`").strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    for a, b in (("{", "}"), ("[", "]")):
        i, j = raw.find(a), raw.rfind(b)
        if i >= 0 and j > i:
            try:
                return json.loads(raw[i : j + 1])
            except Exception:
                continue
    fixed = _repair_json(raw)
    if fixed is not None:
        return fixed
    raise ValueError("no valid JSON in model output")


def _repair_json(raw: str) -> Any:
    """Close a JSON object that was cut off mid-way (token limit) or has trailing commas; None if it can't be saved.
    Missing fields are still caught by the caller's validation, so a repaired object is never trusted blindly."""
    i = raw.find("{")
    if i < 0:
        return None
    s = re.sub(r'("|\]|\}|\d|true|false|null)([ \t]*\n\s*")', r'\1,\2', raw[i:].rstrip("`\n "))   # a forgotten comma before the next key
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    s = re.sub(r",\s*$", "", s.rstrip()) + ('"' if in_str else "")
    s = re.sub(r",\s*$", "", s) + "".join(reversed(stack))
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    try:
        return json.loads(s)
    except Exception:
        return None


async def chat_json(
    role: str,
    system: str,
    user: str,
    schema: dict,
    *,
    schema_name: str = "output",
    temperature: float = 0.1,
    max_tokens: int = 900,
    validate: Optional[Callable[[Any], Optional[str]]] = None,
    retries: int = 1,
) -> Any:
    """Schema-constrained JSON call with validation and bounded retry. Raises LLMError on failure."""
    if _schema_unsupported:
        user = user + "\n\nRespond with ONLY a JSON object matching this schema:\n" + json.dumps(schema)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last_err = "unknown"
    for attempt in range(retries + 1):
        raw = await chat(role, messages, schema=schema, schema_name=schema_name,
                         temperature=temperature, max_tokens=max_tokens)
        try:
            obj = _parse_json(raw)
        except ValueError as e:
            last_err = str(e)
            max_tokens = int(max_tokens * 1.5)      # most "no valid JSON" is output cut off at the limit: give the retry room
            log.info("json parse failed (%d chars, tail %r)", len(raw), raw[-60:])
        else:
            err = validate(obj) if validate else None
            if not err:
                return obj
            last_err = err
        messages = messages + [
            {"role": "assistant", "content": raw[:1500]},
            {"role": "user", "content": f"That output was rejected: {last_err}. Return corrected JSON only."},
        ]
    raise LLMError(f"structured output failed validation after {retries + 1} attempts: {last_err}")


async def _stream_local(role: str, messages: list[dict], *, temperature: float = 0.3, max_tokens: int = 700):
    """Async generator of text deltas from LM Studio. Raises LLMError on connection failure."""
    model = await resolve_model(role)
    payload = {"model": model, "messages": messages, "temperature": temperature,
               "max_tokens": max_tokens, "stream": True}
    in_think = False
    async with _sem:
        try:
            await _ensure_resident(model)
            async with httpx.AsyncClient(timeout=240) as c:
                for attempt in (0, 1):
                    async with c.stream("POST", f"{LLM_BASE_URL}/chat/completions", headers=_headers(), json=payload) as r:
                        if r.status_code == 400 and attempt == 0 and "unloaded" in (await r.aread()).decode("utf-8", "replace").lower():
                            log.warning("model was unloaded under us; reloading and retrying the stream once")
                            lms.invalidate()
                            await _ensure_resident(model)
                            continue
                        r.raise_for_status()
                        async for line in r.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            body = line[6:].strip()
                            if body == "[DONE]":
                                break
                            try:
                                delta = json.loads(body)["choices"][0].get("delta", {})
                            except Exception:
                                continue
                            piece = delta.get("content") or ""
                            if not piece:
                                continue
                            if "<think>" in piece:
                                in_think = True
                                piece = piece.split("<think>", 1)[0]
                            if in_think:
                                if "</think>" in piece:
                                    in_think = False
                                    piece = piece.split("</think>", 1)[1]
                                else:
                                    continue
                            if piece:
                                yield piece
                    break
        except Exception as e:
            raise LLMError(f"stream failed: {e}") from e


async def stream_chat(role: str, messages: list[dict], *, temperature: float = 0.3, max_tokens: int = 700):
    """Cloud first (one whole answer — keeps the first-sentence latency low on fast providers), then LM Studio token streaming."""
    cloud_err = None
    if cloud.configured():
        try:
            yield await _chat_cloud(role, messages, None, temperature, max_tokens)
            return
        except LLMError as e:
            cloud_err = e
            log.warning("cloud chain failed, streaming from LM Studio: %s", str(e)[:160])
    try:
        async for piece in _stream_local(role, messages, temperature=temperature, max_tokens=max_tokens):
            yield piece
    except LLMError as e:
        if not cloud_err:
            raise
        raise LLMError(f"{e} | {cloud_err}") from e


# ── embeddings ───────────────────────────────────────────────────────────────

_minilm = None


def _minilm_embed(texts: list[str]) -> np.ndarray:
    global _minilm
    if _minilm is None:
        from sentence_transformers import SentenceTransformer
        _minilm = SentenceTransformer("all-MiniLM-L6-v2")
    return np.asarray(_minilm.encode(texts, normalize_embeddings=True, show_progress_bar=False), dtype=np.float32)


async def embed(texts: list[str], kind: str = "document") -> np.ndarray:
    """L2-normalised embeddings, shape (n, d). kind = document | query (nomic prefixes)."""
    if not texts:
        return np.zeros((0, 1), dtype=np.float32)
    prefix = "search_query: " if kind == "query" else "search_document: "
    keys = [hashlib.sha1((prefix + t).encode("utf-8")).hexdigest() for t in texts]
    missing = [i for i, k in enumerate(keys) if k not in _embed_cache]
    if missing:
        model = None
        try:
            model = await resolve_model("embed")
        except LLMError as e:
            log.warning("embedding model unavailable (%s) — falling back to MiniLM", e)
        vecs: Optional[np.ndarray] = None
        if model:
            last = ""
            for attempt in range(3):
                try:
                    out = []
                    for s_ in range(0, len(missing), 24):
                        batch = [prefix + texts[i][:1500] for i in missing[s_ : s_ + 24]]
                        async with httpx.AsyncClient(timeout=180) as c:
                            r = await c.post(f"{LLM_BASE_URL}/embeddings", headers=_headers(),
                                             json={"model": model, "input": batch})
                            if r.status_code != 200:
                                last = f"{r.status_code}: {r.text[:200]}"
                                raise RuntimeError(last)
                            out.extend(d["embedding"] for d in r.json()["data"])
                    vecs = np.asarray(out, dtype=np.float32)
                    break
                except Exception as e:
                    last = str(e)[:200]
                    await asyncio.sleep(2 + attempt * 3)
            if vecs is None:
                log.warning("embedding call failed after retries (%s) — falling back to MiniLM", last)
        if vecs is None:
            if os.getenv("EMBED_FALLBACK") != "1":
                raise LLMError("Embedding model unavailable. Load '" + (model or "the embedding model") + "' in LM Studio "
                               "(or set EMBED_FALLBACK=1 to use the slower built-in MiniLM, which needs ~1 GB RAM).")
            loop = asyncio.get_running_loop()
            vecs = await loop.run_in_executor(None, _minilm_embed, [texts[i][:1800] for i in missing])
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        vecs = vecs / np.clip(norms, 1e-9, None)
        for i, v in zip(missing, vecs):
            _embed_cache[keys[i]] = v
        if len(_embed_cache) > 20000:
            for k in list(_embed_cache)[:5000]:
                _embed_cache.pop(k, None)
    return np.stack([_embed_cache[k] for k in keys])


def to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def from_blob(b: Optional[bytes]) -> Optional[np.ndarray]:
    if not b:
        return None
    return np.frombuffer(b, dtype=np.float32)
