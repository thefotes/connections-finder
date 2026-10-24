"""Shared infrastructure: on-disk caching, LLM client, HTTP client, JSONL io,
company normalization, and the regex+LLM hybrid string classifier.

Resumability contract:
  - Every stage reads only data/NN-*.jsonl (the artifact before it) and writes its own.
  - Every network call is cached on disk keyed by URL. A re-run never re-fetches.
  - Every LLM verdict is cached on disk keyed by the exact input string.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

# GitHub rejects blobs >100MB. Any JSONL artifact bigger than this is transparently
# stored gzipped (data/NN-*.jsonl.gz); read_jsonl falls back to the .gz automatically.
GZIP_OVER_BYTES = 40 * 1024 * 1024

import httpx
from openai import AsyncOpenAI, OpenAI

import config
from pipeline import settings

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
HTTP_META = CACHE / "http"        # committed: {url,status,ts,body_sha} — inspectable
HTTP_RAW = CACHE / "raw"          # gitignored: raw response bodies
LLM_CACHE = CACHE / "llm"         # committed: full prompt+response per call

for _d in (DATA, CACHE, HTTP_META, HTTP_RAW, LLM_CACHE):
    _d.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------------------------
# JSONL / JSON io
# ------------------------------------------------------------------------------------
def _open_text(path: Path, mode: str):
    if str(path).endswith(".gz"):
        return gzip.open(path, mode + "t", encoding="utf-8")
    return path.open(mode, encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict], *, autogzip: bool = True) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with _open_text(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            n += 1
    # if the plain artifact is too big for git, re-store it gzipped and drop the plain
    if autogzip and not str(path).endswith(".gz") and path.stat().st_size > GZIP_OVER_BYTES:
        gz = path.with_suffix(path.suffix + ".gz")
        with path.open("rb") as src, gzip.open(gz, "wb") as dst:
            dst.writelines(src)
        path.unlink()
        log(f"  {path.name} ({path_size(gz)}) stored gzipped -> {gz.name}")
    return n


def path_size(p: Path) -> str:
    b = p.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f}{unit}"
        b /= 1024
    return f"{b:.0f}TB"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        gz = path.with_suffix(path.suffix + ".gz")
        if gz.exists():
            path = gz
        else:
            return []
    out = []
    # split on "\n" ONLY — not str.splitlines(), which also breaks on U+2028/U+2029
    # and other unicode separators that json.dumps(ensure_ascii=False) leaves raw.
    with _open_text(path, "r") as f:
        for line in f.read().split("\n"):
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def append_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            n += 1
    return n


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------------------------
# HTTP with on-disk cache keyed by URL
# ------------------------------------------------------------------------------------
# Every outbound crawler request must land on an allow-listed ATS host — including
# redirect hops. The single source of truth lives here so pipeline/audit.py and the
# clients below enforce the same list.
ATS_URL_ALLOW_RE = re.compile(
    r"^https://("
    r"boards-api\.greenhouse\.io|api\.ashbyhq\.com|api\.lever\.co|"
    r"api\.smartrecruiters\.com|apply\.workable\.com|"
    r"[a-z0-9-]+\.recruitee\.com|"
    r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)/", re.I)

_MAX_REDIRECTS = 5


def _validate_redirect(current_url: str, location: str) -> str:
    """Resolve a Location header against the current URL and require it to stay on
    an allow-listed ATS host. Raises on anything else (client code caches the
    failure like any other network error)."""
    nxt = str(httpx.URL(current_url).join(location))
    if not ATS_URL_ALLOW_RE.match(nxt):
        raise ValueError(f"blocked redirect to non-allow-listed host: {nxt}")
    return nxt


async def _get_no_follow(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET with manual redirects: every hop is re-validated against the allow-list."""
    r = await client.get(url, follow_redirects=False)
    for _ in range(_MAX_REDIRECTS):
        if not r.is_redirect:
            return r
        url = _validate_redirect(url, r.headers.get("location", ""))
        r = await client.get(url, follow_redirects=False)
    raise ValueError(f"too many redirects fetching {url}")


