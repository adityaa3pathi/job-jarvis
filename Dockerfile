# API image for Render: FastAPI + Tectonic (LaTeX to PDF). The Next.js app deploys to Vercel.
FROM python:3.12-slim

ARG TECTONIC_VERSION=0.17.0
ARG TECTONIC_SHA256=8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /tmp/tectonic.tgz "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
    && echo "${TECTONIC_SHA256}  /tmp/tectonic.tgz" | sha256sum -c - \
    && tar -xzf /tmp/tectonic.tgz -C /usr/local/bin tectonic && rm /tmp/tectonic.tgz

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
RUN useradd --create-home app
WORKDIR /app
COPY --chown=app pyproject.toml uv.lock ./
COPY --chown=app src ./src
COPY --chown=app config.yaml policy.yaml companies.yaml searches.yaml ./
RUN uv sync --frozen --no-dev --extra agent --extra turso && chown -R app /app

USER app
ENV PATH="/app/.venv/bin:$PATH" \
    JOBAGENT_DATA_DIR=/tmp/jobagent/data \
    JOBAGENT_CONFIG_DIR=/tmp/jobagent/config
# Download the LaTeX packages once at build time, so the first PDF is not slow.
# The first run fetches the whole bundle, so it gets a longer timeout.
COPY --chown=app tests/fixtures/resume/sample.tex /tmp/warm/resume.tex
RUN python -c "from pathlib import Path; from jobagent.web.app import compile_pdf; \
compile_pdf(Path('/tmp/warm/resume.tex').read_text(), timeout=1200)" && rm -rf /tmp/warm

EXPOSE 8000
CMD ["sh", "-c", "uvicorn --factory jobagent.web.app:app_from_env --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
