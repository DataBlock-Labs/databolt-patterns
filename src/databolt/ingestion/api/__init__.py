"""databolt.ingestion.api: the standard library of REST ingestion patterns.

from databolt.ingestion import api

src = api.rest(
    base_url="https://pokeapi.co/api/v2/",
    path="pokemon",
    params={"limit": 100},
    data_selector="results",
    paginator="auto",
    max_pages=3,
)
src.to_ndjson("out/")
"""

from databolt.ingestion.api import auth, paginators
from databolt.ingestion.api.config import ClientConfig, ResourceConfig, ResponseAction, RestConfig
from databolt.ingestion.api.http_get import HttpGetConfig, HttpGetPattern, http_get
from databolt.ingestion.api.incremental import IncrementalConfig
from databolt.ingestion.api.rest import RestApiPattern, rest
from databolt.patterns._version import __version__

__all__ = [
    "ClientConfig",
    "HttpGetConfig",
    "HttpGetPattern",
    "IncrementalConfig",
    "ResourceConfig",
    "ResponseAction",
    "RestApiPattern",
    "RestConfig",
    "__version__",
    "auth",
    "http_get",
    "paginators",
    "rest",
]
