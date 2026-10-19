"""Regex + LLM hybrid string classifier.

Two verdict kinds, both cached to disk keyed by the exact input string so identical
strings get one identical verdict forever and re-runs are free:

  - position verdicts  -> data/cache/position-verdicts.json
      {seniority, function, is_recruiter}
  - title verdicts     -> data/cache/title-verdicts.json   (Stage 4; custom tracks get
      title-verdicts-<tracks fingerprint>.json)
      {bucket: <track id>|"reject"}

Flow: dedupe strings -> regex the unambiguous extremes (free) -> LLM the middle
(strict json_schema, concurrency 8) -> cache every verdict.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pipeline import settings
from pipeline.common import CACHE, llm_json_many, log, run

POSITION_CACHE = CACHE / "position-verdicts.json"
TITLE_CACHE = CACHE / "title-verdicts.json"

SENIORITY_BUCKETS = ["c_level", "vp", "head", "director", "senior_manager", "manager",
                     "staff_principal_lead", "senior_ic", "ic", "unknown"]
FUNCTION_BUCKETS = ["engineering", "data", "product", "design", "recruiting", "executive",
                    "marketing_growth", "other", "unknown"]


# ----------------------------------------------------------------------------------
# cache helpers
# ----------------------------------------------------------------------------------
def _load(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _save(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


# ----------------------------------------------------------------------------------
# POSITION classification (seniority + function + is_recruiter)
# ----------------------------------------------------------------------------------
_RECRUIT_RE = re.compile(
    r"\b(recruit|recruiter|recruiting|talent acquisition|talent partner|talent advisor|"
    r"technical sourcer|sourcer|sourcing|staffing|headhunt|people operations|people ops|"
    r"human resources|\bhr\b|hris|hrbp|people partner|people team)\b", re.I)

_CLEVEL_RE = re.compile(
    r"\b(ceo|cto|cfo|coo|cio|ciso|cmo|cpo|cro|chief|co-?founder|founder|owner|"
    r"president|partner)\b", re.I)
_VP_RE = re.compile(r"\b(vp|svp|evp|vice president|senior vice president)\b", re.I)
_HEAD_RE = re.compile(r"(^|\b)head of\b|\bglobal head\b", re.I)
_DIRECTOR_RE = re.compile(r"\b(director|dir\.)\b", re.I)
_SRMGR_RE = re.compile(r"\b(senior manager|sr\.? manager|group manager)\b", re.I)
_MGR_RE = re.compile(r"\b(manager|mgr|engineering manager|em|people manager)\b", re.I)
_STAFF_RE = re.compile(r"\b(staff|principal|distinguished|fellow|architect|tech lead|"
                       r"team lead|lead engineer|technical lead)\b", re.I)
_SENIOR_RE = re.compile(r"\b(senior|sr\.?|staff|lead)\b", re.I)

_ENG_RE = re.compile(
    r"\b(engineer|engineering|developer|software|sde|sre|devops|platform|infrastructure|"
    r"backend|back-end|frontend|front-end|full.?stack|mobile|ios|android|web|"
    r"technical|technology|systems|security engineer|qa|test|programmer|swe)\b", re.I)
_DATA_RE = re.compile(
    r"\b(data scientist|data science|data engineer|machine learning|ml engineer|"
    r"analytics|data analyst|business intelligence|\bbi\b|research scientist)\b", re.I)
_PRODUCT_RE = re.compile(
    r"\b(product manager|product management|product owner|group product|"
    r"head of product|vp product|chief product|\bpm\b|product lead|product director)\b", re.I)
_DESIGN_RE = re.compile(
    r"\b(design|designer|\bux\b|\bui\b|user experience|user research|"
    r"creative director|brand design)\b", re.I)
_MKTG_RE = re.compile(
    r"\b(marketing|growth|demand gen|demand generation|brand|content strategist|"
    r"seo|sem|social media|communications|pr manager|lifecycle marketing)\b", re.I)
_OTHER_RE = re.compile(
    r"\b(sales|account executive|account manager|business development|\bbd\b|"
    r"finance|accounting|controller|legal|counsel|attorney|operations|"
    r"customer success|support|procurement|supply chain|logistics|warehouse|"
    r"nurse|physician|teacher|professor|realtor|consultant|advisor)\b", re.I)


def _regex_position(pos: str) -> dict | None:
    """Return a full verdict only when confident on BOTH seniority and function."""
    p = (pos or "").strip()
    if not p:
        return {"seniority": "unknown", "function": "unknown", "is_recruiter": False}

    is_recruiter = bool(_RECRUIT_RE.search(p))

    # seniority
    sen = None
    if _CLEVEL_RE.search(p):
        sen = "c_level"
    elif _VP_RE.search(p):
        sen = "vp"
    elif _HEAD_RE.search(p):
        sen = "head"
    elif _DIRECTOR_RE.search(p):
        sen = "director"
    elif _SRMGR_RE.search(p):
        sen = "senior_manager"
    elif _MGR_RE.search(p):
        sen = "manager"
    elif _STAFF_RE.search(p):
        sen = "staff_principal_lead"

    # function
    fn = None
    if is_recruiter:
        fn = "recruiting"
    elif _DATA_RE.search(p):
        fn = "data"
    elif _PRODUCT_RE.search(p):
        fn = "product"
    elif _DESIGN_RE.search(p):
        fn = "design"
    elif _ENG_RE.search(p):
        fn = "engineering"
    elif _MKTG_RE.search(p):
        fn = "marketing_growth"
    elif _OTHER_RE.search(p):
        fn = "other"

    if sen and fn:
        return {"seniority": sen, "function": fn, "is_recruiter": is_recruiter}
    if sen == "ic" and fn:
        return {"seniority": "ic", "function": fn, "is_recruiter": is_recruiter}
    return None


_POS_SCHEMA = {
    "type": "object",
    "properties": {
        "seniority": {"type": "string", "enum": SENIORITY_BUCKETS},
        "function": {"type": "string", "enum": FUNCTION_BUCKETS},
        "is_recruiter": {"type": "boolean"},
    },
    "required": ["seniority", "function", "is_recruiter"],
    "additionalProperties": False,
}

_POS_SYS = (
    "You classify a person's LinkedIn job title into a seniority bucket and a function "
    "bucket for a referral-value model. Classify on the ROLE, ignoring team/product "
    "suffixes (e.g. 'Engineering Manager, Payments Platform' is a manager in engineering). "
    "seniority: c_level (CxO/founder/president), vp (VP/SVP/EVP), head (Head of X), "
    "director (Director/Sr Director), senior_manager, manager (incl. Engineering Manager), "
    "staff_principal_lead (Staff/Principal/Lead/Architect IC), senior_ic (Senior/Sr <IC>), "
    "ic (other individual contributor), unknown. "
    "function: engineering, data, product, design, recruiting (recruiting/talent/sourcing/"
    "people/HR), executive (general management not tied to one function), marketing_growth, "
    "other (sales/finance/legal/ops/unrelated), unknown. "
    "is_recruiter: true if the person works in recruiting/talent/sourcing/staffing/people/HR."
)


def classify_positions(positions: list[str]) -> dict[str, dict]:
    """positions -> {position_string: {seniority, function, is_recruiter}}. Cached on disk."""
    cache = _load(POSITION_CACHE)
    distinct = sorted({(p or "").strip() for p in positions})
    todo: list[str] = []
    for p in distinct:
        if p in cache:
            continue
        rx = _regex_position(p)
        if rx is not None:
            cache[p] = {**rx, "_via": "regex"}
        else:
            todo.append(p)

    log(f"classify_positions: {len(distinct)} distinct, {len(distinct) - len(todo)} via "
        f"regex/cache, {len(todo)} -> LLM")

    if todo:
        items = [(p, f"Job title: {p!r}") for p in todo]
        results = run(llm_json_many(items, _POS_SCHEMA, schema_name="position",
                                    system=_POS_SYS, max_tokens=120))
        for p, parsed in results:
            if "_llm_error" in parsed:
                cache[p] = {"seniority": "unknown", "function": "unknown",
                            "is_recruiter": bool(_RECRUIT_RE.search(p)), "_via": "llm_error"}
            else:
                cache[p] = {**parsed, "_via": "llm"}
        _save(POSITION_CACHE, cache)
    else:
        _save(POSITION_CACHE, cache)

    return {p: cache[p] for p in distinct}


# ----------------------------------------------------------------------------------
# TITLE classification for job postings (Stage 4) -> track ids | reject
# ----------------------------------------------------------------------------------
# --- decisive regexes (the target is narrow, so aggressive regex matching is intended;
# --- used only for the default tracks — custom tracks send every title to the LLM) ---

# leadership: Engineering Manager and above, engineering org only
_ENG_CTX = (r"(software|software\s+engineering|engineering|eng|platform\s+engineering|"
            r"backend|back-end|frontend|front-end|full[ -]?stack|mobile|ios|android|"
            r"data\s+platform|data\s+engineering|site\s+reliability|sre|cloud\s+engineering|"
            r"developer|web\s+engineering|devops|product\s+engineering|"
            r"machine\s+learning\s+engineering)")
_TITLE_LEADER_RE = re.compile(
    r"(^|\b)("
    r"engineering manager|eng(ineering)? mgr|"
    r"(senior|sr\.?|staff|group|principal|first[- ]line|second[- ]line) engineering manager|"
    rf"{_ENG_CTX}\s+(development\s+)?manager|"
    rf"manager[\s,\-–—]+(of\s+)?{_ENG_CTX}|"
    rf"(senior\s+|sr\.?\s+|group\s+)?(director|dir\.?|senior director|sr\.? director|"
    rf"executive director|head)[\s,\-–—]+(of\s+)?{_ENG_CTX}|"
    rf"{_ENG_CTX}\s+(senior\s+)?director|engineering director|"
    rf"head of {_ENG_CTX}|"
    rf"(vp|svp|evp|vice president|senior vice president)[\s,\-–—]+(of\s+)?(software\s+|"
    rf"platform\s+|product\s+)?(engineering|eng|technology)|"
    r"chief technology officer|\bcto\b|chief engineer|"
    rf"(senior\s+|sr\.?\s+)?technical director|"
    rf"technical (lead )?manager"
    r")(\b|$)", re.I)

# ic-accept: Senior/Staff *Software* Engineer (+close software equivalents), nothing above
_TITLE_IC_RE = re.compile(
    r"(^|\b)(senior|sr\.?|staff)\s+"
    r"(software|software development|backend|back-end|frontend|front-end|full[ -]?stack|"
    r"fullstack|web|platform|application|applications|product|distributed systems|api)\s*"
    r"(software\s+)?(engineer|developer|swe)(\b|$)", re.I)
_TITLE_IC_PLAIN_RE = re.compile(
    r"(^|\b)(senior|sr\.?|staff)\s+(software\s+)?(engineer|developer)(\b|,|$| [ivx0-9])", re.I)
_TITLE_IC_EXCLUDE_RE = re.compile(
    r"\b(principal|senior staff|sr\.? staff|senior principal|distinguished|fellow|architect|"
    r"\blead\b|manager|director|\bvp\b|head of|intern|ii+\b|iii\b|iv\b|\b[2-9]\b)\b", re.I)

# obvious reject: any clearly non-(eng-leadership / senior-staff-SWE) role
_TITLE_OBVIOUS_REJECT_RE = re.compile(
    r"\b("
    r"intern|internship|co-?op|apprentice|trainee|fellow(ship)?|graduate|new ?grad|"
    r"junior|entry[- ]level|associate (engineer|developer)|"
    r"nurse|physician|clinician|therapist|counselor|behavior analyst|behavior technician|"
    r"\brbt\b|\bbcba\b|caregiver|aide|driver|warehouse|technician|electrician|mechanic|"
    r"plumber|hvac|welder|machinist|operator|custodian|cook|chef|barista|cashier|server|"
    r"account executive|account manager|account director|account partner|"
    r"sales|seller|business development|\bbdr\b|\bsdr\b|quota|territory|"
    r"recruiter|recruiting|talent acquisition|sourcer|"
    r"designer|\bux\b|\bui\b|creative|copywriter|brand|"
    r"marketing|growth marketing|demand gen|seo|content|social media|communications|\bpr\b|"
    r"accountant|accounting|controller|bookkeeper|payroll|treasury|\bfp&a\b|auditor|"
    r"attorney|counsel|paralegal|legal|compliance officer|"
    r"data scientist|data analyst|business analyst|\bbi\b|analytics engineer|"
    r"research (scientist|engineer)|applied (scientist|research)|"
    r"machine learning engineer|\bml engineer\b|\bai engineer\b|ml scientist|"
    r"deep learning|nlp engineer|computer vision engineer|"
    r"product manager|product owner|program manager|project manager|scrum master|"
    r"delivery manager|product marketing|"
    r"customer success|customer support|support engineer|technical support|"
    r"solutions engineer|solutions architect|sales engineer|solution consultant|"
    r"field engineer|implementation|onboarding|"
    r"forward deployed|member of technical staff|"
    r"consultant|consulting|advisory|"
    r"principal engineer|principal software|distinguished engineer|"
    r"software engineer (i|ii|iii|1|2|3)\b|"
    r"office manager|operations manager|program coordinator|administrative|executive assistant|"
    r"facilities|security guard|"
    r"\bengenheiro\b|\bd[ée]veloppeur\b|\bdesarrollador\b|\banalista\b|\bt[ée]cnico\b|"
    r"\bgest[aã]o\b|\bvendas\b|\bstage\b|\balternance\b|\bwerkstudent\b|\bpraktikum\b"
    r")\b", re.I)

_LOC_SUFFIX_RE = re.compile(
    r"\s*[\-–—,|/(]\s*(remote|hybrid|onsite|on-site|us|u\.s\.|usa|united states|"
    r"multiple|various|anywhere|new york|nyc|san francisco|seattle|austin|boston|chicago|"
    r"los angeles|denver|atlanta|dallas|houston|miami|london|toronto|bengaluru|bangalore|"
    r"remote[- ]us|\d{4,}).*$", re.I)
_REQ_RE = re.compile(r"\s*[\(\[]?\b(req|requisition|job|posting)\s*(id|#|no|number)?[:#\-\s]*"
                     r"[a-z]?-?\d{2,}\b[\)\]]?", re.I)


# any of these present = the title *might* be an eng-leadership or SWE role -> keep it
# for finer classification. None present = it cannot be in scope -> decisive reject.
_TECH_SIGNAL_RE = re.compile(
    r"\b(engineer|engineering|eng|developer|development|software|swe|technical|technolog|"
    r"platform|devops|infrastructure|infra|backend|back-end|frontend|front-end|"
    r"full[ -]?stack|fullstack|\bdata\b|database|security|\bcloud\b|systems|\bsystem\b|"
    r"mobile|\bios\b|android|\bai\b|\bml\b|\bapi\b|architect|programmer|\bsre\b|"
    r"reliability|\bweb\b|firmware|embedded|blockchain|robotics|machine learning|"
    r"distributed|compiler|kernel|graphics|\bqa\b|automation|\bsdk\b|\bcto\b)\b", re.I)


def _title_core(t: str) -> str:
    t = re.sub(r"\(.*?\)", " ", t)
    t = _REQ_RE.sub(" ", t)
    t = _LOC_SUFFIX_RE.sub("", t)
    return re.sub(r"\s+", " ", t).strip()

_TITLE_SCHEMA = {
    "type": "object",
    "properties": {
        "bucket": {"type": "string", "enum": ["leadership", "ic", "reject"]},
        "reason": {"type": "string"},
    },
    "required": ["bucket", "reason"],
    "additionalProperties": False,
}
_TITLE_SYS = (
    "You bucket a software job posting TITLE for a candidate who wants either an "
    "engineering-leadership role or a narrowly-scoped senior IC role. Classify on the "
    "role, not the team/product suffix.\n"
    "leadership = Engineering Manager, Senior Engineering Manager, Director/Sr Director of "
    "Engineering, Head of Engineering, VP/SVP Engineering, CTO.\n"
    "ic = ONLY 'Senior Software Engineer' or 'Staff Software Engineer' (and close "
    "equivalents like Senior/Staff Backend/Frontend/Full-Stack Engineer). NOT Principal, "
    "NOT Senior Staff, NOT Distinguished, NOT Fellow, NOT Architect, NOT Lead, NOT any "
    "non-software-engineering role.\n"
    "reject = everything else, including Principal+, IC below Senior, non-engineering "
    "roles, PM/EM-adjacent roles that aren't the above, data/ML/design/support/sales."
)


def _title_cache_path() -> Path:
    """Title verdicts depend on the configured tracks, so custom tracks get a
    fingerprinted cache file. Default tracks keep the legacy name."""
    s = settings.load()
    if not s.custom_tracks:
        return TITLE_CACHE
    return CACHE / f"title-verdicts-{s.tracks_fingerprint}.json"


def _custom_title_sys() -> str:
    s = settings.load()
    lines = [
        "You bucket a job posting TITLE for a candidate with the following target tracks. "
        "Classify on the role, not the team/product suffix.",
    ]
    for t in s.tracks:
        lines.append(f"- {t.id} ({t.name}): titles like {', '.join(t.titles)}."
                     + (f" NOT when the title involves {', '.join(t.exclude)}."
                        if t.exclude else ""))
    lines.append(
        "Pick the single best track id when the posting's role is the same role as (or a "
        "close equivalent of) one of that track's titles at the same level; otherwise "
        "return reject. Respect the exclusions above.")
    return "\n".join(lines)


def _custom_title_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "bucket": {"type": "string",
                       "enum": [t.id for t in settings.load().tracks] + ["reject"]},
            "reason": {"type": "string"},
        },
        "required": ["bucket", "reason"],
        "additionalProperties": False,
    }


def classify_titles(titles: list[str]) -> dict[str, dict]:
    """titles -> {title: {bucket, reason, via}}. Cached on disk keyed by exact title."""
    cache_path = _title_cache_path()
    cache = _load(cache_path)
    distinct = sorted({(t or "").strip() for t in titles if (t or "").strip()})
    custom = settings.load().custom_tracks
    todo: list[str] = []
    for t in distinct:
        if t in cache:
            continue
        if not custom:
            core = _title_core(t)
            low = core.lower()
            if _TITLE_LEADER_RE.search(core):
                cache[t] = {"bucket": "leadership", "reason": f"regex: leadership title ({core!r})",
                            "_via": "regex"}
            elif not _TECH_SIGNAL_RE.search(core):
                cache[t] = {"bucket": "reject",
                            "reason": f"regex: no technical signal in title ({core!r})",
                            "_via": "regex"}
            elif _TITLE_OBVIOUS_REJECT_RE.search(low):
                cache[t] = {"bucket": "reject", "reason": f"regex: non-target role ({core!r})",
                            "_via": "regex"}
            elif (_TITLE_IC_RE.search(core) or _TITLE_IC_PLAIN_RE.search(core)) \
                    and not _TITLE_IC_EXCLUDE_RE.search(low):
                cache[t] = {"bucket": "ic", "reason": f"regex: senior/staff software engineer ({core!r})",
                            "_via": "regex"}
            elif _TITLE_IC_EXCLUDE_RE.search(low) and not _TITLE_LEADER_RE.search(core) \
                    and re.search(r"\b(engineer|developer|architect)\b", low):
                cache[t] = {"bucket": "reject",
                            "reason": f"regex: IC above Staff / lead / non-SWE ({core!r})",
                            "_via": "regex"}
            elif re.search(r"\b(engineer|developer|programmer|swe)\b", low) and not re.search(
                    r"\b(senior|sr|staff|principal|lead|manager|director|head|vp|chief|"
                    r"president)\b", low):
                # a plain / mid / numbered IC engineer title — below Senior, out of scope
                cache[t] = {"bucket": "reject",
                            "reason": f"regex: IC below Senior ({core!r})", "_via": "regex"}
            else:
                todo.append(t)
        else:
            # custom tracks: no engineering-specific regexes, every title -> LLM
            todo.append(t)

    log(f"classify_titles: {len(distinct)} distinct, {len(distinct) - len(todo)} via "
        f"regex/cache, {len(todo)} -> LLM")

    if todo:
        items = [(t, f"Job posting title: {t!r}") for t in todo]
        if custom:
            schema, system = _custom_title_schema(), _custom_title_sys()
        else:
            schema, system = _TITLE_SCHEMA, _TITLE_SYS
        results = run(llm_json_many(items, schema, schema_name="title",
                                    system=system, max_tokens=120))
        for t, parsed in results:
            if "_llm_error" in parsed:
                cache[t] = {"bucket": "reject", "reason": "llm_error -> conservative reject",
                            "_via": "llm_error"}
            else:
                cache[t] = {**parsed, "_via": "llm"}
    _save(cache_path, cache)
    return {t: cache[t] for t in distinct}
