# Contributing to the DatRail proxy

The proxy sits in the path of an agent's outbound calls to its MCP servers, and keeps the credential identifying that agent out of the sandbox.
- [README.md](README.md) says what it does;
- [SECURITY.md](SECURITY.md) says where the sharp edges are.

## Before you write code

Open an issue first for anything beyond an obvious fix. Anything touching which agent a call is attributed to should be discussed before it is written — that logic is what every downstream decision trusts.

## Running it

- From source: [proxy-standalone](proxy-standalone/README.md#from-source) or
  [proxy-envoy-grpc](proxy-envoy-grpc/README.md#run-it-beside-envoy). Each
  README also covers its configuration.
- The suite, lint and e2e: [Development](README.md#development) in the README.
  CI runs the same `make` targets.

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
