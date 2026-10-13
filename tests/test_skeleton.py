import json
import subprocess
import sys
from pathlib import Path

import databolt
from databolt.ingestion import api
from databolt.patterns import PROTOCOL_VERSION, __version__
from databolt.patterns.cli import main


def test_databolt_is_a_namespace_package() -> None:
    # PEP 420: no dist may own databolt/__init__.py, so `databolt` has no __file__.
    assert getattr(databolt, "__file__", None) is None
    assert not any((Path(p) / "__init__.py").exists() for p in databolt.__path__)


def test_api_importable() -> None:
    assert api.__version__ == __version__


def test_version_json(capsys) -> None:
    assert main(["version", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["version"] == __version__
    assert info["protocol"] == PROTOCOL_VERSION == 1


def test_module_entrypoint() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "databolt.patterns", "version", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout)["package"] == "databolt-standard-patterns"
