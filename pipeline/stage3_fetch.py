"""Stage 3 — fetch jobs.

data/02-boards.jsonl -> data/03-jobs-raw.jsonl

For most providers the Stage 2 board response already carries descriptions (Greenhouse
?content=true, Lever mode=json, Ashby, Workable ?details=true, Recruitee). Two need a
per-posting follow-up fetch (cached): SmartRecruiters, and Workday — whose list endpoint
returns title/location/path only.

Workday postings are also paged (20 per POST), so its boards are crawled through
pipeline/providers/workday.py rather than re-read from one cached Stage 2 response.
Description fetches there are gated by `worth_detail_fetch` — a title with no technical
signal can never survive the Stage 4 filter, so buying its description is pure waste on
boards that run to thousands of postings.

Every record carries a canonical public `url`. A posting with no derivable URL is a
bug) and is written to data/rejects-nourl.jsonl instead of the artifact.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

import config
from pipeline.common import (
    DATA, cached_http_get, http_get_many, log, read_jsonl, run, write_jsonl,
)
from pipeline.providers import workday as wd
from pipeline.stage2_boards import extract_jobs

_WS = re.compile(r"[ \t ]+")
_NL = re.compile(r"\n{3,}")
# strip C0/C1 control chars + unicode line/para separators (U+2028/U+2029, NBSP)
_CTRL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f   ]")


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    if "<" not in html:
        return _NL.sub("\n\n", _WS.sub(" ", _CTRL.sub(" ", html))).strip()
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = _CTRL.sub(" ", soup.get_text("\n"))
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _NL.sub("\n\n", text).strip()


def _loc_from_list(cats: dict | None, key: str = "location") -> str:
    if not isinstance(cats, dict):
        return ""
    v = cats.get(key)
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v or "")


def normalize_posting(board: dict, provider: str, job: dict) -> dict | None:
    company = board["company"]
    cn = board["company_normalized"]
    token = board["board_token"]

    if provider == "greenhouse":
        rec = {
            "job_id": str(job.get("id", "")),
            "title": job.get("title", ""),
            "location_raw": (job.get("location") or {}).get("name", "") if isinstance(job.get("location"), dict) else str(job.get("location") or ""),
            "department": ", ".join(d.get("name", "") for d in (job.get("departments") or []) if isinstance(d, dict)),
            "employment_type": "",
            "description_text": html_to_text(job.get("content", "")),
            "compensation_raw": _gh_comp(job),
            "posted_at": job.get("updated_at") or job.get("first_published") or "",
            "url": job.get("absolute_url", ""),
        }
    elif provider == "ashby":
        comp = job.get("compensation") or {}
        rec = {
            "job_id": str(job.get("id", "")),
            "title": job.get("title", ""),
            "location_raw": job.get("location", "") or _ashby_loc(job),
            "department": job.get("department", "") or job.get("team", ""),
            "employment_type": job.get("employmentType", ""),
            "description_text": html_to_text(job.get("descriptionHtml") or job.get("descriptionPlain", "")),
            "compensation_raw": json.dumps(comp.get("compensationTierSummary") or comp.get("summaryComponents") or comp) if comp else "",
            "posted_at": job.get("publishedAt") or job.get("updatedAt") or "",
            "url": job.get("jobUrl") or f"https://jobs.ashbyhq.com/{token}/{job.get('id','')}",
        }
    elif provider == "lever":
        cats = job.get("categories") or {}
        salary = ""
        if job.get("salaryRange"):
            sr = job["salaryRange"]
            salary = f"{sr.get('currency','')} {sr.get('min','')}-{sr.get('max','')} {sr.get('interval','')}".strip()
        rec = {
            "job_id": str(job.get("id", "")),
            "title": job.get("text", ""),
            "location_raw": _loc_from_list(cats, "location") or _loc_from_list(cats, "allLocations"),
            "department": _loc_from_list(cats, "team") or _loc_from_list(cats, "department"),
            "employment_type": _loc_from_list(cats, "commitment"),
            "description_text": job.get("descriptionPlain") or html_to_text(job.get("description", "")),
            "compensation_raw": salary,
            "posted_at": _epoch_iso(job.get("createdAt")),
            "url": job.get("hostedUrl") or job.get("applyUrl", ""),
        }
    elif provider == "smartrecruiters":
        loc = job.get("location") or {}
        rec = {
            "job_id": str(job.get("id", "")),
            "title": job.get("name", ""),
            "location_raw": ", ".join(x for x in [loc.get("city", ""), loc.get("region", ""), loc.get("country", "")] if x),
            "department": (job.get("department") or {}).get("label", "") if isinstance(job.get("department"), dict) else "",
            "employment_type": (job.get("typeOfEmployment") or {}).get("label", "") if isinstance(job.get("typeOfEmployment"), dict) else "",
            "description_text": _sr_desc(job),
            "compensation_raw": "",
            "posted_at": job.get("releasedDate") or job.get("createdOn") or "",
            "url": job.get("postingUrl") or job.get("applyUrl") or job.get("ref", ""),
        }
    elif provider == "workable":
        rec = {
            "job_id": str(job.get("id") or job.get("shortcode", "")),
            "title": job.get("title", ""),
            "location_raw": _workable_loc(job),
            "department": job.get("department", "") or "",
            "employment_type": job.get("type", "") or job.get("employment_type", ""),
            "description_text": html_to_text(job.get("description", "") + " " + (job.get("requirements") or "")),
            "compensation_raw": "",
            "posted_at": job.get("published_on") or job.get("created_at") or "",
            "url": job.get("url") or job.get("application_url") or job.get("shortlink", ""),
        }
    elif provider == "recruitee":
        rec = {
            "job_id": str(job.get("id", "")),
            "title": job.get("title", ""),
            "location_raw": job.get("location", "") or ", ".join(job.get("locations_string", "") if isinstance(job.get("locations_string"), list) else [str(job.get("locations_string") or "")]),
            "department": job.get("department", "") or job.get("category", ""),
            "employment_type": job.get("employment_type_code", "") or job.get("kind", ""),
            "description_text": html_to_text((job.get("description") or "") + " " + (job.get("requirements") or "")),
            "compensation_raw": job.get("salary", "") or "",
            "posted_at": job.get("published_at") or job.get("created_at") or "",
            "url": job.get("careers_url") or job.get("careers_apply_url") or job.get("url", ""),
        }
    elif provider == "workday":
        info = job.get("_detail") or {}
        path = str(job.get("externalPath") or "")
        tenant, shard, site = board["board_token"], board["workday_shard"], board["workday_site"]
        rec = {
            "job_id": str(info.get("jobReqId") or (job.get("bulletFields") or [""])[0]
                          or path.rsplit("_", 1)[-1]),
            "title": job.get("title", "") or info.get("title", ""),
            "location_raw": job.get("locationsText", "") or info.get("location", ""),
            "department": "",
            "employment_type": info.get("timeType", ""),
            "description_text": html_to_text(info.get("jobDescription", "")),
            "compensation_raw": "",
            "posted_at": info.get("startDate") or _workday_posted(job.get("postedOn", "")),
            "url": info.get("externalUrl") or (
                wd.posting_url(tenant, shard, site, path) if path else ""),
        }
    else:
        return None

    if not rec["url"]:
        return None
    # URLs come verbatim from ATS JSON and are rendered as links in report.html /
    # the board app. Only http(s) is safe there: e.g. a `javascript:` url executes
    # on click. Anything else is treated as no derivable URL and rejected.
    if not rec["url"].startswith(("http://", "https://")):
        return None
    rec.update({
        "company": company, "company_normalized": cn, "ats": provider, "board_token": token,
    })
    # canonical column order
    return {k: rec.get(k, "") for k in [
        "company", "company_normalized", "ats", "board_token", "job_id", "title",
        "location_raw", "department", "employment_type", "description_text",
        "compensation_raw", "posted_at", "url",
    ]}


def _gh_comp(job: dict) -> str:
    for md in job.get("metadata") or []:
        if isinstance(md, dict) and "salary" in str(md.get("name", "")).lower():
            return str(md.get("value") or "")
    return ""


def _ashby_loc(job: dict) -> str:
    sl = job.get("secondaryLocations") or []
    names = [job.get("locationName", "")] + [x.get("locationName", "") for x in sl if isinstance(x, dict)]
    return ", ".join(n for n in names if n)


def _workable_loc(job: dict) -> str:
    loc = job.get("location") or {}
    if isinstance(loc, dict):
        parts = [loc.get("city", ""), loc.get("region", ""), loc.get("country", "")]
        s = ", ".join(p for p in parts if p)
        if loc.get("telecommuting"):
            s = (s + " (remote)").strip()
        return s
    return str(loc or "")


def _sr_desc(job: dict) -> str:
    ja = job.get("jobAd") or {}
    sections = (ja.get("sections") or {}) if isinstance(ja, dict) else {}
    chunks = []
    for key in ("jobDescription", "qualifications", "additionalInformation", "companyDescription"):
        sec = sections.get(key) or {}
        if isinstance(sec, dict) and sec.get("text"):
            chunks.append(html_to_text(sec["text"]))
    return "\n\n".join(chunks)


_WD_DAYS_RE = re.compile(r"(\d+)\+?\s*Days?\s*Ago", re.I)


def _workday_posted(posted_on: str) -> str:
    """Workday's list endpoint gives 'Posted Today' / 'Posted 30+ Days Ago' rather than a
    date. Turn it into an ISO date so downstream sorting works; the detail fetch supplies
    a real `startDate` whenever we bought one."""
    s = (posted_on or "").strip()
    if not s:
        return ""
    from datetime import date, timedelta
    low = s.lower()
    if "today" in low:
        return date.today().isoformat()
    if "yesterday" in low:
        return (date.today() - timedelta(days=1)).isoformat()
    m = _WD_DAYS_RE.search(s)
    if m:
        return (date.today() - timedelta(days=int(m.group(1)))).isoformat()
    return ""


def _epoch_iso(ms) -> str:
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


def fetch_workday(boards: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Crawl every Workday board: page the list endpoint, then buy descriptions only for
    postings that could plausibly survive Stage 4. Returns (records, no_url_rejects, stats)."""
    stats = {"boards": len(boards), "postings": 0, "detail_candidates": 0,
             "details_fetched": 0, "details_skipped_cap": 0}
    if not boards:
        return [], [], stats

    listings: dict[str, list[dict]] = {}
    for b in boards:
        tenant, shard, site = b["board_token"], b["workday_shard"], b["workday_site"]
        jobs = run(wd.fetch_all_postings(tenant, shard, site, b.get("job_count") or 0))
        listings[b["board_url"]] = jobs
        stats["postings"] += len(jobs)
        log(f"  workday {b['company']}: {len(jobs)} postings "
            f"({tenant}/{shard}/{site}, board advertises {b.get('job_count')})")

    # description fetches, gated then globally capped, richest boards' best titles first
    wanted: list[tuple[str, dict, str]] = []
    for b in boards:
        tenant, shard, site = b["board_token"], b["workday_shard"], b["workday_site"]
        for j in listings[b["board_url"]]:
            path = str(j.get("externalPath") or "")
            if path and wd.worth_detail_fetch(j):
                wanted.append((b["board_url"], j, wd.detail_url(tenant, shard, site, path)))
    stats["detail_candidates"] = len(wanted)
    cap = config.WORKDAY_MAX_DETAIL_FETCHES
    if len(wanted) > cap:
        stats["details_skipped_cap"] = len(wanted) - cap
        log(f"  workday: {len(wanted)} detail candidates exceeds cap {cap}; "
            f"{stats['details_skipped_cap']} postings keep list-only data")
        wanted = wanted[:cap]
    if wanted:
        log(f"  workday: fetching {len(wanted)} job descriptions "
            f"(of {stats['postings']} postings; the rest cannot pass the title filter)")
        run(http_get_many([u for _, _, u in wanted]))
        for _, j, u in wanted:
            resp = cached_http_get(u)
            if not resp or resp["status"] != 200:
                continue
            try:
                j["_detail"] = (json.loads(resp["body"]) or {}).get("jobPostingInfo") or {}
                stats["details_fetched"] += 1
            except (json.JSONDecodeError, ValueError):
                pass

    out, nourl = [], []
    for b in boards:
        for j in listings[b["board_url"]]:
            rec = normalize_posting(b, "workday", j)
            if rec is None:
                nourl.append({"company": b["company"], "ats": "workday",
                              "board_token": b["board_token"],
                              "title": j.get("title", ""),
                              "reason": "no externalPath -> no canonical URL"})
                continue
            out.append(rec)
    return out, nourl, stats


