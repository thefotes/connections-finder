"""Stage 3: posting normalization."""

from __future__ import annotations

from pipeline.stage3_fetch import normalize_posting

BOARD = {"company": "ACME", "company_normalized": "acme", "board_token": "acme"}


def test_normalize_posting_rejects_non_http_url_scheme():
    for url in ("javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,x", "//evil.example/x", "http://["):
        job = {"title": "t", "content": "", "absolute_url": url}
        assert normalize_posting(BOARD, "greenhouse", job) is None, url


def test_normalize_posting_accepts_http_urls_case_insensitively():
    for url in ("https://jobs.acme.com/x", "HTTPS://jobs.acme.com/x", "http://jobs.acme.com/x"):
        job = {"title": "t", "content": "", "absolute_url": url}
        assert normalize_posting(BOARD, "greenhouse", job)["url"] == url
