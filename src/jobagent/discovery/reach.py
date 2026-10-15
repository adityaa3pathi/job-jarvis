"""One-off reachability check for LinkedIn, Naukri and Indeed (plan U1). Read only, logged out.

Prints one JSON line per site and saves each page's HTML for building fixtures.
Run: `python -m jobagent.discovery.reach OUT_DIR` (Naukri/Indeed need the `browser` extra).
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from jobagent.discovery.fetch import HttpFetcher

LINKEDIN = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    "?keywords=python%20developer&location=India&start=0"
)
NAUKRI = "https://www.naukri.com/python-developer-jobs-in-bangalore"
INDEED = "https://in.indeed.com/jobs?q=python+developer&l=Bangalore"
# Candidate card markers per site; counts only, so a wrong guess just reads as 0.
CARDS = {
    "linkedin": r'class="[^"]*base-search-card[ "]',
    "naukri": r'class="[^"]*srp-jobtuple-wrapper[ "]|class="[^"]*jobTuple[ "]',
    "indeed": r'class="[^"]*job_seen_beacon[ "]|data-jk="',
}
CHALLENGE = re.compile(
    r"just a moment|verify you are human|cf-chl|captcha|authwall|access denied|unusual traffic",
    re.IGNORECASE,
)


@dataclass
class Probe:
    site: str
    via: str
    status: int | None
    title: str
    cards: int
    challenge: bool
    error: str = ""


def _title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""


def _judge(site: str, via: str, status: int | None, html: str) -> Probe:
    cards = len(re.findall(CARDS[site], html))
    return Probe(site, via, status, _title(html), cards, bool(CHALLENGE.search(html[:50000])))


def probe_linkedin(out: Path) -> Probe:
    fetcher = HttpFetcher(allowed_hosts={"www.linkedin.com"})
    try:
        res = fetcher.get(LINKEDIN)
    except Exception as exc:  # report, never crash the check
        return Probe("linkedin", "http", None, "", 0, False, f"{type(exc).__name__}: {exc}")
    (out / "linkedin.html").write_text(res.text)
    return _judge("linkedin", "http", res.status, res.text)


def probe_browser(sites: dict[str, str], out: Path) -> list[Probe]:
    """Logged out, one tab, slow, no stealth: what a plain Chromium sees."""
    try:
        from playwright.sync_api import (
            sync_playwright,  # type: ignore[import-not-found,unused-ignore]
        )
    except ImportError:
        return [Probe(s, "browser", None, "", 0, False, "playwright not installed") for s in sites]
    probes = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for site, url in sites.items():
                page = browser.new_context().new_page()
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(8000)  # let the page's own scripts render jobs
                    html = page.content()
                    (out / f"{site}.html").write_text(html)
                    probes.append(_judge(site, "browser", resp.status if resp else None, html))
                except Exception as exc:
                    probes.append(
                        Probe(site, "browser", None, "", 0, False, f"{type(exc).__name__}: {exc}")
                    )
                finally:
                    page.context.close()
                time.sleep(15)
        finally:
            browser.close()
    return probes


def main(argv: list[str]) -> int:
    out = Path(argv[0] if argv else "reach-out")
    out.mkdir(parents=True, exist_ok=True)
    probes = [probe_linkedin(out), *probe_browser({"naukri": NAUKRI, "indeed": INDEED}, out)]
    for p in probes:
        print(json.dumps(asdict(p)))
    (out / "summary.json").write_text(json.dumps([asdict(p) for p in probes], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
