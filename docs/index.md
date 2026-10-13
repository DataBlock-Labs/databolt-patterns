# databolt-standard-patterns documentation

`databolt-standard-patterns` provides the ingestion patterns used by the DataBolt data platform. It works as a standalone library and CLI, and as the Python engine behind DataBolt Studio's standard and custom patterns.

| Page | What it covers |
|---|---|
| [patterns/rest.md](patterns/rest.md) | `databolt.ingestion.api.rest`: auth, pagination, retries, incremental |
| [patterns/http_get.md](patterns/http_get.md) | `databolt.ingestion.api.http_get`: one URL, parsed as JSON, NDJSON, CSV or TSV |
| [protocol.md](protocol.md) | Runner protocol v1, the stdin/stdout contract Studio depends on |
| [manifest.md](manifest.md) | Manifest v1, the JSON Schema description of each pattern that Studio renders |
| [security.md](security.md) | Egress guard, secrets, redaction, and the trust model for custom code |
| [authoring/yaml.md](authoring/yaml.md) | Custom patterns in YAML (`kind: databolt.pattern/v1`) |
| [authoring/python.md](authoring/python.md) | Custom patterns in Python (`@pattern`) |
| [authoring/package.md](authoring/package.md) | Shipping patterns in a pip package (entry points) |
| [../ARCHITECTURE.md](../ARCHITECTURE.md) | How the package fits the DataBolt system |

## Install

```bash
pip install databolt-standard-patterns              # core: httpx, pydantic, pyyaml
pip install "databolt-standard-patterns[all]"       # + Parquet (pyarrow) and DuckDB output
# straight from git, pinned to a release tag:
pip install "databolt-standard-patterns @ git+https://github.com/DataBlock-Labs/databolt-standard-patterns@v0.1.0"
```

## Three ways to run a pattern

```python
from databolt.ingestion import api

# 1. functional
api.rest(
    base_url="https://pokeapi.co/api/v2/",
    path="pokemon",
    params={"limit": 100},
    data_selector="results",
    max_pages=3,
).to_ndjson("out/")

# 2. class + config (e.g. loaded from YAML)
api.RestApiPattern(api.RestConfig.model_validate(cfg)).bind().to_parquet("out/p.parquet")
```

```bash
# 3. CLI
databolt-patterns run databolt.ingestion.api.rest -c examples/pokeapi.yaml --out out/
```

## CLI reference

| Command | Purpose |
|---|---|
| `databolt-patterns version [--json]` | Package, protocol and manifest versions |
| `databolt-patterns list [--json] [--custom DIR]` | Available patterns |
| `databolt-patterns describe ID [--json]` | One pattern's manifest entry |
| `databolt-patterns manifest [--out FILE] [--custom DIR]` | The full manifest |
| `databolt-patterns validate FILE [--pattern ID]` | Validate a custom pattern, or a params file against a pattern |
| `databolt-patterns run ID\|FILE ...` | Run a pattern and write NDJSON, Parquet or DuckDB |
| `databolt-patterns protocol` | Speak runner protocol v1 on stdin/stdout |

`run` flags:

- **Params:** `-c/--params-file FILE` and `--param key.path=value`. Values are YAML-parsed.
- **Secrets:** `--secret name=@env:VAR`, `name=@file:PATH` or `name=value`. Prefer `@env`, because a plain value shows up in the process list.
- **State:** `--state FILE` reads the state before the run and commits it after a successful one. `--no-commit` leaves the file untouched.
- **Output:** `--out DIR|FILE` and `--format ndjson|parquet|duckdb`.
- **Limits:** `--max-pages`, `--max-records` and `--max-seconds`.
- **Egress:** `--allow-private` permits loopback and private addresses.
- **Preview:** `--preview` fetches one page and at most 50 records, prints them, and never commits state.
