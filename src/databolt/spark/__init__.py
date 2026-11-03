"""Studio's Spark worker (spec 023): one Spark session, driven by JSON lines.

Studio starts `python -I -m databolt.spark` and writes one JSON object per line
on stdin; the worker answers one line on stdout:

    {"op": "open", "session": {...}}       -> {"ok": true, "version": "4.1.0"}
    {"op": "execute", "sql": "..."}        -> {"ok": true}
    {"op": "sql", "sql": "...", "limit": n}
                                           -> {"ok": true, "columns": [...], "rows": [[...]]}
    {"op": "columns", "table": "`s`.`t`"}
                                           -> {"ok": true, "columns": [{"name", "type", ...}]}
    {"op": "recipe", "file", "sha256", "function", "inputs": {id: table}, "output", "params"}
                                           -> {"ok": true, "rows": n}
    {"op": "close"}                        -> {"ok": true}

A failure answers {"ok": false, "error": "..."} and the worker keeps serving.

The same code drives two kinds of session. `connect` is a Spark Connect server:
a local container or a cloud endpoint (`sc://host:port`). `databricks` is
Databricks Connect, on a cluster or serverless. pyspark and databricks-connect
conflict, so each kind runs in its own environment, and both import lazily.

The token arrives inside the `open` request on stdin, never in argv or the
environment. It is masked out of every error the worker sends.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from databolt.patterns.redact import Redactor

Opener = Callable[[dict[str, Any]], Any]


class SparkMissing(RuntimeError):
    """The Spark client for this session kind is not installed."""


# `token=…` (or any `;key=value` parameter) that an error message could echo.
_URL_PARAM = re.compile(r"(;\s*[A-Za-z_]+\s*=)[^;\s'\"]*")


def plain_remote(remote: str) -> str:
    """`sc://host[:port]`, refusing parameters: the token comes only in `token`."""
    if not remote.startswith("sc://"):
        raise ValueError("a Spark Connect remote starts with sc://")
    if ";" in remote or "@" in remote:
        raise ValueError(
            "a Spark Connect remote is sc://host:port only; put the token in the token field"
        )
    return remote


def connect_url(remote: str, token: str | None) -> str:
    """The Spark Connect URL: Spark takes the token as a URL parameter (it turns on TLS)."""
    return f"{remote.rstrip('/')}/;token={token}" if token else remote


def mask_url_params(text: str) -> str:
    """Masks every `;key=value` parameter of a Spark Connect URL in `text`."""
    return _URL_PARAM.sub(r"\1***", text)


def open_session(spec: dict[str, Any]) -> Any:
    """A Spark session for a `connect` or `databricks` spec."""
    kind = spec.get("kind")
    token = spec.get("token") or None
    if kind == "connect":
        remote = plain_remote(str(spec.get("remote", "")))
        try:
            from pyspark.sql import SparkSession
        except ImportError as e:
            raise SparkMissing(
                "pyspark is not installed: install the `spark` extra "
                "(databolt-standard-patterns[spark])"
            ) from e
        # Spark Connect takes the token as a URL parameter; it turns on TLS.
        url = connect_url(remote, token)
        return SparkSession.builder.remote(url).getOrCreate()
    if kind == "databricks":
        try:
            from databricks.connect import DatabricksSession
        except ImportError as e:
            raise SparkMissing(
                "databricks-connect is not installed: install the `dbconnect` extra; "
                "its version must match your cluster's Databricks Runtime"
            ) from e
        args: dict[str, Any] = {"host": spec["host"], "token": token}
        if spec.get("cluster_id"):
            args["cluster_id"] = spec["cluster_id"]
        else:
            # shortcut: the serverless keyword needs databricks-connect 15.1 or later;
            # check it against the pinned version on the first live run.
            args["serverless"] = True
        return DatabricksSession.builder.remote(**args).getOrCreate()
    raise ValueError(f"unknown Spark session kind {kind!r}")


def _cell(v: Any) -> Any:
    if v is None or isinstance(v, bool | int | float | str):
        return v
    if isinstance(v, decimal.Decimal | _dt.date | _dt.datetime):
        return str(v)
    if isinstance(v, list | tuple):
        return [_cell(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _cell(x) for k, x in v.items()}
    return str(v)


def _load(file: str, sha256: str, function: str) -> Callable[..., Any]:
    """The recipe function, only if the file's bytes are the ones Studio pinned."""
    data = Path(file).read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f"{file} changed since Studio pinned it")
    spec = importlib.util.spec_from_file_location("databolt_spark_recipe", file)
    if spec is None or spec.loader is None:
        raise ValueError(f"{file} is not a Python file")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, function, None)
    if not callable(fn):
        raise ValueError(f"{file} has no function {function}")
    return fn  # type: ignore[no-any-return]


class Worker:
    def __init__(self, opener: Opener = open_session) -> None:
        self.opener = opener
        self.spark: Any = None
        self.kind = ""
        self.redact = Redactor()

    def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        if op == "open":
            spec = dict(req.get("session") or {})
            token = spec.get("token") or None
            self.redact.add(token)
            if spec.get("kind") == "connect" and token:
                self.redact.add(connect_url(str(spec.get("remote", "")), token))
            self.kind = str(spec.get("kind", ""))
            self.spark = self.opener(spec)
            return {"ok": True, "version": str(getattr(self.spark, "version", ""))}
        if op == "close":
            if self.spark is not None:
                self.spark.stop()
                self.spark = None
            return {"ok": True}
        if self.spark is None:
            raise ValueError("open a session first")
        if op == "execute":
            self.spark.sql(str(req["sql"])).collect()
            return {"ok": True}
        if op == "sql":
            df = self.spark.sql(str(req["sql"]))
            limit = int(req.get("limit") or 0)
            rows = df.limit(limit).collect() if limit > 0 else []
            return {
                "ok": True,
                "columns": list(df.columns),
                "rows": [[_cell(v) for v in r] for r in rows],
            }
        if op == "columns":
            try:
                fields = self.spark.table(str(req["table"])).schema.fields
            except Exception as e:
                # A table that does not exist yet has no columns.
                if "TABLE_OR_VIEW_NOT_FOUND" in str(e):
                    return {"ok": True, "columns": []}
                raise
            return {
                "ok": True,
                "columns": [
                    {"name": f.name, "type": f.dataType.simpleString(), "nullable": f.nullable}
                    for f in fields
                ],
            }
        if op == "recipe":
            fn = _load(str(req["file"]), str(req["sha256"]), str(req["function"]))
            inputs = {k: self.spark.table(v) for k, v in dict(req.get("inputs") or {}).items()}
            df = fn(self.spark, inputs, dict(req.get("params") or {}))
            writer = df.write.mode("overwrite")
            # Databricks tables are Delta already; OSS Spark's default is Parquet.
            if self.kind == "connect":
                writer = writer.format("delta")
            writer.saveAsTable(str(req["output"]))
            return {"ok": True, "rows": int(self.spark.table(str(req["output"])).count())}
        raise ValueError(f"unknown op {op!r}")

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                out = self.handle(json.loads(line))
            except Exception as e:  # every failure is an answer, never a crash
                out = {
                    "ok": False,
                    "error": mask_url_params(self.redact(f"{type(e).__name__}: {e}")),
                }
            stdout.write(json.dumps(out, default=str) + "\n")
            stdout.flush()
            if out.get("ok") and json.loads(line).get("op") == "close":
                return


def main() -> None:
    Worker().serve(sys.stdin, sys.stdout)
