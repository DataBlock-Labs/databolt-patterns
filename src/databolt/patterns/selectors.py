"""Pick records out of a parsed JSON document with a dotted path.

Parity with Studio's `applyJsonPath` (lib/studio/json-path.ts): an optional `$` or
`$.` prefix, dot-separated keys; an array result becomes its elements, an object
becomes one row, a scalar becomes `{"value": x}`.

Two extensions, both a no-op for paths Studio already accepts:

- `[*]` (or `.*`) fans out over an array: `data[*].items` concatenates every
  element's `items`;
- `[0]` (or `.0` on a list) indexes into an array.

One deliberate difference: a path that does not exist yields **no rows** (Studio's
selector yields one empty row), so an empty page stops pagination cleanly.
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(r"\[(\*|\d+)\]|([^.\[\]]+)")
_WILD = "*"


def parse_path(path: str | None) -> list[str]:
    if not path:
        return []
    clean = re.sub(r"^\$\.?", "", path.strip())
    tokens: list[str] = []
    for m in _TOKEN.finditer(clean):
        tokens.append(m.group(1) if m.group(1) is not None else m.group(2))
    return tokens


def _step(values: list[Any], token: str) -> list[Any]:
    out: list[Any] = []
    for cur in values:
        if token == _WILD:
            if isinstance(cur, list):
                out.extend(cur)
            elif isinstance(cur, dict):
                out.extend(cur.values())
        elif isinstance(cur, dict):
            if token in cur:
                out.append(cur[token])
        elif isinstance(cur, list) and token.isdigit():
            i = int(token)
            if i < len(cur):
                out.append(cur[i])
    return out


def get_path(data: Any, path: str | None) -> Any:
    """The single value at `path`, or None. A `[*]` path returns the fanned-out list.

    A top-level key that itself contains dots (`@odata.nextLink`) is matched literally first.
    """
    if isinstance(data, dict) and path and path in data:
        return data[path]
    tokens = parse_path(path)
    values = [data]
    fanned = False
    for t in tokens:
        fanned = fanned or t == _WILD
        values = _step(values, t)
        if not values:
            return None
    if fanned:
        return values
    return values[0] if values else None


def as_row(item: Any) -> dict[str, Any]:
    return item if isinstance(item, dict) else {"value": item}


def select_records(data: Any, path: str | None) -> list[dict[str, Any]]:
    """The rows at `path` (see module docstring for the rules)."""
    tokens = parse_path(path)
    values = [data]
    for t in tokens:
        values = _step(values, t)
    fanned = _WILD in tokens
    rows: list[dict[str, Any]] = []
    for v in values:
        if v is None:
            continue
        if isinstance(v, list):
            rows.extend(as_row(x) for x in v)
        elif fanned or isinstance(v, dict):
            rows.append(as_row(v))
        else:
            rows.append({"value": v})
    return rows
