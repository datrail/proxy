# Contributing to the DatRail proxy

The proxy sits in front of an agent's outbound MCP calls and attaches the `x-rail` ticket that the rest of DatRail relies on, so the sandbox never holds the credential that identifies it.

## Before you write code

Open an issue first for anything beyond an obvious fix. Anything touching how a ticket is attached, or which agent a call is attributed to, should be discussed before it is written — that logic is what every downstream decision trusts.

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
