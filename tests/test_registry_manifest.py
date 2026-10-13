from __future__ import annotations

from importlib import metadata
from typing import Any

import pytest
from pydantic import BaseModel, Field

from databolt.patterns import (
    MANIFEST_VERSION,
    Pattern,
    Registry,
    SecretRef,
    build_manifest,
    pattern,
    ui,
)
from databolt.patterns.errors import PatternNotFound, ValidationError
from databolt.patterns.manifest import pattern_entry
from databolt.patterns.registry import ENTRY_POINT_GROUP
from tests import fixture_patterns as fp


def test_register_and_get() -> None:
    reg = Registry()
    p = reg.register(fp.numbers)
    assert reg.get("test.numbers") is p
    assert reg.get("test.numbers", "^1.2") is p
    assert "test.numbers" in reg
    assert p.origin == "standard" and p.provider_name == "databolt-standard-patterns"
    with pytest.raises(ValidationError) as ei:
        reg.get("test.numbers", "^2")
    assert ei.value.code == "pattern_version_mismatch"
    with pytest.raises(PatternNotFound):
        reg.get("nope")


def test_duplicate_ids_are_rejected() -> None:
    reg = Registry()
    reg.register(fp.numbers)
    with pytest.raises(ValidationError, match="already registered"):
        reg.register(fp.numbers)
    reg.register(fp.numbers, replace=True)


def test_register_rejects_non_patterns_and_bad_versions() -> None:
    with pytest.raises(ValidationError):
        Registry().register(object())

    @pattern(id="bad.version", version="one")
    def bad(params: fp.NumbersParams, ctx: Any) -> Any:
        yield {}

    with pytest.raises(ValidationError):
        Registry().register(bad)


def test_class_based_pattern() -> None:
    class Hello(Pattern):
        id = "test.hello"
        version = "2.0.0"
        title = "Hello"

        def iter_records(self, params: BaseModel, ctx: Any) -> Any:
            yield {"hello": "world"}

    p = Registry().register(Hello)
    assert p.id == "test.hello"
    assert list(p.iter_records(p.parse_params({}), None)) == [{"hello": "world"}]


class _FakeDist:
    def __init__(self, name: str, version: str) -> None:
        self.metadata = {"Name": name}
        self.version = version


class _FakeEP:
    def __init__(self, name: str, target: Any, dist: _FakeDist) -> None:
        self.name = name
        self._target = target
        self.dist = dist

    def load(self) -> Any:
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


def test_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    eps = [
        _FakeEP("numbers", fp.numbers, _FakeDist("acme-patterns", "3.1.0")),
        _FakeEP("both", [fp.tracked, fp.proposes], _FakeDist("acme-patterns", "3.1.0")),
        _FakeEP("broken", ImportError("no module named acme"), _FakeDist("broken-dist", "0.1")),
        _FakeEP("dupe", fp.numbers, _FakeDist("other-dist", "1.0")),
    ]

    def fake_entry_points(*, group: str) -> list[_FakeEP]:
        assert group == ENTRY_POINT_GROUP
        return eps

    monkeypatch.setattr(metadata, "entry_points", fake_entry_points)
    reg = Registry()
    reg.load_entry_points()
    assert [p.id for p in reg.all()] == ["test.numbers", "test.proposes", "test.tracked"]
    p = reg.get("test.numbers")
    assert (p.origin, p.provider_name, p.provider_version) == ("package", "acme-patterns", "3.1.0")
    assert any("broken-dist" in w and "failed to load" in w for w in reg.warnings)
    assert any("other-dist provides 'test.numbers'" in w for w in reg.warnings)


class SecretParams(BaseModel):
    base_url: str = Field(..., description="API root", json_schema_extra=ui(group="connection"))
    token: SecretRef = "${secrets.token}"
    limit: int = 100


@pattern(
    id="test.manifest",
    version="1.0.0",
    title="Manifest test",
    secrets=["token"],
    capabilities={"pagination": False},
    examples=[{"title": "basic", "params": {"base_url": "https://x.test"}}],
)
def manifest_pattern(params: SecretParams, ctx: Any) -> Any:
    yield {}


def test_manifest_entry_shape() -> None:
    reg = Registry()
    reg.register(manifest_pattern)
    m = build_manifest(reg)
    assert m["manifest_version"] == MANIFEST_VERSION == 1
    assert m["protocol"] == 1
    assert m["package"]["name"] == "databolt-standard-patterns"
    (e,) = m["patterns"]
    assert set(e) == {
        "id", "version", "title", "description", "category", "origin", "provider", "runtime",
        "icon", "params_schema", "secrets", "defaults", "outputs", "capabilities", "examples",
    }  # fmt: skip
    assert e["runtime"] == "python"
    schema = e["params_schema"]
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["required"] == ["base_url"]
    assert schema["properties"]["token"]["format"] == "secret-ref"
    assert schema["properties"]["token"]["x-databolt-ui"] == {"widget": "secret-ref"}
    assert schema["properties"]["base_url"]["x-databolt-ui"] == {"group": "connection"}
    assert e["defaults"] == {"token": "${secrets.token}", "limit": 100}
    assert e["secrets"] == [{"name": "token", "description": "", "required": True}]
    assert e["outputs"]["formats"] == ["ndjson", "parquet", "duckdb"]


def test_secret_ref_rejects_literal_secrets() -> None:
    p = Registry().register(manifest_pattern)
    with pytest.raises(ValidationError, match="token"):
        p.parse_params({"base_url": "https://x.test", "token": "ghp_literal_token"})


def test_pattern_entry_is_json_serialisable() -> None:
    import json

    json.dumps(pattern_entry(Registry().register(fp.numbers)))
