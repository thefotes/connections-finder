"""Stage 5 — score roles.

data/04-jobs-eligible.jsonl + profile.md -> data/05-jobs-scored.jsonl

One LLM call per eligible role, strict json_schema. profile.md is read at RUNTIME and
placed in the prompt, so you re-weight by editing profile.md and re-running this
stage — no code change, no re-crawl. Every verdict is cached on disk by payload hash.
"""
from __future__ import annotations

import re
import sys

import config
from pipeline import settings
from pipeline.common import (
    DATA, ROOT, llm_healthcheck, llm_json_many, log, read_jsonl, run, write_jsonl,
)

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_score": {"type": "integer", "minimum": 0, "maximum": 10},
        "domain_match": {"type": "integer", "minimum": 0, "maximum": 10},
        "scope_match": {"type": "integer", "minimum": 0, "maximum": 10},
        "industry_fit": {"type": "integer", "minimum": 0, "maximum": 10},
        "comp_signal": {"type": "integer", "minimum": 0, "maximum": 10},
        "seniority_read": {"type": "string"},
        "manages_managers": {"type": "boolean"},
        "comp_min": {"type": ["integer", "null"]},
        "comp_max": {"type": ["integer", "null"]},
        "industry_flag": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
        "concerns": {"type": "string"},
    },
    "required": ["overall_score", "domain_match", "scope_match", "industry_fit",
                 "comp_signal", "seniority_read", "manages_managers", "comp_min",
                 "comp_max", "industry_flag", "rationale", "concerns"],
    "additionalProperties": False,
}

SYS = (
    "You are scoring a single job posting against a specific candidate's profile and "
    "rubric, which is given verbatim below. Score every axis 0-10 using the rubric's "
    "stated weight order (domain match highest, then scope, then location, then "
    "industry, then stage/size lowest). Be calibrated and honest; most roles are a 3-6. "
    "Reserve 8-10 for genuinely strong matches on domain AND scope.\n"
    "- comp_min/comp_max: integers in USD/year extracted from the description ONLY if a "
    "band is explicitly published; otherwise null. Do not guess.\n"
    "- comp_signal: per the rubric's compensation target — a published band topping out "
    "under the target => low (0-3); clears it => higher, scaled; no band published => "
    "neutral (5).\n"
    "- manages_managers: for a people-management track, true only if the role manages "
    "other managers / owns a multi-team org; for any other track always false. A "
    "people-management-track role that manages no one is mis-classified — say so in "
    "concerns and score scope_match low.\n"
    "- industry_flag: set a short lowercase tag (e.g. \"defense\", \"gambling\", "
    "\"crypto\", \"surveillance\") ONLY if the company/role plausibly falls in a category "
    "the candidate might object to. Flag, never filter. null otherwise.\n"
    "Keep 'rationale' to 2-3 sentences and 'concerns' to 2-3 sentences. "
    "Return strictly the JSON schema."
)


def _track_for(bucket: str | None) -> settings.Track | None:
    tid = bucket or ""
    return next((t for t in settings.load().tracks if t.id == tid), None)


def build_prompt(profile_md: str, job: dict) -> str:
    desc = (job.get("description_text") or "")[:6000]
    track = _track_for(job.get("title_bucket"))
    track_line = (f"{track.name} — titles: {', '.join(track.titles)}" if track
                  else str(job.get("title_bucket")))
    manages = "yes (a people-management track)" if track and track.manages_people else "no"
    return (
        f"=== CANDIDATE PROFILE & RUBRIC (profile.md) ===\n{profile_md}\n\n"
        f"=== JOB POSTING ===\n"
        f"Company: {job['company']}\n"
        f"Title: {job['title']}\n"
        f"Target track (pre-classified): {track_line}\n"
        f"People-management track: {manages}\n"
        f"Location (raw): {job.get('location_raw')}\n"
        f"Location tier (pre-classified): {job.get('location_tier')} "
        f"({job.get('location_reason')})\n"
        f"Department: {job.get('department')}\n"
        f"Employment type: {job.get('employment_type')}\n"
        f"Compensation (raw, if any): {job.get('compensation_raw') or '(none listed)'}\n"
        f"Posted: {job.get('posted_at')}\n\n"
        f"Description:\n{desc or '(no description text available)'}\n"
    )


