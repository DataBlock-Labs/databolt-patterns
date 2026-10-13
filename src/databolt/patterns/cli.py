"""The `databolt-patterns` command line (also `python -m databolt.patterns`)."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from databolt.patterns._version import __version__
from databolt.patterns.contract import Pattern
from databolt.patterns.errors import PatternError, ValidationError
from databolt.patterns.limits import Limits
from databolt.patterns.net.guard import EgressPolicy
from databolt.patterns.protocol import PROTOCOL_VERSION
from databolt.patterns.redact import Redactor
from databolt.patterns.registry import Registry

DIST_NAME = "databolt-standard-patterns"
CUSTOM_SUFFIXES = (".yaml", ".yml", ".py")


def _emit(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _registry(args: argparse.Namespace) -> Registry:
    from databolt.patterns.registry import default_registry

    reg = default_registry()
    for d in getattr(args, "custom", None) or []:
        reg.load_custom_dir(d, trust_python=getattr(args, "trust_python", False))
    return reg


def _resolve(reg: Registry, target: str, version: str | None = None) -> Pattern:
    """A pattern id, or a path to a custom .yaml/.py pattern file."""
    path = Path(target)
    if path.suffix in CUSTOM_SUFFIXES and path.exists():
        if path.suffix == ".py":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            p: Pattern = reg.load_python_file(str(path), entry=None, sha256=digest)
            return p
        return reg.load_yaml_file(str(path))
    return reg.get(target, version)


def _set_dotted(d: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    cur = d
    for k in keys[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[keys[-1]] = value


def _load_mapping(path: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise ValidationError(f"cannot read {path}: {e.strerror}") from None
    except yaml.YAMLError as e:
        raise ValidationError(f"{path} is not valid YAML/JSON: {e}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValidationError(f"{path} must contain a mapping")
    return data


def _params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if args.params_file:
        params = _load_mapping(args.params_file)
    for kv in args.param or []:
        k, sep, v = kv.partition("=")
        if not sep:
            raise ValidationError(f"--param expects key=value, got {kv!r}")
        _set_dotted(params, k.strip(), yaml.safe_load(v) if v else "")
    return params


def _secret_value(name: str, spec: str) -> str:
    if spec.startswith("@env:"):
        var = spec[5:]
        if var not in os.environ:
            raise ValidationError(f"--secret {name}: environment variable {var} is not set")
        return os.environ[var]
    if spec.startswith("@file:"):
        try:
            return Path(spec[6:]).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ValidationError(f"--secret {name}: cannot read file: {e.strerror}") from None
    return spec


def _secrets(args: argparse.Namespace) -> dict[str, str]:
    out: dict[str, str] = {}
    for kv in args.secret or []:
        k, sep, v = kv.partition("=")
        if not sep:
            raise ValidationError("--secret expects name=value, name=@env:VAR or name=@file:path")
        out[k.strip()] = _secret_value(k.strip(), v)
    return out


# -- commands ------------------------------------------------------------------------------


def cmd_version(args: argparse.Namespace) -> int:
    from databolt.patterns.manifest import MANIFEST_VERSION

    if args.json:
        _emit(
            {
                "package": DIST_NAME,
                "version": __version__,
                "protocol": PROTOCOL_VERSION,
                "manifest_version": MANIFEST_VERSION,
                "python": platform.python_version(),
            }
        )
    else:
        print(f"{DIST_NAME} {__version__} (protocol {PROTOCOL_VERSION})")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    reg = _registry(args)
    rows = [
        {
            "id": p.id,
            "version": p.version,
            "origin": p.origin,
            "provider": p.provider_name,
            "title": p.title,
        }
        for p in reg.all()
    ]
    if args.json:
        _emit(rows)
        return 0
    if not rows:
        print("no patterns found")
    width = max((len(r["id"]) for r in rows), default=0)
    for r in rows:
        print(f"{r['id']:<{width}}  {r['version']:<8}  {r['origin']:<8}  {r['title']}")
    for w in reg.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    from databolt.patterns.manifest import pattern_entry

    reg = _registry(args)
    _emit(pattern_entry(_resolve(reg, args.pattern)))
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    from databolt.patterns.manifest import build_manifest

    text = json.dumps(build_manifest(_registry(args)), indent=2, default=str) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    reg = _registry(args)
    if args.pattern:
        # Validate a params file against an installed pattern.
        p = _resolve(reg, args.pattern)
        parsed = p.parse_params(_load_mapping(args.file))
        _emit(
            {
                "ok": True,
                "pattern": {"id": p.id, "version": p.version},
                "required_secrets": sorted(p.required_secrets(parsed)),
            }
        )
        return 0
    path = Path(args.file)
    if path.suffix not in CUSTOM_SUFFIXES:
        raise ValidationError("validate expects a .yaml/.yml/.py pattern file (or --pattern ID)")
    p = _resolve(reg, str(path))
    _emit({"ok": True, "pattern": {"id": p.id, "version": p.version, "origin": p.origin}})
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from databolt.patterns.runtime import run
    from databolt.patterns.sinks import sink_for
    from databolt.patterns.state import commit_state, load_state

    reg = _registry(args)
    p = _resolve(reg, args.pattern, args.pattern_version)
    secrets = _secrets(args)
    redactor = Redactor(secrets.values())
    try:
        limits = Limits(
            max_pages=args.max_pages,
            max_records=args.max_records,
            max_seconds=args.max_seconds,
        )
    except Exception as e:
        raise ValidationError(f"invalid limits: {e}") from None
    egress = EgressPolicy(allow_private=args.allow_private)
    state = load_state(args.state)
    sink = None
    if not args.preview:
        stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in p.id)
        sink = sink_for(args.format, args.out, table=args.table, stem=stem)
    # stdout carries only the result JSON; anything the pattern prints goes to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        result = _run_cli(args, p, secrets, redactor, limits, egress, state, sink, run)
    if args.state and result.state_changed and not args.no_commit and not args.preview:
        commit_state(args.state, result.state)
    print(redactor(json.dumps(result.to_dict(), indent=2, default=str)))
    return 0


def _run_cli(
    args: argparse.Namespace,
    p: Pattern,
    secrets: dict[str, str],
    redactor: Redactor,
    limits: Limits,
    egress: EgressPolicy,
    state: dict[str, Any] | None,
    sink: Any,
    run: Any,
) -> Any:
    return run(
        p,
        _params(args),
        secrets=secrets,
        state=state,
        limits=limits,
        egress=egress,
        sink=sink,
        preview=args.preview,
        redactor=redactor,
    )


def cmd_protocol(args: argparse.Namespace) -> int:
    from databolt.patterns.protocol import main as protocol_main

    return protocol_main()


# -- parser --------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="databolt-patterns",
        description="Run and inspect DataBolt ingestion patterns.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def custom_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--custom",
            action="append",
            metavar="DIR",
            help="also load custom .yaml patterns from DIR (repeatable)",
        )
        p.add_argument(
            "--trust-python",
            action="store_true",
            help="with --custom, also execute .py patterns found there",
        )

    p = sub.add_parser("version", help="print the package and protocol version")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("list", help="list available patterns")
    p.add_argument("--json", action="store_true")
    custom_flag(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("describe", help="print one pattern's manifest entry")
    p.add_argument("pattern", help="pattern id or custom pattern file")
    p.add_argument("--json", action="store_true", help="(default) JSON output")
    custom_flag(p)
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("manifest", help="print the manifest for every available pattern")
    p.add_argument("--json", action="store_true", help="(default) JSON output")
    p.add_argument("--out", help="write to a file instead of stdout")
    custom_flag(p)
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("validate", help="validate a custom pattern file, or params for a pattern")
    p.add_argument("file", help="custom .yaml/.py pattern, or a params file with --pattern")
    p.add_argument("--pattern", help="validate FILE as params for this pattern id")
    custom_flag(p)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("run", help="run a pattern and write its records")
    p.add_argument("pattern", help="pattern id or custom pattern file")
    p.add_argument("--pattern-version", help="required version range, e.g. ^1")
    p.add_argument("-c", "--params-file", help="YAML or JSON file with the pattern's params")
    p.add_argument("--param", action="append", metavar="KEY=VALUE", help="set a param (dotted)")
    p.add_argument(
        "--secret",
        action="append",
        metavar="NAME=VALUE",
        help="a secret: NAME=@env:VAR (preferred), NAME=@file:PATH or NAME=VALUE",
    )
    p.add_argument("--state", help="state file: read before the run, committed after success")
    p.add_argument("--no-commit", action="store_true", help="never write the state file")
    p.add_argument("--out", default="out", help="output directory or file (default: ./out)")
    p.add_argument("--format", default="ndjson", choices=["ndjson", "parquet", "duckdb"])
    p.add_argument("--table", default="records", help="DuckDB table name")
    p.add_argument("--max-pages", type=int)
    p.add_argument("--max-records", type=int)
    p.add_argument("--max-seconds", type=float, default=300.0)
    p.add_argument(
        "--allow-private",
        action="store_true",
        help="allow loopback/private destinations (off by default)",
    )
    p.add_argument("--preview", action="store_true", help="one page, 50 records, printed")
    custom_flag(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("protocol", help="speak runner protocol v1 on stdin/stdout")
    p.set_defaults(func=cmd_protocol)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rc: int = args.func(args)
    except PatternError as e:
        print(f"error [{e.code}]: {e.message}", file=sys.stderr)
        return e.exit_code
    sys.stdout.flush()
    return rc
