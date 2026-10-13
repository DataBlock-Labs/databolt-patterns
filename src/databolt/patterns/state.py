"""Incremental state: a *proposal* the caller commits only after landing succeeds.

A pattern never writes state. The runtime returns `state` + `state_changed` in the
run result; DataBolt Studio commits it after the Bronze write, the CLI commits it
after the output files are closed. A failed run proposes nothing.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from databolt.patterns.errors import ValidationError

StateDict = dict[str, Any]


def load_state(path: str | os.PathLike[str] | None) -> StateDict | None:
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "null")
    except json.JSONDecodeError as e:
        raise ValidationError(f"state file {p} is not valid JSON: {e.msg}") from None
    if data is not None and not isinstance(data, dict):
        raise ValidationError(f"state file {p} must hold a JSON object")
    return data


def commit_state(path: str | os.PathLike[str], state: StateDict | None) -> None:
    """Write state atomically (temp file + rename) so a crash never leaves half a file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True, default=str)
            f.write("\n")
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
