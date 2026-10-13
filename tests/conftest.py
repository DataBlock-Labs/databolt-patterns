from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from databolt.patterns.limits import Budget, Limits
from databolt.patterns.net.client import HttpClient
from databolt.patterns.net.guard import EgressGuard, EgressPolicy, Resolver
from databolt.patterns.redact import Redactor


class Sleeps(list[float]):
    """Records requested sleeps instead of sleeping."""

    def __call__(self, seconds: float) -> None:
        self.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def make_client(sleeps: Sleeps) -> Callable[..., HttpClient]:
    """An HttpClient that may reach the local test server and never really sleeps."""
    clients: list[HttpClient] = []

    def factory(
        *,
        allow_private: bool = True,
        blocked_cidrs: list[str] | None = None,
        resolver: Resolver | None = None,
        redactor: Redactor | None = None,
        max_redirects: int = 5,
        **limits: Any,
    ) -> HttpClient:
        policy = EgressPolicy(
            allow_private=allow_private,
            blocked_cidrs=blocked_cidrs,
            max_redirects=max_redirects,
            trust_env=False,
        )
        c = HttpClient(
            EgressGuard(policy, resolver=resolver),
            Budget(Limits(**limits)),
            redactor or Redactor(),
            sleep=sleeps,
        )
        clients.append(c)
        return c

    yield factory  # type: ignore[misc]
    for c in clients:
        c.close()


# A blocklist without 127.0.0.0/8, so the local test server counts as "public" while
# everything else private stays blocked. Lets redirect-to-private tests run offline.
BLOCKLIST_WITHOUT_LOOPBACK = [
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "224.0.0.0/3",
    "::/128",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
]


@pytest.fixture(scope="session")
def httpserver_listen_address() -> tuple[str, int]:
    # An IP literal, so URLs are http://127.0.0.1:<port>/ and `localhost` stays free to
    # act as a second origin in the cross-origin redirect tests.
    return ("127.0.0.1", 0)
