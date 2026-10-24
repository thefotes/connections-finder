"""Workday adapter — tenant/shard/site discovery + paged posting fetch.

Workday is the single biggest unresolved bucket in `data/02-unresolved.csv`: Capital One,
PayPal, Adobe, Salesforce, Boeing, NBCUniversal, Disney, Lockheed Martin, American Express,
Cisco, Edward Jones and a long tail. Unlike Greenhouse/Ashby/Lever there is no
`{token} -> board` probe: a board is `{tenant}.wd{N}.myworkdayjobs.com/{site}` and all
three parts are unknown.

## The discovery oracle

`GET https://{tenant}.wd{N}.myworkdayjobs.com/robots.txt` answers BOTH unknowns in one
request:

    HTTP 200 ->  Sitemap: https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/siteMap.xml
                 User-agent: *
                 Allow: /NVIDIAExternalCareerSite/          <- the site path(s)
    HTTP 422 ->  tenant does not live on this shard (also what a bogus tenant returns)

So the search space is `len(tenant_candidates) x len(SHARDS)` cheap GETs, and a hit hands
us the site name instead of making us guess it. Measured against nvidia: wd5 returns 200
with the site, every other shard and a nonexistent tenant return 422.

`SHARDS` is not `wd1..wd105`. Only twelve `wd{N}.myworkdayjobs.com` hostnames resolve in
DNS at all (measured 2026-09-10); the other ninety-odd fail to resolve, which is why the
sweep is affordable.

## Postings

`POST /wday/cxs/{tenant}/{site}/jobs` with `{"appliedFacets":{},"limit":20,"offset":N,
"searchText":""}` returns `{"total": int, "jobPostings":[{title, externalPath,
locationsText, postedOn, bulletFields}]}`. `limit` is capped at 20 server-side, so a
2,000-posting board is 100 requests.

The list carries no description, so Stage 3 follows up with
`GET /wday/cxs/{tenant}/{site}{externalPath}` per posting — gated by `title_may_be_in_scope`
so we only pay for titles that could survive the Stage 4 title filter.

## Verification

A tenant token match is explicitly NOT proof of company. The job-detail payload
carries `hiringOrganization.name` — the Workday legal entity, e.g. "2100 NVIDIA USA" — and
that, plus sample titles, is what Stage 2 hands the LLM verifier. Workday is treated as a
low-trust provider exactly like Recruitee/SmartRecruiters.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import re
import time

import httpx

import config
from pipeline.common import _store_http, cached_http_get, log, normalize_company

# Only these wd{N} hosts resolve in DNS (measured 2026-09-10 over wd1..wd110). Ordered
# most-populated-first so the common case short-circuits early.
SHARDS: list[str] = getattr(
    config, "WORKDAY_SHARDS",
    ["wd1", "wd5", "wd3", "wd103", "wd12", "wd10", "wd102", "wd105", "wd104", "wd107",
     "wd108", "wd109"],
)

_HOST = "https://{tenant}.{shard}.myworkdayjobs.com"
_ALLOW_RE = re.compile(r"^Allow:\s*/([^/\s]+)/?\s*$", re.M)
_SITEMAP_RE = re.compile(r"^Sitemap:\s*https?://[^/]+/([^/\s]+)/", re.M)
# robots.txt paths that are Workday plumbing, not career sites
_NOT_A_SITE = {"talentcommunity", "refreshfacet", "wday", "en-us", "static", "assets"}


# ------------------------------------------------------------------------------------
# URLs
# ------------------------------------------------------------------------------------
def robots_url(tenant: str, shard: str) -> str:
    return _HOST.format(tenant=tenant, shard=shard) + "/robots.txt"


def jobs_url(tenant: str, shard: str, site: str) -> str:
    return _HOST.format(tenant=tenant, shard=shard) + f"/wday/cxs/{tenant}/{site}/jobs"


def detail_url(tenant: str, shard: str, site: str, external_path: str) -> str:
    return _HOST.format(tenant=tenant, shard=shard) + f"/wday/cxs/{tenant}/{site}{external_path}"


def posting_url(tenant: str, shard: str, site: str, external_path: str) -> str:
    """The canonical public listing URL (non-negotiable on every record)."""
    return _HOST.format(tenant=tenant, shard=shard) + f"/{site}{external_path}"


def board_url(tenant: str, shard: str, site: str) -> str:
    """Stable identifier for the board; also what Stage 3 keys its page cache from."""
    return jobs_url(tenant, shard, site)


def parse_board_url(url: str) -> tuple[str, str, str] | None:
    """Inverse of `board_url` / a pasted browser URL -> (tenant, shard, site).

    Accepts both the cxs form and what a user would paste out of the address bar:
      https://capitalone.wd12.myworkdayjobs.com/en-US/Capital_One
      https://capitalone.wd12.myworkdayjobs.com/wday/cxs/capitalone/Capital_One/jobs
    """
    m = re.match(r"https?://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/(.*)$", (url or "").strip())
    if not m:
        return None
    tenant, shard, rest = m.group(1), m.group(2), m.group(3)
    cxs = re.match(rf"wday/cxs/{re.escape(tenant)}/([^/?#]+)", rest)
    if cxs:
        return tenant, shard, cxs.group(1)
    parts = [p for p in rest.split("?")[0].split("#")[0].split("/") if p]
    parts = [p for p in parts if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    if parts:
        return tenant, shard, parts[0]
    return None


# ------------------------------------------------------------------------------------
# Tenant candidates
# ------------------------------------------------------------------------------------
def tenant_candidates(display_name: str) -> list[str]:
    """Workday tenants are almost always the compacted company name (`capitalone`,
    `nbcuniversal`, `lockheedmartin`). Hyphens appear occasionally; a bare first word
    ('capital', 'american', 'walt') is a false-positive magnet, so it is only tried when
    it is long enough to be distinctive — and verification still has to pass."""
    n = normalize_company(display_name)
    words = n.split()
    if not words:
        return []
    compact = "".join(words)
    cands = [compact]
    if len(words) > 1:
        cands.append("-".join(words))
        if len(words[0]) >= 6:
            cands.append(words[0])
    return [c for c in dict.fromkeys(cands) if len(c) >= 3][: config.WORKDAY_MAX_TENANT_TOKENS]


# ------------------------------------------------------------------------------------
# Discovery
# ------------------------------------------------------------------------------------
def sites_from_robots(body: str) -> list[str]:
    """Career-site paths advertised by a tenant's robots.txt, most-canonical first."""
    out: list[str] = []
    for m in _SITEMAP_RE.finditer(body or ""):
        out.append(m.group(1))
    for m in _ALLOW_RE.finditer(body or ""):
        out.append(m.group(1))
    seen: list[str] = []
    for s in out:
        if s.lower() in _NOT_A_SITE or s in seen:
            continue
        seen.append(s)
    return seen


