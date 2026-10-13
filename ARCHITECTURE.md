# Architecture

How `databolt-standard-patterns` fits into the DataBolt system.

## The role it plays

DataBolt Studio is a drag-and-drop canvas. Each ingestion node on it reads a source into **Bronze**, and later hops build Silver and Gold. A source is configured in one of two ways:

- **A standard pattern** handles the hard parts of an API pull for you: auth, pagination, retries and rate limits, and incremental cursors. You fill in a form.
- **A custom pattern** is one you write: a YAML config that extends a standard pattern, a Python function, or a pip package.

This package is the **engine** for both. It fetches records and hands them to Studio's Node runner as NDJSON files, and Studio lands them in Bronze with DuckDB, exactly as it does for its other sources. The same code also runs standalone, as a Python library or the `databolt-patterns` CLI, so a pattern behaves the same in a notebook, a cron job, an orchestrator or Studio.

```
 Studio canvas ──► spec (source.type: pattern, pattern_id, params, secret names)
                     │
 Node runner ── resolves secrets from the keystore, reads committed state
                     │  spawn: python -I -m databolt.patterns protocol   (allowlisted env)
                     ▼  stdin: one JSON request
            ┌──────────────────────────────────────────────┐
            │ databolt.patterns  (framework)               │
            │   registry ─► pattern ─► runtime ─► sink     │
            │   net: egress guard + retrying HTTP client   │
            │ databolt.ingestion.api  (standard library)   │
            │   rest · http_get                            │
            └──────────────────────────────────────────────┘
                     │  stdout: one JSON result   files: NDJSON in output.dir
                     ▼
 Node runner ── read_json_auto(files) ─► Bronze write ─► commit proposed state
```

## Contracts

Three interfaces are versioned. Everything else is free to change.

1. **Runner protocol v1** ([docs/protocol.md](docs/protocol.md)). Studio sends one JSON request on stdin and receives one JSON result on stdout. Exit codes are 0/2/3/4. State comes back as a *proposal*, and Studio commits it only after Bronze has landed, so a failed landing can never skip data. `PROTOCOL_VERSION` changes only on a breaking change, and Studio refuses to run a mismatched package.
2. **Manifest v1** ([docs/manifest.md](docs/manifest.md)). Studio builds its palette and its parameter forms from JSON Schema 2020-12 plus `x-databolt-ui` hints. It does not hand-code a form per pattern. Secret params are `format: secret-ref`, so the UI only ever handles keystore *names*. Studio commits a manifest snapshot for display on Vercel or when Python is absent.
3. **Pattern ids and versions.** `databolt.ingestion.api.rest@1.x.y` is stable within a major version, and a spec pins a range (`^1`).

## Inside the package

- **`databolt.patterns`** is the framework. It holds no source-specific logic.
  - `contract`: a pattern is `Params` (a Pydantic model) plus `iter_records(params, ctx)`. `RunContext` gives a pattern the guarded `ctx.http`, a redacting `ctx.log`, the committed `ctx.state`, and a way to propose state.
  - `runtime`: validates params and checks secrets, then streams records into a sink. It enforces record caps and turns time and byte budgets into truncation. It asks the incremental tracker for a state proposal, and only *landed* records count. On failure it deletes partial output.
  - `registry`: built-ins and installed packages, both discovered through the `databolt.patterns` entry-point group, plus custom YAML and Python files.
  - `net`: the egress guard and the HTTP client. Every request goes through them, so the security properties hold for standard and custom patterns alike.
  - `protocol`, `manifest`, `cli`: the external surfaces.
- **`databolt.ingestion.api`** is the standard library. `rest` is a configurable REST engine, shaped like dlt's `rest_api` and Airbyte's low-code CDK: a connection, auth, a paginator, a record selector, incremental settings and response actions. `http_get` is the single-file case.

## Standard vs custom patterns

| | Standard | Custom YAML | Custom Python | Package |
|---|---|---|---|---|
| Lives in | this repo | `.data/studio/patterns/custom/*.yaml` | `.../custom/*.py` | any pip dist |
| Code? | reviewed here | none (config only) | arbitrary | arbitrary |
| Trust | implicit | implicit (inherits the base pattern's guard) | sha256-pinned, re-approved on edit | you installed it |
| Discovery | entry points | file / `{yaml}` in the request | `{file, entry, sha256}` | `databolt.patterns` entry points |
| `origin` in the manifest | `standard` | `custom` | `custom` | `package` |

A custom YAML pattern is the safe default for an API that the REST pattern can already describe. It exposes a few friendly params and hides the rest. Python is the escape hatch for anything else. See [docs/authoring](docs/authoring).

## Namespace: `databolt` is shared

`databolt` and `databolt.ingestion` are **PEP 420 namespace packages**, and this distribution ships no `databolt/__init__.py`. A future core distribution (`databolt`), and other add-ons such as files or databases, can then install their own subpackages next to `databolt.patterns` and `databolt.ingestion.api` without clashing. CI fails the build if a namespace `__init__.py` ever appears in the wheel.

The console script is `databolt-patterns`, not `databolt`, because the bare name is reserved for that future core distribution.

## What it deliberately does not do (v1)

- Files, databases and websockets: they stay on Studio's TypeScript engine.
- Multiple resources per run, and GraphQL.
- Sandboxing of custom Python code.
- Cloud execution. The protocol is local-only; Studio never spawns Python on Vercel.
