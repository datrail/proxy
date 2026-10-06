# Build from the repo root: docker build -f proxy-envoy-grpc/Dockerfile .
# What is sent to the build is limited by Dockerfile.dockerignore, next to this file.
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
COPY proxy-envoy-grpc/pyproject.toml proxy-envoy-grpc/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package proxy-envoy-grpc --no-install-workspace

# Then our code, installed (not editable) so the runtime stage needs only .venv.
COPY proxy-core proxy-core
COPY proxy-envoy-grpc proxy-envoy-grpc
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package proxy-envoy-grpc --no-editable

FROM python:3.12-slim

WORKDIR /app

# Apache-2.0 asks that recipients get a copy of the licence.
COPY LICENSE NOTICE ./
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Non-root, as the standalone image: uid 10001. Not `proxy`: Debian has that user.
# No --system: it asks for a uid below SYS_UID_MAX, which 10001 contradicts.
RUN useradd --no-create-home --uid 10001 railproxy
# The socket's directory: the server creates the socket here, and Envoy can
# reach it if it runs in group 10001. The server makes the socket 0660.
RUN install -d -o 10001 -g 10001 -m 0750 /run/rail
USER 10001

# Envoy calls the server on this socket (RAIL_PROXY_EXT_SOCKET moves it). Mount
# a volume here that Envoy's container shares.
VOLUME /run/rail

# Exec form, so python is PID 1 and receives SIGTERM.
CMD ["python", "-m", "proxy.envoy_grpc"]
