from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from databolt.patterns import Registry, default_registry
from databolt.patterns.cli import main
from databolt.patterns.errors import TrustError, ValidationError
from databolt.patterns.manifest import pattern_entry
from databolt.patterns.protocol import handle

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
LOCAL = {"allow_private": True, "trust_env": False}


@pytest.fixture
def reg() -> Registry:
    r = Registry()
    r.load_builtins()
    return r


def acme_yaml(base_url: str) -> str:
    return textwrap.dedent(
        f"""
        kind: databolt.pattern/v1
        id: acme.orders
        version: 1.2.0
        title: Acme orders
        extends: databolt.ingestion.api.rest
        extends_version: "^1"
        params:
          status:
            type: string
            enum: [open, closed]
            default: open
          since:
            type: string
            format: date
            required: true
          token:
            type: string
            format: secret-ref
            default: "${{secrets.acme_token}}"
        secrets:
          - name: acme_token
            description: Acme API token
        config:
          client:
            base_url: {base_url}
            auth: {{type: bearer, token: "${{params.token}}"}}
            paginator: single_page
          resource:
            path: orders
            params: {{status: "${{params.status}}", since: "${{params.since}}"}}
            data_selector: orders
        """
    )


# -- YAML ----------------------------------------------------------------------------------


def test_yaml_pattern_manifest(reg: Registry) -> None:
    p = reg.load_yaml_text(acme_yaml("https://api.acme.test/v1/"))
    entry = pattern_entry(p)
    assert entry["id"] == "acme.orders" and entry["origin"] == "custom"
    props = entry["params_schema"]["properties"]
    assert props["status"]["enum"] == ["open", "closed"]
    assert props["since"]["format"] == "date"
    assert props["token"]["format"] == "secret-ref"
    assert entry["params_schema"]["required"] == ["since"]
    assert entry["capabilities"]["extends"] == "databolt.ingestion.api.rest"
    assert entry["secrets"][0]["name"] == "acme_token"


def test_yaml_pattern_runs_over_protocol(httpserver: HTTPServer, tmp_path: Path) -> None:
    httpserver.expect_request(
        "/orders",
        query_string="status=closed&since=2026-10-01",
        headers={"Authorization": "Bearer acme-secret"},
    ).respond_with_json({"orders": [{"id": 1}, {"id": 2}]})
    request = {
        "protocol": 1,
        "op": "run",
        "pattern": {"yaml": acme_yaml(httpserver.url_for("/"))},
        "params": {"status": "closed", "since": "2026-10-01"},
        "secrets": {"acme_token": "acme-secret"},
        "egress": LOCAL,
        "output": {"dir": str(tmp_path)},
    }
    result, rc = handle(json.dumps(request))
    assert rc == 0, result
    assert result["rows"] == 2
    assert result["pattern"] == {"id": "acme.orders", "version": "1.2.0"}


def test_yaml_pattern_validate_reports_required_secrets(reg: Registry) -> None:
    request = {
        "protocol": 1,
        "op": "validate",
        "pattern": {"yaml": acme_yaml("https://api.acme.test/")},
        "params": {"since": "2026-01-01"},
    }
    result, rc = handle(json.dumps(request), registry=reg)
    assert rc == 0
    assert result["required_secrets"] == ["acme_token"]


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda y: y.replace("kind: databolt.pattern/v1", "kind: other/v9"), "kind"),
        (lambda y: y.replace("id: acme.orders", "id: NoDots"), "pattern.id"),
        (lambda y: y.replace("extends: databolt.ingestion.api.rest", "extends: nope"), "nope"),
        (lambda y: y.replace('extends_version: "^1"', 'extends_version: "^2"'), "does not satisfy"),
        (
            lambda y: y.replace("paginator: single_page", "paginator: sideways"),
            "config does not fit",
        ),
        (
            lambda y: y.replace("data_selector: orders", "data_selector: ${params.nope}"),
            "undeclared",
        ),
        (lambda y: y.replace("version: 1.2.0", "version: one"), "invalid version"),
        (lambda y: y + "\nunexpected: 1\n", "unexpected"),
        (lambda y: "[1, 2]", "mapping"),
        (lambda y: "kind: [", "not valid YAML"),
    ],
)  # fmt: skip
def test_yaml_pattern_rejected_at_load(reg: Registry, mutate: object, error: str) -> None:
    text = mutate(acme_yaml("https://api.acme.test/"))  # type: ignore[operator]
    with pytest.raises(ValidationError, match=error):
        reg.load_yaml_text(text)


def test_yaml_params_are_validated(reg: Registry) -> None:
    p = reg.load_yaml_text(acme_yaml("https://api.acme.test/"))
    with pytest.raises(ValidationError, match="since"):
        p.parse_params({})
    with pytest.raises(ValidationError, match="status"):
        p.parse_params({"since": "x", "status": "pending"})
    with pytest.raises(ValidationError, match="token"):
        p.parse_params({"since": "x", "token": "literal-secret"})


def test_example_yaml_pattern(reg: Registry) -> None:
    p = reg.load_yaml_file(str(EXAMPLES / "pokemon_custom.yaml"))
    base = p.bind_config(p.parse_params({"resource": "berry"}))  # type: ignore[attr-defined]
    assert base.resource.path == "berry"
    assert base.resource.params == {"limit": 100}
    assert base.max_pages == 3


# -- Python files ----------------------------------------------------------------------------

PY_PATTERN = """
from pydantic import BaseModel
from databolt.patterns import pattern

class P(BaseModel):
    n: int = 3

@pattern(id="local.squares", version="0.1.0")
def squares(params: P, ctx):
    print("noise from the pattern")
    for i in range(params.n):
        yield {"i": i, "sq": i * i}
"""


