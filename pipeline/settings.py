"""Per-user settings from `settings.yaml` (copy `settings.example.yaml` to start).

Everything specific to the person running the pipeline lives here, not in code:
their own LinkedIn URL, employment history (for former-colleague inference), companies
to exclude, and where they will work onsite. `config.py` holds the scoring weights.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "settings.yaml"
EXAMPLE_PATH = ROOT / "settings.example.yaml"

_RESERVED_TRACK_IDS = {"reject", "recruiters"}


@dataclass(frozen=True)
class Track:
    id: str                       # slug used in artifacts, e.g. "engineering_leadership"
    name: str                     # display name, e.g. "Engineering leadership"
    titles: tuple[str, ...]       # example titles that are this track
    exclude: tuple[str, ...]      # titles/words that are NOT this track
    manages_people: bool          # roles in this track manage people


@dataclass(frozen=True)
class Employer:
    tag: str          # slug used in artifacts, e.g. "acme_corp"
    name: str
    start: date
    end: date
    weight: float     # 0..1, how strongly a connection from this era counts


# Built-in tracks when settings.yaml has no `tracks` key. The ids are hard-coded (not
# slugs of the names) so existing artifacts keep their meaning.
DEFAULT_TRACKS = (
    Track(id="leadership", name="Engineering leadership", manages_people=True,
          titles=("Engineering Manager", "Senior Engineering Manager",
                  "Director of Engineering", "Senior Director of Engineering",
                  "Head of Engineering", "VP Engineering", "SVP Engineering", "CTO"),
          exclude=()),
    Track(id="ic", name="Senior/Staff software engineering", manages_people=False,
          titles=("Senior Software Engineer", "Staff Software Engineer"),
          exclude=()),
)


@dataclass(frozen=True)
class Settings:
    linkedin_url: str = ""
    employers: tuple[Employer, ...] = ()
    excluded_companies: tuple[str, ...] = ()
    home_state: str = ""            # full US state name, lowercase, e.g. "new york"
    home_state_abbr: str = ""       # e.g. "ny"
    onsite_metro_patterns: tuple[str, ...] = field(default_factory=tuple)
    tracks: tuple[Track, ...] = DEFAULT_TRACKS
    _tracks_custom: bool = False  # user supplied `tracks` (vs built-in DEFAULT_TRACKS)

    @property
    def custom_tracks(self) -> bool:
        """True when the user supplied their own tracks (vs the built-in SWE ones)."""
        return self._tracks_custom

    @property
    def tracks_fingerprint(self) -> str:
        payload = [{"id": t.id, "name": t.name, "titles": list(t.titles),
                    "exclude": list(t.exclude), "manages_people": t.manages_people}
                   for t in self.tracks]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]

    @property
    def onsite_metro_re(self) -> re.Pattern | None:
        if not self.onsite_metro_patterns:
            return None
        return re.compile(r"\b(" + "|".join(self.onsite_metro_patterns) + r")\b", re.I)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "employer"


def _month(v, *, end: bool) -> date:
    """'2018-01' / '2018-01-15' / 'present' -> date (end-of-month for an end bound)."""
    s = str(v).strip().lower()
    if s in ("present", "now", "current", ""):
        return date.today()
    parts = [int(x) for x in s.split("-")]
    y, m = parts[0], parts[1] if len(parts) > 1 else (12 if end else 1)
    if len(parts) > 2:
        return date(y, m, parts[2])
    if not end:
        return date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def _parse_tracks(raw: dict) -> tuple[Track, ...]:
    tracks_raw = raw.get("tracks") or []
    if not tracks_raw:
        return DEFAULT_TRACKS
    tracks, seen = [], set()
    for i, t in enumerate(tracks_raw):
        name = str(t.get("name") or "").strip()
        titles = tuple(str(x).strip() for x in (t.get("titles") or []) if str(x).strip())
        if not name:
            raise ValueError(f"tracks[{i}]: every track needs a non-empty name")
        if not titles:
            raise ValueError(f"tracks[{i}] ({name!r}): at least one title is required")
        tid = _slug(name)
        if tid in _RESERVED_TRACK_IDS:
            raise ValueError(f"tracks[{i}] ({name!r}): track id {tid!r} is reserved")
        if tid in seen:
            raise ValueError(f"tracks[{i}]: duplicate track id {tid!r}")
        seen.add(tid)
        tracks.append(Track(
            id=tid, name=name, titles=titles,
            exclude=tuple(str(x).strip() for x in (t.get("exclude") or []) if str(x).strip()),
            manages_people=bool(t.get("manages_people", False))))
    return tuple(tracks)


def parse(raw: dict) -> Settings:
    raw = raw or {}
    employers = []
    for e in raw.get("employers") or []:
        employers.append(Employer(
            tag=_slug(e["name"]), name=e["name"],
            start=_month(e["start"], end=False), end=_month(e.get("end", "present"), end=True),
            weight=float(e.get("weight", 1.0))))
    loc = raw.get("location") or {}
    return Settings(
        linkedin_url=(raw.get("linkedin_url") or "").strip(),
        employers=tuple(employers),
        excluded_companies=tuple(raw.get("excluded_companies") or ()),
        home_state=(loc.get("home_state") or "").strip().lower(),
        home_state_abbr=(loc.get("home_state_abbr") or "").strip().lower(),
        onsite_metro_patterns=tuple(loc.get("onsite_metro_patterns") or ()),
        tracks=_parse_tracks(raw),
        _tracks_custom=bool(raw.get("tracks")),
    )


@lru_cache(maxsize=1)
def load() -> Settings:
    if not SETTINGS_PATH.exists():
        print(f"WARNING: settings.yaml not found; running without employers, exclusions or a "
              f"home location. Copy {EXAMPLE_PATH.name} to settings.yaml and fill it in.",
              file=sys.stderr)
        return Settings()
    return parse(yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")))
