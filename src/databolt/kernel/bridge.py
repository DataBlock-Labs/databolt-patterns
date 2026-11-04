"""`python -I -m databolt.kernel.bridge --kernel <spec>`: a Jupyter kernel on stdio.

Studio talks to this bridge in JSON lines, one object per line, at most 8 MiB:

    Studio -> bridge   {"op": "execute", "code": "...", "params": {...}}
                       {"op": "interrupt"}
                       {"op": "sql_result", "ok": true, "columns": [...], "rows": [...]}
                       {"op": "close"}
    bridge -> Studio   {"op": "output", "type": "stream" | "display_data"
                                         | "execute_result" | "error", "content": {...}}
                       {"op": "sql", "sql": "...", "params": {...}}
                       {"op": "interrupted", "ok": bool, "error": "..."}
                       {"op": "done", "status": "ok" | "error" | "aborted"}

`content` is the Jupyter message content, cleaned by `clean_output`: a bundle keeps
only passive MIME types (`ALLOWED`), and an error traceback is plain text.
The kernel runs on loopback inside the container; Studio sees only stdio.
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import subprocess
import sys
import threading
from typing import Any, Protocol, TextIO

from databolt.kernel import PARAMS_PROMPT, SQL_PROMPT, TABLE_MIME

MAX_LINE = 8 * 1024 * 1024
# Jupyter messages the bridge passes to Studio.
RELAY = {"stream", "display_data", "execute_result", "error"}
# Kernels that ignore an interrupt (evcxr: a Rust thread cannot be stopped).
NO_INTERRUPT = {"rust"}

Inbox = queue.Queue[dict[str, Any]]

# MIME types a notebook output may carry: none of them is active content.
ALLOWED = {
    "text/plain",
    "text/markdown",
    "image/png",
    "image/jpeg",
    "application/json",
    TABLE_MIME,
}
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def clean_output(kind: str, content: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """The content Studio may show, and a note per dropped MIME type.

    Returns None for content when nothing is left to show.
    """
    if kind == "stream":
        name = "stderr" if content.get("name") == "stderr" else "stdout"
        return {"name": name, "text": str(content.get("text", ""))}, []
    if kind == "error":
        tb = content.get("traceback")
        lines = [strip_ansi(str(t)) for t in tb] if isinstance(tb, list) else []
        return {
            "ename": strip_ansi(str(content.get("ename", ""))),
            "evalue": strip_ansi(str(content.get("evalue", ""))),
            "traceback": lines,
        }, []
    raw = content.get("data")
    data = raw if isinstance(raw, dict) else {}
    keep = {k: v for k, v in data.items() if k in ALLOWED}
    notes = [f"dropped {k}: not allowed in notebook output" for k in data if k not in ALLOWED]
    meta = content.get("metadata")
    size: dict[str, Any] = {}
    for k in keep:
        m = meta.get(k) if isinstance(meta, dict) else None
        if k.startswith("image/") and isinstance(m, dict):
            dims = {d: m[d] for d in ("width", "height") if isinstance(m.get(d), int)}
            if dims:
                size[k] = dims
    if not keep:
        return None, notes
    out: dict[str, Any] = {"data": keep, "metadata": size}
    if kind == "execute_result":
        out["execution_count"] = content.get("execution_count")
    return out, notes


class Client(Protocol):
    """The kernel side: a jupyter_client BlockingKernelClient, or a test fake."""

    def execute(self, code: str) -> str: ...
    def iopub(self, timeout: float) -> dict[str, Any] | None: ...
    def stdin(self, timeout: float) -> dict[str, Any] | None: ...
    def shell(self, timeout: float) -> dict[str, Any] | None: ...
    def answer(self, value: str) -> None: ...
    def interrupt(self) -> None: ...


class Bridge:
    """Relays one kernel to Studio. `inbox` holds Studio's messages, read by a thread."""

    def __init__(self, client: Client, out: TextIO, inbox: Inbox, can_interrupt: bool):
        self.client = client
        self.out = out
        self.inbox = inbox
        self.can_interrupt = can_interrupt
        self.closed = False
        # A sql_result read while polling, kept for the request that waits for it.
        self.pending: dict[str, Any] | None = None

    def send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, default=str)
        if len(line) >= MAX_LINE:
            line = json.dumps(
                {
                    "op": "output",
                    "type": "stream",
                    "content": {"name": "stderr", "text": "An output over 8 MiB was dropped.\n"},
                }
            )
        self.out.write(line + "\n")
        self.out.flush()

    def relay(self, kind: str, content: Any) -> None:
        clean, notes = clean_output(kind, content if isinstance(content, dict) else {})
        if clean is not None:
            self.send({"op": "output", "type": kind, "content": clean})
        for note in notes:
            self.send(
                {
                    "op": "output",
                    "type": "stream",
                    "content": {"name": "stderr", "text": note + "\n"},
                }
            )

    def interrupt(self) -> None:
        if not self.can_interrupt:
            self.send(
                {
                    "op": "interrupted",
                    "ok": False,
                    "error": "This kernel cannot be interrupted; stop the session to end the cell.",
                }
            )
            return
        self.client.interrupt()
        self.send({"op": "interrupted", "ok": True})

    def poll_inbox(self) -> dict[str, Any] | None:
        """One message for the running cell, if any; handles interrupt and close."""
        try:
            msg: dict[str, Any] = self.inbox.get_nowait()
        except queue.Empty:
            return None
        op = msg.get("op")
        if op == "interrupt":
            self.interrupt()
            return None
        if op == "close":
            self.closed = True
        elif op == "sql_result":
            self.pending = msg
        return msg

    def ask_sql(self, request: dict[str, Any]) -> dict[str, Any]:
        """Sends a `sql` request and waits for Studio's answer."""
        self.send({"op": "sql", "sql": request.get("sql", ""), "params": request.get("params", {})})
        if self.pending is not None:
            msg, self.pending = self.pending, None
            return {k: v for k, v in msg.items() if k != "op"}
        while not self.closed:
            try:
                msg = self.inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            if msg.get("op") == "interrupt":
                self.interrupt()
            elif msg.get("op") == "close":
                self.closed = True
            elif msg.get("op") == "sql_result":
                return {k: v for k, v in msg.items() if k != "op"}
        return {"ok": False, "error": "the session closed"}

    def answer_input(self, prompt: str, params: dict[str, Any]) -> None:
        if prompt.startswith(SQL_PROMPT):
            try:
                req = json.loads(prompt[len(SQL_PROMPT) :])
            except json.JSONDecodeError:
                req = {}
            self.client.answer(json.dumps(self.ask_sql(req if isinstance(req, dict) else {})))
        elif prompt == PARAMS_PROMPT:
            self.client.answer(json.dumps(params))
        else:
            # A user input() has no one to answer it in a notebook run.
            self.client.answer("")
            self.send(
                {
                    "op": "output",
                    "type": "stream",
                    "content": {"name": "stderr", "text": "input() is not supported here.\n"},
                }
            )

    def execute(self, code: str, params: dict[str, Any]) -> None:
        msg_id = self.client.execute(code)

        def mine(m: dict[str, Any] | None) -> bool:
            return m is not None and m.get("parent_header", {}).get("msg_id") == msg_id

        idle, reply = False, None
        while not self.closed and not (idle and reply is not None):
            self.poll_inbox()
            m = self.client.stdin(0)
            if mine(m) and m["msg_type"] == "input_request":  # type: ignore[index]
                self.answer_input(str(m["content"].get("prompt", "")), params)  # type: ignore[index]
            m = self.client.iopub(0.05)
            if mine(m):
                kind = m["msg_type"]  # type: ignore[index]
                if kind in RELAY:
                    self.relay(kind, m["content"])  # type: ignore[index]
                elif kind == "status" and m["content"].get("execution_state") == "idle":  # type: ignore[index]
                    idle = True
            m = self.client.shell(0)
            if mine(m) and m["msg_type"] == "execute_reply":  # type: ignore[index]
                reply = m["content"]  # type: ignore[index]
        status = reply.get("status", "ok") if reply else "aborted"
        self.send({"op": "done", "status": status})

    def serve(self) -> None:
        while not self.closed:
            msg = self.inbox.get()
            op = msg.get("op")
            if op == "execute":
                params = msg.get("params")
                self.execute(str(msg.get("code", "")), params if isinstance(params, dict) else {})
            elif op == "interrupt":
                self.send({"op": "interrupted", "ok": True})
            elif op == "close":
                self.closed = True
            else:
                self.send({"op": "done", "status": "error"})


