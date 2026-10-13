# Contributing

Thanks for helping. Bug reports, new standard patterns and docs fixes are all welcome.

## Development setup

```bash
git clone https://github.com/DataBlock-Labs/databolt-standard-patterns
cd databolt-standard-patterns
python -m venv .venv && . .venv/bin/activate      # or: uv venv && . .venv/bin/activate
pip install -e ".[dev]"
pip install -e examples/entrypoint_pkg              # used by the entry-point tests
```

## The checks CI runs

```bash
ruff check . && ruff format --check .
mypy                  # strict, configured in pyproject.toml
pytest                # offline; uses pytest-httpserver
pytest -m live        # opt-in: hits PokeAPI and USGS
python -m build && python scripts/check_wheel.py dist
```

All of them must pass before a PR merges. Snapshot tests (the manifest) are regenerated
with `UPDATE_SNAPSHOTS=1 pytest`; review the diff, because the manifest is a contract
that DataBolt Studio reads.

## Rules that are easy to break

- **Never add `src/databolt/__init__.py` or `src/databolt/ingestion/__init__.py`.**
  `databolt` is a PEP 420 namespace shared with other dists. The build check fails if one appears.
- **The runner protocol and manifest are contracts.** A change that alters their shape needs a
  `PROTOCOL_VERSION` or `MANIFEST_VERSION` bump and a CHANGELOG entry. See `docs/protocol.md`.
- **Secrets never appear in errors, logs, state or manifests.** Route every message that can
  contain user data through the redactor.
- **All network access goes through the egress guard** (`databolt.patterns.net`). Do not call
  `httpx` or `urllib` directly from a pattern.

## Commit and PR style

Small, focused PRs. Describe the behaviour change and how you tested it. Add a line under
*Unreleased* in `CHANGELOG.md` for anything a user would notice.

By contributing you agree that your contributions are licensed under Apache-2.0.
