"""Starting text for each message. The owner rewrites it; Send stays locked until they do."""

from __future__ import annotations

import hashlib
import re
from typing import Literal
from urllib.parse import quote_plus, unquote

from pydantic import BaseModel, Field

from jobagent.outreach.facts import Fact

Channel = Literal["email", "linkedin"]
LINKEDIN_NOTE_MAX = 300  # LinkedIn's limit for a connection note
EMAIL_STEPS = 3  # first email and two follow-ups: past three, replies drop
LINKEDIN_STEPS = 3  # connection note, a message once they accept, then one follow-up
FILL = "[write one line about you]"
FILL_PROOF = "[link to something you built + how you measured it]"
FILL_WHY = "[say why this company, in one line]"
URL = re.compile(r"https?://\S+|\b[\w-]+\.(?:in|com|dev|io|ai|app|co|org)\b", re.I)
PROOF_WITHIN = 400  # characters: the proof link must sit in the first lines, not the signature


class SenderProfile(BaseModel):
    """The owner's own details for every note; saved once on the Outreach page."""

    name: str = Field(default="", max_length=100)
    # One line, link first: what you built and how you measured it (the 2026 signal hirers say
    # they never get). Example: "docnow.in: live booking platform; its AI agent scores 92-96%."
    proof: str = Field(default="", max_length=300)
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
        return f"I'm reaching out about the {title} role. {FILL_WHY}"
    points = "; ".join(f.fact.rstrip(".") for f in facts)
    return f"I'm reaching out about the {title} role. What stood out to me: {points}."


def has_early_proof(body: str) -> bool:
    """A link to real work near the top; links in the signature do not count."""
    return bool(URL.search(body[:PROOF_WITHIN]))


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
        extra = [me.pitch] if me.pitch else []
        proof = me.proof or FILL_PROOF
        body = "\n\n".join([hi, proof, _hook(title, facts), *extra, ask, _sign(me)])
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
        head = [hi, f"I'm applying for the {title} role at {company}.", me.proof or FILL_PROOF]
        fact = [facts[0].fact.rstrip(".") + " caught my eye."] if facts else []
        note = " ".join([*head, *fact, "Would love to connect."])
        if len(note) > LINKEDIN_NOTE_MAX:  # the proof outranks the fact
            note = " ".join([*head, "Would love to connect."])
        return Draft(body=note[:LINKEDIN_NOTE_MAX])
    if step == 3:
        nudge = (f"{hi} following up on my note about the {title} role. Happy to share more, "
                 f"or to be pointed to the right person at {company}.")  # fmt: skip
        return Draft(body=nudge)
    body = "\n\n".join([
        f"{hi} thanks for connecting.", me.proof or FILL_PROOF, _hook(title, facts),
        "Would you be open to a short chat, or could you point me to the right person?",
    ])  # fmt: skip
    return Draft(body=body)


def linkedin_slug(url: str) -> str:
    """The /in/<slug> part of a profile link, so differently written links still match."""
    found = re.search(r"linkedin\.com/in/([^/?#]+)", url or "", re.I)
    return unquote(found.group(1)).strip().lower() if found else ""


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
