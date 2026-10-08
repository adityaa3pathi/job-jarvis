"""Job description analyzer: an extractor and a critic in a small LangGraph loop."""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from jobagent.llm import LLM, LLMError, ask, data_block
from jobagent.resume.jd import (
    Critique,
    JDProfile,
    Pass,
    SkillNeed,
    implicit_from,
    level_for,
    verified_years,
    vote,
    weight,
    work_mode,
)
from jobagent.resume.ontology import all_terms, canonical, mention
from jobagent.sanitize import clean_posting_text

PASSES = 3
MAX_ROUNDS = 2
MAX_RESPONSIBILITIES = 8

EXTRACT = (
    "You read one job posting. List what it asks for. Per skill give importance: 'must' only "
    "when the posting calls it required or core to the role, else 'nice'. Copy skills as the "
    "posting spells them. implicit_skills are things the role clearly needs but the posting "
    'never names. Reply as {"role_title": str, "level": str, "years_min": int|null, '
    '"domain": str, "responsibilities": [str], "skills": [{"name": str, "importance": str}], '
    '"implicit_skills": [str]}.'
)
CRITIC = (
    "You check a skill list against the posting. missing: skills the posting names that the "
    "list lacks. unsupported: listed skills the posting does not support. "
    'Reply as {"missing": [str], "unsupported": [str]}.'
)


class State(TypedDict, total=False):
    text: str
    title: str
    hints: list[str]
    rounds: int
    passes: list[Pass]
    skills: list[SkillNeed]
    misses: list[str]


def build_graph(llm: LLM) -> Any:
    def extract(state: State) -> State:
        hint = f"\nAlso check for: {', '.join(state['hints'])}." if state.get("hints") else ""
        user = data_block("job_posting", state["text"]) + "\nList the requirements." + hint
        passes = []
        for _ in range(PASSES):
            try:
                passes.append(ask(llm, Pass, system=EXTRACT, user=user))
            except LLMError:
                continue
        title = next((p.role_title for p in passes if p.role_title), state["title"])
        skills = vote(passes, state["text"], title) if passes else []
        return {"passes": passes, "title": title, "skills": skills, "rounds": state["rounds"] + 1}

    def critic(state: State) -> State:
        if not state["passes"]:
            return {"misses": []}
        listed = ", ".join(s.name for s in state["skills"]) or "none"
        user = data_block("job_posting", state["text"]) + f"\nSkill list: {listed}"
        try:
            found = ask(llm, Critique, system=CRITIC, user=user)
        except LLMError:
            return {"misses": []}
        have = {s.name for s in state["skills"]}
        real = {canonical(m) for m in found.missing if mention(m, state["text"])}
        return {"misses": sorted(real - have)}

    def again(state: State) -> str:
        return "extract" if state["misses"] and state["rounds"] < MAX_ROUNDS else "end"

    def hint(state: State) -> State:
        return {"hints": state["misses"]}

    graph = StateGraph(State)
    graph.add_node("extract", extract)
    graph.add_node("critic", critic)
    graph.add_node("hint", hint)
    graph.add_edge(START, "extract")
    graph.add_edge("extract", "critic")
    graph.add_conditional_edges("critic", again, {"extract": "hint", "end": END})
    graph.add_edge("hint", "extract")
    return graph.compile()


def analyze_jd(posting: str, llm: LLM) -> JDProfile:
    text, flagged = clean_posting_text(posting)
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    out = build_graph(llm).invoke(
        {"text": text, "title": first_line, "hints": [], "rounds": 0}, {"recursion_limit": 12}
    )
    passes: list[Pass] = out["passes"]
    if not passes:
        return _fallback(text, first_line, flagged)
    skills: list[SkillNeed] = list(out["skills"])
    for name in out["misses"]:
        verbatim = mention(name, text) or name
        skills.append(SkillNeed(name, verbatim, "nice", weight(name, "nice", text, out["title"])))
    skills.sort(key=lambda n: (-n.weight, n.name))
    lead = passes[0]
    return JDProfile(
        role_title=out["title"].strip()[:120],
        level=level_for(lead.level, out["title"]),
        years_min=verified_years(lead.years_min, text),
        work_mode=work_mode(text),
        domain=lead.domain.strip()[:80],
        responsibilities=[r.strip()[:200] for r in lead.responsibilities if r.strip()][
            :MAX_RESPONSIBILITIES
        ],
        skills=skills,
        implicit_skills=implicit_from(passes, text),
        injection_flagged=flagged,
        rounds=out["rounds"],
    )


def _fallback(text: str, title: str, flagged: bool) -> JDProfile:
    """Model unreachable: scan for known terms. Everything is 'nice' so nothing is auto-added."""
    found = [(t, mention(t, text)) for t in all_terms()]
    skills = [SkillNeed(t, v, "nice", weight(t, "nice", text, title)) for t, v in found if v]
    return JDProfile(
        role_title=title[:120],
        level=level_for("", title),
        years_min=verified_years(None, text),
        work_mode=work_mode(text),
        skills=skills,
        degraded=True,
        injection_flagged=flagged,
    )
