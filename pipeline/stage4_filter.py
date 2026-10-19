"""Stage 4 — filter roles.

data/03-jobs-raw.jsonl -> data/04-jobs-eligible.jsonl
                       -> data/rejects-title.jsonl
                       -> data/rejects-location.jsonl
                       -> data/04-location-review.jsonl   (ambiguous ACCEPTS, for tuning)

Title filter: one of your tracks (settings.yaml) | reject  (hybrid regex+LLM, cached —
pipeline/classify.py)
Location filter: tier_1 (Remote US) | tier_2 (onsite/hybrid in your metro, settings.yaml)
                 | reject
Both filters log every drop with a reason.
"""
from __future__ import annotations

import re
from pathlib import Path

from pipeline import settings
from pipeline.classify import classify_titles
from pipeline.common import DATA, log, read_jsonl, write_jsonl

# ---------------------------------------------------------------------------------
# Location classification — deterministic
# ---------------------------------------------------------------------------------
US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
}
US_STATE_ABBR = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in",
    "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv",
    "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn",
    "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
}
US_WORDS = re.compile(
    r"\b(united states|u\.?s\.?a?\.?|us-based|us remote|remote us|america|anywhere in the us)\b",
    re.I)
NON_US = re.compile(
    r"\b(canada|toronto|vancouver|ontario|united kingdom|\buk\b|london|england|ireland|"
    r"dublin|germany|berlin|munich|france|paris|spain|madrid|barcelona|portugal|lisbon|"
    r"netherlands|amsterdam|poland|warsaw|krak(o|ó)w|romania|bucharest|india|bangalore|"
    r"bengaluru|hyderabad|pune|gurgaon|noida|singapore|australia|sydney|melbourne|"
    r"new zealand|brazil|s(a|ã)o paulo|mexico city|argentina|colombia|emea|apac|latam|"
    r"europe|european|philippines|manila|japan|tokyo|china|shanghai|hong kong|israel|"
    r"tel aviv|dubai|\buae\b|nigeria|lagos|kenya|nairobi|south africa|sweden|stockholm|"
    r"denmark|copenhagen|norway|oslo|finland|helsinki|switzerland|zurich|austria|vienna|"
    r"belgium|brussels|czech|prague|hungary|budapest|ukraine|kyiv|bulgaria|sofia|"
    r"costa rica|guatemala)\b", re.I)
REMOTE = re.compile(r"\bremote\b|\bwork from home\b|\bwfh\b|\bdistributed\b|\banywhere\b", re.I)
HYBRID = re.compile(r"\bhybrid\b", re.I)



def _home_in(states: set[str]) -> bool:
    s = settings.load()
    return bool({s.home_state, s.home_state_abbr} - {""} & states)


def _tokens_states(text: str) -> set[str]:
    low = text.lower()
    found = {s for s in US_STATES if re.search(rf"\b{re.escape(s)}\b", low)}
    for m in re.findall(r"\b([a-z]{2})\b", low):
        if m in US_STATE_ABBR:
            found.add(m)
    return found


