"""Nomenclature guard (design D12): the unit is a session and the project is agents-orchestrator.

Every tracked file is guarded except the dated-history files. A line fails when it matches the
retired unit word or the retired project name after every allowlisted phrase (a Kubernetes or
CNPG object, never the unit) is removed from it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RETIRED = re.compile(r"c[l]uster|agent[-_]orchestrator", re.IGNORECASE)
HISTORY = ("MEMORY.md", "CHANGELOG.md", "ROADMAP.md")
HISTORY_DIRS = ("docs/plans/",)
ALLOWED = (
    "cluster.local",
    "ClusterIP",
    "clusterIP",
    "ClusterIssuer",
    "cluster-issuer",
    "ClusterRole",
    "in-cluster",
    "cluster-internal",
    "cluster node",
    "cluster-info",
    "kind: Cluster",
    "db-cluster",
)


def _hits(line: str) -> bool:
    for phrase in ALLOWED:
        line = line.replace(phrase, "")
    return RETIRED.search(line) is not None


def _guarded() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return [
        path
        for path in out.splitlines()
        if path not in HISTORY and not path.startswith(HISTORY_DIRS)
    ]


def test_guard_flags_retired_names_and_spares_allowlisted_phrases():
    unit = "clu" + "ster"
    assert _hits(f"create_{unit}(preset_id)")
    assert _hits(f"the {unit.upper()} id")
    assert _hits("agent" + "-orchestrator-services")
    assert _hits("agent" + "_orchestrator")
    assert not _hits("http://agents-orchestrator-services.agents.svc.cluster.local:8002")
    assert not _hits("create_session(preset_id)")


def test_guard_covers_the_whole_repo_but_dated_history():
    guarded = _guarded()
    assert "src/agents_common/orchestrator.py" in guarded
    assert "README.md" in guarded
    assert "tests/test_nomenclature.py" in guarded
    assert not any(path in HISTORY for path in guarded)


def test_no_retired_names_in_tracked_files():
    offending = []
    for path in _guarded():
        text = (ROOT / path).read_bytes().decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            if _hits(line):
                offending.append(f"{path}:{number}: {line.strip()}")
    assert offending == []
