# Contributing to openreserve

Thanks for your interest. openreserve is early and unproven, so contributions,
questions, and critiques are all welcome — especially on the API and the threat model.

## Development setup

Requires Python 3.11+.

```bash
git clone https://github.com/Hiroshi-Ichiyanagi/openreserve
cd openreserve
pip install -e ".[test]"   # editable install + pytest
python -m pytest -q        # run the suite
```

Run the determinism guard — the check behind the project's "verify me" claim:

```bash
python -m pytest tests/test_determinism_guard.py -v
```

Try the runnable examples (each uses only the public API):

```bash
python examples/proof_of_reserves.py
python examples/publish_and_verify_offline.py
python examples/tamper_detection.py
python examples/reproducibility.py
```

## Principles

- **Standard library only at runtime.** openreserve has zero runtime dependencies. New
  runtime dependencies are not added without discussion in an issue first.
- **Determinism is a contract.** Proof generation takes an explicit as-of / event time
  and must never read the wall clock. Changes that touch proof or audit generation must
  keep `tests/test_determinism_guard.py` passing.
- **Tests required.** Behavior changes need tests. Keep the suite green.
- **Honest documentation.** No marketing language. Keep docs accurate about what the
  project does and does not do, and keep the early / unproven status visible.

## Pull request flow

1. Fork and create a branch.
2. Make your change with tests; run `python -m pytest -q` until green.
3. Update docs if behavior or the public API changes.
4. Open a PR with a clear description of the change and its motivation.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the module layout and extension
points. For larger ideas, open an issue or a discussion first so we can scope it
together — see [ROADMAP.md](ROADMAP.md) for current directions.

## Questions and discussion

Use GitHub Issues for bugs and concrete proposals, and Discussions (if enabled) for
open-ended questions. Security reports go through a private channel — see
[SECURITY.md](SECURITY.md).

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md).
