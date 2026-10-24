"""Stage 6: final JSON shape and report rendering with tracks."""

from __future__ import annotations

import json
import re

from pipeline import stage6_assemble


def _fixture(**kwargs):
    role = {
        "company": "Acme", "company_normalized": "acme", "title": "Senior Engineering Manager",
        "url": "https://jobs/acme", "ats": "greenhouse",
        "title_bucket": kwargs.get("bucket", "leadership"),
        "location_tier": "tier_1", "location_raw": "Remote",
        "location_reason": "US remote", "role_score": 8.0, "role_composite": 8.0,
        "comp_penalty_flag": False, "industry_flag": None,
        "score": {"comp_min": None, "comp_max": None, "overall_score": 8,
                  "domain_match": 8, "scope_match": 8, "industry_fit": 8,
                  "comp_signal": 5, "seniority_read": "senior",
                  "manages_managers": True, "rationale": "", "concerns": ""},
        "description_text": "",
    }
    return [role], [], []


def test_stage6_final_shape(tmp_path, monkeypatch):
    roles, conns, recruiters = _fixture()
    monkeypatch.setattr(stage6_assemble, "read_jsonl",
                        lambda p: roles if p.name.startswith("05") else
                                  (recruiters if p.name.startswith("01-recruiters") else conns))
    monkeypatch.setattr(stage6_assemble, "load_outreach", lambda: {})
    final_path = tmp_path / "06-final.json"
    monkeypatch.setattr(stage6_assemble, "write_json", lambda p, obj: final_path.write_text(json.dumps(obj)))
    rendered = {}
    monkeypatch.setattr(stage6_assemble, "render_report",
                        lambda final, path: rendered.update(html=stage6_assemble.
                                                            _REPORT_TEMPLATE.
                                                            replace("/*__DATA__*/",
                                                                    json.dumps(final))))
    stage6_assemble.main()
    final = json.loads(final_path.read_text())
    assert final["tracks"] == [
        {"id": "leadership", "name": "Engineering leadership", "manages_people": True},
        {"id": "ic", "name": "Senior/Staff software engineering", "manages_people": False},
    ]
    assert final["roles"][0]["tab"] == "leadership"
    assert final["stats"]["by_track"] == {"leadership": 1, "ic": 0}
    assert "leadership" not in final["stats"] and "ic" not in final["stats"]

    html = rendered["html"]
    embedded = json.loads(re.search(
        r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1))
    assert embedded["tracks"][0]["id"] == "leadership"
    assert "data-tab" not in html.split("<script id=\"data\"")[0].split('id="tabs"')[1]


def test_redact_pii_strips_identity_fields():
    final = {"stats": {"recruiters_total": 2, "roles_total": 1},
             "tracks": [],
             "recruiters": [{"name": "A", "profile_url": "u"}],
             "roles": [{"company": "ACME", "connection_count": 1,
                        "connections": [{"name": "X", "profile_url": "u",
                                         "message_count": 3, "connection_score": 9.0}],
                        "best_connection": {"name": "X"}}]}
    out = stage6_assemble.redact_pii(final)
    assert out["recruiters"] == []
    assert out["roles"][0]["best_connection"] is None
    assert out["roles"][0]["connection_count"] == 1
    c = out["roles"][0]["connections"][0]
    assert "name" not in c and "profile_url" not in c and "message_count" not in c
    assert c["connection_score"] == 9.0


def test_render_report_neutralizes_script_breakout(tmp_path):
    final = {"roles": [{"title": "</script><img src=x onerror=alert(1)>"}],
             "recruiters": [], "tracks": [], "meta": {}, "stats": {}}
    out = tmp_path / "report.html"
    stage6_assemble.render_report(final, out)
    html = out.read_text()
    island = re.search(
        r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
    assert "</script>" not in island
    # the escape is JSON-safe: parses back to the original string
    embedded = json.loads(island)
    assert embedded["roles"][0]["title"] == "</script><img src=x onerror=alert(1)>"
