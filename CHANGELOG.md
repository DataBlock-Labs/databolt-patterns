# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Core framework `databolt.patterns`:
  - `Pattern` and `@pattern`, `RunContext`, a registry fed by the `databolt.patterns` entry
    points, manifest v1 (JSON Schema 2020-12 plus `x-databolt-ui`, secret-ref params) and
    runner protocol v1 (`databolt-patterns protocol`);
  - the runtime, with state *proposals*, preview, and partial-output cleanup;
  - `${params|secrets|state.*}` templating, record selectors, and secret redaction;
  - NDJSON, Parquet and DuckDB sinks.
- Egress guard: the Studio CIDR blocklist (unwrapping IPv4-mapped, IPv4-compatible and
  NAT64 addresses), blocked host suffixes, and checks before the request, at connect time
  (the connection is pinned to a vetted IP) and after connect (peer address). Redirects are
  followed manually and re-validated at every hop, and auth headers are stripped when a hop
  changes origin.
- Guarded HTTP client: retries 429 and 5xx with full-jitter backoff, honours `Retry-After`
  in both its seconds and HTTP-date forms, and supports a rate limit, a per-response byte
  cap, and run byte and time budgets.
- CLI: `version`, `list`, `describe`, `manifest`, `validate`, `run` and `protocol`.
- Project skeleton: PEP 420 `databolt` namespace, `databolt-patterns` CLI (`version`),
  CI (ruff, mypy --strict, pytest on 3.10–3.13 plus macOS and Windows, wheel namespace check),
  CodeQL, Dependabot and a trusted-publishing release workflow.
