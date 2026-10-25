import tomllib
from pathlib import Path

import agents_common

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_package_imports_and_reports_version():
    expected = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    assert agents_common.__version__ == expected