async def _probe_get_many(urls: list[str], *, deadline: float | None = None) -> None:
    """Cached GETs tuned for the discovery sweep, not for the shared crawler.

    `http_get_many` is wrong here: its 20s timeout x 3 retries means a handful of hosts
    that hang can occupy every one of the 10 concurrency slots for ~90s each, which is
    exactly what happened on the first measured run (2 req/s instead of 28). A shard
    probe is a 200-byte static file — it either answers in under a second or it is not
    going to. Short timeout, one retry, wider concurrency, and DNS is given a real thread
    pool because ~1,100 distinct FQDNs otherwise queue behind asyncio's default executor.
    """
    todo = [u for u in dict.fromkeys(urls) if cached_http_get(u) is None]
    if not todo:
        return
    # ~1,100 distinct FQDNs otherwise queue behind asyncio's small default executor for
    # DNS. Install our own pool, and shut it down at the end of the sweep — installing a
    # new one per shard wave without closing the last leaks a thread pool each time.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=64, thread_name_prefix="wd-dns")
    try:
        asyncio.get_running_loop().set_default_executor(pool)
    except RuntimeError:
        pass

    conc = config.WORKDAY_DISCOVERY_CONCURRENCY
    sem = asyncio.Semaphore(conc)
    hdrs = {"User-Agent": config.USER_AGENT, "Accept": "text/plain, */*"}
    async with httpx.AsyncClient(
        timeout=config.WORKDAY_PROBE_TIMEOUT, headers=hdrs, follow_redirects=True,
        limits=httpx.Limits(max_connections=conc + 20, max_keepalive_connections=conc),
    ) as client:

        async def one(u: str) -> None:
            async with sem:
                if deadline and time.time() > deadline:
                    return
                for attempt in range(2):
                    try:
                        r = await client.get(u)
                        if r.status_code == 429 and attempt == 0:
                            await asyncio.sleep(config.HTTP_BACKOFF_BASE)
                            continue
                        _store_http(u, r.status_code, r.text)
                        return
                    except Exception:  # noqa: BLE001 — a probe that fails is a miss
                        if attempt == 0:
                            continue
                        # Deliberately NOT cached. A transport failure is not evidence
                        # that the tenant doesn't exist, and _store_http(u, 0, "") would
                        # make cached_http_get report it as a settled miss forever.
                        return

        done = 0
        tasks = [asyncio.create_task(one(u)) for u in todo]
        for t in asyncio.as_completed(tasks):
            await t
            done += 1
            if done % 500 == 0 or done == len(tasks):
                log(f"  workday probe {done}/{len(tasks)} "
                    f"({len(urls) - len(todo)} from cache)")
    pool.shutdown(wait=False)


