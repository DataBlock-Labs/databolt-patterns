from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import databolt.patterns.registry as registry_mod
from databolt.patterns import Registry
from databolt.patterns.cli import main
from tests import fixture_patterns as fp


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    def make(**_: Any) -> Registry:
        r = Registry()
        for p in (fp.numbers, fp.tracked, fp.fetch):
            r.register(p)
        return r

    monkeypatch.setattr(registry_mod, "default_registry", make)


def out_json(capsys: pytest.CaptureFixture[str]) -> Any:
    return json.loads(capsys.readouterr().out)


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version", "--json"]) == 0
    v = out_json(capsys)
    assert v["protocol"] == 1 and v["manifest_version"] == 1
    assert main(["version"]) == 0
    assert "protocol 1" in capsys.readouterr().out


def test_list_and_describe(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list", "--json"]) == 0
    assert [r["id"] for r in out_json(capsys)] == ["test.fetch", "test.numbers", "test.tracked"]
    assert main(["list"]) == 0
    assert "test.numbers" in capsys.readouterr().out
    assert main(["describe", "test.numbers", "--json"]) == 0
    assert out_json(capsys)["id"] == "test.numbers"


def test_manifest_to_file(tmp_path: Path) -> None:
    out = tmp_path / "m" / "manifest.json"
    assert main(["manifest", "--json", "--out", str(out)]) == 0
    assert len(json.loads(out.read_text())["patterns"]) == 3


def test_run_with_params_and_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state.json"
    params = tmp_path / "p.yaml"
    params.write_text("count: 2\n")
    args = ["run", "test.tracked", "-c", str(params), "--state", str(state)]
    assert main([*args, "--param", "count=4", "--out", str(tmp_path / "out")]) == 0
    res = out_json(capsys)
    assert res["rows"] == 4
    assert json.loads(state.read_text()) == {"cursor": 3}
    assert Path(res["files"][0]["path"]).exists()


def test_run_no_commit_and_preview(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state.json"
    assert main(["run", "test.tracked", "--state", str(state), "--no-commit",
                 "--out", str(tmp_path)]) == 0  # fmt: skip
    capsys.readouterr()
    assert not state.exists()
    assert main(["run", "test.numbers", "--preview", "--param", "count=3"]) == 0
    assert out_json(capsys)["records"] == [{"n": 0}, {"n": 1}, {"n": 2}]


def test_secret_from_env_is_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_TOKEN", "env-secret-xyz")
    rc = main(["run", "test.fetch", "--param", "url=http://127.0.0.1:9/?t=env-secret-xyz",
               "--secret", "token=@env:TEST_TOKEN", "--out", str(tmp_path)])  # fmt: skip
    assert rc == 3
    err = capsys.readouterr().err
    assert "egress_blocked" in err
    assert "env-secret-xyz" not in err


def test_errors_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "nope"]) == 2
    assert "pattern_not_found" in capsys.readouterr().err
    assert main(["run", "test.numbers", "--param", "count=-1"]) == 2
    assert main(["run", "test.fetch", "--param", "url=x", "--secret", "token=@env:NOPE_X"]) == 2
    assert main(["validate", "params.txt"]) == 2


def test_validate_params_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "p.yaml"
    f.write_text("url: https://example.com\n")
    assert main(["validate", str(f), "--pattern", "test.fetch"]) == 0
    assert out_json(capsys)["required_secrets"] == ["token"]
