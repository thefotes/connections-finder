"""Stage 1 — score connections + recruiter track.

data/00-connections.jsonl -> data/01-connections-scored.jsonl
                          -> data/01-recruiters.jsonl   (ship-first, zero crawling)

Composite per connection from weighted, normalized components (see config.py):
  seniority, function, era overlap, DM volume, DM recency.
Company-level connection score = MAX of its connections' scores.
"""
from __future__ import annotations

from datetime import date

import config
from pipeline import settings
from pipeline.classify import classify_positions
from pipeline.common import DATA, log, read_jsonl, write_jsonl

SENIORITY_RANK = {b: i for i, b in enumerate(
    ["unknown", "ic", "senior_ic", "staff_principal_lead", "manager", "senior_manager",
     "director", "head", "vp", "c_level"])}  # higher = more senior


def era_for(connected_on: str | None) -> str:
    if not connected_on:
        return "none"
    try:
        d = date.fromisoformat(connected_on)
    except ValueError:
        return "none"
    # employer windows from settings.yaml; overlapping windows -> highest weight wins
    hits = [e for e in settings.load().employers if e.start <= d <= e.end]
    return max(hits, key=lambda e: e.weight).tag if hits else "none"


def era_score(tag: str) -> float:
    return next((e.weight for e in settings.load().employers if e.tag == tag), 0.0)


def year_of(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(iso[:4])
    except ValueError:
        return None


def connection_score(rec: dict, verdict: dict) -> dict:
    sen = verdict.get("seniority", "unknown")
    fn = verdict.get("function", "unknown")
    era = era_for(rec.get("connected_on"))
    last_year = year_of(rec.get("last_message_date"))
    mc = int(rec.get("message_count") or 0)

    comps = {
        "seniority": config.SENIORITY_SCORES.get(sen, config.SENIORITY_SCORES["unknown"]),
        "function": config.FUNCTION_SCORES.get(fn, config.FUNCTION_SCORES["unknown"]),
        "era": era_score(era),
        "dm_volume": config.dm_volume_score(mc),
        "dm_recency": config.dm_recency_score(last_year),
    }
    composite = sum(config.CONNECTION_WEIGHTS[k] * v for k, v in comps.items())
    score = round(composite * config.CONNECTION_SCORE_SCALE, 2)

    why_bits = [f"{sen.replace('_', ' ')}", f"{fn}"]
    if era != "none":
        why_bits.append(f"{era.replace('_', ' ')} era")
    if mc >= 4:
        why_bits.append(f"{mc} DMs")
    return {
        "seniority_bucket": sen,
        "function_bucket": fn,
        "era": era,
        "is_recruiter": bool(verdict.get("is_recruiter")),
        "score_components": {k: round(v, 3) for k, v in comps.items()},
        "connection_score": score,
        "why_ranked": ", ".join(why_bits),
    }


def main() -> None:
    conns = read_jsonl(DATA / "00-connections.jsonl")
    if not conns:
        raise SystemExit("Stage 1: data/00-connections.jsonl missing or empty. Run stage 0.")

    verdicts = classify_positions([c["raw_position"] for c in conns])

    scored = []
    for c in conns:
        v = verdicts.get((c["raw_position"] or "").strip(),
                         {"seniority": "unknown", "function": "unknown", "is_recruiter": False})
        s = connection_score(c, v)
        scored.append({**c, **s})

    # company-level connection score = MAX
    by_company: dict[str, float] = {}
    for r in scored:
        nc = r["normalized_company"]
        if not nc:
            continue
        by_company[nc] = max(by_company.get(nc, 0.0), r["connection_score"])
    for r in scored:
        r["company_connection_score"] = round(by_company.get(r["normalized_company"], 0.0), 2)

    n = write_jsonl(DATA / "01-connections-scored.jsonl", scored)
    log(f"Stage 1: wrote {n} -> data/01-connections-scored.jsonl "
        f"({len(by_company)} companies with a score)")

    # ---------------- recruiter track ----------------
    recruiters = [r for r in scored if r["is_recruiter"] or r["function_bucket"] == "recruiting"]
    # rank: DM volume, then recency, then seniority, then era overlap
    for r in recruiters:
        c = r["score_components"]
        r["recruiter_score"] = round(
            (config.RECRUITER_WEIGHTS["dm_volume"] * c["dm_volume"]
             + config.RECRUITER_WEIGHTS["dm_recency"] * c["dm_recency"]
             + config.RECRUITER_WEIGHTS["seniority"] * c["seniority"]
             + config.RECRUITER_WEIGHTS["era"] * c["era"])
            * config.RECRUITER_SCORE_SCALE, 2)
    recruiters.sort(key=lambda r: (
        r["message_count"],
        year_of(r["last_message_date"]) or 0,
        SENIORITY_RANK.get(r["seniority_bucket"], 0),
        era_score(r["era"]),
    ), reverse=True)
    for i, r in enumerate(recruiters, 1):
        r["recruiter_rank"] = i

    nr = write_jsonl(DATA / "01-recruiters.jsonl", recruiters)
    messaged = sum(1 for r in recruiters if r["message_count"] > 0)
    log(f"Stage 1: wrote {nr} -> data/01-recruiters.jsonl ({messaged} with DM history)")


if __name__ == "__main__":
    main()
