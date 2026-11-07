"""Starting text for each message. The owner rewrites it; Send stays locked until they do."""

from __future__ import annotations

import hashlib
from typing import Literal
from urllib.parse import quote_plus

from pydantic import BaseModel, Field

from jobagent.outreach.facts import Fact

Channel = Literal["email", "linkedin"]
LINKEDIN_NOTE_MAX = 300  # LinkedIn's limit for a connection note
EMAIL_STEPS = 3  # first email and two follow-ups: past three, replies drop
LINKEDIN_STEPS = 2  # connection note, then one message after they accept
FILL = "[write one line about you]"


class SenderProfile(BaseModel):
    """The owner's own details for every note; saved once on the Outreach page."""

    name: str = Field(default="", max_length=100)
    pitch: str = Field(default="", max_length=400)
    links: list[str] = Field(default_factory=list, max_length=4)


class Draft(BaseModel):
    subject: str = ""
    body: str


def text_hash(subject: str, body: str) -> str:
    return hashlib.sha256(f"{subject}\x00{body}".encode()).hexdigest()


def first_name(name: str) -> str:
    words = name.split()
    return words[0] if words else "there"


def _hook(title: str, facts: list[Fact]) -> str:
    if not facts:
        return f"I'm reaching out about the {title} role. [say why this company, in one line]"
    points = "; ".join(f.fact.rstrip(".") for f in facts)
    return f"I'm reaching out about the {title} role. What stood out to me: {points}."


def _sign(me: SenderProfile) -> str:
    return "\n".join(["Thanks,", me.name or "[your name]", *me.links])


def email_draft(
    step: int, *, name: str, title: str, company: str, facts: list[Fact], me: SenderProfile
) -> Draft:
    """Step 1 is the first email; 2 and 3 are follow-ups in the same thread."""
    hi = f"Hi {first_name(name)},"
    if step == 1:
        ask = (f"Would you be open to a short call, or could you point me to the right person "
               f"for the {title} role?")  # fmt: skip
        body = "\n\n".join([hi, _hook(title, facts), me.pitch or FILL, ask, _sign(me)])
        return Draft(subject=f"{title} role at {company}"[:78], body=body)
    if step == 2:
        nudge = (f"Bringing this back to the top of your inbox. I'd still love to talk about "
                 f"the {title} role, even for 15 minutes.")  # fmt: skip
        return Draft(body="\n\n".join([hi, nudge, _sign(me)]))
    last = (f"Last note from me on this. If the {title} role isn't the right fit, a pointer "
            f"to the right person at {company} would help a lot.")  # fmt: skip
    return Draft(body="\n\n".join([hi, last, _sign(me)]))


def linkedin_draft(
    step: int, *, name: str, title: str, company: str, facts: list[Fact], me: SenderProfile
) -> Draft:
    """Step 1 is the connection note (300 characters at most); step 2 the message after."""
    hi = f"Hi {first_name(name)},"
    if step == 1:
        parts = [hi, f"I'm applying for the {title} role at {company}."]
        if facts:
            parts.append(facts[0].fact.rstrip(".") + " caught my eye.")
        parts.append("Would love to connect.")
        note = " ".join(parts)
        if len(note) > LINKEDIN_NOTE_MAX:
            note = " ".join([parts[0], parts[1], parts[-1]])
        return Draft(body=note[:LINKEDIN_NOTE_MAX])
    body = "\n\n".join([
        f"{hi} thanks for connecting.", _hook(title, facts), me.pitch or FILL,
        "Would you be open to a short chat, or could you point me to the right person?",
    ])  # fmt: skip
    return Draft(body=body)


def draft_for(
    channel: Channel, step: int, *, name: str, title: str, company: str, facts: list[Fact],
    me: SenderProfile,
) -> Draft:  # fmt: skip
    make = email_draft if channel == "email" else linkedin_draft
    return make(step, name=name, title=title, company=company, facts=facts, me=me)


def linkedin_search_url(company: str, who: str = "engineering manager") -> str:
    """A people search the owner opens by hand; the app never loads LinkedIn itself."""
    words = quote_plus(f"{company} {who}")
    return f"https://www.linkedin.com/search/results/people/?keywords={words}"
