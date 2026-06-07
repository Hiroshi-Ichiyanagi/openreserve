# Roadmap

These are **directions, not promises**. openreserve is early and exploratory; there are
no dates or committed milestones, and APIs may change. What gets worked on next will be
driven by feedback and real use — if something here matters to you, open an issue or a
discussion.

## Directions under consideration

- **Publish to PyPI.** Make `pip install openreserve` work without cloning. *Why:* lower
  the barrier to trying it.

- **Deterministic (content-addressed) identifiers.** Today `account_id` /
  `transaction_id` are UUID-based, so two independently rebuilt ledgers do not produce
  the same hashes — determinism is on the time axis only. Content-addressed identifiers
  would let artifacts be byte-identical across rebuilds. *Why:* removes the main caveat
  in the determinism story.

- **Reference HTTP serving layer (optional, separate).** A small, optional way to publish
  proofs over HTTP, kept out of the core. *Why:* makes the "fetch with curl and verify"
  flow turnkey without putting a server in the core.

- **On-chain anchoring adapter (optional).** An optional way to anchor an audit-chain
  commitment hash on-chain, with the core staying off-chain. *Why:* some users want an
  external timestamp/anchor; it should be additive, not a dependency. This is
  complementary to on-chain proof-of-reserves systems, not a replacement for them.

- **Additional reserve / deposit models.** Room for more point-in-time reserve rules
  alongside the current calculation. *Why:* different jurisdictions and products compute
  required reserves differently. (openreserve is not a compliance product and makes no
  claim to satisfy any specific regulation.)

- **Signed releases and build provenance.** Sign release artifacts and publish
  provenance. *Why:* consistent with the project's "verify me" stance — let users verify
  what they install.

- **Documentation site.** Render the docs as a browsable site. *Why:* easier navigation
  than reading Markdown in the repo.

- **Lint and type-checking in CI (ruff, mypy).** Add style and type gates. *Why:* keep
  contributions consistent as the project grows.

- **Performance and scale.** Characterize behavior on larger ledgers and consider
  Merkle/aggregation optimizations. *Why:* understand limits before recommending it for
  bigger datasets.

## Out of scope (for now)

- Becoming a compliance/attestation product or offering legal or financial advice.
- Replacing on-chain proof-of-reserves oracles (a different domain).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for where these would slot in, and
[CONTRIBUTING.md](CONTRIBUTING.md) to get involved.