def classify_location(location_raw: str, description_text: str) -> dict:
    loc = (location_raw or "").strip()
    combo = f"{loc}\n{(description_text or '')[:1500]}"
    low_loc = loc.lower()

    # split multi-location strings and evaluate each; best tier wins
    parts = [p.strip() for p in re.split(r"[;/|]| and |\n|,? or ", loc) if p.strip()] or [loc]

    best = ("reject", "no location signal")
    rank = {"tier_1": 3, "tier_2": 2, "reject": 1}

    def consider(tier: str, reason: str):
        nonlocal best
        if rank[tier] > rank[best[0]]:
            best = (tier, reason)
        elif tier == best[0] == "reject" and best[1] == "no location signal":
            best = (tier, reason)  # keep a specific reject reason for the tuning log

    is_remote_anywhere = bool(REMOTE.search(low_loc)) or (not loc and REMOTE.search(combo))

    for part in parts:
        p = part.lower()
        remote = bool(REMOTE.search(p))
        us_here = bool(US_WORDS.search(part)) or bool(_tokens_states(part))
        non_us_here = bool(NON_US.search(part))

        metro = settings.load().onsite_metro_re
        if metro and metro.search(part):
            consider("tier_2", f"onsite metro: {part!r}")
            continue
        if remote:
            if non_us_here and not us_here:
                consider("reject", f"non-US remote: {part!r}")
            elif us_here or "remote" == p.strip() or p.strip() in ("remote - us", "us remote", "remote (us)"):
                consider("tier_1", f"US remote: {part!r}")
            else:
                consider("tier_1", f"remote (country unspecified, assumed US): {part!r}")
            continue
        # non-remote physical location
        if non_us_here:
            consider("reject", f"non-US onsite: {part!r}")
        elif _tokens_states(part) or US_WORDS.search(part):
            consider("reject", f"US onsite outside your metro: {part!r}")
        else:
            consider("reject", f"unrecognized location: {part!r}")

    tier, reason = best

    # whole-string checks that override part-splitting
    non_us_combo = bool(NON_US.search(loc)) or bool(NON_US.search((description_text or "")[:400]))
    us_combo = bool(US_WORDS.search(loc)) or bool(_tokens_states(loc))
    if tier != "reject" and non_us_combo and not us_combo:
        tier, reason = "reject", f"non-US remote/onsite ({loc!r})"
    elif tier == "reject" and is_remote_anywhere:
        # remote appeared somewhere but no part resolved it
        states = _tokens_states(loc)  # from location string only, not prose
        if non_us_combo and not us_combo:
            tier, reason = "reject", f"non-US remote ({loc!r})"
        elif states and not _home_in(states) and len(states) <= 12:
            tier, reason = "reject", f"remote but state-restricted, home state not in list ({sorted(states)})"
        elif _home_in(states) or US_WORDS.search(loc):
            tier, reason = "tier_1", "US remote (from combined text)"
        else:
            tier, reason = "tier_1", "remote, US assumed (no explicit country)"

    # state-restriction refinement for accepted remote roles
    ambiguous = False
    if tier == "tier_1":
        states = _tokens_states(combo)
        # a plausible explicit state allow-list without the home state
        if 2 <= len(states) <= 15 and not _home_in(states) \
           and re.search(r"\b(eligible|available|hire|open) (in|to|for|only)\b|states? (of|where|listed)", combo, re.I):
            tier, reason = "reject", f"remote state allow-list without home state ({sorted(states)})"
        elif "country unspecified" in reason or "US assumed" in reason or "no explicit country" in reason:
            ambiguous = True

    return {"location_tier": tier, "location_reason": reason, "location_ambiguous": ambiguous}


def main() -> None:
    jobs = read_jsonl(DATA / "03-jobs-raw.jsonl")
    if not jobs:
        raise SystemExit("Stage 4: data/03-jobs-raw.jsonl missing/empty. Run stage 3.")
    log(f"Stage 4: {len(jobs)} raw postings")

    title_verdicts = classify_titles([j["title"] for j in jobs])

    eligible, rej_title, rej_loc, loc_review = [], [], [], []
    for j in jobs:
        tv = title_verdicts.get((j["title"] or "").strip(),
                                {"bucket": "reject", "reason": "no verdict"})
        if tv["bucket"] == "reject":
            rej_title.append({**_slim(j), "title_reason": tv.get("reason", ""),
                              "title_via": tv.get("_via", "")})
            continue

        lv = classify_location(j["location_raw"], j["description_text"])
        if lv["location_tier"] == "reject":
            rej_loc.append({**_slim(j), "title_bucket": tv["bucket"],
                            "location_reason": lv["location_reason"]})
            continue

        rec = {**j, "title_bucket": tv["bucket"], "title_reason": tv.get("reason", ""),
               "title_via": tv.get("_via", ""), **lv}
        eligible.append(rec)
        if lv["location_ambiguous"]:
            loc_review.append({**_slim(j), "location_reason": lv["location_reason"]})

    write_jsonl(DATA / "04-jobs-eligible.jsonl", eligible)
    write_jsonl(DATA / "rejects-title.jsonl", rej_title)
    write_jsonl(DATA / "rejects-location.jsonl", rej_loc)
    write_jsonl(DATA / "04-location-review.jsonl", loc_review)

    by_track = {}
    for r in eligible:
        by_track[r["title_bucket"]] = by_track.get(r["title_bucket"], 0) + 1
    t1 = sum(1 for r in eligible if r["location_tier"] == "tier_1")
    t2 = sum(1 for r in eligible if r["location_tier"] == "tier_2")
    tracks_str = " ".join(f"{tid}={n}" for tid, n in sorted(by_track.items()))
    log(f"Stage 4: eligible={len(eligible)} ({tracks_str}; "
        f"tier_1={t1} tier_2={t2}); rejects: title={len(rej_title)} location={len(rej_loc)}; "
        f"ambiguous-accepts logged={len(loc_review)}")


def _slim(j: dict) -> dict:
    return {k: j[k] for k in ("company", "company_normalized", "ats", "title",
                              "location_raw", "url")}


if __name__ == "__main__":
    main()
