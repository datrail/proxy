# Contributing to the DatRail proxy

The proxy sits in front of an agent's outbound MCP calls, and keeps the credential identifying that agent out of the sandbox. [README.md](README.md) says what it does; [SECURITY.md](SECURITY.md) says where the sharp edges are, and reading that one first will save you a rejected pull request.

## Before you write code

Open an issue first for anything beyond an obvious fix. Anything touching which agent a call is attributed to should be discussed before it is written — that logic is what every downstream decision trusts.

## Running it

```
pip install -r requirements.txt -r requirements-test.txt -r requirements-dev.txt
cp fastmcp_proxy/bridge.yaml.example fastmcp_proxy/bridge.yaml   # then edit it
python -m fastmcp_proxy.proxy
```

`make test` runs the suite and `make lint` the linter; CI runs both. The proxy
listens on `0.0.0.0:8091` by default, serving `POST /mcp` and `GET /health`.
Every setting is an environment variable, listed in
[bridge.yaml.example](fastmcp_proxy/bridge.yaml.example).

## The rule that is not negotiable

**A request must never carry an identity that is not its own.** If a change
makes attribution depend on something the caller controls, or introduces a path
where a ticket is reused across agents, it will be declined however good the
rest of it is.

## Sending a change

- One coherent change per pull request, with a message that says *why* — the
  diff already says what.
- Branch from `master`.
- **Sign off your commits** (`git commit -s`). We use the
  [Developer Certificate of Origin](https://developercertificate.org/); the
  sign-off is your statement that you wrote the change or have the right to
  contribute it. No CLA.

## Reporting a vulnerability

Not here — see [SECURITY.md](SECURITY.md), and please do not open a public
issue.