def read_lines(src: TextIO, inbox: Inbox) -> None:
    """Studio's lines into `inbox`; a bad or oversized line, or EOF, closes."""
    while True:
        line = src.readline(MAX_LINE + 1)
        if not line or len(line) > MAX_LINE:
            break
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            break
        if not isinstance(msg, dict):
            break
        inbox.put(msg)
    inbox.put({"op": "close"})


class JupyterClient:
    """A local Jupyter kernel through jupyter_client, on loopback."""

    def __init__(self, kernel: str) -> None:
        from jupyter_client import KernelManager

        self.km = KernelManager(kernel_name=kernel)
        # The kernel must not inherit the bridge's stdio: user code could write
        # protocol lines to Studio. Studio still treats every line as untrusted.
        null = subprocess.DEVNULL
        self.km.start_kernel(stdin=null, stdout=null, stderr=null)
        self.kc = self.km.blocking_client()
        self.kc.start_channels()
        self.kc.wait_for_ready(timeout=120)

    def _get(self, get: Any, timeout: float) -> dict[str, Any] | None:
        try:
            return dict(get(timeout=timeout))
        except queue.Empty:
            return None

    def execute(self, code: str) -> str:
        return str(self.kc.execute(code, allow_stdin=True))

    def iopub(self, timeout: float) -> dict[str, Any] | None:
        return self._get(self.kc.get_iopub_msg, timeout)

    def stdin(self, timeout: float) -> dict[str, Any] | None:
        return self._get(self.kc.get_stdin_msg, timeout)

    def shell(self, timeout: float) -> dict[str, Any] | None:
        return self._get(self.kc.get_shell_msg, timeout)

    def answer(self, value: str) -> None:
        self.kc.input(value)

    def interrupt(self) -> None:
        self.km.interrupt_kernel()

    def shutdown(self) -> None:
        self.kc.stop_channels()
        self.km.shutdown_kernel(now=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="databolt.kernel.bridge")
    ap.add_argument("--kernel", required=True, help="the Jupyter kernelspec name")
    args = ap.parse_args(argv)
    client = JupyterClient(args.kernel)
    inbox: Inbox = queue.Queue()
    threading.Thread(target=read_lines, args=(sys.stdin, inbox), daemon=True).start()
    try:
        Bridge(client, sys.stdout, inbox, args.kernel not in NO_INTERRUPT).serve()
    finally:
        client.shutdown()


if __name__ == "__main__":
    main()
