"""Stage 2: board verification acceptance gate."""

from __future__ import annotations

from pipeline.stage2_boards import verdict_accepts


def test_low_trust_board_needs_high_confidence():
    board = {"ats": "recruitee"}
    assert not verdict_accepts({"match": True, "confidence": "medium"}, board)
    assert verdict_accepts({"match": True, "confidence": "high"}, board)
    assert not verdict_accepts({"match": False, "confidence": "high"}, board)


def test_trusted_and_manual_boards_accept_medium():
    medium = {"match": True, "confidence": "medium"}
    assert verdict_accepts(medium, {"ats": "greenhouse"})
    assert verdict_accepts(medium, {"ats": "workday", "source": "manual"})
    assert not verdict_accepts({"match": True, "confidence": "low"}, {"ats": "greenhouse"})
