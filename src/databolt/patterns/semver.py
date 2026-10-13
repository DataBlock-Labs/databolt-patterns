"""Just enough semver to resolve `pattern.version` ranges like "^1", "~1.2", ">=1.0 <2"."""

from __future__ import annotations

import re

from databolt.patterns.errors import ValidationError

_VERSION = re.compile(r"^v?(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?(?:[-+].*)?$")
_CMP = re.compile(r"^(>=|<=|>|<|=|\^|~)?\s*(.+)$")

Version = tuple[int, int, int]


def parse(v: str) -> Version:
    m = _VERSION.match(v.strip())
    if not m:
        raise ValidationError(f"invalid version {v!r}")
    return tuple(int(g) if g and g.isdigit() else 0 for g in m.groups())  # type: ignore[return-value]


def _parts(v: str) -> tuple[list[int], int]:
    """Numeric parts and how many were given (for partial versions like "1" or "1.2")."""
    m = _VERSION.match(v.strip())
    if not m:
        raise ValidationError(f"invalid version range part {v!r}")
    nums = [int(g) for g in m.groups() if g and g.isdigit()]
    return nums + [0] * (3 - len(nums)), len(nums)


def _check(version: Version, op: str, target: str) -> bool:
    nums, given = _parts(target)
    t: Version = (nums[0], nums[1], nums[2])
    if op == "^":
        if t[0] > 0 or given == 1:
            upper: Version = (t[0] + 1, 0, 0)
        elif t[1] > 0 or given == 2:
            upper = (0, t[1] + 1, 0)
        else:
            upper = (0, 0, t[2] + 1)
        return t <= version < upper
    if op == "~":
        upper = (t[0] + 1, 0, 0) if given == 1 else (t[0], t[1] + 1, 0)
        return t <= version < upper
    if op in ("", "="):
        if given == 3:
            return version == t
        return version[:given] == t[:given]
    return {
        ">=": version >= t,
        ">": version > t,
        "<=": version <= t,
        "<": version < t,
    }[op]


def satisfies(version: str, spec: str | None) -> bool:
    """True if `version` matches `spec`. Empty, "*", "latest" match anything.

    Space-separated comparators are ANDed; `||` separates alternatives.
    """
    if spec is None or spec.strip() in ("", "*", "x", "latest"):
        return True
    v = parse(version)
    for alt in spec.split("||"):
        ok = True
        for part in alt.split():
            m = _CMP.match(part)
            if not m:
                raise ValidationError(f"invalid version range {spec!r}")
            if not _check(v, m.group(1) or "", m.group(2)):
                ok = False
                break
        if ok:
            return True
    return False
