# `databolt.ingestion.api.http_get`

Fetches a single URL and parses the body into records. Use it for a CSV export, a JSON document or an NDJSON dump; use the [REST pattern](rest.md) when the API paginates.

```yaml
url: https://example.com/exports/daily.csv
format: auto            # auto | json | ndjson | csv | tsv
data_selector: null     # JSON only: dotted path to the records
headers: {}
params: {}
auth: {type: none}      # the same auth types as the REST pattern
max_records: null
```

With `format: auto`, the format is detected in this order:

1. the `Content-Type` header (`application/json`, `application/x-ndjson`, `text/csv`, `text/tab-separated-values`);
2. the URL's extension (`.json`, `.ndjson`, `.jsonl`, `.csv`, `.tsv`);
3. the first byte of the body (`{` or `[` means JSON).

If none of these match, the body is parsed as CSV. CSV and TSV values are strings, with the header row as keys.

```python
from databolt.ingestion import api

api.http_get("https://example.com/data.csv").to_parquet("data.parquet")
```

The request goes through the same egress guard, retries and caps as every other pattern.