def main() -> None:
    boards = read_jsonl(DATA / "02-boards.jsonl")
    if not boards:
        raise SystemExit("Stage 3: data/02-boards.jsonl missing/empty. Run stage 2.")
    log(f"Stage 3: {len(boards)} resolved boards")

    workday_boards = [b for b in boards if b["ats"] == "workday"]
    boards = [b for b in boards if b["ats"] != "workday"]

    # SmartRecruiters needs per-posting detail fetches
    sr_detail_urls: list[str] = []
    sr_index: dict[str, tuple[dict, str]] = {}
    for b in boards:
        if b["ats"] != "smartrecruiters":
            continue
        resp = cached_http_get(b["board_url"])
        if not resp or resp["status"] != 200:
            continue
        jobs, _ = extract_jobs("smartrecruiters", resp["body"])
        for j in jobs:
            jid = j.get("id")
            if jid:
                u = f"https://api.smartrecruiters.com/v1/companies/{b['board_token']}/postings/{jid}"
                sr_detail_urls.append(u)
                sr_index[u] = (b, jid)
    if sr_detail_urls:
        log(f"Stage 3: fetching {len(sr_detail_urls)} SmartRecruiters posting details")
        run(http_get_many(sr_detail_urls))

    out, nourl, wd_stats = fetch_workday(workday_boards)
    per_ats = {"workday": len(out)} if out else {}
    for b in boards:
        resp = cached_http_get(b["board_url"])
        if not resp or resp["status"] != 200:
            log(f"Stage 3: WARN no cached 200 for {b['company']} ({b['board_url']})")
            continue
        jobs, _ = extract_jobs(b["ats"], resp["body"])
        for j in jobs:
            if b["ats"] == "smartrecruiters":
                jid = j.get("id")
                du = f"https://api.smartrecruiters.com/v1/companies/{b['board_token']}/postings/{jid}"
                dresp = cached_http_get(du)
                if dresp and dresp["status"] == 200:
                    try:
                        j = {**j, **json.loads(dresp["body"])}
                    except (json.JSONDecodeError, ValueError):
                        pass
            rec = normalize_posting(b, b["ats"], j)
            if rec is None:
                nourl.append({"company": b["company"], "ats": b["ats"],
                              "board_token": b["board_token"],
                              "title": j.get("title") or j.get("text") or j.get("name") or "",
                              "reason": "no derivable canonical URL"})
                continue
            out.append(rec)
            per_ats[b["ats"]] = per_ats.get(b["ats"], 0) + 1

    n = write_jsonl(DATA / "03-jobs-raw.jsonl", out)
    if nourl:
        write_jsonl(DATA / "rejects-nourl.jsonl", nourl)
    with_desc = sum(1 for r in out if r["description_text"])
    log(f"Stage 3: wrote {n} postings -> data/03-jobs-raw.jsonl "
        f"(by ats={per_ats}, {with_desc} with descriptions, {len(nourl)} dropped no-url)")
    if workday_boards:
        log(f"Stage 3: workday {wd_stats}")


if __name__ == "__main__":
    main()
