"""`${params.x}`, `${secrets.y}`, `${state.cursor}` substitution in configs.

A string that is exactly one placeholder becomes the referenced value with its type
intact (`"${params.limit}"` -> 100). A placeholder inside a longer string is
interpolated as text. `$${` is a literal `${`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Set
from typing import Any

from databolt.patterns.errors import TemplateError

ROOTS = ("params", "secrets", "state")
_PLACEHOLDER = re.compile(r"(?<!\$)\$\{\s*([A-Za-z_][A-Za-z0-9_]*)((?:\.[A-Za-z0-9_\-]+)*)\s*\}")
_WHOLE = re.compile(r"^\$\{\s*([A-Za-z_][A-Za-z0-9_]*)((?:\.[A-Za-z0-9_\-]+)*)\s*\}$")

_MISSING = object()


def _lookup(scope: Mapping[str, Any], root: str, dotted: str) -> Any:
    cur: Any = scope.get(root, _MISSING)
    for key in [k for k in dotted.split(".") if k]:
        if isinstance(cur, Mapping) and key in cur:
            cur = cur[key]
        elif isinstance(cur, list) and key.isdigit() and int(key) < len(cur):
            cur = cur[int(key)]
        else:
            return _MISSING
    return cur


def _stringify(v: Any) -> str:
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v, separators=(",", ":"), default=str)


def render(
    obj: Any,
    scope: Mapping[str, Any],
    *,
    only: Set[str] | None = None,
) -> Any:
    """Render placeholders throughout `obj` (dicts, lists, strings).

    `only` limits which roots are substituted; placeholders for other roots are left
    untouched so a later stage can render them (custom YAML patterns render params at
    bind time and leave secrets/state for run time).
    """
    if isinstance(obj, str):
        return _render_str(obj, scope, only)
    if isinstance(obj, Mapping):
        return {k: render(v, scope, only=only) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render(v, scope, only=only) for v in obj]
    return obj


def _render_str(s: str, scope: Mapping[str, Any], only: Set[str] | None) -> Any:
    if "${" not in s:
        return s
    whole = _WHOLE.match(s)
    if whole and (only is None or whole.group(1) in only):
        return _resolve(whole.group(1), whole.group(2), scope, s)

    def sub(m: re.Match[str]) -> str:
        root = m.group(1)
        if only is not None and root not in only:
            return m.group(0)
        return _stringify(_resolve(root, m.group(2), scope, m.group(0)))

    out = _PLACEHOLDER.sub(sub, s)
    return out.replace("$${", "${") if only is None else out


def _resolve(root: str, dotted: str, scope: Mapping[str, Any], text: str) -> Any:
    if root not in ROOTS:
        raise TemplateError(f"unknown template root {root!r} in {text!r}; use {', '.join(ROOTS)}")
    value = _lookup(scope, root, dotted)
    if value is _MISSING:
        # Never echo a secret's value; the reference name is safe to show.
        raise TemplateError(f"template {text!r} references a missing value")
    return value


def references(obj: Any, root: str) -> set[str]:
    """Names referenced under `root` anywhere in `obj` (e.g. the secrets a config needs)."""
    found: set[str] = set()
    if isinstance(obj, str):
        for m in _PLACEHOLDER.finditer(obj):
            if m.group(1) == root and m.group(2):
                found.add(m.group(2).lstrip(".").split(".")[0])
    elif isinstance(obj, Mapping):
        for v in obj.values():
            found |= references(v, root)
    elif isinstance(obj, list):
        for v in obj:
            found |= references(v, root)
    return found


def has_placeholders(obj: Any) -> bool:
    if isinstance(obj, str):
        return bool(_PLACEHOLDER.search(obj))
    if isinstance(obj, Mapping):
        return any(has_placeholders(v) for v in obj.values())
    if isinstance(obj, list):
        return any(has_placeholders(v) for v in obj)
    return False
