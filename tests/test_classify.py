"""Title classifier (stage 4): default regex path vs custom tracks -> stubbed LLM."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import classify, settings

CUSTOM = {"tracks": [{"name": "Product management", "titles": ["Senior Product Manager",
                                                              "Staff Product Manager"],
                      "exclude": ["Intern"]}]}
CUSTOM_FP = settings.parse(CUSTOM).tracks_fingerprint


class _FakeCacheRoot:
    """Stands in for pipeline.classify.CACHE so the fingerprinted cache lands in tmp."""

    def __init__(self, root: Path):
        self._root = root

    def __truediv__(self, key: str) -> Path:
        return self._root / key


@pytest.fixture
def no_llm(monkeypatch, tmp_path):
    """Stub llm_json_many + isolate the title cache in tmp_path."""
    calls = {}

    async def fake_llm_json_many(items, schema, *, schema_name, system, max_tokens, **kw):
        calls["items"] = items
        calls["schema"] = schema
        calls["system"] = system
        return [(t, {"bucket": "product_management", "reason": "stub"}) for t, _ in items]

    monkeypatch.setattr(classify, "llm_json_many", fake_llm_json_many)
    monkeypatch.setattr(classify, "TITLE_CACHE", tmp_path / "title-verdicts.json")
    monkeypatch.setattr(classify, "CACHE", _FakeCacheRoot(tmp_path))
    return calls


def test_default_tracks_regex_no_llm(no_llm):
    verdicts = classify.classify_titles(
        ["Senior Engineering Manager", "Staff Software Engineer", "Account Executive"])
    assert not no_llm  # no LLM call was made
    assert verdicts["Senior Engineering Manager"]["bucket"] == "leadership"
    assert verdicts["Staff Software Engineer"]["bucket"] == "ic"
    assert verdicts["Account Executive"]["bucket"] == "reject"
    assert all(v["_via"] == "regex" for v in verdicts.values())


def test_custom_tracks_send_to_llm(no_llm, monkeypatch):
    s = settings.parse(CUSTOM)
    monkeypatch.setattr(classify.settings, "load", lambda: s)
    verdicts = classify.classify_titles(["Senior Product Manager"])
    assert no_llm["items"] == [("Senior Product Manager", "Job posting title: "
                                "'Senior Product Manager'")]
    assert "Senior Product Manager" in no_llm["system"]
    assert "product_management" in no_llm["schema"]["properties"]["bucket"]["enum"]
    assert "reject" in no_llm["schema"]["properties"]["bucket"]["enum"]
    assert verdicts["Senior Product Manager"]["bucket"] == "product_management"


def test_custom_tracks_verdicts_cached_in_fingerprinted_file(no_llm, monkeypatch, tmp_path):
    s = settings.parse(CUSTOM)
    monkeypatch.setattr(classify.settings, "load", lambda: s)
    classify.classify_titles(["Senior Product Manager"])
    cached = json.loads((tmp_path / f"title-verdicts-{CUSTOM_FP}.json").read_text())
    assert cached["Senior Product Manager"]["bucket"] == "product_management"
