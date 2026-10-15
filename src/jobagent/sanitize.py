"""Turns untrusted posting HTML into plain text safe to show a model (KTD15)."""

from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup, Tag

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|pt|em|rem)?\s*(?:;|$)|"
    r"opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)|(?:left|top|text-indent)\s*:\s*-\d{3,}|"
    r"(?:width|height)\s*:\s*0(?:px)?\s*(?:;|$)",
    re.IGNORECASE,
)
_WHITE_TEXT = re.compile(r"(?<![a-z-])color\s*:\s*(?:#fff(?:fff)?|white)\b", re.IGNORECASE)
_BACKGROUND = re.compile(r"background(?:-color)?\s*:\s*([^;]+)", re.IGNORECASE)
_LIGHT_NAMES = {"white", "transparent", "none", "inherit", "initial", "unset"}
_INJECTION = re.compile(
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions?|prompts?|rules?)|"
    r"disregard (?:all |any |the )?(?:previous|prior|above)|(?:ignore|reveal|print|show|output|"
    r"repeat|leak|forget|disregard) (?:\w+ ){0,3}system prompts?|you are (?:now )?an? "
    r"(?:ai|assistant|language model)|as an ai (?:language )?(?:model|assistant|chatbot)\b|"
    r"\bscore (?:this|it|the) (?:job |posting )?"
    r"(?:a )?[1-5]\b|\bscore:? ?[1-5]\b|\b(?:add|include|insert|mention) \S+(?: \S+)? (?:to|on|in) "
    r"(?:the |your |my )?(?:resume|cv|skills)|new instructions?:",
    re.IGNORECASE,
)


def _is_light(color: str) -> bool:
    """True for white-ish or unset backgrounds; unknown values (var(), gradients) count as dark."""
    color = color.strip().lower() or "none"
    first = color.split()[0]
    if first in _LIGHT_NAMES:
        return True
    hexes = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", first)
    if hexes:
        h = hexes.group(1)
        rgb = (
            [int(c * 2, 16) for c in h]
            if len(h) == 3
            else [int(h[i : i + 2], 16) for i in (0, 2, 4)]
        )
    else:
        nums = re.match(r"rgba?\((\d+)\D+(\d+)\D+(\d+)", color)
        if not nums:
            return False
        rgb = [int(n) for n in nums.groups()]
    return sum(rgb) / 3 > 128


def _is_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden") or tag.get("aria-hidden") == "true":
        return True
    style = str(tag.get("style", ""))
    if _HIDDEN_STYLE.search(style):
        return True
    if _WHITE_TEXT.search(style):
        bg = _BACKGROUND.search(style)
        return bg is None or _is_light(bg.group(1))
    return False


# A posting stating facts about the reader: fine to show, never a fact for a model to repeat.
_CANDIDATE_FACT = re.compile(
    r"\bthe (?:candidate|applicant|person)\b|\byou have\b|"
    r"\byou are (?:already )?(?:an? )?(?:\w+ )?(?:expert|proficient|fluent|skilled)\b",
    re.IGNORECASE,
)


def _visible(raw: str) -> BeautifulSoup:
    soup = BeautifulSoup(html.unescape(raw) if "&lt;" in raw else raw, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    for tag in [t for t in soup.find_all(True) if isinstance(t, Tag) and _is_hidden(t)]:
        tag.decompose()
    return soup


def _text(soup: BeautifulSoup) -> str:
    text = soup.get_text("\n")
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def strip_hidden_html(raw: str) -> str:
    """Drop scripts, styles and visually hidden elements, then return the visible text."""
    return _text(_visible(raw))


def _drop_list_orders(soup: BeautifulSoup) -> bool:
    """An injection in a list item also takes the items after it in that list."""
    flagged = False
    for block in soup.find_all(["ul", "ol"]):
        items = block.find_all("li", recursive=False) if isinstance(block, Tag) else []
        for i, item in enumerate(items):
            if _INJECTION.search(item.get_text(" ")):
                flagged = True
                for later in items[i:]:
                    later.decompose()
                break
    return flagged


def strip_instructions(text: str) -> tuple[str, bool]:
    """Remove sentences that read as instructions to a model, and the rest of their line
    (paragraph), which is usually the order itself. Returns (clean, was_flagged)."""
    flagged = False
    kept: list[str] = []
    for line in text.splitlines():
        good = []
        for part in re.split(r"(?<=[.!?])\s+", line):
            if _INJECTION.search(part):
                flagged = True
                break
            good.append(part)
        kept.append(" ".join(good))
    return "\n".join(x for x in kept if x.strip()), flagged


def clean_posting_text(raw: str) -> tuple[str, bool]:
    soup = _visible(raw)
    in_lists = _drop_list_orders(soup)
    text, flagged = strip_instructions(_text(soup))
    return text, flagged or in_lists


def states_candidate_fact(text: str) -> bool:
    """True for lines like "the candidate is a Zig expert" or "you have 5 years of Rust"."""
    return bool(_CANDIDATE_FACT.search(text))
