"""Workday provider: defensive parsing of remote payloads."""

from __future__ import annotations

from pipeline.providers import workday as wd


def test_workday_parse_jobs_page_tolerates_malformed_total():
    jobs, total = wd.parse_jobs_page('{"total": "1,200", "jobPostings": [{"title": "t"}]}')
    assert total == 0 and len(jobs) == 1
    for bad in ('{"bad": 1}', "1e999"):
        jobs, total = wd.parse_jobs_page('{"total": %s, "jobPostings": []}' % bad)
        assert total == 0
    jobs, total = wd.parse_jobs_page('{"total": 42, "jobPostings": []}')
    assert total == 42