def _url_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def cached_http_get(url: str) -> dict | None:
    """Return the cached response for `url` if present: {url,status,body,ts}. Else None."""
    meta_p = HTTP_META / f"{_url_key(url)}.json"
    if not meta_p.exists():
        return None
    meta = json.loads(meta_p.read_text())
    body = ""
    if meta.get("body_sha"):
        bp = HTTP_RAW / f"{meta['body_sha']}.body"
        if bp.exists():
            body = bp.read_text(encoding="utf-8", errors="replace")
    return {"url": url, "status": meta["status"], "body": body, "ts": meta.get("ts"), "cached": True}


def _store_http(url: str, status: int, body: str) -> None:
    body_sha = ""
    if body:
        body_sha = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()
        (HTTP_RAW / f"{body_sha}.body").write_text(body, encoding="utf-8")
    (HTTP_META / f"{_url_key(url)}.json").write_text(
        json.dumps({"url": url, "status": status, "ts": time.time(), "body_sha": body_sha}),
        encoding="utf-8",
    )


async def http_get_many(
    urls: list[str], *, concurrency: int | None = None, headers: dict | None = None
) -> dict[str, dict]:
    """Fetch many URLs with caching, concurrency cap, and backoff on 429/5xx.

    Returns {url: {url,status,body,ts,cached}}. Cached URLs are served from disk and
    never re-fetched.
    """
    concurrency = concurrency or config.HTTP_CONCURRENCY
    hdrs = {"User-Agent": config.USER_AGENT, "Accept": "application/json, */*"}
    if headers:
        hdrs.update(headers)

    results: dict[str, dict] = {}
    to_fetch: list[str] = []
    for u in dict.fromkeys(urls):
        c = cached_http_get(u)
        if c is not None:
            results[u] = c
        else:
            to_fetch.append(u)

    if not to_fetch:
        return results

    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        timeout=config.HTTP_TIMEOUT, headers=hdrs, follow_redirects=False
    ) as client:

        async def one(u: str) -> None:
            async with sem:
                for attempt in range(config.HTTP_RETRIES + 1):
                    try:
                        r = await _get_no_follow(client, u)
                        status, text = r.status_code, r.text
                        # retry transient server errors only; 429 is cached-and-skipped
                        # (bulk token probing; a rate-limited provider is dropped)
                        if status in (500, 502, 503, 504) and attempt < config.HTTP_RETRIES:
                            await asyncio.sleep(config.HTTP_BACKOFF_BASE * (2 ** attempt))
                            continue
                        _store_http(u, status, text)
                        results[u] = {"url": u, "status": status, "body": text,
                                      "ts": time.time(), "cached": False}
                        return
                    except Exception as e:  # noqa: BLE001 — cache the failure too
                        # retry timeouts; do not retry DNS/connection failures (a
                        # non-existent {token}.recruitee.com just does not exist)
                        transient = isinstance(e, (httpx.TimeoutException, httpx.RemoteProtocolError))
                        if transient and attempt < config.HTTP_RETRIES:
                            await asyncio.sleep(config.HTTP_BACKOFF_BASE * (2 ** attempt))
                            continue
                        _store_http(u, 0, "")
                        results[u] = {"url": u, "status": 0, "body": "",
                                      "ts": time.time(), "cached": False, "error": str(e)}

        # progress
        done = 0
        tasks = [asyncio.create_task(one(u)) for u in to_fetch]
        for t in asyncio.as_completed(tasks):
            await t
            done += 1
            if done % 50 == 0 or done == len(tasks):
                log(f"  http {done}/{len(tasks)} fetched (of {len(urls)} requested, "
                    f"{len(urls) - len(to_fetch)} from cache)")
    return results


