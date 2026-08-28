# The proxy, as the agent zone runs it.
#
# bridge.yaml is not in the image: .dockerignore keeps it out and only
# bridge.yaml.example ships, so a container without a mounted config stops at
# startup rather than serving an upstream list nobody chose. Mount one:
#   -v ./bridge.yaml:/app/fastmcp_proxy/bridge.yaml:ro
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Created before the source is copied: the account depends on nothing in it, and
# placed after, every edit to the package rebuilds this layer. No --system: it
# asks for a uid below SYS_UID_MAX, which the explicit uid then contradicts, and
# useradd warns about it on every build.
RUN useradd --no-create-home --uid 10001 railproxy

# The licence travels with the distribution. An image is a way of shipping this
# software, and Apache-2.0 asks that recipients get a copy.
COPY LICENSE NOTICE ./
COPY fastmcp_proxy/ ./fastmcp_proxy/

# python:3.12-slim defines no non-root user. Nothing after the install step
# needs root, and this process listens on a network for an agent it is placed
# in front of precisely because that agent is not trusted. The name is not
# `proxy`: Debian ships a system user of that name in the base image.
USER 10001

ENV RAIL_PROXY_BIND=0.0.0.0 \
    RAIL_PROXY_PORT=8091 \
    RAIL_PROXY_CONFIG_FILE=/app/fastmcp_proxy/bridge.yaml

# The default only. RAIL_PROXY_PORT moves the listener and this does not follow
# it, so a run that changes the port needs an explicit -p rather than -P.
EXPOSE 8091
CMD ["python", "-m", "fastmcp_proxy.proxy"]
