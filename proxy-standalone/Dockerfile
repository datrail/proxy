# Build from the repo root: docker build -f proxy-standalone/Dockerfile .
# What is sent to the build is limited by Dockerfile.dockerignore, next to this file.
# bridge.yaml is not in the image; mount one: -v ./bridge.yaml:/app/standalone/bridge.yaml:ro
FROM python:3.12-slim AS builder
# Pinned by digest: uv is part of the build, like the lockfile it reads.
COPY --from=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /bin/uv
# Precompile .pyc (the runtime user can't write them); copy rather than hardlink
# from the cache mount; use the base image's Python, which the runtime stage has too.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# Third-party dependencies first, cached until uv.lock or a pyproject.toml changes.
COPY pyproject.toml uv.lock ./
COPY proxy-core/pyproject.toml proxy-core/
COPY proxy-standalone/pyproject.toml proxy-standalone/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package proxy-standalone --no-install-workspace

# Then our code, installed (not editable) so the runtime stage needs only .venv.
COPY proxy-core proxy-core
COPY proxy-standalone proxy-standalone
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package proxy-standalone --no-editable

FROM python:3.12-slim

WORKDIR /app

# Apache-2.0 asks that recipients get a copy of the licence.
COPY LICENSE NOTICE ./
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Non-root: the proxy faces an untrusted agent. Not `proxy`: Debian has that user.
# No --system: it asks for a uid below SYS_UID_MAX, which 10001 contradicts.
RUN useradd --no-create-home --uid 10001 railproxy
USER 10001

ENV RAIL_PROXY_BIND=0.0.0.0 \
    RAIL_PROXY_PORT=8091 \
    RAIL_PROXY_CONFIG_FILE=/app/standalone/bridge.yaml

# The default port; RAIL_PROXY_PORT moves the listener but not this.
EXPOSE 8091

# Exec form, so python is PID 1 and receives SIGTERM. CMD rather than ENTRYPOINT,
# so a replacement command (e2e's `sleep infinity`) runs as given.
CMD ["python", "-m", "proxy.standalone.server"]