# ------------------------------------------------------------------------------------
# LLM client — OpenAI SDK, strict json_schema, on-disk cache
# Endpoint and key come from OPENAI_BASE_URL / OPENAI_API_KEY (.env or the environment);
# any OpenAI-compatible server works. The SDK's own retries are off: llm_json_many
# retries itself.
# ------------------------------------------------------------------------------------
# vLLM/LiteLLM-style controls to turn off reasoning on local models. Only sent when
# LLM_DISABLE_THINKING=1: OpenAI itself rejects unknown request fields.
NO_THINKING = ({"chat_template_kwargs": {"enable_thinking": False}, "include_reasoning": False}
               if config.LLM_DISABLE_THINKING else None)
def _llm_key(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _llm_payload(prompt: str, schema: dict, *, schema_name: str, temperature: float,
                 max_tokens: int, system: str | None) -> dict:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
    }


def _sdk_kwargs(payload: dict) -> dict:
    """Legacy cache payload -> chat.completions.create kwargs.

    The payload keeps its original shape because its hash is the on-disk cache key;
    changing it would invalidate every committed verdict in data/cache/llm/.
    """
    return {
        "model": payload["model"],
        "messages": payload["messages"],
        "temperature": payload["temperature"],
        "max_completion_tokens": payload["max_tokens"],
        "response_format": payload["response_format"],
        "extra_body": NO_THINKING,
    }


def _cached_llm(payload: dict) -> dict | None:
    p = LLM_CACHE / f"{_llm_key(payload)}.json"
    if p.exists():
        rec = json.loads(p.read_text())
        return rec.get("parsed")
    return None


def _store_llm(payload: dict, raw: str, parsed: dict) -> None:
    p = LLM_CACHE / f"{_llm_key(payload)}.json"
    p.write_text(json.dumps({"payload": payload, "raw": raw, "parsed": parsed},
                            ensure_ascii=False, indent=2), encoding="utf-8")


async def llm_json_many(
    items: list[tuple[Any, str]],
    schema: dict,
    *,
    schema_name: str,
    system: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    concurrency: int | None = None,
) -> list[tuple[Any, dict]]:
    """items = [(key, prompt), ...] -> [(key, parsed_json), ...] in the same order.

    Cached per (payload) on disk. strict json_schema is enforced server-side, so the
    response body is parsed directly with json.loads — no repair code.
    """
    concurrency = concurrency or config.LLM_CONCURRENCY
    out: dict[int, tuple[Any, dict]] = {}

    pending: list[tuple[int, Any, dict]] = []
    for i, (k, prompt) in enumerate(items):
        payload = _llm_payload(prompt, schema, schema_name=schema_name,
                               temperature=temperature, max_tokens=max_tokens, system=system)
        c = _cached_llm(payload)
        if c is not None:
            out[i] = (k, c)
        else:
            pending.append((i, k, payload))

    if pending:
        sem = asyncio.Semaphore(concurrency)
        async with AsyncOpenAI(timeout=config.LLM_TIMEOUT, max_retries=0) as client:

            async def one(i: int, k: Any, payload: dict) -> None:
                async with sem:
                    last_err = None
                    for attempt in range(config.LLM_RETRIES + 1):
                        try:
                            resp = await client.chat.completions.create(**_sdk_kwargs(payload))
                            choice = resp.choices[0]
                            content = choice.message.content
                            if choice.finish_reason == "length":
                                raise ValueError("response truncated at max_tokens "
                                                 "(finish_reason=length)")
                            parsed = json.loads(content)
                            _store_llm(payload, content, parsed)
                            out[i] = (k, parsed)
                            return
                        except Exception as e:  # noqa: BLE001
                            last_err = f"{type(e).__name__}: {e}"
                            if attempt < config.LLM_RETRIES:
                                await asyncio.sleep(2.0 * (2 ** attempt))
                    log(f"  LLM FAILED for item {i} ({k!r}): {last_err}")
                    out[i] = (k, {"_llm_error": str(last_err)})

            done = 0
            tasks = [asyncio.create_task(one(i, k, p)) for (i, k, p) in pending]
            for t in asyncio.as_completed(tasks):
                await t
                done += 1
                if done % 100 == 0 or done == len(tasks):
                    log(f"  llm {done}/{len(tasks)} ({len(items) - len(pending)} cached)")

    return [out[i] for i in range(len(items))]