def write_py(tmp_path: Path, body: str = PY_PATTERN, name: str = "squares.py") -> tuple[Path, str]:
    f = tmp_path / name
    f.write_text(body)
    return f, hashlib.sha256(f.read_bytes()).hexdigest()


def test_python_file_trusted_by_sha(reg: Registry, tmp_path: Path) -> None:
    f, sha = write_py(tmp_path)
    p = reg.load_python_file(str(f), entry=None, sha256=sha)
    assert p.id == "local.squares" and p.origin == "custom"
    assert p.provider_name == "squares.py" and p.provider_version == sha[:12]


def test_python_file_sha_mismatch_is_refused(reg: Registry, tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    f, _ = write_py(tmp_path, f"open({str(marker)!r}, 'w').write('x')\n" + PY_PATTERN)
    with pytest.raises(TrustError):
        reg.load_python_file(str(f), entry=None, sha256="0" * 64)
    assert not marker.exists()  # nothing ran


def test_python_file_entry_selection(reg: Registry, tmp_path: Path) -> None:
    two = PY_PATTERN + (
        '\n@pattern(id="local.cubes", version="0.1.0")\ndef cubes(params: P, ctx):\n    yield {}\n'
    )
    f, sha = write_py(tmp_path, two)
    with pytest.raises(ValidationError, match="several patterns"):
        reg.load_python_file(str(f), entry=None, sha256=sha)
    assert reg.load_python_file(str(f), entry="cubes", sha256=sha).id == "local.cubes"
    with pytest.raises(ValidationError, match="has no 'nope'"):
        reg.load_python_file(str(f), entry="nope", sha256=sha)
    with pytest.raises(ValidationError, match="needs the sha256"):
        reg.load_python_file(str(f), entry=None, sha256=None)


def test_python_file_errors(reg: Registry, tmp_path: Path) -> None:
    f, sha = write_py(tmp_path, "raise RuntimeError('import-time boom')\n", "bad.py")
    with pytest.raises(ValidationError, match="import-time boom"):
        reg.load_python_file(str(f), entry=None, sha256=sha)
    f, sha = write_py(tmp_path, "x = 1\n", "empty.py")
    with pytest.raises(ValidationError, match="defines no"):
        reg.load_python_file(str(f), entry=None, sha256=sha)


def test_protocol_trust_mismatch_exit_2(tmp_path: Path) -> None:
    f, _ = write_py(tmp_path)
    request = {
        "protocol": 1,
        "op": "run",
        "pattern": {"file": str(f), "sha256": "ab" * 32},
        "output": {"dir": str(tmp_path)},
    }
    result, rc = handle(json.dumps(request))
    assert rc == 2 and result["error"]["code"] == "trust_mismatch"


def test_golden_python_file_over_protocol_subprocess(tmp_path: Path) -> None:
    f, sha = write_py(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    request = {
        "protocol": 1,
        "op": "run",
        "pattern": {"file": str(f), "entry": "squares", "sha256": sha},
        "params": {"n": 4},
        "output": {"dir": str(out), "format": "ndjson"},
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
    assert result["rows"] == 4
    assert "noise from the pattern" in proc.stderr
    rows = [json.loads(x) for x in Path(result["files"][0]["path"]).read_text().splitlines()]
    assert rows[-1] == {"i": 3, "sq": 9}


# -- directories and CLI -------------------------------------------------------------------


def test_custom_dir_loads_yaml_and_skips_python(reg: Registry, tmp_path: Path) -> None:
    (tmp_path / "acme.yaml").write_text(acme_yaml("https://api.acme.test/"))
    (tmp_path / "broken.yaml").write_text("kind: databolt.pattern/v1\nid: x\n")
    write_py(tmp_path)
    reg.load_custom_dir(str(tmp_path))
    assert "acme.orders" in reg and "local.squares" not in reg
    assert any("broken.yaml" in w for w in reg.warnings)
    assert any("skipped squares.py" in w for w in reg.warnings)
    reg.load_custom_dir(str(tmp_path), trust_python=True)
    assert "local.squares" in reg


def test_cli_with_custom_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f, _ = write_py(tmp_path)
    assert main(["validate", str(f)]) == 0
    assert json.loads(capsys.readouterr().out)["pattern"]["id"] == "local.squares"
    assert main(["run", str(f), "--preview", "--param", "n=2"]) == 0
    assert json.loads(capsys.readouterr().out)["records"] == [{"i": 0, "sq": 0}, {"i": 1, "sq": 1}]
    assert main(["validate", str(EXAMPLES / "pokemon_custom.yaml")]) == 0
    capsys.readouterr()
    (tmp_path / "acme.yaml").write_text(acme_yaml("https://api.acme.test/"))
    assert main(["list", "--json", "--custom", str(tmp_path)]) == 0
    ids = [r["id"] for r in json.loads(capsys.readouterr().out)]
    assert "acme.orders" in ids and "local.squares" not in ids


# -- entry-point package (examples/entrypoint_pkg) --------------------------------------------


def test_example_entry_point_package() -> None:
    try:
        import databolt_example_patterns  # noqa: F401
    except ImportError:
        if os.environ.get("DATABOLT_REQUIRE_EXAMPLE_PKG") == "1":
            raise
        pytest.skip("pip install -e examples/entrypoint_pkg to run this test")
    reg = default_registry()
    countdown = reg.get("example.countdown", "^1")
    assert countdown.origin == "package"
    assert countdown.provider_name == "databolt-example-patterns"
    assert countdown.provider_version == "0.1.0"
    assert reg.get("example.static_rows").origin == "package"
    from databolt.patterns import run

    res = run(countdown, {"start": 3}, preview=True)
    assert res.records == [{"n": 3}, {"n": 2}, {"n": 1}]
