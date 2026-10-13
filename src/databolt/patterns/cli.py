"""The `databolt-patterns` command line (also `python -m databolt.patterns`)."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from collections.abc import Sequence

from databolt.patterns._version import __version__
from databolt.patterns.protocol import PROTOCOL_VERSION

DIST_NAME = "databolt-standard-patterns"


def _version(args: argparse.Namespace) -> int:
    if args.json:
        info = {
            "package": DIST_NAME,
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "python": platform.python_version(),
        }
        print(json.dumps(info))
    else:
        print(f"{DIST_NAME} {__version__} (protocol {PROTOCOL_VERSION})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="databolt-patterns",
        description="Run and inspect DataBolt ingestion patterns.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("version", help="print the package and protocol version")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=_version)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc: int = args.func(args)
    sys.stdout.flush()
    return rc
