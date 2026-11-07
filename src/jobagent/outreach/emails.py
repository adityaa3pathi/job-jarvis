"""Email addresses: find them in text, guess them from a name, check the domain takes mail.
Patterns and filters adapted from yc-outreach (MIT, see LICENSES/yc-outreach-MIT.txt)."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable
from urllib.parse import unquote, urlsplit

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
BAD_EMAIL = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|css|js)$|sentry|wixpress|example\.|@2x|u00|domain\.com|email\.com",
    re.I,
)
# Shared inboxes reach no one in particular; kept only when nothing better exists.
ROLE_INBOXES = frozenset(
    {"info", "hello", "contact", "support", "help", "sales", "admin", "team", "press", "media",
     "privacy", "legal", "security", "billing", "noreply", "no-reply", "abuse", "office",
     "jobs", "careers", "hiring", "hr", "recruiting", "talent"}
)  # fmt: skip
PERSONAL_HOSTS = frozenset({"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "proton.me"})
MxCheck = Callable[[str], bool]


def domain_of(url_or_host: str) -> str:
    raw = url_or_host.strip()
    host = urlsplit(raw if "//" in raw else f"https://{raw}").hostname or ""
    return host.removeprefix("www.").lower()


def ascii_words(name: str) -> list[str]:
    flat = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", " ", flat).split()


def guesses(full_name: str, domain: str) -> list[str]:
    """Most likely first: first@ was right for 80% of 151 verified founder emails."""
    parts = ascii_words(full_name)
    if not parts or not domain:
        return []
    first, last = parts[0], parts[-1] if len(parts) > 1 else ""
    if not last:
        return [f"{first}@{domain}"]
    return [f"{first}@{domain}", f"{first}.{last}@{domain}", f"{first[0]}{last}@{domain}",
            f"{first}{last}@{domain}"]  # fmt: skip


def emails_in(text: str, domain: str = "") -> list[str]:
    """Addresses in page or posting text, at `domain` (or a subdomain) when one is given."""
    found: list[str] = []
    for raw in EMAIL_RE.findall(unquote(html.unescape(text or ""))):
        email = raw.lower().strip(".")
        host = email.split("@")[1]
        if BAD_EMAIL.search(email) or email in found:
            continue
        if domain and host != domain and not host.endswith("." + domain):
            continue
        found.append(email)
    return found


def is_role_inbox(email: str) -> bool:
    return email.split("@")[0] in ROLE_INBOXES


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.fullmatch(email)) and not BAD_EMAIL.search(email)


def has_mail_server(domain: str, timeout_s: float = 4.0) -> bool:
    """MX lookup only: never connects to the server or sends a probe email."""
    import dns.exception
    import dns.resolver

    try:
        answer = dns.resolver.resolve(domain, "MX", lifetime=timeout_s)
    except (dns.exception.DNSException, OSError):
        return False
    return any(str(r.exchange).strip(".") for r in answer)
