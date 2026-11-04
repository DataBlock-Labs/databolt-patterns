"""The kernel bridge (spec 027) against a fake Jupyter kernel: no container, no ZMQ."""

from __future__ import annotations

import io
import json
import queue
import threading
import time
from typing import Any

from databolt.kernel import PARAMS_PROMPT, SQL_PROMPT, TABLE_MIME, Table
from databolt.kernel.bridge import MAX_LINE, Bridge, read_lines
from databolt.kernel.helper import table_bundle


class FakeKernel:
    """Speaks the Jupyter message shapes the bridge reads, scripted per cell."""

    def __init__(self) -> None:
        self.q = {"iopub": queue.Queue(), "stdin": queue.Queue(), "shell": queue.Queue()}
        self.answers: list[str] = []
        self.busy: str | None = None
        self.n = 0

    def put(self, ch: str, kind: str, content: dict[str, Any]) -> None:
        msg = {"msg_type": kind, "content": content, "parent_header": {"msg_id": self.mid}}
        self.q[ch].put(msg)

    def end(self, status: str = "ok") -> None:
        self.put("iopub", "status", {"execution_state": "idle"})
        self.put("shell", "execute_reply", {"status": status})

    def execute(self, code: str) -> str:
        self.n += 1
        self.mid = f"m{self.n}"
        if code == "print('hi')":
            self.put("iopub", "stream", {"name": "stdout", "text": "hi\n"})
            self.end()
        elif code.startswith("sql"):
            q = json.dumps({"sql": "SELECT 1 AS x", "params": {}})
            self.put("stdin", "input_request", {"prompt": SQL_PROMPT + q})
            self.busy = "sql"
        elif code == "widgets":
            self.put("stdin", "input_request", {"prompt": PARAMS_PROMPT})
            self.busy = "widgets"
        elif code == "input()":
            self.put("stdin", "input_request", {"prompt": "name? "})
            self.busy = "input"
        elif code == "1/0":
            self.put(
                "iopub", "error", {"ename": "ZeroDivisionError", "evalue": "x", "traceback": []}
            )
            self.end("error")
        elif code == "loop":
            self.busy = "loop"
        return self.mid

    def answer(self, value: str) -> None:
        self.answers.append(value)
        if self.busy in ("sql", "widgets"):
            self.put("iopub", "execute_result", {"data": {"text/plain": value}})
        self.end()
        self.busy = None

    def interrupt(self) -> None:
        self.put("iopub", "error", {"ename": "KeyboardInterrupt", "evalue": "", "traceback": []})
        self.end("error")

    def _get(self, ch: str, timeout: float) -> dict[str, Any] | None:
        try:
            return self.q[ch].get(timeout=timeout) if timeout else self.q[ch].get_nowait()
        except queue.Empty:
            return None

    def iopub(self, timeout: float) -> dict[str, Any] | None:
        return self._get("iopub", timeout)

    def stdin(self, timeout: float) -> dict[str, Any] | None:
        return self._get("stdin", timeout)

    def shell(self, timeout: float) -> dict[str, Any] | None:
        return self._get("shell", timeout)


def run(code: str, inbox_msgs: list[dict], can_interrupt: bool = True, params=None):
    k, out, inbox = FakeKernel(), io.StringIO(), queue.Queue()
    b = Bridge(k, out, inbox, can_interrupt)
    for m in inbox_msgs:
        inbox.put(m)
    t = threading.Thread(target=b.execute, args=(code, params or {}), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "the cell never ended"
    return k, [json.loads(line) for line in out.getvalue().splitlines()]


def test_stdout_streams_then_done() -> None:
    _, out = run("print('hi')", [])
    assert out == [
        {"op": "output", "type": "stream", "content": {"name": "stdout", "text": "hi\n"}},
        {"op": "done", "status": "ok"},
    ]


def test_an_error_is_relayed_and_the_cell_ends_in_error() -> None:
    _, out = run("1/0", [])
    assert out[0]["type"] == "error" and out[0]["content"]["ename"] == "ZeroDivisionError"
    assert out[-1] == {"op": "done", "status": "error"}


def test_sql_asks_studio_and_answers_the_kernel() -> None:
    reply = {"op": "sql_result", "ok": True, "columns": ["x"], "rows": [[1]]}
    k, out = run("sql()", [reply])
    assert out[0] == {"op": "sql", "sql": "SELECT 1 AS x", "params": {}}
    assert json.loads(k.answers[0]) == {"ok": True, "columns": ["x"], "rows": [[1]]}
    assert out[-1] == {"op": "done", "status": "ok"}


def test_widgets_are_answered_from_the_cell_params() -> None:
    k, _ = run("widgets", [], params={"day": "2026-11-03"})
    assert json.loads(k.answers[0]) == {"day": "2026-11-03"}


def test_a_user_input_gets_an_empty_answer_and_a_note() -> None:
    k, out = run("input()", [])
    assert k.answers == [""]
    assert "not supported" in out[0]["content"]["text"]


def test_interrupt_stops_a_running_cell() -> None:
    k, out, inbox = FakeKernel(), io.StringIO(), queue.Queue()
    b = Bridge(k, out, inbox, True)
    t = threading.Thread(target=b.execute, args=("loop", {}), daemon=True)
    t.start()
    time.sleep(0.2)
    inbox.put({"op": "interrupt"})
    t.join(5)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert {"op": "interrupted", "ok": True} in lines
    assert lines[-1] == {"op": "done", "status": "error"}


def test_a_kernel_that_cannot_interrupt_says_so() -> None:
    k, out, inbox = FakeKernel(), io.StringIO(), queue.Queue()
    b = Bridge(k, out, inbox, can_interrupt=False)
    t = threading.Thread(target=b.execute, args=("loop", {}), daemon=True)
    t.start()
    time.sleep(0.2)
    inbox.put({"op": "interrupt"})
    time.sleep(0.2)
    inbox.put({"op": "close"})
    t.join(5)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[0]["op"] == "interrupted" and lines[0]["ok"] is False
    assert lines[-1] == {"op": "done", "status": "aborted"}


def test_an_oversized_output_is_dropped_with_a_note() -> None:
    out = io.StringIO()
    Bridge(FakeKernel(), out, queue.Queue(), True).send(
        {"op": "output", "type": "stream", "content": {"text": "x" * MAX_LINE}}
    )
    assert "dropped" in json.loads(out.getvalue())["content"]["text"]


def test_bad_lines_close_the_session() -> None:
    for bad in ["not json\n", "[1]\n", "x" * (MAX_LINE + 2)]:
        inbox: queue.Queue = queue.Queue()
        read_lines(io.StringIO(bad), inbox)
        assert inbox.get_nowait() == {"op": "close"}


def test_a_table_bundle_carries_the_table_mime() -> None:
    b = table_bundle(Table(["a"], [[1]]), chart={"x": "a", "y": "a"})
    assert b is not None and b[TABLE_MIME]["rows"] == [[1]] and b[TABLE_MIME]["chart"]["x"] == "a"
    assert table_bundle([{"a": 1}])[TABLE_MIME]["columns"] == ["a"]  # type: ignore[index]
    assert table_bundle(42) is None