async def discover_many(
    tenants: list[str], *, shards: list[str] | None = None, deadline: float | None = None,
) -> dict[str, list[tuple[str, list[str]]]]:
    """{tenant: [(shard, [site, ...]), ...]} for every tenant that exists on some shard.

    Probed shard-by-shard so a tenant found on wd1 costs one request, not twelve: each
    wave only carries the tenants still unaccounted for. One cached GET per (tenant,
    shard); re-running is free from the HTTP cache.
    """
    shards = shards or SHARDS
    found: dict[str, list[tuple[str, list[str]]]] = {}
    remaining = list(dict.fromkeys(tenants))
    for i, s in enumerate(shards):
        if not remaining:
            break
        if deadline and time.time() > deadline:
            log(f"  workday: discovery timebox hit after shard wave {i} of {len(shards)}; "
                f"{len(remaining)} tenant candidates left unprobed")
            break
        log(f"  workday: shard wave {i + 1}/{len(shards)} ({s}) over "
            f"{len(remaining)} tenant candidates")
        await _probe_get_many([robots_url(t, s) for t in remaining], deadline=deadline)
        still = []
        for t in remaining:
            resp = cached_http_get(robots_url(t, s))
            sites = sites_from_robots(resp["body"]) if resp and resp["status"] == 200 else []
            if sites:
                found.setdefault(t, []).append((s, sites))
            else:
                still.append(t)
        remaining = still
    return found


# ------------------------------------------------------------------------------------
# POST with the same on-disk cache contract as GET
# ------------------------------------------------------------------------------------
def _post_cache_url(url: str, body: dict) -> str:
    """Synthetic cache key so POSTs share the URL-keyed HTTP cache. The body hash is part
    of the key, so page 3 and page 4 of the same board are distinct entries."""
    h = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return f"{url}#POST:{h}"


def cached_post(url: str, body: dict) -> dict | None:
    return cached_http_get(_post_cache_url(url, body))


