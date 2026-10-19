"""Stage 2 — resolve job boards.

data/01-connections-scored.jsonl (unique companies) ->
    data/02-boards.jsonl              (resolved + verified)
    data/02-unresolved.csv           (hand-patch file for tomorrow)
    data/02-resolution-review.csv    (~all inexact / LLM-judged rows to eyeball)

Providers, priority order: hand-mapped overrides from data/manual-boards.yaml, then
Greenhouse, Ashby, Lever (core), then SmartRecruiters, Recruitee (optional, timeboxed
45 min), then Workday (own timebox, see pipeline/providers/workday.py).

Verification is MANDATORY: an HTTP 200 is not proof the board belongs to
the company. Auto-accept exact-normalization tokens; LLM-verify truncations / first-word
guesses / ambiguous tokens; reject on negative or low-confidence.
"""
from __future__ import annotations

import csv
import json
import time
from collections import Counter
from pathlib import Path

import config
from pipeline.common import (
    DATA, board_token_candidates, cached_http_get, http_get_many, is_non_company, llm_json_many,
    log, normalize_company, read_jsonl, run, write_jsonl, EXCLUDED_COMPANIES_NORM,
)
from pipeline.providers import manual as manual_boards
from pipeline.providers import workday as wd

CORE_PROVIDERS = ["greenhouse", "ashby", "lever"]
# Workable is DROPPED for bulk probing: it returns HTTP 429 (rate limited) on nearly
# every token probe, which makes it both useless and slow here. A rate-limited provider
# is explicitly allowed to be dropped and noted.
OPTIONAL_PROVIDERS = ["smartrecruiters", "recruitee"]
DROPPED_PROVIDERS = ["workable (HTTP 429 on bulk token probing)"]
# Providers where an HTTP 200 on a guessed token is worth little on its own: vanity
# subdomains, demo accounts, and — for Workday — tenant names that belong to whoever
# registered them first. Every hit goes to the LLM verifier.
LOW_TRUST_PROVIDERS = ("recruitee", "smartrecruiters", "workday")

ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=true",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=true",
    "lever": "https://api.lever.co/v0/postings/{t}?mode=json",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=100",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{t}?details=true",
    "recruitee": "https://{t}.recruitee.com/api/offers/",
}

MAX_TOKENS_CORE = 3
MAX_TOKENS_OPTIONAL = 2


def board_url(provider: str, token: str) -> str:
    return ENDPOINTS[provider].format(t=token)


