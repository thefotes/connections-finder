"""Hand-mapped board overrides — `manual-boards.yaml` (optional; see the .example).

Auto-discovery is best-effort. This file is the escape hatch: paste a careers URL
out of your browser, and the next run picks the board up. It is checked BEFORE any probing
and it wins over auto-discovery, so a hand-mapped board is also the way to correct a
wrong auto-resolution.

Entries still go through board verification. A typo'd URL that happens to return HTTP
200 is exactly the failure mode verification exists to catch, and "someone pasted it" is
not evidence about which company operates the board.

Format (any of the supported ATS URLs; the provider is inferred from the host):

    boards:
      - company: Capital One
        url: https://capitalone.wd12.myworkdayjobs.com/Capital_One
      - company: WillowTree
        url: https://boards.greenhouse.io/willowtree
      - company: Impel
        url: https://jobs.lever.co/impel
        # optional: skip: true   -> never probe this company at all
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from pipeline.common import ROOT, log, normalize_company
from pipeline.providers import workday

MANUAL_PATH = ROOT / "manual-boards.yaml"


def _infer(url: str) -> dict | None:
    u = (url or "").strip()
    if not u:
        return None
    if "myworkdayjobs.com" in u:
        parsed = workday.parse_board_url(u)
        if not parsed:
            return None
        tenant, shard, site = parsed
        return {"ats": "workday", "token": tenant, "shard": shard, "site": site,
                "board_url": workday.board_url(tenant, shard, site)}
    m = re.search(r"greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", u)
    if m and "boards-api" not in u:
        t = m.group(1)
        return {"ats": "greenhouse", "token": t,
                "board_url": f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=true"}
    m = re.search(r"(?:jobs|api)\.lever\.co/(?:v0/postings/)?([A-Za-z0-9_-]+)", u)
    if m:
        t = m.group(1)
        return {"ats": "lever", "token": t,
                "board_url": f"https://api.lever.co/v0/postings/{t}?mode=json"}
    m = re.search(r"(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)/([A-Za-z0-9_-]+)", u)
    if m:
        t = m.group(1)
        return {"ats": "ashby", "token": t,
                "board_url": f"https://api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=true"}
    m = re.search(r"careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)", u)
    if m:
        t = m.group(1)
        return {"ats": "smartrecruiters", "token": t,
                "board_url": f"https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=100"}
    m = re.search(r"https?://([A-Za-z0-9_-]+)\.recruitee\.com", u)
    if m:
        t = m.group(1)
        return {"ats": "recruitee", "token": t,
                "board_url": f"https://{t}.recruitee.com/api/offers/"}
    return None


def load() -> tuple[dict[str, dict], set[str]]:
    """-> ({company_normalized: board_spec}, {company_normalized to never probe})."""
    if not MANUAL_PATH.exists():
        return {}, set()
    doc = yaml.safe_load(MANUAL_PATH.read_text(encoding="utf-8")) or {}
    entries = doc.get("boards") or []
    out: dict[str, dict] = {}
    skip: set[str] = set()
    bad = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        company = str(e.get("company") or "").strip()
        if not company:
            continue
        nc = normalize_company(company)
        if e.get("skip"):
            skip.add(nc)
            continue
        spec = _infer(str(e.get("url") or ""))
        if spec is None:
            bad += 1
            log(f"  manual-boards: UNPARSEABLE url for {company!r}: {e.get('url')!r}")
            continue
        out[nc] = {**spec, "company_display": company, "source": "manual"}
    log(f"  manual-boards: {len(out)} hand-mapped boards, {len(skip)} skips"
        + (f", {bad} unparseable" if bad else ""))
    return out, skip
