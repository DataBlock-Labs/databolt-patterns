# `databolt.ingestion.api.rest`

Pulls records from a REST/JSON API. The pattern takes care of:

- auth;
- pagination, auto-detected or explicit;
- retries with backoff on 429 and 5xx;
- rate limits;
- incremental cursors.

Its params have the same shape as dlt's `rest_api`: a `client` block for the connection and a `resource` block for what to fetch.

```yaml
client:
  base_url: https://api.example.com/v1/
  headers: {Accept: application/json}
  auth: {type: bearer, token: "${secrets.token}"}
  paginator: auto
resource:
  path: orders
  method: GET                  # or POST, with `json:` as the body
  params: {status: open, per_page: 100}
  data_selector: data.items    # dotted path; supports [*] and [0]
  incremental: {...}           # see below
  response_actions:
    - {status_code: 404, action: ignore}
max_pages: 50
max_records: 100000
requests_per_second: 5
```

A flat form is accepted too, with `base_url`, `path`, `params`, `data_selector`, `paginator` and the other keys at the top level. So is dlt's `endpoint:` in place of `resource:`. Both forms are folded into the nested shape.

## Auth

| `type` | Fields | Sends |
|---|---|---|
| `none` | | nothing |
| `bearer` | `token` (secret-ref) | `Authorization: Bearer …` |
| `api_key` | `name` (default `X-API-Key`), `value` (secret-ref), `location: header\|query` | a header or a query param |
| `http_basic` | `username`, `password` (secret-ref) | `Authorization: Basic …` |
| `oauth2_client_credentials` | `token_url`, `client_id`, `client_secret` (secret-ref), `scope`, `audience`, `client_auth: basic\|body`, `extra` | fetches a token once per run, then sends `Bearer` |

A secret-ref field holds `${secrets.<name>}`. The default names are `token`, `api_key`, `password` and `client_secret`, but any name works, as long as the caller supplies it in `secrets`. Credentials go only to the origin of `base_url`.

## Pagination

| `type` | Fields | Stops when |
|---|---|---|
| `auto` (default) | | see detection order below |
| `single_page` | | after one request |
| `json_link` | `next_url_path` (default `next`) | the next URL is missing or empty |
| `header_link` | `rel` (default `next`) | there is no `Link: <…>; rel="next"` |
| `header_cursor` | `cursor_header`, `cursor_param` | the header is absent |
| `cursor` | `cursor_path`, `cursor_param`, `location: query\|json` | the cursor is null or empty |
| `offset` | `limit`, `offset` (alias `base`; for example 1 for USGS FDSN), `offset_param`, `limit_param`, `total_path`, `maximum_offset` | a page is short or empty, `total_path` is reached, or `maximum_offset` is reached |
| `page_number` | `base_page`, `page_param`, `page_size`, `page_size_param`, `total_path` (in pages), `maximum_page` | a page is empty or short, or a limit is reached |

**`auto` detection** looks at the first response and picks the first rule that matches:

1. an RFC 8288 `Link` header with `rel="next"` → `header_link`;
2. a next-page URL in the body: `next`, `next_url`, `nextUrl`, `next_page_url`, `@odata.nextLink`, `nextLink`, `links.next`, `_links.next.href`, `paging.next`, `pagination.next` or `meta.next`. The value must be a URL or null → `json_link`;
3. a cursor in the body: `next_cursor`, `nextCursor`, `next_page_token`, `nextPageToken`, `meta.next_cursor`, `response_metadata.next_cursor`, `pagination.next_cursor` or `paging.cursors.after`. The cursor is sent back as a guessed query param (`cursor`, `page_token`, `pageToken` or `after`), and the run gets a warning → `cursor`;
4. otherwise `single_page`.

The result reports the choice as `paginator_detected`.

**Loop protection.** If a next request repeats a page already fetched, the run stops with a warning.

**Caps.** `max_pages` (from params or limits, whichever is tighter) sets `truncated: true` when more pages exist. `max_records` stops at exactly N records, without requesting another page.

**Records.** If `data_selector` is unset, a list body is the record list. For an object body, the first list under `results`, `data`, `items`, `records`, `features`, `entries`, `value`, `rows` or `hits` is used, with a warning. Otherwise the body is a single record.

## Retries and rate limits

Handled by the guarded client (see [../protocol.md](../protocol.md#limits)):

- **What is retried:** 429, 5xx (except 501 and 505) and transport errors.
- **Retry count:** up to 4 retries, so 5 attempts in total.
- **Delay:** full-jitter exponential backoff, or the `Retry-After` value in either its seconds or HTTP-date form.
- **Not retried:** other 4xx statuses fail at once.
- **Rate limit:** `requests_per_second` spaces requests out evenly.

## Incremental

```yaml
resource:
  params: {orderby: time-asc}
  incremental:
    param: starttime              # query param that receives the start value (optional)
    cursor_path: properties.time  # where the cursor lives in each record
    initial_value: "2026-10-01T00:00:00Z"
    transform: epoch_ms_to_iso    # none | iso8601 | epoch_ms_to_iso | epoch_s_to_iso
    order: asc                    # asc | desc | unordered (default)
    lookback: 0                   # seconds (or units) to re-read before the cursor
    range_start: open             # open (default) | closed
```

1. **Start value.** It is the committed `state.cursor` minus `lookback`, or `initial_value` on a first run. It is sent as `param`, and is also available as `${state.cursor}` in `params` and `json`.
2. **Filtering.** Records whose cursor is below the start value are dropped, which covers APIs that ignore the filter. On a resumed run, `range_start: open` also drops records *equal* to the committed cursor. That way an inclusive API filter, such as USGS `starttime`, does not land the boundary record twice.
3. **Proposal.** The state proposal is `{"cursor": <max landed value>, "cursor_path": …}`. It never moves backwards. The `transform` decides how the value is read and written: `epoch_ms_to_iso` reads milliseconds and writes `2026-10-12T08:30:00.123Z`.
4. **Truncation rule.** A **truncated** run, stopped by a page, record, time or byte cap, advances the cursor only when `order: asc`. For any other order, the pages not yet fetched may hold older records, and advancing would skip them for good. The run keeps the old state and adds a warning instead.
5. **Commit after landing.** The caller commits the proposal only after the records have landed.
6. **Changed `cursor_path`.** If `cursor_path` changes, the stored cursor is ignored and the run starts from `initial_value`, with a warning.

## Response actions

`response_actions: [{status_code: 404, content: "optional substring", action: ignore}]` treats a matching response as an empty final page, instead of failing the run.

## Examples

- [`examples/pokeapi.yaml`](../../examples/pokeapi.yaml): auto-detects `json_link` and returns 300 rows over 3 pages.
- [`examples/usgs_earthquakes.yaml`](../../examples/usgs_earthquakes.yaml): an offset paginator with base 1, incremental on `starttime`.
- [`examples/github_repos.yaml`](../../examples/github_repos.yaml): a `header_link` paginator with bearer auth.

```python
from databolt.ingestion import api

src = api.rest(
    base_url="https://earthquake.usgs.gov/fdsnws/event/1/",
    path="query",
    params={"format": "geojson", "orderby": "time-asc"},
    data_selector="features",
    paginator={"type": "offset", "offset": 1, "limit": 1000},
    incremental={
        "param": "starttime",
        "cursor_path": "properties.time",
        "initial_value": "2026-10-01T00:00:00Z",
        "transform": "epoch_ms_to_iso",
        "order": "asc",
    },
)
result = src.to_ndjson("out/")
src.commit()  # adopt result.state for the next run
```
