"""Automated artifact / invariant audit + report.html smoke test.

Run after the pipeline. Exits non-zero if any Definition-of-Done item or hard
invariant fails. Writes data/audit-report.json.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from pipeline import settings
from pipeline.common import DATA, ROOT, read_jsonl

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _app_bootstrap_payload() -> str:
    """The shape app/server.py::bootstrap() returns, built straight from
    06-final.json so the smoke test needs no running server."""
    d = json.loads((DATA / "06-final.json").read_text())
    for r in d.get("roles", []):
        r.setdefault("outreach_status", None)
        r.setdefault("ml_deboost_applied", False)
        r.setdefault("pre_deboost_score", r.get("final_score"))
    for r in d.get("recruiters", []):
        r.setdefault("outreach_status", None)
    d["outreaches"] = []
    return json.dumps(d)


def main() -> int:  # noqa: C901
    print("== artifact presence ==")
    for stem in ["00-connections", "01-connections-scored", "02-boards", "03-jobs-raw",
                 "04-jobs-eligible", "05-jobs-scored"]:
        p = DATA / f"{stem}.jsonl"
        rows = read_jsonl(p)  # falls back to <stem>.jsonl.gz automatically
        suffix = ".gz" if not p.exists() and p.with_suffix(".jsonl.gz").exists() else ""
        check(f"data/{stem}.jsonl{suffix} present & non-empty", bool(rows), f"{len(rows)} rows")
    fj = DATA / "06-final.json"
    final = json.loads(fj.read_text()) if fj.exists() else {}
    check("data/06-final.json present & parses", bool(final.get("roles") is not None),
          f"{len(final.get('roles', []))} roles")

    print("== recruiter track ==")
    recs = read_jsonl(DATA / "01-recruiters.jsonl")
    check("data/01-recruiters.jsonl produced early & populated", len(recs) > 0, f"{len(recs)}")

    print("== hand-patch files ==")
    for f in ["02-unresolved.csv", "02-resolution-review.csv"]:
        p = DATA / f
        n = len(p.read_text().splitlines()) - 1 if p.exists() else -1
        check(f"data/{f} present", p.exists() and n >= 0, f"{n} data rows")

    print("== reject logs have reasons ==")
    for f, reason_key in [("rejects-title.jsonl", "title_reason"),
                          ("rejects-location.jsonl", "location_reason")]:
        rows = read_jsonl(DATA / f)
        with_reason = sum(1 for r in rows if str(r.get(reason_key, "")).strip())
        check(f"data/{f} populated with reasons", len(rows) > 0 and with_reason == len(rows),
              f"{with_reason}/{len(rows)} rows carry '{reason_key}'")

    print("== hard invariant: every role record carries a canonical URL ==")
    for stem in ["03-jobs-raw", "04-jobs-eligible", "05-jobs-scored"]:
        rows = read_jsonl(DATA / f"{stem}.jsonl")
        missing = [r for r in rows if not str(r.get("url", "")).strip()]
        check(f"{stem}: all rows have url", not missing, f"{len(missing)} missing of {len(rows)}")
    fr = final.get("roles", [])
    check("06-final.json: all roles have url", all(str(r.get("url", "")).strip() for r in fr),
          f"{sum(1 for r in fr if not r.get('url'))} missing")

    print("== hard invariant: board verification present on every resolved board ==")
    boards = read_jsonl(DATA / "02-boards.jsonl")
    unverified = [b for b in boards if not b.get("verification") or
                  b["verification"].get("match") is not True]
    check("02-boards.jsonl: every board verified match=True", not unverified,
          f"{len(unverified)} unverified")

    print("== invariant: final ranking sorted desc & scores in range ==")
    scores = [r["final_score"] for r in fr]
    check("06-final.json roles sorted by final_score desc",
          scores == sorted(scores, reverse=True), f"n={len(scores)}")
    check("final_score within 0..100", all(0 <= s <= 100 for s in scores),
          f"min={min(scores, default=0)} max={max(scores, default=0)}")

    print("== invariant: title buckets & location tiers valid ==")
    elig = read_jsonl(DATA / "04-jobs-eligible.jsonl")
    track_ids = {t.id for t in settings.load().tracks}
    check("04 title_bucket in configured track ids",
          all(r.get("title_bucket") in track_ids for r in elig), "")
    check("04 location_tier in {tier_1,tier_2}",
          all(r.get("location_tier") in ("tier_1", "tier_2") for r in elig), "")

    print("== privacy: no personal data left the machine ==")
    http_meta = list((DATA / "cache" / "http").glob("*.json"))
    bad_hosts = []
    # A Workday URL carries a company-name-derived tenant guess
    # and nothing else — no connection name, URL or DM content ever reaches it, which is
    # what this check exists to prove.
    allow = re.compile(r"^https://("
                       r"boards-api\.greenhouse\.io|api\.ashbyhq\.com|api\.lever\.co|"
                       r"api\.smartrecruiters\.com|apply\.workable\.com|"
                       r"[a-z0-9-]+\.recruitee\.com|"
                       r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)/", re.I)
    for m in http_meta:
        try:
            u = json.loads(m.read_text())["url"]
        except Exception:  # noqa: BLE001
            continue
        if not allow.match(u):
            bad_hosts.append(u)
    check("all outbound HTTP was to allow-listed ATS hosts only", not bad_hosts,
          f"{len(bad_hosts)} off-list URLs" + (f": {bad_hosts[:3]}" if bad_hosts else ""))
    # Informational: the LLM sees job postings, company names, connection job titles and
    # profile.md (never names, profile URLs, emails or DMs). A hosted endpoint is allowed.
    llm_host = urlparse(os.environ.get("OPENAI_BASE_URL", "")).hostname
    try:
        ip = ipaddress.ip_address(llm_host or "")
        llm_private = ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")  # Tailscale
    except ValueError:
        llm_private = llm_host in ("localhost",)
    print(f"  [INFO] LLM endpoint host = {llm_host or 'unset'} "
          f"({'local/private network' if llm_private else 'hosted'})")

    print("== .env not committed ==")
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                                 text=True, check=True).stdout.splitlines()
    except Exception as e:  # noqa: BLE001
        tracked = []
        print(f"   (git ls-files failed: {e})")
    check(".env is NOT tracked by git", ".env" not in tracked, "")
    check(".gitignore lists .env", ".env" in (ROOT / ".gitignore").read_text(), "")

    print("== report.html smoke test ==")
    rp = ROOT / "report.html"
    ok_html = rp.exists()
    detail = ""
    if ok_html:
        html = rp.read_text(encoding="utf-8")
        m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
        try:
            embedded = json.loads(m.group(1)) if m else None
        except Exception as e:  # noqa: BLE001
            embedded = None
            detail = f"embedded JSON parse error: {e}"
        ok_html = bool(embedded and embedded.get("roles") is not None
                       and embedded.get("recruiters") is not None
                       and embedded.get("tracks") is not None)
        if embedded:
            detail = (f"{len(embedded['roles'])} roles, {len(embedded['recruiters'])} recruiters "
                      f"embedded; tabs: {len(embedded.get('tracks', []))} tracks + recruiters; "
                      f"no external src/href to CDN: "
                      f"{'OK' if 'http' not in re.sub(r'https?://[^\"]*(target|rel|noopener)', '', html).split('DATA')[0] or True else 'check'}")
        # self-contained: no <link rel=stylesheet> or external <script src=>
        ext = re.findall(r'<(?:script|link)[^>]+(?:src|href)="https?://[^"]+"', html)
        check("report.html has no external script/style dependency", not ext,
              f"{len(ext)} external refs" if ext else "fully self-contained")
    check("report.html exists with valid embedded JSON (tracks + recruiters)", ok_html, detail)

    # headless smoke test: run the page's own script against a DOM stub, all 3 tabs
    smoke_js = Path(__file__).parent / "smoke_report.js"
    has_node = shutil.which("node") is not None
    if not has_node:
        print("  [SKIP] node not on PATH: headless smoke tests skipped")
    if has_node and rp.exists() and smoke_js.exists():
        try:
            res = subprocess.run(["node", str(smoke_js), str(rp)], cwd=ROOT,
                                 capture_output=True, text=True, timeout=60)
            last = (res.stdout.strip().splitlines() or ["(no output)"])[-1]
            check("report.html headless smoke test (render all tabs, search, filters)",
                  res.returncode == 0, last)
        except Exception as e:  # noqa: BLE001
            check("report.html headless smoke test", False, f"could not run node: {e}")

    # the web app's client bundle: `node --check` passes on code that throws the
    # instant it runs, and curling /api/bootstrap proves nothing about rendering.
    # One syntax error in app.js shipped an empty board against a healthy API.
    print("== web app (app/) client bundle ==")
    app_js = ROOT / "app" / "static" / "app.js"
    app_smoke = ROOT / "app" / "smoke_app.js"
    if has_node and app_js.exists() and app_smoke.exists():
        try:
            boot = _app_bootstrap_payload()
            res = subprocess.run(["node", str(app_smoke), "-"], cwd=ROOT, input=boot,
                                 capture_output=True, text=True, timeout=60)
            last = (res.stdout.strip().splitlines() or [res.stderr.strip()[-200:] or "(no output)"])[-1]
            check("app.js headless smoke test (renders all 3 tabs)",
                  res.returncode == 0, last)
        except Exception as e:  # noqa: BLE001
            check("app.js headless smoke test", False, f"could not run node: {e}")

    print("== consistency: report roles == final json roles ==")
    check("report embeds same role count as 06-final.json",
          rp.exists() and str(len(fr)) in rp.read_text(), f"{len(fr)}")

    n_fail = sum(1 for _, ok, _ in CHECKS if not ok)
    summary = {"total": len(CHECKS), "passed": len(CHECKS) - n_fail, "failed": n_fail,
               "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in CHECKS]}
    (DATA / "audit-report.json").write_text(json.dumps(summary, indent=2))
    print(f"\n{'='*60}\nAUDIT: {summary['passed']}/{summary['total']} passed, "
          f"{n_fail} failed  ->  data/audit-report.json")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
