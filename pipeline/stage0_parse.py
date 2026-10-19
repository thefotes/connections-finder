"""Stage 0 — parse + join.

Connections.csv + messages.csv -> data/00-connections.jsonl

- Connections.csv has 3 junk lines before the header.
- Join messages on normalized profile URL (lowercase, strip trailing slash).
- The user's own URL (settings.yaml, or auto-detected) is excluded when identifying
  the counterparty.
- messages.csv is optional; without it every DM count is zero.
- RECIPIENT PROFILE URLS may be comma-separated.
"""
from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pipeline import settings
from pipeline.common import DATA, ROOT, log, normalize_company, write_jsonl

INPUT = ROOT / "input"


def norm_url(u: str) -> str:
    u = (u or "").strip().lower()
    u = u.split("?")[0].rstrip("/")
    return u


def parse_connected_on(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_msg_date(s: str) -> datetime | None:
    s = (s or "").strip().replace(" UTC", "")
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_connections(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8-sig")
    # drop 3 junk lines before the header
    body = raw.split("\n", 3)[3]
    rows = list(csv.DictReader(io.StringIO(body)))
    return rows


def _participants(row: dict) -> list[str]:
    urls = [norm_url(row.get("SENDER PROFILE URL", ""))]
    urls += [norm_url(x) for x in (row.get("RECIPIENT PROFILE URLS", "") or "").split(",")]
    return [u for u in urls if u]


def detect_own_url(path: Path) -> str:
    """The account owner is a participant in (nearly) every message row."""
    counts: Counter[str] = Counter()
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            counts.update(set(_participants(row)))
    return counts.most_common(1)[0][0] if counts else ""


def load_message_stats(path: Path, own_url: str) -> dict[str, dict]:
    """Per counterparty normalized URL: {count, first, last}."""
    stats: dict[str, dict] = {}
    own = norm_url(own_url)
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sender = norm_url(row.get("SENDER PROFILE URL", ""))
            recips = [norm_url(x) for x in (row.get("RECIPIENT PROFILE URLS", "") or "").split(",")]
            recips = [r for r in recips if r]
            dt = parse_msg_date(row.get("DATE", ""))

            counterparties: set[str] = set()
            if sender and sender != own:
                counterparties.add(sender)
            for r in recips:
                if r and r != own:
                    counterparties.add(r)

            for cp in counterparties:
                s = stats.setdefault(cp, {"count": 0, "first": None, "last": None})
                s["count"] += 1
                if dt is not None:
                    if s["first"] is None or dt < s["first"]:
                        s["first"] = dt
                    if s["last"] is None or dt > s["last"]:
                        s["last"] = dt
    return stats


def main() -> None:
    conns_path, msgs_path = INPUT / "Connections.csv", INPUT / "messages.csv"
    if not conns_path.exists():
        raise SystemExit(f"Stage 0: {conns_path} not found. Put your LinkedIn export there "
                         f"(see README).")
    conns = load_connections(conns_path)
    log(f"Stage 0: {len(conns)} connection rows parsed")
    if msgs_path.exists():
        own_url = settings.load().linkedin_url or detect_own_url(msgs_path)
        log(f"Stage 0: own profile URL = {own_url or '(unknown)'}")
        msg_stats = load_message_stats(msgs_path, own_url)
    else:
        log("Stage 0: input/messages.csv not found; DM counts will be zero")
        msg_stats = {}
    log(f"Stage 0: {len(msg_stats)} distinct message counterparties")

    out = []
    joined = 0
    for r in conns:
        url = norm_url(r.get("URL", ""))
        first = (r.get("First Name") or "").strip()
        last = (r.get("Last Name") or "").strip()
        raw_company = (r.get("Company") or "").strip()
        ms = msg_stats.get(url, {"count": 0, "first": None, "last": None})
        if ms["count"]:
            joined += 1
        rec = {
            "name": f"{first} {last}".strip(),
            "first_name": first,
            "last_name": last,
            "profile_url": r.get("URL", "").strip(),
            "profile_url_norm": url,
            "email": (r.get("Email Address") or "").strip(),
            "raw_company": raw_company,
            "normalized_company": normalize_company(raw_company),
            "raw_position": (r.get("Position") or "").strip(),
            "connected_on": parse_connected_on(r.get("Connected On", "")),
            "message_count": ms["count"],
            "first_message_date": ms["first"].date().isoformat() if ms["first"] else None,
            "last_message_date": ms["last"].date().isoformat() if ms["last"] else None,
        }
        out.append(rec)

    n = write_jsonl(DATA / "00-connections.jsonl", out)
    with_company = sum(1 for r in out if r["normalized_company"])
    log(f"Stage 0: wrote {n} -> data/00-connections.jsonl "
        f"({joined} with >=1 message, {with_company} with a company)")


if __name__ == "__main__":
    main()
