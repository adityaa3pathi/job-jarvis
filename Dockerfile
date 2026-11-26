# API image: FastAPI + Tectonic (LaTeX to PDF), for x86 (Render) and ARM (AWS t4g).
# The Next.js app deploys to Vercel.
FROM python:3.12-slim

ARG TARGETARCH
ARG TECTONIC_VERSION=0.17.0
ARG TECTONIC_SHA256_AMD64=8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7
ARG TECTONIC_SHA256_ARM64=b10954a95404f3ab2328d2fa59a5ebab8e657f893fab096f98be8db7c0c979b8
RUN set -eu; arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) triple=x86_64-unknown-linux-musl; sum="$TECTONIC_SHA256_AMD64" ;; \
      arm64) triple=aarch64-unknown-linux-musl; sum="$TECTONIC_SHA256_ARM64" ;; \
      *) echo "no Tectonic build for $arch" >&2; exit 1 ;; \
    esac; \
    apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /tmp/tectonic.tgz "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-${triple}.tar.gz" \
    && echo "${sum}  /tmp/tectonic.tgz" | sha256sum -c - \
    && tar -xzf /tmp/tectonic.tgz -C /usr/local/bin tectonic && rm /tmp/tectonic.tgz

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
RUN useradd --create-home app
WORKDIR /app
COPY --chown=app pyproject.toml uv.lock ./
COPY --chown=app src ./src
COPY --chown=app config.yaml policy.yaml companies.yaml searches.yaml ./
# libsql (the Turso driver) ships x86 wheels only, so on Arm it compiles from Rust source:
# that needs a C toolchain, installed only on Arm and removed again in the same layer.
RUN set -eu; arm=0; [ "$(dpkg --print-architecture)" = arm64 ] && arm=1; \
    if [ "$arm" = 1 ]; then apt-get update \
      && apt-get install -y --no-install-recommends build-essential pkg-config libssl-dev; fi; \
    uv sync --frozen --no-dev --extra agent --extra turso --extra embed --extra bedrock \
    && chown -R app /app; \
    if [ "$arm" = 1 ]; then apt-get purge -y --auto-remove build-essential pkg-config libssl-dev \
      && rm -rf /var/lib/apt/lists/* /root/.cache /root/.cargo /root/.rustup; fi

USER app
ENV PATH="/app/.venv/bin:$PATH" \
    JOBAGENT_DATA_DIR=/tmp/jobagent/data \
    JOBAGENT_CONFIG_DIR=/tmp/jobagent/config \
    FASTEMBED_CACHE_PATH=/app/.fastembed
# Fetch the ranking model at build time; Render's disk is wiped on every wake.
RUN python -c "from jobagent.index import FastEmbedEmbedder; FastEmbedEmbedder().embed(['warm'])"
# Download the LaTeX packages once at build time, so the first PDF is not slow.
# The first run fetches the whole bundle, so it gets a longer timeout.
COPY --chown=app tests/fixtures/resume/sample.tex /tmp/warm/resume.tex
RUN python -c "from pathlib import Path; from jobagent.web.app import compile_pdf; \
compile_pdf(Path('/tmp/warm/resume.tex').read_text(), timeout=1200)" && rm -rf /tmp/warm

EXPOSE 8000
# On Render set FORWARDED_ALLOW_IPS=* : Render only accepts traffic through its own proxy,
# whose address is not fixed. Login keys come from headers Vercel writes (see login_key).
CMD ["sh", "-c", "uvicorn --factory jobagent.web.app:app_from_env --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\" --timeout-graceful-shutdown 20"]
