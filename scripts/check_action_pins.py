"""Fail if any workflow `uses:` a third-party action by tag or branch instead of a SHA.

A tag can be moved; a 40-character commit SHA cannot. The release workflow holds
`id-token: write` for PyPI trusted publishing, so an unpinned action there would be a
supply-chain path into the published package. Local actions (`./...`) are exempt.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

USES = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)
PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def check(root: Path) -> list[str]:
    errors: list[str] = []
    for wf in sorted(
        [*root.glob(".github/workflows/*.yml"), *root.glob(".github/workflows/*.yaml")]
    ):
        text = wf.read_text(encoding="utf-8")
        for m in USES.finditer(text):
            ref = m.group(1).strip("'\"")
            if ref.startswith("./") or ref.startswith("docker://"):
                continue
            if not PINNED.match(ref):
                line = text.count("\n", 0, m.start()) + 1
                errors.append(f"{wf}:{line}: {ref} is not pinned to a 40-character commit SHA")
    return errors


def main() -> int:
    errors = check(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
    for e in errors:
        print(e, file=sys.stderr)
    if not errors:
        print("ok: every action is pinned to a commit SHA")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
