# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-13

First release.

### Added

- Custom patterns:
  - **YAML** (`kind: databolt.pattern/v1`). A pattern extends an installed pattern
    (`extends`, `extends_version`), exposes typed `params`, and supplies a `config` in which
    `${params.*}` renders at bind time and `${secrets.*}` and `${state.*}` render at run time.
    The config is validated against the base pattern at load time.
  - **Python files.** A file is executed only when its bytes match the trusted sha256, and
    the verified bytes are what run. An `entry` selects the pattern when a file defines more
    than one.
  - **Directories.** `--custom DIR` loads every YAML pattern in a directory; `.py` files load
    only with `--trust-python`.
  - **Packages.** Patterns are discovered through the `databolt.patterns` entry points;
    `examples/entrypoint_pkg` shows how, and CI installs it.
- Examples: `custom_pattern.py` (Hacker News) and `pokemon_custom.yaml`.
- Authoring docs for YAML, Python and packages.
- In `run`, the CLI sends anything a pattern prints to stderr, so stdout holds only the
  result.
- `databolt.ingestion.api`, the standard library:
  - `rest` (`databolt.ingestion.api.rest@1.0.0`). Its params are shaped like dlt's
    (`client` and `resource`) and it supports five auth types and eight paginators,
    including `auto` detection. It also covers incremental cursors (proposals,
    open/closed range, lookback, the truncated-descending rule), response actions,
    same-origin-only credentials and a rate limit.
  - `http_get` (`databolt.ingestion.api.http_get@1.0.0`) for a single JSON, NDJSON, CSV
    or TSV URL.
  - Functional (`api.rest(...)`), class (`api.RestApiPattern(cfg)`) and CLI forms.
- Examples: PokeAPI, USGS earthquakes (incremental) and GitHub repos.
- Docs: protocol, manifest, security, the patterns, and ARCHITECTURE.md.
- A manifest snapshot test, plus opt-in live tests (`pytest -m live`).
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
