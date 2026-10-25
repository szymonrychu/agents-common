import importlib
import re
import tomllib
from pathlib import Path

import agents_common

ROOT = Path(__file__).resolve().parent.parent
INSTALL = "agents-common @ git+https://github.com/szymonrychu/agents-common@v0.1.0"
MODULES = ["settings", "db", "runtime", "orchestrator", "fake_orchestrator", "runs", "testing"]


def test_versions_agree():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    changelog = (ROOT / "CHANGELOG.md").read_text()
    top = re.search(r"^## v(\S+)", changelog, re.M)
    assert top is not None
    readme = (ROOT / "README.md").read_text()
    assert meta == agents_common.__version__ == top.group(1) == "0.1.0"
    assert INSTALL in readme
    assert "## Versioning" in readme


def test_modules_documented_and_exist():
    readme = (ROOT / "README.md").read_text()
    changelog = (ROOT / "CHANGELOG.md").read_text()
    for name in MODULES:
        assert re.search(rf"^## `{name}`", readme, re.M), name
        assert f"`{name}`" in changelog, name
        importlib.import_module(f"agents_common.{name}")
