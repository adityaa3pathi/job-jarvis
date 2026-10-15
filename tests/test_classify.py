"""Role, work-mode and city guesses from a job's title and location (R4, R6, R7)."""

from __future__ import annotations

import pytest

from jobagent.classify import city_of, role_of, work_mode_of


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("Senior Full Stack Engineer", "full_stack"),
        ("Full Stack AI Engineer", "ai_ml"),
        ("React Native Developer", "mobile"),
        ("Backend Engineer (Go)", "backend"),
        ("Frontend Developer", "frontend"),
        ("Account Executive", "other"),
        ("Machine Learning Engineer", "ai_ml"),
        ("GenAI / LLM Engineer", "ai_ml"),
        ("Android Developer", "mobile"),
        ("Front-End Engineer, React", "frontend"),
        ("React.js Developer", "frontend"),
        ("Back End Developer - Node.js", "backend"),
        ("Fullstack Developer (MERN)", "full_stack"),
        ("Chennai Sales Associate", "other"),
        ("Software Engineer", "software"),
        ("SDE II", "software"),
        ("Software Development Engineer", "software"),
        ("Product Engineer", "software"),
        ("Software Engineer, Backend", "backend"),
        ("Software Sales Manager", "other"),
        ("React JS Developer", "frontend"),
        ("Node JS Developer", "backend"),
        ("Agentic Systems Engineer", "ai_ml"),
    ],
)
def test_role_of(title: str, role: str) -> None:
    assert role_of(title) == role


@pytest.mark.parametrize(
    ("location", "title", "mode"),
    [
        ("Bangalore / Remote", "AI Engineer", "remote"),
        ("Hybrid - Pune", "AI Engineer", "hybrid"),
        ("Pune", "AI Engineer", "onsite"),
        ("", "AI Engineer", "unknown"),
        ("", "Remote Backend Engineer", "remote"),
        ("Gurugram", "Full Stack Developer (Hybrid)", "hybrid"),
        ("India", "AI Engineer", "unknown"),
        ("Anywhere", "AI Engineer", "remote"),
    ],
)
def test_work_mode_of(location: str, title: str, mode: str) -> None:
    assert work_mode_of(location, title) == mode


def test_city_of_normalises_names_and_takes_the_first_city() -> None:
    assert city_of("Bengaluru, Karnataka, India") == "bangalore"
    assert city_of("Bangalore Urban / Remote") == "bangalore"
    assert city_of("Remote") == ""
    assert city_of("India") == ""
    assert city_of("Gurugram, Haryana") == "gurgaon"
    assert city_of("Remote; Mumbai or Pune") == "mumbai"
    assert city_of("New Delhi") == "delhi"
