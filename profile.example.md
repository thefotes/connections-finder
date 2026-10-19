# Candidate Profile

Copy this file to `profile.md` (gitignored) and rewrite it about yourself. Stage 5 sends
it verbatim to the LLM as the rubric for scoring every eligible role, so plain, specific
language works best. Re-scoring after an edit needs no re-crawl:

    uv run python -m pipeline.stage5_score_roles && uv run python -m pipeline.stage6_assemble

Your employment history (for former-colleague inference), excluded companies and location
settings live in `settings.yaml`, not here.

## Summary

- **Current / most recent title:** Senior Engineering Manager
- **Location:** City, State (US, time zone)
- **Years of experience:** 12 (6 managing)

## What I am looking for

### Target roles — LEADERSHIP track (your "Engineering leadership" track in settings.yaml)

- Engineering Manager (floor: include, but weight below the rest)
- Senior Engineering Manager
- Director of Engineering

### Target roles — IC track (ranked separately; your "Senior/Staff software engineering" track)

- Senior Software Engineer
- Staff Software Engineer

The headings above are your tracks from `settings.yaml`; they must match it, since
stage 4 only keeps postings classified into a configured track.

### Location

- Remote (US) is the top preference.
- Onsite or hybrid in my metro (see `settings.yaml`) is a distant second.

### Compensation

- **Target: $180,000 base.** A soft signal, not a hard filter (many postings publish no band).
- Published band topping out below the target → heavy penalty.
- Published band clearing the target → positive signal, scaled by how much.
- No band published → neutral.

## Scoring rubric for roles

Weights in order of importance.

### 1. Domain match (highest weight)

Areas where I have real credibility, e.g.:

- Payments and billing systems
- Consumer mobile apps
- Backend platforms on AWS

### 2. Scope match

- Describe the scope you have held (team size, managers reporting to you) and what
  counts as a step up, sideways or down.

### 3. Location tier

Remote (US) scores meaningfully above onsite/hybrid.

### 4. Industry

- **Preferred:** industries where your experience transfers directly.
- **Neutral:** everything else.
- **Hard nos:** list them, or say "none declared". Categories you might object to are
  flagged (`industry_flag`), never filtered.

### 5. Stage and company size (lowest weight)

State a preference or "no preference; use as a tiebreaker".
