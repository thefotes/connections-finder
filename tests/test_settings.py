"""settings.yaml drives employers (era), own-URL exclusion and the location filter."""

from __future__ import annotations

from datetime import date

import pytest

from pipeline import settings, stage0_parse, stage1_score, stage4_filter

RAW = {
    "employers": [
        {"name": "Acme Corp", "start": "2019-06", "end": "present", "weight": 1.0},
        {"name": "Old Co", "start": "2015-01", "end": "2019-07", "weight": 0.5},
    ],
    "excluded_companies": ["Acme Corp"],
    "location": {"home_state": "Ohio", "home_state_abbr": "OH",
                 "onsite_metro_patterns": ["columbus, ?oh", "columbus"]},
}


@pytest.fixture
def user(monkeypatch):
    s = settings.parse(RAW)
    monkeypatch.setattr(settings, "load", lambda: s)
    return s


def test_parse_months_and_slugs():
    s = settings.parse(RAW)
    acme, old = s.employers
    assert (acme.tag, acme.start, acme.end) == ("acme_corp", date(2019, 6, 1), date.today())
    assert old.end == date(2019, 7, 31)
    assert s.home_state == "ohio" and s.home_state_abbr == "oh"


def test_empty_settings_are_neutral():
    s = settings.parse({})
    assert s.employers == () and s.onsite_metro_re is None


def test_era_overlap_prefers_highest_weight(user):
    assert stage1_score.era_for("2019-07-15") == "acme_corp"
    assert stage1_score.era_for("2016-03-01") == "old_co"
    assert stage1_score.era_for("2010-01-01") == "none"
    assert stage1_score.era_score("old_co") == 0.5
    assert stage1_score.era_score("none") == 0.0


def test_own_url_is_detected_and_excluded(tmp_path):
    me = "https://www.linkedin.com/in/me"
    csv = tmp_path / "messages.csv"
    csv.write_text(
        "SENDER PROFILE URL,RECIPIENT PROFILE URLS,DATE\n"
        f"{me},https://www.linkedin.com/in/alice,2025-01-01 10:00:00 UTC\n"
        f"https://www.linkedin.com/in/alice,{me},2025-01-02 10:00:00 UTC\n"
        f"{me},https://www.linkedin.com/in/bob,2024-05-01 10:00:00 UTC\n",
        encoding="utf-8")
    assert stage0_parse.detect_own_url(csv) == me
    stats = stage0_parse.load_message_stats(csv, me)
    assert set(stats) == {"https://www.linkedin.com/in/alice", "https://www.linkedin.com/in/bob"}
    assert stats["https://www.linkedin.com/in/alice"]["count"] == 2


@pytest.mark.parametrize(("location", "tier"), [
    ("Remote - US", "tier_1"),
    ("Columbus, OH", "tier_2"),
    ("Austin, TX", "reject"),
    ("Remote (Canada)", "reject"),
])
def test_location_uses_home_metro(user, location, tier):
    assert stage4_filter.classify_location(location, "")["location_tier"] == tier


def test_state_allow_list_needs_home_state(user):
    desc = "This remote role is open to candidates in the following states: TX, CA, WA."
    assert stage4_filter.classify_location("Remote", desc)["location_tier"] == "reject"
    desc_ok = "This remote role is open to candidates in the following states: TX, OH, WA."
    assert stage4_filter.classify_location("Remote", desc_ok)["location_tier"] == "tier_1"


def test_default_tracks_when_absent():
    s = settings.parse({})
    assert not s.custom_tracks
    assert [t.id for t in s.tracks] == ["leadership", "ic"]
    assert s.tracks[0].manages_people and not s.tracks[1].manages_people


def test_custom_tracks_parse():
    s = settings.parse({"tracks": [
        {"name": "Engineering leadership", "titles": ["Engineering Manager"],
         "manages_people": True},
        {"name": "Senior IC", "titles": ["Senior Software Engineer"],
         "exclude": ["Intern"]},
    ]})
    assert s.custom_tracks
    assert [t.id for t in s.tracks] == ["engineering_leadership", "senior_ic"]
    assert s.tracks[1].manages_people is False
    assert s.tracks[1].exclude == ("Intern",)


def test_track_validation_errors():
    with pytest.raises(ValueError, match="non-empty name"):
        settings.parse({"tracks": [{"titles": ["PM"]}]})
    with pytest.raises(ValueError, match="at least one title"):
        settings.parse({"tracks": [{"name": "PM", "titles": []}]})
    with pytest.raises(ValueError, match="duplicate"):
        settings.parse({"tracks": [
            {"name": "Product", "titles": ["PM"]}, {"name": "product", "titles": ["Pm"]}]})
    with pytest.raises(ValueError, match="reserved"):
        settings.parse({"tracks": [{"name": "Reject", "titles": ["x"]}]})


def test_tracks_fingerprint_changes_with_titles():
    a = settings.parse({"tracks": [{"name": "PM", "titles": ["Senior PM"]}]})
    b = settings.parse({"tracks": [{"name": "PM", "titles": ["Staff PM"]}]})
    assert a.tracks_fingerprint != b.tracks_fingerprint
    assert a.tracks_fingerprint == settings.parse(
        {"tracks": [{"name": "PM", "titles": ["Senior PM"]}]}).tracks_fingerprint
