# databolt-standard-patterns

Open-source ingestion patterns for the DataBolt data platform.

- **`databolt.ingestion.api`**: standard REST ingestion that handles the hard parts of an API pull: auth, pagination (including auto-detection), retries with backoff on 429 and 5xx, rate limits, and incremental cursors.
- **`databolt.patterns`**: the small framework underneath. It covers the pattern contract, an egress-guarded HTTP client, a runner protocol and a JSON-Schema manifest, and supports custom patterns written in YAML, Python or as pip packages.

It works on its own as a library and CLI, and it is the Python engine behind DataBolt Studio's standard and custom patterns ([ARCHITECTURE.md](ARCHITECTURE.md)).

## Install

```bash
pip install databolt-standard-patterns           # Python 3.10+
pip install "databolt-standard-patterns[all]"    # + Parquet and DuckDB output
# or from a git tag:
pip install "databolt-standard-patterns @ git+https://github.com/DataBlock-Labs/databolt-standard-patterns@v0.1.1"
```

## Quick start

```python
from databolt.ingestion import api

pokemon = api.rest(
    base_url="https://pokeapi.co/api/v2/",
    path="pokemon",
    params={"limit": 100},
    data_selector="results",
    paginator="auto",  # detects the `next` link in the body
    max_pages=3,
)
result = pokemon.to_ndjson("out/")  # or .to_parquet(...), .to_duckdb(...), .iter_records()
print(result.rows, result.paginator_detected)  # 300 json_link
```

With auth and incremental loading:

```python
import os

orders = api.rest(
    base_url="https://api.example.com/v1/",
    path="orders",
    auth=api.auth.bearer("${secrets.token}"),
    incremental={
        "param": "updated_since",
        "cursor_path": "updated_at",
        "transform": "iso8601",
        "order": "asc",
        "initial_value": "2026-01-01T00:00:00Z",
    },
    secrets={"token": os.environ["ACME_TOKEN"]},
)
orders.to_parquet("orders.parquet")
orders.commit()  # the next run starts after the newest updated_at
```

From the command line:

```bash
databolt-patterns list
databolt-patterns run databolt.ingestion.api.rest -c examples/pokeapi.yaml --out out/
databolt-patterns run databolt.ingestion.api.rest -c examples/usgs_earthquakes.yaml \
    --state out/usgs.json --out out/usgs          # run it again: only new earthquakes
databolt-patterns describe databolt.ingestion.api.rest   # the JSON Schema for its params
```

## What the REST pattern manages

| | |
|---|---|
| Auth | `none`, `bearer`, `api_key` (header/query), `http_basic`, `oauth2_client_credentials`; credentials only ever go to `base_url`'s origin |
| Pagination | `auto`, `single_page`, `json_link`, `header_link`, `header_cursor`, `cursor`, `offset`, `page_number` |
| Retries | 5 attempts, full-jitter exponential backoff on 429/5xx/transport errors, `Retry-After` (seconds or HTTP-date) |
| Incremental | `param`, `cursor_path`, `initial_value`, `transform`, `order`, `lookback`; state is a proposal committed after landing; truncated non-ascending runs never advance |
| Limits | `max_pages`, `max_records`, total bytes and time, 25 MiB per response, optional requests/second |
| Safety | Egress guard on by default: no loopback/private/link-local/CGNAT/metadata targets, re-checked on every redirect hop and pinned at connect time; secrets redacted everywhere |

Full reference: [docs/patterns/rest.md](docs/patterns/rest.md).

## Custom patterns

```python
from collections.abc import Iterator
from pydantic import BaseModel
from databolt.patterns import pattern, RunContext


class Params(BaseModel):
    city: str


@pattern(id="acme.weather", version="1.0.0", secrets=["api_key"])
def weather(params: Params, ctx: RunContext) -> Iterator[dict]:
    resp = ctx.http.get(
        "https://api.example.com/weather", params={"q": params.city, "key": ctx.secrets["api_key"]}
    )
    yield from resp.json()["observations"]
```

You can also extend the REST pattern in YAML (`kind: databolt.pattern/v1`), or ship patterns in a pip package. See [docs/authoring](docs/authoring).

## Documentation

- [docs/index.md](docs/index.md): an overview of the docs and the CLI reference.
- [docs/protocol.md](docs/protocol.md): runner protocol v1.
- [docs/manifest.md](docs/manifest.md): manifest v1.
- [docs/security.md](docs/security.md): the security model.
- [ARCHITECTURE.md](ARCHITECTURE.md): how the package fits the DataBolt system.
- [CONTRIBUTING.md](CONTRIBUTING.md): how to contribute.
- [RELEASING.md](RELEASING.md): how releases are cut.

## License

Apache-2.0. See [LICENSE](LICENSE).
