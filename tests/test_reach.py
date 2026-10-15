from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.discovery import reach
from jobagent.discovery.base import FetchResult


def test_judge_counts_cards_and_spots_challenges() -> None:
    cards = "<title>Jobs</title>" + '<div class="base-card base-search-card x">' * 3
    ok = reach._judge("linkedin", "http", 200, cards)
    assert (ok.cards, ok.challenge, ok.title) == (3, False, "Jobs")
    wall = reach._judge("indeed", "browser", 403, "<title>Just a moment...</title>")
    assert wall.challenge and wall.cards == 0


def test_linkedin_probe_saves_html_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = '<li><div class="base-card base-search-card">a</div></li>'
    monkeypatch.setattr(reach.HttpFetcher, "get", lambda self, url: FetchResult(200, page, url))
    got = reach.probe_linkedin(tmp_path)
    assert got.cards == 1 and (tmp_path / "linkedin.html").read_text() == page

    def boom(self: object, url: str) -> FetchResult:
        raise OSError("blocked")

    monkeypatch.setattr(reach.HttpFetcher, "get", boom)
    assert reach.probe_linkedin(tmp_path).error == "OSError: blocked"
