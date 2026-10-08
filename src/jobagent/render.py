"""Resume structures and their Markdown, PDF and gaps-note renderings (KTD8)."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from jobagent.facts import FactSheet, Track

_MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class ResumeBullet:
    text: str
    fact_ids: list[str]


@dataclass
class ResumeEntry:
    kind: str  # "role" or "project"
    ref_id: str
    heading: str  # a title or project name picked from the fact sheet
    bullets: list[ResumeBullet] = field(default_factory=list)


@dataclass
class SkillLineOut:
    label: str
    skill_ids: list[str]
    names: list[str]


@dataclass
class TailoredResume:
    track: Track
    summary: ResumeBullet
    roles: list[ResumeEntry]
    projects: list[ResumeEntry]
    skill_lines: list[SkillLineOut]
    education_ids: list[str]

    def all_fact_ids(self) -> list[str]:
        ids = [*self.summary.fact_ids, *self.education_ids]
        for entry in [*self.roles, *self.projects]:
            ids.append(entry.ref_id)
            for b in entry.bullets:
                ids += b.fact_ids
        for line in self.skill_lines:
            ids += line.skill_ids
        return list(dict.fromkeys(ids))

    def labels(self) -> list[str]:
        return [ln.label for ln in self.skill_lines]


def _month(value: str) -> str:
    if value == "present":
        return "Present"
    year, month = value.split("-")
    return f"{_MONTH[int(month) - 1]} {year}"


def header_values(sheet: FactSheet) -> list[str]:
    """Contact values taken from confirmed answers only."""
    values = []
    for key in ("full_name", "phone", "email", "github", "website"):
        ans = sheet.confirmed_answer(key)
        if ans:
            values.append(f"github.com/{ans.value}" if key == "github" else ans.value)
    return values


def header_markdown(sheet: FactSheet) -> str:
    values = header_values(sheet)
    if not values:
        return ""
    name = sheet.confirmed_answer("full_name")
    contact = [v for v in values if not name or v != name.value]
    return f"# {name.value if name else ''}\n{' | '.join(contact)}\n\n"


def body_markdown(resume: TailoredResume, sheet: FactSheet) -> str:
    lines = ["## Summary", resume.summary.text, "", "## Experience"]
    for entry in resume.roles:
        role = sheet.roles[entry.ref_id]
        dates = f"{_month(role.start)} - {_month(role.end)}"
        place = f" ({role.descriptor})" if role.descriptor else ""
        lines.append(f"**{entry.heading}** | {role.employer}{place}, {role.location} | {dates}")
        lines += [f"- {b.text}" for b in entry.bullets] + [""]
    if resume.projects:
        lines.append("## Projects")
    for entry in resume.projects:
        proj = sheet.projects[entry.ref_id]
        tech = ", ".join(
            sheet.skills[s].canonical for s in proj.tech[:6] if sheet.skills[s].confirmed
        )
        url = f" | {proj.url}" if proj.url else ""
        lines.append(f"**{entry.heading}** | {tech}{url}")
        lines += [f"- {b.text}" for b in entry.bullets] + [""]
    lines.append("## Skills")
    lines += [f"- **{ln.label}:** {', '.join(ln.names)}" for ln in resume.skill_lines]
    lines += ["", "## Education"]
    for eid in resume.education_ids:
        edu = sheet.education[eid]
        lines.append(f"{edu.degree} | {edu.institution} | {edu.start_year} - {edu.end_year}")
    return "\n".join(lines) + "\n"


def to_markdown(resume: TailoredResume, sheet: FactSheet) -> str:
    return header_markdown(sheet) + body_markdown(resume, sheet)


def strip_contact(text: str, sheet: FactSheet) -> str:
    """Remove the contact values so claim checks look only at the body."""
    for value in sorted(header_values(sheet), key=len, reverse=True):
        text = text.replace(value, " ")
    return text


@dataclass
class Gap:
    name: str
    reason: str


def gaps_markdown(title: str, company: str, gaps: list[Gap]) -> str:
    lines = [f"# Gaps to learn: {title} at {company}", ""]
    if not gaps:
        lines.append("No gaps found.")
    lines += [f"- {g.name}: {g.reason}" for g in gaps]
    return "\n".join(lines) + "\n"


def render_pdf(markdown: str) -> bytes:
    """Local PDF render for forms that need a file. Needs the optional `pdf` extra."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    for raw in markdown.splitlines():
        line = re.sub(r"\*\*|^#+\s*", "", raw).encode("latin-1", "replace").decode("latin-1")
        pdf.multi_cell(0, 5, line or " ", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)
