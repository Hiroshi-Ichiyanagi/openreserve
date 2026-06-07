# Security Policy

openreserve is early (v0.1.x) and unproven. We still take reports seriously and
appreciate them.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

Only the latest 0.1.x line receives fixes while the project is pre-1.0.

## Reporting a vulnerability

Please report security issues **privately**, not in public issues or pull requests.

- Use GitHub's private vulnerability reporting: go to the repository's **Security** tab
  and choose **Report a vulnerability** (this opens a private advisory visible only to
  the maintainers).

Please include enough detail to reproduce: affected version or commit, environment, and
a minimal example. A proof-of-concept is helpful but not required.

## Response

Responses are best-effort given the project's early stage. We will acknowledge a valid
report, work on a fix, and coordinate disclosure once a fix is available. There is no
bug-bounty program.

## Scope

openreserve is a library with no runtime dependencies and no network or serving layer.
Reports about the verification core (ledger, Merkle tree, audit chain, proof
generation, reserve calculation) are in scope. Issues in code you build on top of it
(servers, deployments, integrations) are out of scope here.
