# Runner protocol v1

The runner protocol is the stable contract between a caller and this package; DataBolt Studio's Node runner is one such caller. One process handles one request:

```bash
python -I -m databolt.patterns protocol < request.json > result.json
# equivalently: databolt-patterns protocol
```

- **stdin:** exactly one JSON request, followed by EOF.
- **stdout:** exactly one JSON result on a single line, and nothing else. Anything the pattern prints, including `print()`, C extensions and child processes writing to file descriptor 1, is redirected to stderr.
- **stderr:** progress lines (`[databolt] page 2: 100 records`) with known secret values masked.
- **exit code:** `0` ok, `2` validation error, `3` runtime error, `4` protocol mismatch.

`PROTOCOL_VERSION` is `1`. It changes only on a breaking change to the request or result shape. Additive fields do not change it. Unknown top-level, `limits` and `egress` keys are ignored, and each one produces a warning.

## Request

```json
{
  "protocol": 1,
  "op": "run",
  "run_id": "6f1c…",
  "pattern": {"id": "databolt.ingestion.api.rest", "version": "^1"},
  "params": {"client": {"base_url": "https://…"}, "resource": {"path": "…"}},
  "secrets": {"token": "…"},
  "state": {"cursor": "2026-10-01T00:00:00.000Z", "cursor_path": "properties.time"},
  "limits": {"total_timeout_s": 110, "max_response_bytes": 26214400, "max_total_bytes": 268435456},
  "egress": {"blocked_cidrs": ["…"], "unwrap_ipv4": ["::ffff:0:0/96", "::/96", "64:ff9b::/96"], "allow_private": false},
  "output": {"dir": "/abs/tmp/dir", "format": "ndjson"}
}
```

| Field | Notes |
|---|---|
| `op` | One of the four operations below. |
| `pattern` | Exactly one of three forms (see below). |
| `params` | Validated against the pattern's `params_schema`. |
| `secrets` | `{name: value}`. Params reference these as `${secrets.name}`. Values are never echoed. |
| `state` | The last *committed* state, or `null`. |
| `limits` | Optional caps (see the table below). |
| `egress` | Optional guard settings (see the table below). |
| `output` | Required for `run`. `format` is `ndjson` in v1. |
| `run_id` | Optional. Used in the output file name. |

### `op`

| Value | What it does |
|---|---|
| `run` | Fetch the records and write them to `output.dir`. |
| `preview` | One page and at most 50 records. They are returned inline as `records`. It writes no files and never changes state. |
| `validate` | Check the params. Returns `required_secrets`, and adds a warning for each secret not provided. |
| `describe` | Returns the manifest entry for `pattern`, or the whole manifest if `pattern` is omitted. |

### `pattern`

Give exactly one of these forms:

- `{"id": "...", "version": "^1"}`: an installed pattern. `version` is a semver range: `^1`, `~1.2`, `>=1.0 <2`, `1.2.3`, or `*`.
- `{"yaml": "<text>"}`: a custom YAML pattern. See [authoring/yaml.md](authoring/yaml.md).
- `{"file": "/abs/p.py", "entry": "my_fn", "sha256": "<hex>"}`: a custom Python file. The file is only imported if its bytes hash to `sha256`. See [authoring/python.md](authoring/python.md).

### `limits`

All fields are optional.

| Key | Alias | Default | Effect |
|---|---|---|---|
| `max_seconds` | `total_timeout_s` | 300 | Wall-clock budget. When it runs out, the run is truncated with a warning. |
| `max_bytes` | `max_total_bytes` | 256 MiB | Total response bytes. When it runs out, the run is truncated with a warning. |
| `max_response_bytes` | | 25 MiB | Any single response larger than this is an **error** (`response_too_large`). |
| `max_pages`, `max_records` | | none | The run is truncated when either is reached. |
| `request_timeout_s` | | 30 | Per request, and never past the run deadline. |
| `max_retries` | | 4 | Retries on 429, 5xx and transport errors, so 5 attempts in total. |
| `backoff_base_s`, `backoff_max_s` | | 0.5, 30 | Full-jitter exponential backoff. |
| `max_retry_after_s` | | 120 | A longer `Retry-After` fails at once with `retryable: true`. |

