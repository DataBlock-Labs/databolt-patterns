# Custom patterns in Python

Write a function, then decorate it:

```python
from collections.abc import Iterator
from pydantic import BaseModel, Field
from databolt.patterns import RunContext, pattern


class Params(BaseModel):  # becomes the manifest's params_schema
    city: str
    days: int = Field(3, ge=1, le=16)


@pattern(id="acme.forecast", version="1.0.0", title="Forecast", secrets=["api_key"])
def forecast(params: Params, ctx: RunContext) -> Iterator[dict]:
    resp = ctx.http.get(
        "https://api.example.com/forecast",
        params={"q": params.city, "days": params.days},
        headers={"X-Key": ctx.secrets["api_key"]},
        sensitive_headers=["x-key"],  # also stripped on cross-origin redirects
    )
    ctx.stats.pages = 1
    for day in resp.json()["days"]:
        yield {"city": params.city, **day}
```

- **Params.** The params model comes from the type hint on the first argument. Leave it out to get no params.
- **Records.** Yield dicts. A non-dict value is wrapped as `{"value": x}`.
- **Return value.** The decorated function is returned unchanged, so you can still call it directly.
- **Class form.** You can subclass `databolt.patterns.Pattern` instead: set `id`, `version`, `Params` and the other metadata as class attributes, then implement `iter_records(self, params, ctx)`.

## What `ctx` gives you

| | |
|---|---|
| `ctx.http` | The egress-guarded, retrying, budgeted client: `get`, `post`, `request`. **Use it for every request**, because the guard only protects traffic that goes through it. |
| `ctx.secrets` | `{name: value}` for this run. Values are redacted from logs and errors. |
| `ctx.state` | The last *committed* state, or `None`. |
| `ctx.propose_state(dict)` | The state to commit if this run lands successfully. |
| `ctx.track_state(tracker)` | Hands the runtime a tracker. It sees only the records that actually landed, and proposes state from them. |
| `ctx.log(msg)` / `ctx.warn(msg)` | Progress to stderr, and warnings into the result. Both are redacted. |
| `ctx.render(obj)` | Renders the `${params\|secrets\|state.*}` templates in a value. |
| `ctx.max_records`, `ctx.max_pages`, `ctx.truncated` | Run caps. Set `ctx.truncated = True` if you stop early. |
| `ctx.stats` | `pages`, `requests`, `retries`, `paginator_detected` and `source_label`, which are all reported in the result. |

`print()` is safe. In protocol mode it goes to stderr, and it never corrupts the result.

## Running a file

```bash
databolt-patterns validate forecast.py
databolt-patterns run forecast.py --param city=Lisbon --secret api_key=@env:KEY --preview
```

Naming a file on the CLI counts as trusting it. Over the runner protocol, a file runs only when its bytes match the hash the caller recorded:

```json
{"pattern": {"file": "/abs/forecast.py", "entry": "forecast", "sha256": "9f2c…"}}
```

- The hash is checked **before** any code runs, and the verified bytes are what get executed. Editing the file afterwards produces a `trust_mismatch` error (exit code 2) until the caller trusts the new version.
- `entry` names the function or class to use. It can be left out when the file defines exactly one pattern.

## Trust

Custom Python is **not sandboxed**. It runs with the caller's privileges, and the egress guard only covers `ctx.http`. DataBolt Studio shows the code read-only before you choose "Trust & save locally", and asks again after any edit. Only trust code you have read. See [../security.md](../security.md).

A complete example is in [`examples/custom_pattern.py`](../../examples/custom_pattern.py), which pulls the top Hacker News stories.
