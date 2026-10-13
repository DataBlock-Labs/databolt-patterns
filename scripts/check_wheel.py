"""Fail if a built wheel ships a namespace __init__.py.

`databolt` (and `databolt.ingestion`) are PEP 420 namespace packages shared with
other DataBolt dists. A wheel that ships databolt/__init__.py would shadow them.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

FORBIDDEN = {"databolt/__init__.py", "databolt/ingestion/__init__.py"}
REQUIRED = {
    "databolt/patterns/__init__.py",
    "databolt/patterns/py.typed",
    "databolt/ingestion/api/__init__.py",
}


def check(wheel: Path) -> list[str]:
    names = set(zipfile.ZipFile(wheel).namelist())
    errors = [f"{wheel.name}: ships forbidden {n}" for n in sorted(FORBIDDEN & names)]
    errors += [f"{wheel.name}: missing {n}" for n in sorted(REQUIRED - names)]
    return errors


def main() -> int:
    wheels = sorted(Path(sys.argv[1] if len(sys.argv) > 1 else "dist").glob("*.whl"))
    if not wheels:
        print("no wheel found", file=sys.stderr)
        return 1
    errors = [e for w in wheels for e in check(w)]
    for e in errors:
        print(e, file=sys.stderr)
    if not errors:
        print(f"ok: {', '.join(w.name for w in wheels)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