def extract_jobs(provider: str, body: str) -> tuple[list[dict], str | None]:
    """Return (jobs_list, board_name_or_None). jobs_list items are provider-native."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return [], None

    if provider == "greenhouse":
        if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
            name = None
            meta = payload.get("meta") or {}
            if isinstance(meta, dict):
                name = meta.get("title")
            return payload["jobs"], name
        return [], None
    if provider == "ashby":
        if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
            return payload["jobs"], payload.get("name") or payload.get("organizationName")
        return [], None
    if provider == "lever":
        if isinstance(payload, list):
            return payload, None
        return [], None
    if provider == "smartrecruiters":
        if isinstance(payload, dict) and isinstance(payload.get("content"), list):
            jobs = payload["content"]
            name = None
            if jobs and isinstance(jobs[0], dict):
                co = jobs[0].get("company") or {}
                name = (co.get("name") or "").rstrip("_") or co.get("identifier")
            return jobs, name
        return [], None
    if provider == "workable":
        if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
            return payload["jobs"], payload.get("name") or payload.get("description")
        return [], None
    if provider == "workday":
        if isinstance(payload, dict) and isinstance(payload.get("jobPostings"), list):
            return payload["jobPostings"], None
        return [], None
    if provider == "recruitee":
        if isinstance(payload, dict) and isinstance(payload.get("offers"), list):
            offers = payload["offers"]
            # Recruitee serves demo data on unclaimed subdomains ("...-sample-...").
            real = [o for o in offers
                    if "sample" not in str(o.get("careers_url", "")).lower()
                    and "sample" not in str(o.get("slug", "")).lower()]
            return (real if real else []), None
        return [], None
    return [], None


def sample_titles(provider: str, jobs: list[dict], n: int = 5) -> list[str]:
    out = []
    for j in jobs[:n]:
        if provider == "lever":
            out.append(str(j.get("text", "")))
        elif provider == "smartrecruiters":
            out.append(str(j.get("name", "")))
        else:
            out.append(str(j.get("title", "")))
    return [t for t in out if t]


VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "match": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
    },
    "required": ["match", "confidence", "reason"],
    "additionalProperties": False,
}
VERIFY_SYS = (
    "You verify whether a public ATS job board belongs to a specific company. You are "
    "given the company name, the ATS token used, the board's own display name if any, "
    "and a few job titles from the board. Answer whether this board is operated by that "
    "exact company (not a different company with a similar name, not a staffing agency "
    "posting on their behalf). Be strict: 'Capital One' must not match a board named "
    "'Capital' belonging to a real-estate firm. For Workday boards, the tenant name is "
    "whoever registered it and is NOT evidence on its own; weigh the hiring organization "
    "(the legal entity on the posting, e.g. '2100 NVIDIA USA' for NVIDIA), the career "
    "site name, and whether the job titles match a company of that size and industry. "
    "A hand-supplied URL is also not evidence about the operator. "
    "Watch for the case where one company's name contains another's: 'Wheels, LLC' "
    "(fleet leasing) is NOT 'Wheels Up' (private aviation), 'Bloomberg Industry Group' "
    "is NOT 'Bloomberg LP', and 'Northeastern University' is NOT 'Northeastern "
    "Electronics'. If the evidence only supports a company whose name overlaps the one "
    "asked about, that is match=false. "
    "Return match, confidence, reason."
)


def needs_verification(company_norm: str, token: str, board_name: str | None,
                       ats: str = "") -> tuple[bool, str]:
    words = company_norm.split()
    compact = "".join(words)
    hyphen = "-".join(words)
    # Recruitee/SmartRecruiters vanity subdomains and trial accounts are low-trust:
    # a token match is NOT sufficient (meta.recruitee.com is a demo account, not Meta).
    if ats in LOW_TRUST_PROVIDERS:
        return True, "low-trust-provider"
    if token in (compact, hyphen):
        return False, "exact"
    if board_name and normalize_company(board_name).replace(" ", "") == compact:
        return False, "board-name-exact"
    if token == compact[: len(token)] and len(token) >= max(6, len(compact) - 3):
        return True, "truncation"
    if len(words) > 1 and token == words[0]:
        return True, "first-word-only"
    return True, "ambiguous"


# ------------------------------------------------------------------------------------
# Hand-mapped boards (data/manual-boards.yaml) — checked before any probing
# ------------------------------------------------------------------------------------
def resolve_manual(specs: dict[str, dict]) -> dict[str, dict]:
    """Turn hand-mapped board URLs into the same hit shape the probe rounds produce.

    These are NOT trusted past this point: they go through the same verification as
    everything else. A pasted URL proves the user looked at a page, not that the board it
    landed on belongs to the company he thinks it does.
    """
    if not specs:
        return {}
    out: dict[str, dict] = {}
    wd_specs = {nc: sp for nc, sp in specs.items() if sp["ats"] == "workday"}
    other = {nc: sp for nc, sp in specs.items() if sp["ats"] != "workday"}

    if other:
        run(http_get_many([sp["board_url"] for sp in other.values()]))
        for nc, sp in other.items():
            resp = cached_http_get(sp["board_url"])
            if not resp or resp["status"] != 200:
                log(f"  manual-boards: {sp['company_display']} -> {sp['ats']}/{sp['token']} "
                    f"returned HTTP {resp['status'] if resp else 'none'}")
                continue
            jobs, bname = extract_jobs(sp["ats"], resp["body"])
            if not jobs:
                log(f"  manual-boards: {sp['company_display']} -> {sp['ats']}/{sp['token']} "
                    f"returned no postings")
                continue
            out[nc] = {"ats": sp["ats"], "token": sp["token"], "jobs": jobs,
                       "board_name": bname, "board_url": sp["board_url"],
                       "job_count": len(jobs), "source": "manual"}

    for nc, sp in wd_specs.items():
        jobs, total = run(wd.probe_site(sp["token"], sp["shard"], sp["site"]))
        if not jobs:
            log(f"  manual-boards: {sp['company_display']} -> workday "
                f"{sp['token']}/{sp['shard']}/{sp['site']} returned no postings")
            continue
        out[nc] = {"ats": "workday", "token": sp["token"], "shard": sp["shard"],
                   "site": sp["site"], "jobs": jobs, "board_name": None,
                   "board_url": sp["board_url"], "job_count": total or len(jobs),
                   "source": "manual"}
    log(f"Stage 2: manual-boards resolved {len(out)}/{len(specs)} hand-mapped entries")
    return out


# ------------------------------------------------------------------------------------
# Workday round — tenant/shard/site discovery, then one page per candidate site
# ------------------------------------------------------------------------------------
def resolve_workday(companies: list[str], display: dict[str, str],
                    deadline: float) -> tuple[dict[str, dict], str]:
    """{company_normalized: hit} for companies whose Workday board we can find.

    Two phases. Discovery sweeps robots.txt across the twelve real wd{N} shards and comes
    back with (shard, [site]) per existing tenant. Then one cheap page-0 POST per
    candidate site confirms the site path and gives us sample titles for verification.
    """
    if not companies:
        return {}, ""
    tenants: list[str] = []
    owners: dict[str, list[str]] = {}
    for nc in companies:
        for t in wd.tenant_candidates(display[nc]):
            tenants.append(t)
            owners.setdefault(t, []).append(nc)
    tenants = list(dict.fromkeys(tenants))
    log(f"Stage 2: workday discovery over {len(companies)} companies "
        f"({len(tenants)} tenant candidates)")

    found = run(wd.discover_many(tenants, deadline=deadline))
    note = ""
    if time.time() > deadline:
        note = ("workday tenant/shard discovery hit its timebox; remaining companies rely "
                "on data/manual-boards.yaml")
        log(f"Stage 2: NOTE — {note}")
    log(f"Stage 2: workday discovery found {len(found)} live tenants")

    # confirm site paths with a single page-0 POST each
    probes: list[tuple[str, str, str]] = []
    site_rank: dict[tuple[str, str, str], int] = {}
    for tenant, hits in found.items():
        for shard, sites in hits:
            for rank, site in enumerate(sites[: config.WORKDAY_MAX_SITES_PER_TENANT]):
                probes.append((tenant, shard, site))
                site_rank[(tenant, shard, site)] = rank
    if probes:
        log(f"Stage 2: workday confirming {len(probes)} candidate (tenant, shard, site) sites")
        run(wd.post_many([(wd.jobs_url(t, sh, si), wd.page_body(0)) for t, sh, si in probes]))

    live: dict[str, list[dict]] = {}
    for tenant, shard, site in probes:
        resp = wd.cached_post(wd.jobs_url(tenant, shard, site), wd.page_body(0))
        if not resp or resp["status"] != 200:
            continue
        jobs, total = wd.parse_jobs_page(resp["body"])
        if not jobs:
            continue
        live.setdefault(tenant, []).append(
            {"ats": "workday", "token": tenant, "shard": shard, "site": site,
             "jobs": jobs, "board_name": None, "job_count": total or len(jobs),
             "board_url": wd.board_url(tenant, shard, site), "source": "auto",
             "_rank": site_rank[(tenant, shard, site)]})

    out: dict[str, dict] = {}
    for tenant, sites in live.items():
        for nc in owners.get(tenant, []):
            # One tenant often hosts several career sites. Prefer one whose own name
            # carries the company name — the `etsy` tenant advertises Depop, Etsy and
            # Reverb, and Etsy_Careers is the only right answer. Failing that, take
            # robots.txt order, which puts the sitemap's canonical board first: `chewy`
            # advertises recruiting, Student-Ambassador-Contractors and
            # Relief-Veterinarian-Contractors, and only the first is Chewy's real board.
            best = min(sites, key=lambda h: (not wd.site_matches_company(h["site"], nc),
                                             h["_rank"]))
            # copy: one tenant can serve several companies, and each gets its own record
            out[nc] = {k: v for k, v in best.items() if k != "_rank"}

    # `hiringOrganization.name` is the legal entity behind the board — the single best
    # piece of evidence the verifier gets, so it is worth one detail fetch per board.
    detail_urls = []
    for nc, h in out.items():
        path = str((h["jobs"][0] or {}).get("externalPath") or "")
        if path:
            u = wd.detail_url(h["token"], h["shard"], h["site"], path)
            detail_urls.append(u)
            h["_detail_url"] = u
    if detail_urls:
        run(http_get_many(detail_urls))
        for nc, h in out.items():
            resp = cached_http_get(h.pop("_detail_url", "") or "")
            if resp and resp["status"] == 200:
                h["board_name"] = wd.hiring_org_name(resp["body"]) or None

    log(f"Stage 2: workday resolved {len(out)} companies (pre-verification)")
    return out, note


def main() -> None:  # noqa: C901 — linear pipeline, readability over decomposition
    t_start = time.time()
    scored = read_jsonl(DATA / "01-connections-scored.jsonl")
    if not scored:
        raise SystemExit("Stage 2: data/01-connections-scored.jsonl missing. Run stage 1.")

    # unique companies -> display name (most common raw), conn count, company score
    raw_by_norm: dict[str, Counter] = {}
    count_by_norm: Counter = Counter()
    score_by_norm: dict[str, float] = {}
    for r in scored:
        nc = r["normalized_company"]
        if not nc or nc in EXCLUDED_COMPANIES_NORM or is_non_company(nc):
            continue
        raw_by_norm.setdefault(nc, Counter())[r["raw_company"]] += 1
        count_by_norm[nc] += 1
        score_by_norm[nc] = max(score_by_norm.get(nc, 0.0), r.get("company_connection_score", 0.0))

    companies = sorted(count_by_norm, key=lambda n: (-count_by_norm[n], n))
    log(f"Stage 2: {len(companies)} unique non-excluded companies")

    display = {nc: raw_by_norm[nc].most_common(1)[0][0] for nc in companies}
    tokens = {nc: board_token_candidates(display[nc]) for nc in companies}

    # ---------- round 0: hand-mapped boards, and companies not worth probe budget ----------
    manual_specs, manual_skip = manual_boards.load()
    manual_specs = {nc: sp for nc, sp in manual_specs.items() if nc in count_by_norm}
    # Staffing agencies and "Confidential" are not employers; probing them wastes budget
    # and any hit is a false positive by construction. The authoritative list
    # in the scoring section of config.py — union with it if it has landed.
    skip_norm = {normalize_company(x) for x in config.WORKDAY_SKIP_COMPANIES}
    for attr in ("STAFFING_AGENCIES", "EXCLUDED_STAFFING_COMPANIES", "AGENCY_COMPANIES"):
        skip_norm |= {normalize_company(x) for x in getattr(config, attr, [])}
    skip_norm |= manual_skip
    skip_norm = {n for n in skip_norm if n} - set(manual_specs)
    skipped = [nc for nc in companies if nc in skip_norm]
    probe_companies = [nc for nc in companies if nc not in skip_norm]
    log(f"Stage 2: {len(skipped)} companies skipped as staffing/non-employer, "
        f"{len(probe_companies)} to probe")

    resolved: dict[str, dict] = resolve_manual(manual_specs)

    # ---------- round 1: core providers ----------
    core_urls = []
    for nc in probe_companies:
        if nc in resolved:
            continue
        for tok in tokens[nc][:MAX_TOKENS_CORE]:
            for p in CORE_PROVIDERS:
                core_urls.append(board_url(p, tok))
    log(f"Stage 2: fetching {len(core_urls)} core-provider URLs (cached ones skipped)")
    run(http_get_many(core_urls))

    def first_hit(nc: str, providers: list[str], max_tokens: int):
        for tok in tokens[nc][:max_tokens]:
            for p in providers:
                resp = cached_http_get(board_url(p, tok))
                if not resp or resp["status"] != 200 or len(resp["body"]) < 50:
                    continue
                jobs, bname = extract_jobs(p, resp["body"])
                if jobs:
                    return {"ats": p, "token": tok, "jobs": jobs, "board_name": bname,
                            "board_url": board_url(p, tok), "job_count": len(jobs)}
        return None

    for nc in probe_companies:
        if nc in resolved:
            continue
        h = first_hit(nc, CORE_PROVIDERS, MAX_TOKENS_CORE)
        if h:
            resolved[nc] = {**h, "source": "auto"}
    log(f"Stage 2: core round resolved {len(resolved)}/{len(companies)} (pre-verification)")

    # ---------- round 2: optional providers, timeboxed ----------
    unresolved_now = [nc for nc in probe_companies if nc not in resolved]
    elapsed = time.time() - t_start
    remaining = config.ATS_OPTIONAL_TIMEBOX - elapsed
    optional_note = ""
    if remaining > 60:
        opt_urls = []
        for nc in unresolved_now:
            for tok in tokens[nc][:MAX_TOKENS_OPTIONAL]:
                for p in OPTIONAL_PROVIDERS:
                    opt_urls.append(board_url(p, tok))
        log(f"Stage 2: fetching up to {len(opt_urls)} optional-provider URLs "
            f"(timebox {remaining:.0f}s left)")
        try:
            run(http_get_many(opt_urls))
        except Exception as e:  # noqa: BLE001
            optional_note = f"optional-provider fetch aborted: {e}"
            log(f"Stage 2: {optional_note}")
        for nc in unresolved_now:
            if time.time() - t_start > config.ATS_OPTIONAL_TIMEBOX:
                optional_note = "optional-provider resolution hit the 45-min timebox; " \
                                "remaining companies left unresolved"
                log(f"Stage 2: {optional_note}")
                break
            h = first_hit(nc, OPTIONAL_PROVIDERS, MAX_TOKENS_OPTIONAL)
            if h:
                resolved[nc] = {**h, "source": "auto"}
    else:
        optional_note = "no time budget left for optional providers"
    log(f"Stage 2: after optional round {len(resolved)}/{len(companies)} resolved "
        f"(pre-verification)")

    # ---------- round 3: Workday, own timebox ----------
    # Companies with no hit at all, plus those whose only hit still has to face the
    # verifier — the latter become Workday *alternates*, used if verification rejects.
    wd_candidates = [nc for nc in probe_companies
                     if nc not in resolved
                     or needs_verification(nc, resolved[nc]["token"],
                                           resolved[nc].get("board_name"),
                                           resolved[nc]["ats"])[0]]
    wd_candidates = wd_candidates[: config.WORKDAY_MAX_COMPANIES]
    wd_hits, workday_note = resolve_workday(
        wd_candidates, display, deadline=time.time() + config.WORKDAY_DISCOVERY_TIMEBOX)
    wd_alternates = {nc: h for nc, h in wd_hits.items() if nc in resolved}
    resolved.update({nc: h for nc, h in wd_hits.items() if nc not in resolved})
    log(f"Stage 2: after workday round {len(resolved)}/{len(companies)} resolved "
        f"(pre-verification)")

    # ---------- verification ----------
    auto_accepted = 0

    def verify(candidates: dict[str, dict], *, pass_name: str
               ) -> tuple[dict[str, dict], dict[str, dict], list[dict]]:
        """(verified, rejected, review_rows) for one batch of hits. Auto-accept exact
        normalizations, LLM-judge everything else, reject on negative or low confidence."""
        items, rows, ok_now = [], [], {}
        for nc, h in candidates.items():
            need, method = needs_verification(nc, h["token"], h.get("board_name"), h["ats"])
            titles = sample_titles(h["ats"], h["jobs"])
            base = {
                "company": display[nc], "company_normalized": nc, "ats": h["ats"],
                "board_token": h["token"], "board_name": h.get("board_name") or "",
                "sample_titles": " | ".join(titles[:3]),
                "connection_count": count_by_norm[nc],
                "source": h.get("source", "auto"),
                "board_url": h["board_url"],
            }
            if not need:
                ok_now[nc] = {**h, "verification": {
                    "method": method, "match": True, "confidence": "high",
                    "reason": f"token is {method} normalization"}}
                if method != "exact":
                    rows.append({**base, "method": f"auto:{method}", "match": True,
                                 "confidence": "high",
                                 "reason": f"auto-accepted ({method})"})
                continue
            extra = ""
            if h["ats"] == "workday":
                extra = (f"Workday tenant: {h['token']}\nWorkday shard: {h.get('shard','')}\n"
                         f"Workday career site: {h.get('site','')}\n"
                         f"Hiring organization (legal entity on the posting): "
                         f"{h.get('board_name') or '(none)'}\n"
                         f"Total postings on this board: {h.get('job_count', 0)}\n")
            if h.get("source") == "manual":
                extra += "This board URL was supplied by hand in data/manual-boards.yaml.\n"
            prompt = (
                f"Company: {display[nc]}\nATS: {h['ats']}\nToken used: {h['token']}\n"
                f"Board display name: {h.get('board_name') or '(none)'}\n"
                + extra
                + "Sample job titles:\n- " + "\n- ".join(titles[:5] or ["(none)"])
            )
            items.append((nc, prompt))
            rows.append({**base, "method": f"llm:{method}", "match": None,
                         "confidence": None, "reason": "pending"})

        nonlocal auto_accepted
        auto_accepted += len(ok_now)
        log(f"Stage 2: verification ({pass_name}) — {len(ok_now)} auto-accepted, "
            f"{len(items)} -> LLM")
        verdicts = dict(run(llm_json_many(items, VERIFY_SCHEMA, schema_name="board_verify",
                                          system=VERIFY_SYS, max_tokens=200))) if items else {}
        ok_out, bad_out = dict(ok_now), {}
        for nc, v in verdicts.items():
            rec = {**candidates[nc], "verification": {
                "method": "llm", "match": bool(v.get("match")),
                "confidence": v.get("confidence"), "reason": v.get("reason", "")}}
            if bool(v.get("match")) and v.get("confidence") in ("high", "medium"):
                ok_out[nc] = rec
            else:
                bad_out[nc] = rec
            for row in rows:
                if row["company_normalized"] == nc and row["method"].startswith("llm"):
                    row["match"] = v.get("match")
                    row["confidence"] = v.get("confidence")
                    row["reason"] = v.get("reason", "")
        return ok_out, bad_out, rows

    verified, rejected, review_rows = verify(resolved, pass_name="primary")

    # A rejected first-provider hit must not bury a good Workday board. Capital One is the
    # canonical case: Lever token `capital` matches first, fails verification, and without
    # this its real Workday board (capitalone/wd12/Capital_One) is never even tried.
    fallbacks = {nc: h for nc, h in wd_alternates.items() if nc in rejected}
    if fallbacks:
        log(f"Stage 2: {len(fallbacks)} rejected boards have a Workday alternative; "
            f"verifying those too")
        alt_ok, alt_bad, alt_rows = verify(fallbacks, pass_name="workday-fallback")
        for nc, rec in alt_ok.items():
            rejected.pop(nc, None)
            verified[nc] = rec
        for nc, rec in alt_bad.items():
            # keep the original rejection as the headline; the alternative failed too
            rejected.setdefault(nc, rec)
        review_rows.extend(alt_rows)

    # ---------- outputs ----------
    boards = []
    for nc, h in sorted(verified.items(), key=lambda kv: -count_by_norm[kv[0]]):
        boards.append({
            "company": display[nc],
            "company_normalized": nc,
            "connection_count": count_by_norm[nc],
            "company_connection_score": round(score_by_norm.get(nc, 0.0), 2),
            "ats": h["ats"],
            "board_token": h["token"],
            "board_url": h["board_url"],
            "job_count": h["job_count"],
            "board_name": h.get("board_name") or "",
            "source": h.get("source", "auto"),
            # Workday only; harmless empty strings elsewhere (Stage 3 keys off `ats`).
            "workday_shard": h.get("shard", ""),
            "workday_site": h.get("site", ""),
            "verification": h["verification"],
        })
    write_jsonl(DATA / "02-boards.jsonl", boards)

    unresolved_path = DATA / "02-unresolved.csv"
    with unresolved_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["company", "company_normalized", "connection_count",
                    "company_connection_score", "candidate_tokens", "outcome", "reason"])
        for nc in companies:
            if nc in verified:
                continue
            if nc in rejected:
                outcome, reason = "rejected", \
                    f"{rejected[nc]['ats']}/{rejected[nc]['token']} " \
                    f"failed verification: {rejected[nc]['verification']['reason']}"
            elif nc in skip_norm:
                outcome, reason = "skipped", \
                    "staffing agency / not a real employer — deliberately not probed"
            else:
                outcome, reason = "no_board_found", \
                    "no hit on greenhouse/ashby/lever/smartrecruiters/recruitee/workday"
            w.writerow([display[nc], nc, count_by_norm[nc],
                        round(score_by_norm.get(nc, 0.0), 2),
                        " ".join(tokens[nc][:MAX_TOKENS_CORE]), outcome, reason])

    review_path = DATA / "02-resolution-review.csv"
    with review_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["company", "company_normalized", "ats",
                                          "board_token", "board_name", "sample_titles",
                                          "method", "match", "confidence", "reason",
                                          "connection_count", "source", "board_url"])
        w.writeheader()
        for row in sorted(review_rows, key=lambda r: -r["connection_count"]):
            w.writerow(row)

    by_ats = Counter(b["ats"] for b in boards)
    log(f"Stage 2: RESOLVED {len(boards)}/{len(companies)} "
        f"({len(boards) / len(companies) * 100:.0f}%) by ats={dict(by_ats)}")
    log(f"Stage 2: rejected={len(rejected)} review_rows={len(review_rows)} "
        f"unresolved_total={len(companies) - len(boards)}")
    if optional_note:
        log(f"Stage 2: NOTE — {optional_note}")
    by_source = Counter(b["source"] for b in boards)
    wd_boards = [b for b in boards if b["ats"] == "workday"]
    log(f"Stage 2: workday VERIFIED {len(wd_boards)} boards, "
        f"{sum(b['job_count'] for b in wd_boards)} postings advertised; "
        f"by source={dict(by_source)}")

    (DATA / "02-stage-notes.json").write_text(json.dumps({
        "companies_total": len(companies),
        "resolved": len(boards),
        "by_ats": dict(by_ats),
        "rejected": len(rejected),
        "auto_accepted": auto_accepted,
        "llm_verified": sum(1 for r in review_rows if r["method"].startswith("llm")),
        "workday_fallbacks_tried": len(fallbacks),
        "optional_note": optional_note,
        "workday_note": workday_note,
        "workday_resolved_pre_verification": len(wd_hits),
        "manual_boards_supplied": len(manual_specs),
        "skipped_staffing": len(skipped),
        "by_source": dict(Counter(b["source"] for b in boards)),
        "elapsed_sec": round(time.time() - t_start, 1),
    }, indent=2))


if __name__ == "__main__":
    main()
