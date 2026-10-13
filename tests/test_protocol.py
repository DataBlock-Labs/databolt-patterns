from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from databolt.patterns import PROTOCOL_VERSION, Registry
from databolt.patterns.protocol import handle
from tests import fixture_patterns as fp

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def reg() -> Registry:
    r = Registry()
    for p in (fp.numbers, fp.fetch, fp.tracked):
        r.register(p)
    return r


def req(**kw: Any) -> str:
    return json.dumps({"protocol": PROTOCOL_VERSION, **kw})


def test_run_op(reg: Registry, tmp_path: Path) -> None:
    result, code = handle(
        req(
            op="run",
            pattern={"id": "test.tracked", "version": "^1"},
            params={"count": 3},
            state={"cursor": -5},
            output={"dir": str(tmp_path), "format": "ndjson"},
        ),
        registry=reg,
    )
    assert code == 0, result
    assert result["ok"] is True and result["protocol"] == 1
    assert result["rows"] == 3 and result["state"] == {"cursor": 2} and result["state_changed"]
    (f,) = result["files"]
    assert set(f) == {"path", "rows", "bytes"} and f["rows"] == 3
    assert Path(f["path"]).parent == tmp_path
    expected_keys = {
        "ok", "protocol", "pattern", "files", "rows", "pages", "requests", "retries", "bytes",
        "paginator_detected", "source_label", "state", "state_changed", "truncated", "warnings",
        "elapsed_ms",
    }  # fmt: skip
    assert set(result) == expected_keys


def test_preview_op(reg: Registry) -> None:
    result, code = handle(
        req(op="preview", pattern={"id": "test.numbers"}, params={"count": 70}), registry=reg
    )
    assert code == 0 and len(result["records"]) == 50 and result["truncated"]
    assert result["files"] == []


def test_validate_op(reg: Registry) -> None:
    result, code = handle(
        req(op="validate", pattern={"id": "test.fetch"}, params={"url": "https://x.test"}),
        registry=reg,
    )
    assert code == 0
    assert result["required_secrets"] == ["token"]
    assert result["warnings"] == ["secret 'token' is not provided"]

    result, code = handle(
        req(op="validate", pattern={"id": "test.numbers"}, params={"count": "x"}), registry=reg
    )
    assert code == 2 and result["error"]["code"] == "validation_error"


def test_describe_op(reg: Registry) -> None:
    result, code = handle(req(op="describe"), registry=reg)
    assert code == 0
    assert [p["id"] for p in result["manifest"]["patterns"]] == [
        "test.fetch",
        "test.numbers",
        "test.tracked",
    ]
    result, code = handle(req(op="describe", pattern={"id": "test.numbers"}), registry=reg)
    assert result["manifest"]["id"] == "test.numbers"


@pytest.mark.parametrize(
    ("raw", "code", "error"),
    [
        ("{not json", 2, "invalid_request"),
        ("[1,2]", 2, "invalid_request"),
        (json.dumps({"protocol": 99, "op": "run"}), 4, "protocol_mismatch"),
        (json.dumps({"op": "run"}), 4, "protocol_mismatch"),
        (json.dumps({"protocol": 1, "op": "explode"}), 2, "invalid_request"),
        (json.dumps({"protocol": 1, "op": "run"}), 2, "invalid_request"),
        (
            json.dumps({"protocol": 1, "op": "run", "pattern": {"id": "nope"}}),
            2,
            "pattern_not_found",
        ),
        (
            json.dumps({"protocol": 1, "op": "run", "pattern": {"id": "test.numbers"}}),
            2,
            "invalid_request",
        ),
        (
            json.dumps(
                {
                    "protocol": 1,
                    "op": "describe",
                    "pattern": {"id": "test.numbers", "version": "^2"},
                }
            ),
            2,
            "pattern_version_mismatch",
        ),
        (
            json.dumps({"protocol": 1, "op": "run", "pattern": {"id": "a", "yaml": "b"}}),
            2,
            "validation_error",
        ),
    ],
)  # fmt: skip
def test_errors_and_exit_codes(reg: Registry, raw: str, code: int, error: str) -> None:
    result, rc = handle(raw, registry=reg)
    assert rc == code
    assert result["ok"] is False and result["error"]["code"] == error
    assert set(result["error"]) == {"code", "message", "http_status", "retryable"}


def test_runtime_error_exit_3_and_partial_files_removed(reg: Registry, tmp_path: Path) -> None:
    result, rc = handle(
        req(
            op="run",
            pattern={"id": "test.numbers"},
            params={"count": 5, "fail_at": 2},
            output={"dir": str(tmp_path)},
        ),
        registry=reg,
    )
    assert rc == 3 and result["error"]["code"] == "boom"
    assert list(tmp_path.iterdir()) == []