async def post_many(reqs: list[tuple[str, dict]], *, concurrency: int | None = None) -> dict[str, dict]:
    """[(url, json_body)] -> {cache_key: response}. Cached, concurrency-capped, backs off
    on 429/5xx exactly like `http_get_many`."""
    concurrency = concurrency or config.HTTP_CONCURRENCY
    hdrs = {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    results: dict[str, dict] = {}
    todo: list[tuple[str, str, dict]] = []
    seen: set[str] = set()
    for url, body in reqs:
        key = _post_cache_url(url, body)
        if key in seen:
            continue
        seen.add(key)
        c = cached_http_get(key)
        if c is not None:
            results[key] = c
        else:
            todo.append((key, url, body))
    if not todo:
        return results

    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, headers=hdrs,
                                 follow_redirects=True) as client:

        async def one(key: str, url: str, body: dict) -> None:
            async with sem:
                for attempt in range(config.HTTP_RETRIES + 1):
                    try:
                        r = await client.post(url, json=body)
                        if r.status_code in (429, 500, 502, 503, 504) and attempt < config.HTTP_RETRIES:
                            await asyncio.sleep(config.HTTP_BACKOFF_BASE * (2 ** attempt))
                            continue
                        _store_http(key, r.status_code, r.text)
                        results[key] = {"url": key, "status": r.status_code, "body": r.text,
                                        "cached": False}
                        return
                    except Exception as e:  # noqa: BLE001 — cache the failure too
                        transient = isinstance(e, (httpx.TimeoutException, httpx.RemoteProtocolError))
                        if transient and attempt < config.HTTP_RETRIES:
                            await asyncio.sleep(config.HTTP_BACKOFF_BASE * (2 ** attempt))
                            continue
                        _store_http(key, 0, "")
                        results[key] = {"url": key, "status": 0, "body": "", "cached": False,
                                        "error": str(e)}

        done = 0
        tasks = [asyncio.create_task(one(k, u, b)) for (k, u, b) in todo]
        for t in asyncio.as_completed(tasks):
            await t
            done += 1
            if done % 100 == 0 or done == len(tasks):
                log(f"  workday http-post {done}/{len(tasks)} "
                    f"({len(reqs) - len(todo)} from cache)")
    return results


def page_body(offset: int, limit: int | None = None) -> dict:
    return {"appliedFacets": {}, "limit": limit or config.WORKDAY_PAGE_SIZE,
            "offset": offset, "searchText": ""}


def parse_jobs_page(body: str) -> tuple[list[dict], int]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return [], 0
    if not isinstance(payload, dict) or not isinstance(payload.get("jobPostings"), list):
        return [], 0
    # `total` comes from the remote tenant; a malformed value ("1,200", a dict, …)
    # must degrade to 0, not crash the stage-3 run.
    try:
        total = int(payload.get("total") or 0)
    except (TypeError, ValueError, OverflowError):  # OverflowError: 1e999 -> inf
        total = 0
    return payload["jobPostings"], total


async def probe_site(tenant: str, shard: str, site: str) -> tuple[list[dict], int]:
    """One cheap page-0 POST. Non-empty -> the (tenant, shard, site) triple is real."""
    url = jobs_url(tenant, shard, site)
    body = page_body(0, limit=config.WORKDAY_PAGE_SIZE)
    res = await post_many([(url, body)])
    resp = res.get(_post_cache_url(url, body))
    if not resp or resp["status"] != 200:
        return [], 0
    return parse_jobs_page(resp["body"])


async def fetch_all_postings(tenant: str, shard: str, site: str, total: int) -> list[dict]:
    """Every posting on a board, paged. Capped by config.WORKDAY_MAX_POSTINGS_PER_BOARD."""
    url = jobs_url(tenant, shard, site)
    cap = min(total, config.WORKDAY_MAX_POSTINGS_PER_BOARD)
    size = config.WORKDAY_PAGE_SIZE
    bodies = [page_body(off) for off in range(0, cap, size)]
    await post_many([(url, b) for b in bodies])
    out: list[dict] = []
    for b in bodies:
        resp = cached_post(url, b)
        if not resp or resp["status"] != 200:
            continue
        jobs, _ = parse_jobs_page(resp["body"])
        out.extend(jobs)
    # Workday paging can repeat a posting across page boundaries when the board mutates
    # mid-crawl; externalPath is the stable identity.
    seen: set[str] = set()
    uniq = []
    for j in out:
        p = str(j.get("externalPath") or "")
        if p and p not in seen:
            seen.add(p)
            uniq.append(j)
    return uniq


