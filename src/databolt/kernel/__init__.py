"""Studio's notebook kernels (spec 027): Jupyter kernels behind a stdio bridge.

Studio runs `python -I -m databolt.kernel.bridge --kernel <spec>` inside a locked
container (no network, read-only filesystem, no secrets). The bridge starts the
language's Jupyter kernel on loopback and relays its messages as JSON lines on
stdin and stdout (see `databolt.kernel.bridge`).

Inside a Python kernel, `databolt.kernel.helper` gives `sql()`, `display()` and
`dbutils.widgets`. They reach Studio through the kernel's stdin channel: an
`input()` with a reserved prompt, which the bridge answers. Other languages use
`%sql` cells, which Studio runs itself.
"""

from __future__ import annotations

from typing import Any

# Prompts the helper sends with input(); the bridge answers them, never the user.
SQL_PROMPT = "\x00databolt.sql:"
PARAMS_PROMPT = "\x00databolt.params"
# The MIME type of a table output (Studio and the VS Code extension render it).
TABLE_MIME = "application/vnd.databolt.table+json"
MAX_ROWS = 10_000


class Table:
    """Rows from `sql()`: columns plus row lists. `display(t)` shows it."""

    def __init__(self, columns: list[str], rows: list[list[Any]]) -> None:
        self.columns = columns
        self.rows = rows

    def to_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r, strict=False)) for r in self.rows]

    def __len__(self) -> int:
        return len(self.rows)

    def __repr__(self) -> str:
        return (
            f"Table({len(self.rows)} rows x {len(self.columns)} columns: {', '.join(self.columns)})"
        )