def test_secrets_never_reach_the_result(reg: Registry) -> None:
    secret = "sk-very-secret-123"
    result, rc = handle(
        req(
            op="run",
            pattern={"id": "test.fetch"},
            params={"url": f"http://127.0.0.1:9/?k={secret}"},
            secrets={"token": secret},
            output={"dir": "unused"},
        ),
        registry=reg,
    )
    assert rc == 3 and result["error"]["code"] == "egress_blocked"
    assert secret not in json.dumps(result)


def test_unknown_fields_are_ignored_with_a_warning(reg: Registry) -> None:
    result, rc = handle(req(op="describe", future_field=1), registry=reg)
    assert rc == 0
    assert result["warnings"] == ["ignored unknown request field(s): future_field"]


# -- golden: a real subprocess --------------------------------------------------------------

DRIVER = """
import sys
sys.path.insert(0, {root!r})
import databolt.patterns.registry as registry
from databolt.patterns import Registry
from databolt.patterns.protocol import main
from tests import fixture_patterns as fp

def fake_registry(**kw):
    r = Registry()
    r.register(fp.numbers)
    return r

registry.default_registry = fake_registry
raise SystemExit(main())
"""


def run_driver(request: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", DRIVER.format(root=str(ROOT))],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )


def test_golden_stdout_is_exactly_one_json_line(tmp_path: Path) -> None:
    proc = run_driver(
        {
            "protocol": 1,
            "op": "run",
            "pattern": {"id": "test.numbers"},
            "params": {"count": 2, "shout": True},
            "output": {"dir": str(tmp_path), "format": "ndjson"},
        }
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["ok"] and result["rows"] == 2
    # print() inside the pattern went to stderr, not stdout
    assert "hello from a pattern" in proc.stderr
    assert "hello" not in proc.stdout


def test_golden_protocol_mismatch_exit_4() -> None:
    proc = run_driver({"protocol": 2, "op": "describe"})
    assert proc.returncode == 4
    assert json.loads(proc.stdout)["error"]["code"] == "protocol_mismatch"


def test_golden_runtime_error_exit_3(tmp_path: Path) -> None:
    proc = run_driver(
        {
            "protocol": 1,
            "op": "run",
            "pattern": {"id": "test.numbers"},
            "params": {"count": 3, "fail_at": 1},
            "output": {"dir": str(tmp_path)},
        }
    )
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["error"]["message"] == "boom at 1"
    assert list(tmp_path.iterdir()) == []


def test_cli_protocol_subcommand() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "databolt.patterns", "protocol"],
        input='{"protocol": 1, "op": "describe"}',
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads(proc.stdout)["manifest"]
    assert manifest["manifest_version"] == 1 and manifest["protocol"] == 1


# -- Studio S1's exact request shape ---------------------------------------------------------

STUDIO_EGRESS = {
    "blocked_cidrs": [
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/3", "::/128", "::1/128", "fc00::/7",
        "fe80::/10", "fec0::/10", "ff00::/8",
    ],
    "unwrap_ipv4": ["::ffff:0:0/96", "::/96", "64:ff9b::/96"],
    "allow_private": False,
}  # fmt: skip


def test_studio_request_shape(reg: Registry, tmp_path: Path) -> None:
    out = tmp_path / "link"
    real = tmp_path / "real"
    real.mkdir()
    try:
        out.symlink_to(real, target_is_directory=True)
    except OSError:  # no symlink privilege (Windows); a plain dir still checks the prefix
        out = real
    result, rc = handle(
        req(
            op="run",
            run_id="0b7c-run",
            pattern={"id": "test.tracked", "version": "^1"},
            params={"count": 2},
            secrets={},
            state=None,
            limits={
                "total_timeout_s": 110,
                "max_response_bytes": 26214400,
                "max_total_bytes": 268435456,
            },
            egress=STUDIO_EGRESS,
            output={"dir": str(out), "format": "ndjson"},
        ),
        registry=reg,
    )
    assert rc == 0, result
    assert result["warnings"] == []
    (f,) = result["files"]
    assert f["path"].startswith(str(out))
    assert Path(f["path"]).name == "test_tracked-0b7c-run.ndjson"


def test_limit_aliases_and_unknown_keys(reg: Registry) -> None:
    from databolt.patterns.limits import Limits

    lim = Limits.model_validate({"total_timeout_s": 7, "max_total_bytes": 99})
    assert lim.max_seconds == 7
    assert lim.max_bytes == 99
    result, rc = handle(
        req(op="validate", pattern={"id": "test.numbers"}, limits={"warp": 9}, egress={"x": 1}),
        registry=reg,
    )
    assert rc == 0
    assert "ignored unknown limits field(s): warp" in result["warnings"]
    assert "ignored unknown egress field(s): x" in result["warnings"]
