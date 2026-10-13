from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from databolt.patterns import Registry, build_manifest, default_registry

SNAPSHOT = Path(__file__).parent / "snapshots" / "manifest.json"


def builtin_manifest() -> dict[str, object]:
    reg = Registry()
    reg.load_builtins()
    return build_manifest(reg)


def test_manifest_snapshot() -> None:
    """The manifest is a contract Studio commits a copy of. Regenerate deliberately:
    UPDATE_SNAPSHOTS=1 pytest tests/test_api_manifest.py, then review the diff."""
    current = json.dumps(builtin_manifest(), indent=2, sort_keys=False) + "\n"
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.write_text(current, encoding="utf-8")
    assert SNAPSHOT.exists(), "run UPDATE_SNAPSHOTS=1 pytest to create the snapshot"
    assert json.loads(current) == json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_rest_schema_contract() -> None:
    m = builtin_manifest()
    rest = next(p for p in m["patterns"] if p["id"] == "databolt.ingestion.api.rest")  # type: ignore[index]
    schema = rest["params_schema"]
    defs = schema["$defs"]
    # auth and paginator are discriminated oneOfs
    auth = defs["ClientConfig"]["properties"]["auth"]
    assert auth["discriminator"]["propertyName"] == "type"
    assert {o["$ref"].split("/")[-1] for o in auth["oneOf"]} == {
        "NoAuth", "BearerAuth", "ApiKeyAuth", "HttpBasicAuth", "OAuth2ClientCredentials",
    }  # fmt: skip
    pag = defs["ClientConfig"]["properties"]["paginator"]
    assert len(pag["oneOf"]) == 8
    assert defs["BearerAuth"]["properties"]["token"]["format"] == "secret-ref"
    # Studio reads incremental at params.resource.incremental
    assert "incremental" in defs["ResourceConfig"]["properties"]
    assert schema["required"] == ["client"]


def test_builtins_register_through_entry_points() -> None:
    reg = default_registry()
    for pid in ("databolt.ingestion.api.rest", "databolt.ingestion.api.http_get"):
        p = reg.get(pid, "^1")
        assert p.origin == "standard"
        assert p.provider_name == "databolt-standard-patterns"


def test_golden_rest_run_over_protocol(httpserver: HTTPServer, tmp_path: Path) -> None:
    httpserver.expect_request("/items", query_string="limit=2").respond_with_json(
        {"results": [{"i": 1}, {"i": 2}], "next": httpserver.url_for("/items") + "?page=2"}
    )
    httpserver.expect_request("/items", query_string="page=2").respond_with_json(
        {"results": [{"i": 3}], "next": None}
    )
    request = {
        "protocol": 1,
        "op": "run",
        "run_id": "r1",
        "pattern": {"id": "databolt.ingestion.api.rest", "version": "^1"},
        "params": {
            "client": {"base_url": httpserver.url_for("/"), "auth": {"type": "bearer"}},
            "resource": {"path": "items", "params": {"limit": 2}, "data_selector": "results"},
        },
        "secrets": {"token": "golden-secret-1"},
        "state": None,
        "limits": {"total_timeout_s": 30},
        "egress": {"allow_private": True, "trust_env": False},
        "output": {"dir": str(tmp_path), "format": "ndjson"},
    }
    proc = subprocess.run(
        [sys.executable, "-I", "-m", "databolt.patterns", "protocol"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    (line,) = proc.stdout.splitlines()
    result = json.loads(line)
    assert result["rows"] == 3 and result["pages"] == 2
    assert result["paginator_detected"] == "json_link"
    assert result["files"][0]["path"] == str(tmp_path / "databolt_ingestion_api_rest-r1.ndjson")
    assert "golden-secret-1" not in proc.stdout + proc.stderr
    assert httpserver.log[0][0].headers["Authorization"] == "Bearer golden-secret-1"


@pytest.mark.parametrize("pid", ["databolt.ingestion.api.rest", "databolt.ingestion.api.http_get"])
def test_describe_cli(pid: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-I", "-m", "databolt.patterns", "describe", pid, "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert json.loads(proc.stdout)["id"] == pid
