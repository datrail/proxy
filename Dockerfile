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

COPY fastmcp_proxy/ ./fastmcp_proxy/

ENV RAIL_PROXY_BIND=0.0.0.0 \
    RAIL_PROXY_PORT=8091 \
    RAIL_PROXY_CONFIG_FILE=/app/fastmcp_proxy/bridge.yaml

EXPOSE 8091
CMD ["python", "-m", "fastmcp_proxy.proxy"]
