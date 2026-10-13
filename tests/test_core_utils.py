from __future__ import annotations

import json
from pathlib import Path

import pytest

from databolt.patterns import semver
from databolt.patterns.errors import TemplateError, ValidationError
from databolt.patterns.redact import MASK, Redactor, redact_url
from databolt.patterns.selectors import get_path, select_records
from databolt.patterns.state import commit_state, load_state
from databolt.patterns.templating import has_placeholders, references, render

# -- templating ---------------------------------------------------------------------------

SCOPE = {
    "params": {"limit": 100, "region": "eu", "flags": {"debug": True}},
    "secrets": {"token": "s3cr3t-value"},
    "state": {"cursor": "2026-01-01T00:00:00Z"},
}


def test_whole_placeholder_keeps_type() -> None:
    assert render("${params.limit}", SCOPE) == 100
    assert render("${params.flags}", SCOPE) == {"debug": True}


def test_interpolation_stringifies() -> None:
    assert render("https://${params.region}.api/x?l=${params.limit}", SCOPE) == (
        "https://eu.api/x?l=100"
    )
    assert render("Bearer ${secrets.token}", SCOPE) == "Bearer s3cr3t-value"
    assert render("${params.flags.debug}-x", SCOPE) == "true-x"


def test_render_recurses_and_escapes() -> None:
    out = render({"a": ["${state.cursor}", "$${params.limit}"], "b": 1}, SCOPE)
    assert out == {"a": ["2026-01-01T00:00:00Z", "${params.limit}"], "b": 1}


def test_partial_render_leaves_other_roots() -> None:
    out = render({"u": "${params.region}", "t": "${secrets.token}"}, SCOPE, only={"params"})
    assert out == {"u": "eu", "t": "${secrets.token}"}


def test_missing_reference_does_not_echo_values() -> None:
    with pytest.raises(TemplateError, match=r"secrets\.nope"):
        render("${secrets.nope}", SCOPE)
    with pytest.raises(TemplateError, match="unknown template root"):
        render("${env.HOME}", SCOPE)


def test_references_and_detection() -> None:
    cfg = {"auth": {"token": "${secrets.token}"}, "h": "x ${secrets.other.y} ${params.a}"}
    assert references(cfg, "secrets") == {"token", "other"}
    assert has_placeholders(cfg) and not has_placeholders({"a": "plain"})


# -- selectors (parity with Studio's applyJsonPath) ---------------------------------------

DOC = {
    "data": {"items": [{"id": 1}, {"id": 2}]},
    "meta": {"count": 2},
    "groups": [{"rows": [{"a": 1}]}, {"rows": [{"a": 2}, {"a": 3}]}],
    "tags": ["x", "y"],
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("data.items", [{"id": 1}, {"id": 2}]),
        ("$.data.items", [{"id": 1}, {"id": 2}]),
        ("$data.items", [{"id": 1}, {"id": 2}]),
        ("meta", [{"count": 2}]),
        ("meta.count", [{"value": 2}]),
        ("tags", [{"value": "x"}, {"value": "y"}]),
        ("groups[*].rows", [{"a": 1}, {"a": 2}, {"a": 3}]),
        ("groups[1].rows", [{"a": 2}, {"a": 3}]),
        ("groups.0.rows", [{"a": 1}]),
        ("missing.path", []),
    ],
)
def test_select_records(path: str, expected: list[dict[str, object]]) -> None:
    assert select_records(DOC, path) == expected


def test_select_root() -> None:
    assert select_records([{"a": 1}], None) == [{"a": 1}]
    assert select_records([{"a": 1}], "$") == [{"a": 1}]
    assert select_records({"a": 1}, "") == [{"a": 1}]


def test_get_path() -> None:
    assert get_path(DOC, "meta.count") == 2
    assert get_path(DOC, "data.items[1].id") == 2
    assert get_path(DOC, "nope") is None
    assert get_path(DOC, "groups[*].rows[0].a") == [1, 2]


# -- redaction ----------------------------------------------------------------------------


def test_redactor_masks_values_and_encodings() -> None:
    r = Redactor(["p@ss word!", "ab"])  # "ab" is too short to mask
    assert r("auth failed for p@ss word!") == f"auth failed for {MASK}"
    assert r("q=p%40ss%20word%21") == f"q={MASK}"
    assert r("q=p%40ss+word%21") == f"q={MASK}"
    assert r("about") == "about"


def test_redactor_basic_auth() -> None:
    r = Redactor()
    r.add_basic("user", "hunter22")
    assert MASK in r("Authorization: Basic dXNlcjpodW50ZXIyMg==")


def test_redact_url() -> None:
    assert redact_url("https://u:p@api.x.com/v1/items?token=abc#f") == "https://api.x.com/v1/items"
    assert redact_url("not a url?x=1") == "not a url"
    assert redact_url(None) == ""


# -- semver -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "spec", "ok"),
    [
        ("1.2.3", None, True),
        ("1.2.3", "*", True),
        ("1.2.3", "^1", True),
        ("2.0.0", "^1", False),
        ("1.2.3", "^1.2", True),
        ("1.1.9", "^1.2", False),
        ("0.2.5", "^0.2", True),
        ("0.3.0", "^0.2", False),
        ("1.2.9", "~1.2", True),
        ("1.3.0", "~1.2", False),
        ("1.2.3", "1.2.3", True),
        ("1.2.4", "1.2.3", False),
        ("1.2.4", "1.2", True),
        ("1.5.0", ">=1.2 <2", True),
        ("2.1.0", ">=1.2 <2", False),
        ("3.0.0", "^1 || ^3", True),
    ],
)
def test_semver(version: str, spec: str | None, ok: bool) -> None:
    assert semver.satisfies(version, spec) is ok


def test_semver_invalid() -> None:
    with pytest.raises(ValidationError):
        semver.parse("one.two")


# -- state --------------------------------------------------------------------------------


def test_state_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "s" / "state.json"
    assert load_state(p) is None
    commit_state(p, {"cursor": "2026-01-01"})
    assert load_state(p) == {"cursor": "2026-01-01"}
    assert json.loads(p.read_text())["cursor"] == "2026-01-01"
    assert [f.name for f in p.parent.iterdir()] == ["state.json"]


def test_state_bad_file(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text("[1]")
    with pytest.raises(ValidationError):
        load_state(p)
    p.write_text("{nope")
    with pytest.raises(ValidationError):
        load_state(p)