def _ngrams(text: str, n: int = 8) -> set[str]:
    words = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split()
    return {" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def _scrub_profile_leak(text: str, profile_ngrams: set[str]) -> str:
    """Last line of defense against prompt injection exfiltration: a hostile posting
    can instruct the model to quote the CANDIDATE PROFILE section into rationale/
    concerns, which are rendered into the shareable report. If the output contains
    a verbatim 8-word run from profile.md, drop the whole field."""
    t = text or ""
    if not t or not profile_ngrams:
        return t
    if _ngrams(t) & profile_ngrams:
        return "[redacted: field contained candidate-profile text]"
    return t


def composite(parsed: dict) -> float:
    return round(sum(config.ROLE_WEIGHTS[k] * float(parsed.get(k, 0) or 0)
                     for k in config.ROLE_WEIGHTS), 3)


def main() -> None:
    jobs = read_jsonl(DATA / "04-jobs-eligible.jsonl")
    if not jobs:
        raise SystemExit("Stage 5: data/04-jobs-eligible.jsonl missing/empty. Run stage 4.")
    profile_path = ROOT / "profile.md"
    if not profile_path.exists():
        raise SystemExit("Stage 5: profile.md not found. Copy profile.example.md to profile.md "
                         "and describe yourself and what you are looking for.")
    profile_md = profile_path.read_text(encoding="utf-8")

    # count how many still need a live call (rest are cached) before health-gating
    from pipeline.common import _cached_llm, _llm_payload  # noqa: PLC0415
    need_live = 0
    for j in jobs:
        payload = _llm_payload(build_prompt(profile_md, j), SCORE_SCHEMA,
                               schema_name="role_score", temperature=0.0,
                               max_tokens=1400, system=SYS)
        if _cached_llm(payload) is None:
            need_live += 1
    log(f"Stage 5: {len(jobs)} eligible roles; {len(jobs) - need_live} already cached, "
        f"{need_live} need a live LLM call")
    if need_live:
        ok, detail = llm_healthcheck()
        if not ok:
            log(f"Stage 5: LLM endpoint is unreachable ({detail}). "
                f"Aborting so a re-run can resume from cache once it recovers.")
            sys.exit(3)
        log(f"Stage 5: LLM healthcheck OK ({detail!r})")

    items = [((j["url"]), build_prompt(profile_md, j)) for j in jobs]
    results = run(llm_json_many(items, SCORE_SCHEMA, schema_name="role_score", system=SYS,
                                max_tokens=1400,
                                concurrency=config.LLM_CONCURRENCY_HEAVY))
    by_url = dict(results)
    profile_ngrams = _ngrams(profile_md)

    out = []
    errors = 0
    for j in jobs:
        p = by_url.get(j["url"], {})
        if "_llm_error" in p or not p:
            errors += 1
            p = {"overall_score": 0, "domain_match": 0, "scope_match": 0, "industry_fit": 0,
                 "comp_signal": 0, "seniority_read": "", "manages_managers": False,
                 "comp_min": None, "comp_max": None, "industry_flag": None,
                 "rationale": "LLM scoring failed", "concerns": "not scored"}
        comp_pen = bool(p.get("comp_max") and p["comp_max"] < 200_000)
        p["rationale"] = _scrub_profile_leak(p.get("rationale", ""), profile_ngrams)
        p["concerns"] = _scrub_profile_leak(p.get("concerns", ""), profile_ngrams)
        role_composite = composite(p)
        role_score = p["overall_score"] if config.ROLE_USE_LLM_OVERALL else role_composite
        out.append({
            **j,
            "score": p,
            "role_composite": role_composite,
            "role_score": round(float(role_score), 3),
            "comp_penalty_flag": comp_pen,
            "industry_flag": p.get("industry_flag"),
        })

    n = write_jsonl(DATA / "05-jobs-scored.jsonl", out)
    flagged = sum(1 for r in out if r["industry_flag"])
    log(f"Stage 5: wrote {n} -> data/05-jobs-scored.jsonl "
        f"({errors} scoring errors, {flagged} industry-flagged)")


if __name__ == "__main__":
    main()