### `egress`

| Key | Default | Effect |
|---|---|---|
| `allow_private` | false | Allow loopback, private, link-local, CGNAT and metadata addresses. |
| `blocked_cidrs` | Studio's list | Replaces the CIDR blocklist. |
| `unwrap_ipv4` | mapped, compatible, NAT64 | IPv6 prefixes whose embedded IPv4 address is checked again. |
| `max_redirects` | 5 | Each hop is re-validated. |
| `trust_env` | true | Honour `HTTP(S)_PROXY` and `NO_PROXY`. |

## Result: success

```json
{
  "ok": true, "protocol": 1,
  "pattern": {"id": "databolt.ingestion.api.rest", "version": "1.0.0"},
  "files": [{"path": "/abs/tmp/dir/databolt_ingestion_api_rest-6f1c….ndjson", "rows": 300, "bytes": 19785}],
  "rows": 300, "pages": 3, "requests": 3, "retries": 0, "bytes": 20205,
  "paginator_detected": "json_link",
  "source_label": "https://pokeapi.co/api/v2/pokemon",
  "state": null, "state_changed": false,
  "truncated": true, "warnings": [], "elapsed_ms": 191
}
```

- **`files`:**
  - Every path is absolute and directly inside `output.dir`. It is built without resolving symlinks, so it keeps the caller's prefix.
  - Each file is NDJSON, one object per line. Read it with `read_json_auto(format='newline_delimited', union_by_name=true)`.
  - A run with zero rows writes no file and returns `files: []`.
- **`source_label`:** `scheme://host/path` only. The query string and userinfo are dropped, because they often carry tokens.
- **`state`:** a **proposal**. Commit it only if `state_changed` is true, and only **after** the records have landed. A failed run proposes nothing. If the caller never commits, the next run simply repeats the same window.
- **`truncated`:** a page, record, time or byte cap stopped the run early. See [patterns/rest.md](patterns/rest.md#incremental) for how truncation interacts with state.

## Result: failure

```json
{"ok": false, "protocol": 1,
 "error": {"code": "http_error", "message": "GET https://api.example.com/v1/items returned HTTP 401: …",
           "http_status": 401, "retryable": false},
 "warnings": [], "elapsed_ms": 412}
```

Messages are redacted. Every secret value from the request, the derived Basic-auth string and the OAuth access token are masked as `***`. Partial output files are deleted before the result is written.

| `code` | Exit | Meaning |
|---|---|---|
| `invalid_request` | 2 | The request is not JSON, has the wrong shape, or is missing `output` or `pattern`. |
| `validation_error`, `template_error` | 2 | The params are invalid, or a `${...}` reference is missing. |
| `pattern_not_found`, `pattern_version_mismatch` | 2 | The pattern cannot be resolved. |
| `trust_mismatch` | 2 | A custom Python file's sha256 differs from the trusted hash. |
| `protocol_mismatch` | 4 | `protocol` is not 1. |
| `http_error` | 3 | A non-2xx status after retries. `retryable` is true for 429 and 5xx. |
| `transport_error` | 3 | A connection, TLS or timeout failure after retries. |
| `egress_blocked`, `too_many_redirects` | 3 | The guard refused a destination. |
| `response_too_large` | 3 | A single response exceeded `max_response_bytes`. |
| `invalid_json` | 3 | The response body could not be parsed. |
| `internal_error` | 3 | Anything unexpected. The message is still redacted. |

## Version probe

```bash
python -I -m databolt.patterns version --json
# {"package": "databolt-standard-patterns", "version": "0.1.0", "protocol": 1, "manifest_version": 1, "python": "3.12.3"}
```
