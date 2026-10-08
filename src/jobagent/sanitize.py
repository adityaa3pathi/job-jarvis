"""Turns untrusted posting HTML into plain text safe to show a model (KTD15)."""

from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup, Tag

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|pt|em|rem)?\s*(?:;|$)|"
    r"opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)|(?:left|top|text-indent)\s*:\s*-\d{3,}|"
    r"(?:width|height)\s*:\s*0(?:px)?\s*(?:;|$)|"
    r"(?<![a-z-])color\s*:\s*(?:#fff(?:fff)?|white)\b",
    re.IGNORECASE,
)
_INJECTION = re.compile(
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions?|prompts?|rules?)|"
    r"disregard (?:all |any |the )?(?:previous|prior|above)|system prompt|you are (?:now )?an? "
    r"(?:ai|assistant|language model)|as an ai\b|\bscore (?:this|it|the) (?:job |posting )?"
    r"(?:a )?[1-5]\b|\bscore:? ?[1-5]\b|\b(?:add|include|insert|mention) \S+(?: \S+)? (?:to|on|in) "
    r"(?:the |your |my )?(?:resume|cv|skills)|new instructions?:",
    re.IGNORECASE,
)


def _is_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden") or tag.get("aria-hidden") == "true":
        return True
    return bool(_HIDDEN_STYLE.search(str(tag.get("style", ""))))


def strip_hidden_html(raw: str) -> str:
    """Drop scripts, styles and visually hidden elements, then return the visible text."""
    soup = BeautifulSoup(html.unescape(raw) if "&lt;" in raw else raw, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    for tag in [t for t in soup.find_all(True) if isinstance(t, Tag) and _is_hidden(t)]:
        tag.decompose()
    text = soup.get_text("\n")
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def strip_instructions(text: str) -> tuple[str, bool]:
    """Remove sentences that read as instructions to a model. Returns (clean, was_flagged)."""
    flagged = False
    kept: list[str] = []
    for line in text.splitlines():
        parts = re.split(r"(?<=[.!?])\s+", line)
        good = []
        for part in parts:
            if _INJECTION.search(part):
                flagged = True
            else:
                good.append(part)
        kept.append(" ".join(good))
    return "\n".join(x for x in kept if x.strip()), flagged


def clean_posting_text(raw: str) -> tuple[str, bool]:
    return strip_instructions(strip_hidden_html(raw))
