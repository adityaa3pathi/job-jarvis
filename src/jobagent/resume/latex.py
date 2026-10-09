"""Reads and writes a resume in the common one-page LaTeX template without altering the rest."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

BEGIN = r"\begin{document}"
END = r"\end{document}"

_ARG_COUNT = {"section": 1, "resumeItem": 1, "resumeSubheading": 4, "resumeProjectHeading": 2}
_MACRO = re.compile(r"\\(section|resumeItem|resumeSubheading|resumeProjectHeading)(?![A-Za-z*])")
_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


class LatexError(ValueError):
    pass


@dataclass
class Raw:
    text: str

    def emit(self) -> str:
        return self.text


@dataclass
class Command:
    name: str
    args: list[str]
    gaps: list[str]

    def emit(self) -> str:
        parts = [f"\\{self.name}"]
        parts += [f"{gap}{{{arg}}}" for gap, arg in zip(self.gaps, self.args, strict=True)]
        return "".join(parts)


Node = Raw | Command


@dataclass
class Section:
    heading: Command
    nodes: list[Node] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.heading.args[0]

    def commands(self, name: str) -> list[Command]:
        return [n for n in self.nodes if isinstance(n, Command) and n.name == name]

    def emit(self) -> str:
        return self.heading.emit() + "".join(n.emit() for n in self.nodes)


@dataclass
class Resume:
    preamble: str
    head: list[Node]
    sections: list[Section]
    postamble: str

    def section(self, title: str) -> Section:
        for found in self.sections:
            if found.title == title:
                return found
        raise KeyError(title)

    def nodes_of(self, name: str) -> list[Command]:
        every: list[Node] = list(self.head)
        for sec in self.sections:
            every += sec.nodes
        return [n for n in every if isinstance(n, Command) and n.name == name]

    def emit(self) -> str:
        body = "".join(n.emit() for n in self.head) + "".join(s.emit() for s in self.sections)
        return self.preamble + body + self.postamble


def escape_latex(text: str) -> str:
    """Makes plain text safe to place inside a LaTeX argument."""
    return "".join(_ESCAPES.get(ch, ch) for ch in text)


def _in_comment(text: str, pos: int) -> bool:
    line = text[text.rfind("\n", 0, pos) + 1 : pos]
    return re.search(r"(?<!\\)%", line) is not None


def _read_arg(text: str, start: int) -> tuple[str, str, int]:
    """Returns the whitespace before, the text inside the braces, and the index after them."""
    gap_end = start
    while gap_end < len(text) and text[gap_end].isspace():
        gap_end += 1
    if gap_end >= len(text) or text[gap_end] != "{":
        raise LatexError(f"expected '{{' at offset {gap_end}")
    depth, i = 0, gap_end
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "%":
            i = text.find("\n", i)
            if i < 0:
                break
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:gap_end], text[gap_end + 1 : i], i + 1
        i += 1
    raise LatexError(f"unclosed '{{' at offset {gap_end}")


def parse(text: str) -> Resume:
    begin = text.find(BEGIN)
    end = text.rfind(END)
    if begin < 0 or end < begin:
        raise LatexError("document markers not found")
    body = text[begin:end]
    head: list[Node] = []
    sections: list[Section] = []
    pos = 0

    def add(node: Node) -> None:
        (sections[-1].nodes if sections else head).append(node)

    for match in _MACRO.finditer(body):
        if match.start() < pos or _in_comment(body, match.start()):
            continue
        name = match.group(1)
        gaps, args, cursor = [], [], match.end()
        for _ in range(_ARG_COUNT[name]):
            gap, arg, cursor = _read_arg(body, cursor)
            gaps.append(gap)
            args.append(arg)
        if match.start() > pos:
            add(Raw(body[pos : match.start()]))
        command = Command(name, args, gaps)
        if name == "section":
            sections.append(Section(command))
        else:
            add(command)
        pos = cursor
    if pos < len(body):
        add(Raw(body[pos:]))
    return Resume(text[:begin], head, sections, text[end:])


_UNESCAPE = {r"\&": "&", r"\%": "%", r"\$": "$", r"\#": "#", r"\_": "_", r"\{": "{", r"\}": "}"}
_ARROW = r"$\rightarrow$"


def to_plain(arg: str) -> str | None:
    """Plain text for a model, with **bold** markers. None when other LaTeX commands remain."""
    text = arg.replace(_ARROW, "→")
    text = re.sub(r"\\textbf\{([^{}]*)\}", r"**\1**", text)
    text = re.sub(r"\\(?:emph|textit)\{([^{}]*)\}", r"\1", text)
    for tex, plain in _UNESCAPE.items():
        text = text.replace(tex, plain)
    return None if "\\" in text else " ".join(text.split())


def from_plain(text: str) -> str:
    """Inverse of to_plain: escape the text, then turn **bold** and arrows back into LaTeX."""
    out = escape_latex(text).replace("→", _ARROW)
    return re.sub(r"\*\*([^*]+)\*\*", r"\\textbf{\1}", out)
