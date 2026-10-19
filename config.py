"""Explicit, configurable scoring weights and knobs for the whole pipeline.

Edit this file to re-weight. Stages 1, 5, 6 and the recruiter track all read
from here at runtime. Re-running a stage after editing this file requires NO re-crawl
(profile.md is also read at runtime by Stage 5 for the LLM rubric).

All component sub-scores are normalized to 0..1 internally, combined with the weights
below (which should each sum to ~1.0 within a group), then scaled for display.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# OPENAI_BASE_URL, OPENAI_API_KEY, LLM_MODEL, LLM_DISABLE_THINKING; the real env wins.
load_dotenv(Path(__file__).with_name(".env"))

# --------------------------------------------------------------------------------------
# Stage 1 — connection score (components in descending weight)
# --------------------------------------------------------------------------------------
CONNECTION_WEIGHTS = {
    "seniority": 0.40,   # a senior person can actually push a referral through
    "function": 0.30,    # eng/product/design/data/recruiting can move a candidate
    "era": 0.15,         # connected_on inside an employment window => probable ex-colleague
    "dm_volume": 0.10,   # message_count >= 4 => a real relationship
    "dm_recency": 0.05,  # small bonus only; DM history is mostly pre-2024
}

# Seniority ladder -> 0..1. Parsed from the Position string (regex + LLM hybrid, §9).
SENIORITY_SCORES = {
    "c_level": 1.00,      # CEO/CTO/CFO/COO/Founder/Co-Founder/Chief *
    "vp": 0.95,           # VP / SVP / EVP
    "head": 0.90,         # Head of X
    "director": 0.85,     # Director / Senior Director / Sr Director
    "senior_manager": 0.65,
    "manager": 0.55,      # Manager / EM
    "staff_principal_lead": 0.45,  # Staff / Principal / Lead / Architect
    "senior_ic": 0.30,    # Senior <IC role>
    "ic": 0.20,           # everything else with a real title
    "unknown": 0.25,      # unparseable
}

# Function -> 0..1. Which org can plausibly move a candidate through.
FUNCTION_SCORES = {
    "engineering": 1.00,
    "data": 0.85,
    "product": 0.90,
    "design": 0.80,
    "recruiting": 0.90,   # recruiting / talent / sourcing / people / HR
    "executive": 0.70,    # general management not obviously tied to a function
    "marketing_growth": 0.50,
    "other": 0.20,        # finance, sales, ops, legal, unrelated
    "unknown": 0.30,
}

# Era overlap -> 0..1 comes from each employer's `weight` in settings.yaml.

# DM volume -> 0..1 (tiered; the >=4 threshold is the meaningful one).
def dm_volume_score(message_count: int) -> float:
    if message_count >= 10:
        return 1.00
    if message_count >= 4:
        return 0.70
    if message_count >= 1:
        return 0.30
    return 0.00

# DM recency -> 0..1 (small bonus only).
def dm_recency_score(last_message_year: int | None) -> float:
    if last_message_year is None:
        return 0.00
    if last_message_year >= 2026:
        return 1.00
    if last_message_year == 2025:
        return 0.80
    if last_message_year == 2024:
        return 0.60
    if last_message_year >= 2022:
        return 0.30
    return 0.10  # 2021 and earlier

CONNECTION_SCORE_SCALE = 100.0  # display scale for the composite

# Company-level connection score = MAX of its connections' scores.

# --------------------------------------------------------------------------------------
# Recruiter track ranking (DM volume, then recency, then seniority, then era)
# --------------------------------------------------------------------------------------
RECRUITER_WEIGHTS = {
    "dm_volume": 0.45,
    "dm_recency": 0.25,
    "seniority": 0.20,
    "era": 0.10,
}
RECRUITER_SCORE_SCALE = 100.0
# The tuple sort key mirrors the stated priority order exactly, as a tie-safe backstop:
#   (message_count, last_message_year, seniority_rank, era_rank)

# --------------------------------------------------------------------------------------
# Stage 5 — role score composite from LLM sub-scores (0..10 each)
# profile.md rubric order: domain (highest), scope, location, industry, stage/size (lowest).
# location is deterministic (Stage 4), applied as a multiplier in Stage 6, not here.
# --------------------------------------------------------------------------------------
ROLE_WEIGHTS = {
    "domain_match": 0.40,
    "scope_match": 0.30,
    "industry_fit": 0.15,
    "comp_signal": 0.15,
}

# ======================================================================================
# --- ML de-boost, staffing exclusions, outreach-aware ranking --------------------------
# Additive block, kept contiguous at the end of the scoring section. Applied entirely in
# stage 6 + pipeline/common.py; no stage-5 LLM schema change, no profile.md change here,
# no re-crawl.
# ======================================================================================

# --- Title de-boost --- optional: roles you are not credible for (e.g. ML-depth roles
# if you have no hands-on ML background). Title-based, deterministic, applied in stage 6.
# Matched titles are DE-BOOSTED, never dropped, so a role you could still get stays
# visible further down. Empty = off. Example:
#   [r"\bmachine learning\b", r"\bML\b", r"\bMLOps\b", r"\bdeep learning\b",
#    r"\bapplied scien", r"\bresearch scien"]
# (r"\bAI\b" is deliberately not in the example: "Voice AI Platform" etc. are ordinary
# platform jobs wearing an AI label.)
ML_TITLE_PATTERNS: list[str] = []
ML_DEBOOST_MULT = 0.35

# --- Outreach-aware ranking --- stage 6 reads data/outreach.db read-only.
# Marking a company contacted pushes that company's OTHER roles down so the board moves
# you toward new companies. It is not a to-do list. Missing DB => behave as today.
OUTREACH_DEAD_MULT = 0.15     # company status "dead" -> all roles at that company
OUTREACH_TOUCHED_MULT = 0.7   # company touched (contacted/applied/active), role NOT attached

# --- Staffing agencies / recruiting firms / IT body shops --- not employers. Hand-curated
# by reading data/02-unresolved.csv (NOT regex-guessed from "Group"/"Partners"). Wired into
# pipeline/common.py: EXCLUDED_COMPANIES_NORM so board resolution (stage 2) skips them.
# Excludes them from the ROLES board only; their people still flow to the recruiter track.
STAFFING_AGENCIES = [
    # explicitly named in the plan
    "Jobot", "Experis", "SPECTRAFORCE", "Nace Partners", "Continuity Partners",
    "Howell & Gibbs", "CBRE Upstate NY", "Cyperus Group LLC", "Confidential",
    # national / well-known staffing & recruiting firms
    "Robert Half", "Kforce Inc", "TEKsystems", "Randstad Digital US", "Randstad Enterprise",
    "Hays", "LHH", "Korn Ferry", "Riviera Partners", "Motion Recruitment Partners LLC",
    "Eliassen Group", "Collabera", "DIVERSANT, LLC", "Yoh, A Day & Zimmermann Company",
    "Johnson Service Group, Inc.", "Piper Companies", "Matlen Silver", "VARITE INC",
    "IDC Technologies, Inc.", "TSR Consulting Services, Inc",
    # smaller shops with an unambiguous staffing / search / recruiting name
    "Akshar Staffing", "BV Staffing", "Integrated Staffing Solutions",
    "Murtech Staffing & Solutions", "No Limit Staffing, Inc.", "Remedy Intelligent Staffing",
    "Hawkeye Search Group", "SilverSearch, Inc.", "NextStep Recruiting",
    "Nigel Frank International", "Steven Douglas Associates", "StevenDouglas",
]

# --- end additions ---------------------------------------------------------------------

# If True, final role score is the LLM's own overall_score; if False, the weighted
# composite of sub-scores above. We keep BOTH on every record; this picks the ranker.
ROLE_USE_LLM_OVERALL = False

# --------------------------------------------------------------------------------------
# Stage 6 — final ranking
#   final = connection_factor * role_factor * location_multiplier   (scaled to 0..100)
# Connection strength MODULATES rank, it does not gate: a great role at a
# company with one mid-level contact still surfaces. Hence the floor.
# --------------------------------------------------------------------------------------
CONNECTION_FACTOR_FLOOR = 0.30   # connection_factor in [FLOOR .. 1.0]
LOCATION_MULTIPLIER = {
    "tier_1": 1.00,   # Remote (US)
    "tier_2": 0.75,   # Onsite metro (onsite/hybrid)
}
FINAL_SCORE_SCALE = 100.0

# --------------------------------------------------------------------------------------
# Networking / crawl politeness
# --------------------------------------------------------------------------------------
HTTP_CONCURRENCY = 10          # cap 8-12
HTTP_TIMEOUT = 20.0
HTTP_RETRIES = 3
HTTP_BACKOFF_BASE = 1.5        # seconds; exponential on 429/5xx
USER_AGENT = "connections-finder/1.0 (personal job search; +local use only)"

# LLM endpoint + key: OPENAI_BASE_URL / OPENAI_API_KEY in .env (see .env.example).
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4.1-mini")
LLM_DISABLE_THINKING = os.environ.get("LLM_DISABLE_THINKING", "") == "1"
LLM_CONCURRENCY = 8          # empirically plateaus ~8; the server was heavily loaded overnight
LLM_TIMEOUT = 240.0         # per-attempt; long JD-scoring calls are slow under load
LLM_RETRIES = 2
LLM_CONCURRENCY_HEAVY = 7    # for long-prompt stages (5) when the server is loaded

# Timeboxes (seconds) — optional ATS providers (45 min total)
ATS_OPTIONAL_TIMEBOX = 45 * 60


# --------------------------------------------------------------------------------------
# --- Workday --- (Phase 2 optional ATS provider)
# --------------------------------------------------------------------------------------

# A Workday board is {tenant}.wd{N}.myworkdayjobs.com/{site} — three unknowns. Discovery
# probes robots.txt on each shard, which returns 200 + the site name on the right shard
# and 422 everywhere else (see pipeline/providers/workday.py).
#
# Only these wd{N} hostnames resolve in DNS (swept wd1..wd110 on 2026-09-10); the rest
# do not exist, which is what makes the sweep affordable. Ordered most-common first.
WORKDAY_SHARDS = ["wd1", "wd5", "wd3", "wd103", "wd12", "wd10", "wd102", "wd105",
                  "wd104", "wd107", "wd108", "wd109"]

# Tenant guesses per company (compact, hyphenated, and a long-enough first word).
WORKDAY_MAX_TENANT_TOKENS = 3

# robots.txt can advertise several career sites for one tenant (external, internal,
# campus, a country-specific board). They come back canonical-first; probing more than a
# couple is mostly duplicate postings.
WORKDAY_MAX_SITES_PER_TENANT = 3

# Workday caps `limit` at 20 server-side; larger values are rejected.
WORKDAY_PAGE_SIZE = 20

# Megacorp boards run to thousands of postings. This caps the crawl per board; the vast
# majority are non-engineering or non-US and get dropped in Stage 4 anyway.
WORKDAY_MAX_POSTINGS_PER_BOARD = 4000

# The shard sweep is a static 200-byte file per (tenant, shard). Its own concurrency and
# timeout, deliberately not HTTP_CONCURRENCY/HTTP_TIMEOUT: at 10-way concurrency with a
# 20s timeout and 3 retries, a few hanging hosts starve every slot (measured: 2 req/s vs
# 28 expected). Short timeout + wider fan-out for probes only; the actual posting crawl
# still runs at HTTP_CONCURRENCY.
WORKDAY_DISCOVERY_CONCURRENCY = 40
WORKDAY_PROBE_TIMEOUT = 8.0

# Discovery timebox (seconds). If tenant+shard discovery
# is eating the clock, stop and let data/manual-boards.yaml carry the rest.
WORKDAY_DISCOVERY_TIMEBOX = 25 * 60

# Cap on companies put through auto-discovery, highest connection count first. Hand-mapped
# boards in data/manual-boards.yaml are always honoured regardless of this cap.
WORKDAY_MAX_COMPANIES = 900

# Job-detail fetches (descriptions) are gated by a title/location pre-filter, then capped
# globally so one 4,000-posting board cannot consume the whole run.
WORKDAY_MAX_DETAIL_FETCHES = 12000

# Companies that are staffing agencies / not real employers: never worth probe budget.
# A local copy of the authoritative list in the scoring section, so the two land
# independently. Stage 2 unions the scoring-section set with this one.
WORKDAY_SKIP_COMPANIES = [
    "Jobot", "Experis", "SPECTRAFORCE", "Nace Partners", "Continuity Partners",
    "Howell & Gibbs", "CBRE", "Confidential", "Robert Half", "TEKsystems",
    "Insight Global", "Randstad", "Kforce", "Aerotek", "Motion Recruitment",
    "Michael Page", "Hays", "Adecco", "Manpower", "Collabera", "Apex Systems",
    "Cybercoders", "Diversant", "Beacon Hill Staffing Group", "StevenDouglas",
    "Huxley", "Talent Acquisition", "Staffing", "Recruiting",
]
