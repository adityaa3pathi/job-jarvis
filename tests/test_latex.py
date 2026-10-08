from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.resume.latex import LatexError, Raw, escape_latex, parse

SAMPLE = (Path(__file__).parent / "fixtures" / "resume" / "sample.tex").read_text()


def test_round_trip_is_exact() -> None:
    assert parse(SAMPLE).emit() == SAMPLE


def test_sections_are_found_in_order() -> None:
    titles = [s.title for s in parse(SAMPLE).sections]
    assert titles == ["Experience", "Projects", "Technical Skills", "Education"]


def test_items_and_headings_are_read() -> None:
    resume = parse(SAMPLE)
    experience = resume.section("Experience")
    items = experience.commands("resumeItem")
    heads = experience.commands("resumeSubheading")
    assert len(items) == 6
    assert len(heads) == 2
    assert heads[0].args[0] == r"Software Engineer --- AI \& Backend"
    assert "Next.js App Router" in items[-1].args[0]


def test_nested_braces_stay_in_one_argument() -> None:
    item = parse(SAMPLE).section("Experience").commands("resumeItem")[0]
    assert item.args[0].count("{") == item.args[0].count("}")
    assert r"\textbf{production agentic system using LangGraph}" in item.args[0]


def test_editing_one_item_changes_only_that_item() -> None:
    resume = parse(SAMPLE)
    target = resume.section("Experience").commands("resumeItem")[-1]
    target.args[0] = "Rewritten line."
    out = resume.emit()
    assert out == SAMPLE.replace(
        SAMPLE[SAMPLE.index(r"\resumeItem{Migrated the app") :].split("\n")[0],
        r"\resumeItem{Rewritten line.}",
    )


def test_preamble_and_postamble_are_untouched() -> None:
    resume = parse(SAMPLE)
    assert resume.preamble == SAMPLE[: SAMPLE.index(r"\begin{document}")]
    assert resume.postamble.strip() == r"\end{document}"


def test_macro_inside_a_comment_is_not_parsed() -> None:
    text = "\\begin{document}\n% \\resumeItem{ghost}\n\\resumeItem{real}\n\\end{document}\n"
    resume = parse(text)
    assert [n.args[0] for n in resume.nodes_of("resumeItem")] == ["real"]
    assert resume.emit() == text


def test_escaped_braces_and_percent_inside_an_argument() -> None:
    text = "\\begin{document}\n\\resumeItem{cut \\} cost by 40\\% {x}}\n\\end{document}\n"
    resume = parse(text)
    assert resume.nodes_of("resumeItem")[0].args[0] == "cut \\} cost by 40\\% {x}"
    assert resume.emit() == text


def test_whitespace_between_arguments_survives() -> None:
    text = "\\begin{document}\n\\resumeSubheading\n  {A}{B}\n  {C}{D}\n\\end{document}\n"
    assert parse(text).emit() == text


def test_unbalanced_argument_raises() -> None:
    with pytest.raises(LatexError):
        parse("\\begin{document}\n\\resumeItem{never closed\n\\end{document}\n")


def test_missing_document_markers_raise() -> None:
    with pytest.raises(LatexError):
        parse("no document here")


def test_raw_text_of_skills_section_can_be_replaced() -> None:
    resume = parse(SAMPLE)
    skills = resume.section("Technical Skills")
    skills.nodes[:] = [Raw("\n\\small{Python}\n")]
    out = resume.emit()
    assert "\\small{Python}" in out
    assert "\\section{Education}" in out


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("R&D", r"R\&D"),
        ("40%", r"40\%"),
        ("a_b", r"a\_b"),
        ("$5 #1", r"\$5 \#1"),
        ("{x}", r"\{x\}"),
        ("~^", r"\textasciitilde{}\textasciicircum{}"),
        ("a\\b", r"a\textbackslash{}b"),
    ],
)
def test_escape_latex(raw: str, escaped: str) -> None:
    assert escape_latex(raw) == escaped