# ------------------------------------------------------------------------------------
# Detail gating — only pay for descriptions we might actually score
# ------------------------------------------------------------------------------------
# Same signal the Stage 4 title filter uses for its decisive reject: a title with no
# technical signal can never be bucketed leadership or ic, so its description is dead
# weight. Duplicated here (not imported) so Stage 3 does not depend on Stage 4.
_TECH_SIGNAL_RE = re.compile(
    r"\b(engineer|engineering|eng|developer|development|software|swe|technical|technolog|"
    r"platform|devops|infrastructure|infra|backend|back-end|frontend|front-end|"
    r"full[ -]?stack|fullstack|\bdata\b|database|security|\bcloud\b|systems|\bsystem\b|"
    r"mobile|\bios\b|android|\bai\b|\bml\b|\bapi\b|architect|programmer|\bsre\b|"
    r"reliability|\bweb\b|firmware|embedded|blockchain|robotics|machine learning|"
    r"distributed|compiler|kernel|graphics|\bqa\b|automation|\bsdk\b|\bcto\b)\b", re.I)
_NON_US_RE = re.compile(
    r"\b(canada|toronto|vancouver|ontario|united kingdom|\buk\b|london|england|ireland|"
    r"dublin|germany|berlin|munich|france|paris|spain|madrid|barcelona|portugal|lisbon|"
    r"netherlands|amsterdam|poland|warsaw|krak(o|ó)w|romania|bucharest|india|bangalore|"
    r"bengaluru|hyderabad|pune|gurgaon|noida|singapore|australia|sydney|melbourne|"
    r"new zealand|brazil|s(a|ã)o paulo|mexico|argentina|colombia|emea|apac|latam|"
    r"europe|european|philippines|manila|japan|tokyo|china|shanghai|hong kong|israel|"
    r"tel aviv|dubai|\buae\b|nigeria|lagos|kenya|nairobi|south africa|sweden|stockholm|"
    r"denmark|copenhagen|norway|oslo|finland|helsinki|switzerland|zurich|austria|vienna|"
    r"belgium|brussels|czech|prague|hungary|budapest|ukraine|kyiv|bulgaria|sofia|"
    r"costa rica|guatemala|korea|seoul|taiwan|taipei|vietnam|thailand|malaysia|"
    r"indonesia|egypt|cairo|turkey|istanbul|saudi|qatar|chile|peru|uruguay)\b", re.I)
_REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\bwfh\b|\banywhere\b|\bvirtual\b", re.I)


def title_may_be_in_scope(title: str) -> bool:
    return bool(_TECH_SIGNAL_RE.search(title or ""))


def location_may_be_in_scope(locations_text: str) -> bool:
    """Reject only what is unambiguously abroad. A remote posting keeps its detail fetch
    even if a foreign city is named, because Stage 4 needs the description to decide."""
    s = locations_text or ""
    if not s:
        return True
    if _REMOTE_RE.search(s):
        return True
    return not _NON_US_RE.search(s)


def worth_detail_fetch(job: dict) -> bool:
    return (title_may_be_in_scope(str(job.get("title") or ""))
            and location_may_be_in_scope(str(job.get("locationsText") or "")))


# ------------------------------------------------------------------------------------
# Company evidence for board verification
# ------------------------------------------------------------------------------------
def hiring_org_name(detail_body: str) -> str:
    try:
        d = json.loads(detail_body)
    except (json.JSONDecodeError, ValueError):
        return ""
    org = d.get("hiringOrganization") or {}
    return str(org.get("name") or "") if isinstance(org, dict) else ""


def site_matches_company(site: str, company_norm: str) -> bool:
    """Does the career-site path itself name the company? `Etsy_Careers` does,
    `Depop_Careers` does not. Used to pick between the several sites one tenant can host."""
    s = re.sub(r"[^a-z0-9]+", "", (site or "").lower())
    c = re.sub(r"[^a-z0-9]+", "", (company_norm or "").lower())
    return bool(c) and c in s
