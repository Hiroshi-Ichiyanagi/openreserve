## Summary

What does this change and why?

## Checklist

- [ ] `python -m pytest -q` passes locally
- [ ] Tests added/updated for behavior changes
- [ ] Docs updated if behavior or the public API changed
- [ ] No new runtime dependencies (standard library only), or discussed in an issue
- [ ] Determinism preserved — proof/audit generation does not read the wall clock
      (`tests/test_determinism_guard.py` still passes)
- [ ] Documentation stays accurate and free of marketing language

## Notes

Anything reviewers should know (trade-offs, follow-ups, related issues).