def run(coro):
    return asyncio.run(coro)


_HEALTH_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
                  "required": ["ok"], "additionalProperties": False}


def llm_healthcheck(timeout: float = 30.0) -> tuple[bool, str]:
    """One tiny call to the LLM endpoint using the SAME strict json_schema shape the
    pipeline uses, so a model that cannot do structured outputs fails at setup with a
    clear message instead of mid-pipeline. Not cached. Returns (ok, detail)."""
    model = config.LLM_MODEL
    try:
        with OpenAI(timeout=timeout, max_retries=0) as client:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Return ok=true."}],
                temperature=0,
                max_completion_tokens=32,
                response_format={"type": "json_schema",
                                 "json_schema": {"name": "healthcheck", "strict": True,
                                                 "schema": _HEALTH_SCHEMA}},
                extra_body=NO_THINKING,
            )
        content = (resp.choices[0].message.content or "").strip()
        try:
            parsed = json.loads(content)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("ok") is True:
            return True, f"strict JSON OK ({model})"
        got = content[:80] or "(empty)"
        return False, (f"model {model} did not return valid strict JSON (got: {got}); "
                       "choose a model that supports structured outputs")
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        if "response_format" in str(e) or "json_schema" in str(e):
            detail += "; this model/endpoint may not support strict JSON-schema output"
        return False, detail


# ------------------------------------------------------------------------------------
# Company normalization — deterministic, NO LLM
# ------------------------------------------------------------------------------------
_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|corp|corporation|co|ltd|limited|the|company|"
    r"technologies|technology|group|holdings|gmbh|s\.a|plc|lp|llp)\b",
    re.I,
)
_PAREN_RE = re.compile(r"\(.*?\)")
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def normalize_company(name: str) -> str:
    n = (name or "").lower().strip()
    n = _PAREN_RE.sub(" ", n)
    n = n.replace("&", " and ")
    n = _PUNCT_RE.sub(" ", n)
    n = _SUFFIX_RE.sub(" ", n)
    n = _WS_RE.sub(" ", n).strip()
    return n


def board_token_candidates(name: str) -> list[str]:
    """Guess ATS board tokens. Order = most likely first (see ats_probe.py)."""
    n = normalize_company(name)
    words = n.split()
    if not words:
        return []
    cands = [
        "".join(words),
        "-".join(words),
        words[0],
        "".join(words[:2]) if len(words) > 1 else None,
        "-".join(words[:2]) if len(words) > 1 else None,
    ]
    return list(dict.fromkeys(c for c in cands if c))


# settings.yaml `excluded_companies` (e.g. your current employer), plus staffing
# agencies from config.STAFFING_AGENCIES — not employers; they burn board-resolution
# budget and would flood the roles board with agency listings for unnamed companies.
# Roles board only: recruiters at these firms still flow through the recruiter track,
# which does not consult this set.
STAFFING_AGENCIES_NORM = {normalize_company(x) for x in getattr(config, "STAFFING_AGENCIES", ())}
EXCLUDED_COMPANIES_NORM = (
    {normalize_company(x) for x in settings.load().excluded_companies} | STAFFING_AGENCIES_NORM
)
NON_COMPANY_NORM = {
    normalize_company(x)
    for x in ("", "Self-employed", "Self employed", "Freelance", "Freelancer",
              "Retired", "Stealth", "Stealth Startup", "Stealth Mode", "N/A", "None",
              "Unemployed", "Independent", "Independent Consultant", "Looking for opportunities",
              "Open to work")
}


def is_non_company(norm: str) -> bool:
    if norm in NON_COMPANY_NORM:
        return True
    return bool(re.fullmatch(r"(stealth|self|freelance|retired|unemployed|consultant).*", norm))
