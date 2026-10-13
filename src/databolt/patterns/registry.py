"""Find patterns: built-ins, installed packages (entry points), and custom files.

Built-ins register through the same `databolt.patterns` entry-point group as
third-party packages; a fallback list keeps them available when the dist metadata
is missing (vendored or zipapp installs).
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from importlib import metadata
from typing import Any

from databolt.patterns import semver
from databolt.patterns._version import __version__
from databolt.patterns.contract import Pattern, as_pattern
from databolt.patterns.errors import PatternNotFound, ValidationError

ENTRY_POINT_GROUP = "databolt.patterns"
DIST_NAME = "databolt-standard-patterns"

# "module:attr" of each standard pattern. Also registered as entry points; this list keeps
# them available when the dist metadata is missing (vendored or zipapp installs).
BUILTINS: tuple[str, ...] = (
    "databolt.ingestion.api.rest:RestApiPattern",
    "databolt.ingestion.api.http_get:HttpGetPattern",
)


def _load_ref(ref: str) -> Any:
    module, _, attr = ref.partition(":")
    obj: Any = importlib.import_module(module)
    for part in attr.split(".") if attr else ():
        obj = getattr(obj, part)
    return obj


def _expand(obj: Any) -> list[Pattern]:
    """An entry point may export one pattern or a list/tuple of them."""
    if isinstance(obj, (list, tuple)):
        return [as_pattern(o) for o in obj]
    return [as_pattern(obj)]


class Registry:
    def __init__(self) -> None:
        self._patterns: dict[str, Pattern] = {}
        self.warnings: list[str] = []

    def register(
        self,
        obj: Any,
        *,
        origin: str | None = None,
        provider: tuple[str, str] | None = None,
        replace: bool = False,
    ) -> Pattern:
        p = as_pattern(obj)
        if not p.id:
            raise ValidationError(f"{p!r} has no id")
        semver.parse(p.version)
        if origin:
            p.origin = origin
        if provider:
            p.provider_name, p.provider_version = provider
        elif not p.provider_name:
            p.provider_name, p.provider_version = DIST_NAME, __version__
        existing = self._patterns.get(p.id)
        if existing is not None and not replace:
            raise ValidationError(
                f"pattern id {p.id!r} is already registered by {existing.provider_name}"
            )
        self._patterns[p.id] = p
        return p

    def get(self, pattern_id: str, version: str | None = None) -> Pattern:
        p = self._patterns.get(pattern_id)
        if p is None:
            raise PatternNotFound(f"no pattern {pattern_id!r} (try `databolt-patterns list`)")
        if not semver.satisfies(p.version, version):
            raise ValidationError(
                f"pattern {pattern_id!r} is version {p.version}, which does not satisfy "
                f"{version!r}",
                code="pattern_version_mismatch",
            )
        return p

    def __contains__(self, pattern_id: object) -> bool:
        return pattern_id in self._patterns

    def all(self) -> list[Pattern]:
        return [self._patterns[k] for k in sorted(self._patterns)]

    # -- custom patterns -------------------------------------------------------------

    def load_yaml_text(self, text: str) -> Pattern:
        raise ValidationError("custom YAML patterns are not supported by this version")

    def load_yaml_file(self, path: str) -> Pattern:
        raise ValidationError("custom YAML patterns are not supported by this version")

    def load_python_file(self, path: str, *, entry: str | None, sha256: str | None) -> Pattern:
        raise ValidationError("custom Python pattern files are not supported by this version")

    def load_custom_dir(self, path: str) -> None:
        raise ValidationError("custom pattern directories are not supported by this version")

    # -- discovery -------------------------------------------------------------------

    def load_builtins(self, refs: Iterable[str] = BUILTINS) -> None:
        for ref in refs:
            for p in _expand(_load_ref(ref)):
                if p.id not in self._patterns:
                    self.register(p, origin="standard")

    def load_entry_points(self) -> None:
        """Load every `databolt.patterns` entry point. A broken package is a warning,
        not a failure, so one bad install cannot take the whole palette down."""
        for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
            dist = getattr(ep, "dist", None)
            dist_name = dist.metadata["Name"] if dist is not None else ""
            dist_version = dist.version if dist is not None else ""
            standard = (dist_name or "").lower().replace("_", "-") == DIST_NAME
            try:
                patterns = _expand(ep.load())
            except Exception as e:
                self.warnings.append(
                    f"entry point {ep.name!r} from {dist_name or '?'} failed to load: "
                    f"{type(e).__name__}: {e}"
                )
                continue
            for p in patterns:
                if p.id in self._patterns:
                    if not standard:
                        self.warnings.append(
                            f"{dist_name} provides {p.id!r}, which is already registered; ignored"
                        )
                    continue
                self.register(
                    p,
                    origin="standard" if standard else "package",
                    provider=(dist_name, dist_version) if dist_name else None,
                )


def default_registry(*, entry_points: bool = True) -> Registry:
    """Built-ins first, then installed packages."""
    reg = Registry()
    if entry_points:
        reg.load_entry_points()
    reg.load_builtins()
    return reg
