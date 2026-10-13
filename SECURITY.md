# Security policy

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's private vulnerability reporting
(*Security → Report a vulnerability* on this repository). We aim to acknowledge reports
within 3 business days and to ship a fix or mitigation within 30 days.

## Scope

Of particular interest:

- bypasses of the egress guard (reaching loopback, private, link-local, CGNAT or cloud-metadata
  addresses with `allow_private` off), including via redirects or DNS rebinding;
- secret leakage into errors, logs, run results, state files or manifests;
- auth headers or credentials sent to an origin other than the configured API;
- the runner protocol writing anything other than one JSON result to stdout.

Custom Python patterns run with the privileges of the calling process. They are **not**
sandboxed beyond the egress-guarded `ctx.http` client, so running untrusted pattern code
is out of scope. See `docs/security.md`.

## Supported versions

Only the latest minor release receives security fixes while the package is 0.x.

## Supply chain

The release workflow publishes to PyPI through OIDC trusted publishing, so its publish jobs
hold `id-token: write`. To keep that path tamper-resistant:

- **Every `uses:` is pinned to a full 40-character commit SHA**, with the human-readable
  version in a trailing comment (`# v7.0.1`). A tag can be moved; a SHA cannot.
  `scripts/check_action_pins.py` runs in CI and fails on any unpinned action.
- **Dependabot** (`github-actions` ecosystem) proposes SHA bumps weekly, so the pins do not rot.
- **Least privilege**: every workflow defaults to `permissions: contents: read`. Only the
  `testpypi` and `pypi` jobs get `id-token: write`; only the GitHub-release job gets
  `contents: write`. Checkouts use `persist-credentials: false`.
- **Gated publishing**: publish jobs run only in the `testpypi` / `pypi` GitHub environments
  (the `pypi` environment requires a maintainer's approval) and only when the repository
  variable `PYPI_PUBLISH` is `true`. Every job has a `timeout-minutes`.
