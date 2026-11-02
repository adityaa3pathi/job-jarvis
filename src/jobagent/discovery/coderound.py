"""CodeRound AI public featured jobs API on candidate.coderound.tech."""

from __future__ import annotations

from typing import Any

from jobagent.discovery.base import Adapter, BoardSearch, build_each, parse_json, remote_at
from jobagent.models import Posting
from jobagent.sanitize import strip_hidden_html


class CoderoundAdapter(Adapter[BoardSearch]):
    portal = "coderound"
    conditional = True

    def url_for(self, search: BoardSearch) -> str:
        return "https://candidate.coderound.tech/api/jobs/featured"

    def parse(self, text: str, search: BoardSearch) -> list[Posting]:
        term = search.term.lower().strip()

        def build(job: dict[str, Any]) -> Posting | None:
            title = (job.get("title") or "").strip()
            if not title:
                return None

            tags = [t.strip() for t in (job.get("tags") or []) if t and isinstance(t, str)]
            core_tech = (job.get("core_tech_type") or "").strip()
            role_type = (job.get("role_type") or "").strip()

            if term and term != "all":
                searchable = f"{title} {' '.join(tags)} {core_tech} {role_type}".lower()
                if term not in searchable and not any(w in searchable for w in term.split()):
                    return None

            job_id = job.get("job_id") or job.get("id")
            if not job_id:
                return None
            url = f"https://candidate.coderound.tech/jobs/{job_id}"

            client = job.get("client_company")
            company = ""
            if isinstance(client, dict):
                company = (client.get("name") or "").strip()
            elif isinstance(client, str):
                company = client.strip()

            loc = (job.get("location") or "").strip()
            work_mode = (job.get("work_mode") or "").strip()
            if work_mode.lower() == "remote":
                location = remote_at(loc or "Remote")
            elif loc and work_mode:
                location = f"{loc} ({work_mode})"
            else:
                location = loc or work_mode

            min_y = job.get("years_of_experience")
            max_y = job.get("max_years_of_experience")
            if min_y is not None and max_y is not None:
                exp_text = f"{min_y}-{max_y} years"
            elif min_y is not None:
                exp_text = f"{min_y}+ years"
            else:
                exp_text = ""

            parts: list[str] = []
            if desc := (job.get("description") or "").strip():
                parts.append(desc)
            if must := (job.get("must_have_requirements") or "").strip():
                parts.append(f"Requirements:\n{must}")
            if good := (job.get("good_to_have_requirements") or "").strip():
                parts.append(f"Good to have:\n{good}")
            if (resps := job.get("responsibilities")) and isinstance(resps, list):
                bullet_lines = "\n".join(f"- {r}" for r in resps if r)
                if bullet_lines:
                    parts.append(f"Responsibilities:\n{bullet_lines}")

            description = strip_hidden_html("\n\n".join(parts))

            return Posting(
                source=self.portal,
                url=url,
                company=company,
                title=title,
                location=location,
                experience_text=exp_text,
                stack_words=tags,
                description=description,
            )

        data = parse_json(text)
        if not isinstance(data, list):
            raise ValueError("expected a list of jobs from CodeRound API")
        return build_each(data, build)
