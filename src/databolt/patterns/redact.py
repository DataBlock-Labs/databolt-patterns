"""Keep secret values out of anything a caller can read: errors, logs, warnings."""

from __future__ import annotations

import base64
from collections.abc import Iterable
from typing import TextIO
from urllib.parse import quote, quote_plus, urlsplit

MASK = "***"
# Values shorter than this are not masked: replacing "1" or "id" everywhere would
# mangle messages without protecting anything real.
MIN_SECRET_LEN = 4


class Redactor:
    """Replaces every known secret value (and its common encodings) with ***."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._values: set[str] = set()
        for s in secrets:
            self.add(s)

    def add(self, value: object) -> None:
        if not isinstance(value, str) or len(value) < MIN_SECRET_LEN:
            return
        self._values.update({value, quote(value, safe=""), quote_plus(value)})
        self._values.add(base64.b64encode(value.encode()).decode())

    def add_basic(self, username: str, password: str) -> None:
        """Mask the base64 `user:pass` an http_basic header carries."""
        self.add(password)
        self.add(base64.b64encode(f"{username}:{password}".encode()).decode())

    def __call__(self, text: str) -> str:
        # Longest first, so a secret that contains another is masked whole.
        for v in sorted(self._values, key=len, reverse=True):
            if v in text:
                text = text.replace(v, MASK)
        return text


def redact_url(raw: str | None) -> str:
    """A URL reduced to scheme://host/path; query strings and userinfo often carry tokens.

    Mirrors Studio's `redactUrl` so `source_label` reads the same on both sides.
    """
    if not raw:
        return ""
    try:
        u = urlsplit(raw)
    except ValueError:
        return raw.split("?")[0].split("#")[0]
    if not u.scheme or not u.netloc:
        return raw.split("?")[0].split("#")[0]
    host = u.netloc.rsplit("@", 1)[-1]
    return f"{u.scheme}://{host}{u.path}"


class RedactingStream:
    """A text stream wrapper that redacts each write; used to divert print() to stderr."""

    def __init__(self, target: TextIO, redactor: Redactor) -> None:
        self._target = target
        self._redactor = redactor

    def write(self, s: str) -> int:
        self._target.write(self._redactor(s))
        return len(s)

    def flush(self) -> None:
        self._target.flush()

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return getattr(self._target, "encoding", "utf-8") or "utf-8"
