"""Read-only GitHub reader: pulls a repo's languages, manifests and layout into evidence cards."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import httpx

from jobagent.resume.evidence import EvidenceCard, cards_from_manifests, make_card, merge_cards

README_CHARS = 20_000
MAX_MANIFESTS = 8
MAX_MANIFEST_BYTES = 200_000
MIN_LANGUAGE_SHARE = 0.05
_SKIP_DIRS = {"node_modules", "vendor", ".venv", "venv", "dist", "build", "__pycache__"}
_TEST_DIRS = {"tests", "test", "__tests__", "spec"}
_PART = re.compile(r"^[A-Za-z0-9_.\-]+$")
_RAW = {"Accept": "application/vnd.github.raw+json"}


class GitHubError(Exception):
    pass


class RepoNotFound(GitHubError):
    pass


class RateLimited(GitHubError):
    pass


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str


@dataclass
class RepoSnapshot:
    ref: RepoRef
    description: str
    url: str
    topics: list[str]
    languages: dict[str, int]
    readme: str  # untrusted text; only later, delimited prompts may read it
    manifests: dict[str, str] = field(default_factory=dict)
    has_tests: bool = False
    has_ci: bool = False


def parse_repo_ref(raw: str) -> RepoRef:
    text = re.sub(r"^(https?://)?(www\.)?", "", raw.strip())
    if "." in text.split("/")[0]:
        if not text.startswith("github.com/"):
            raise GitHubError(f"not a github.com repo: {raw!r}")
        text = text[len("github.com/") :]
    parts = [p for p in text.split("/") if p]
    if len(parts) < 2 or (len(parts) > 2 and parts[2] not in ("tree", "blob")):
        raise GitHubError(f"expected owner/name, got {raw!r}")
    owner, name = parts[0], parts[1].removesuffix(".git")
    if not (_PART.match(owner) and _PART.match(name)):
        raise GitHubError(f"bad repo name in {raw!r}")
    return RepoRef(owner, name)


def make_client(
    token: str | None = None, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        base_url="https://api.github.com", headers=headers, timeout=20, transport=transport
    )


def _get(client: httpx.Client, path: str, raw: bool = False) -> httpx.Response:
    resp = client.get(path, headers=_RAW if raw else None)
    if resp.status_code == 404:
        raise RepoNotFound(path)
    if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
        raise RateLimited(f"rate limited; resets at {resp.headers.get('x-ratelimit-reset')}")
    if resp.status_code >= 400:
        raise GitHubError(f"{path}: HTTP {resp.status_code}")
    return resp


def _manifest_paths(tree: list[dict[str, object]]) -> list[str]:
    found = []
    for entry in tree:
        path = str(entry.get("path", ""))
        parts = PurePosixPath(path).parts
        size = entry.get("size")
        name = parts[-1] if parts else ""
        wanted = name in ("package.json", "pyproject.toml") or re.fullmatch(
            r"requirements[\w.\-]*\.txt", name
        )
        too_big = isinstance(size, int) and size > MAX_MANIFEST_BYTES
        if entry.get("type") == "blob" and wanted and not too_big and not _SKIP_DIRS & set(parts):
            found.append(path)
    return sorted(found, key=lambda p: (p.count("/"), p))[:MAX_MANIFESTS]


def _has_tests(paths: list[str]) -> bool:
    for path in paths:
        parts = PurePosixPath(path).parts
        name = parts[-1]
        if _TEST_DIRS & set(parts[:-1]) or name.startswith("test_") or ".test." in name:
            return True
    return False


def fetch_repo(client: httpx.Client, ref: RepoRef) -> RepoSnapshot:
    base = f"/repos/{ref.owner}/{ref.name}"
    meta = _get(client, base).json()
    languages = _get(client, f"{base}/languages").json()
    try:
        readme = _get(client, f"{base}/readme", raw=True).text[:README_CHARS]
    except RepoNotFound:
        readme = ""
    branch = meta.get("default_branch", "main")
    tree = _get(client, f"{base}/git/trees/{branch}?recursive=1").json().get("tree", [])
    paths = [str(e.get("path", "")) for e in tree]
    manifests = {
        p: _get(client, f"{base}/contents/{p}", raw=True).text for p in _manifest_paths(tree)
    }
    return RepoSnapshot(
        ref=ref,
        description=(meta.get("description") or "")[:300],
        url=meta.get("html_url", ""),
        topics=list(meta.get("topics", [])),
        languages=languages,
        readme=readme,
        manifests=manifests,
        has_tests=_has_tests(paths),
        has_ci=any(p.startswith(".github/workflows/") for p in paths),
    )


def cards_from_snapshot(snap: RepoSnapshot) -> list[EvidenceCard]:
    source = f"github:{snap.ref.owner}/{snap.ref.name}"
    label = f"{snap.ref.name}: {snap.description}" if snap.description else snap.ref.name
    cards = [make_card("project", label, source, False)]
    total = sum(snap.languages.values()) or 1
    for lang, size in snap.languages.items():
        if size / total >= MIN_LANGUAGE_SHARE:
            cards.append(make_card("skill", lang, f"{source}:languages", False))
    cards += cards_from_manifests(snap.manifests, source)
    if snap.has_tests:
        cards.append(make_card("skill", "Automated tests", f"{source}:tests", False))
    if snap.has_ci:
        cards.append(make_card("skill", "GitHub Actions", f"{source}:.github/workflows", False))
    return merge_cards(cards)
