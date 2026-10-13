"""databolt.patterns: the framework behind DataBolt ingestion patterns."""

from databolt.patterns._version import __version__
from databolt.patterns.protocol import PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION", "__version__"]
