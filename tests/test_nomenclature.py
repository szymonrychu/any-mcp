from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["src/any_mcp/orchestrator.py", "tests/test_orchestrator_plugin.py", "README.md"]
# The acceptance condition fixes this one test name, which carries the old word.
PINNED_NAME = "test_cluster_key_refused"
BANNED = re.compile(r"cluster|agent[-_]orchestrator", re.IGNORECASE)


def test_no_old_nomenclature() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", *FILES], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert sorted(tracked) == sorted(FILES)
    hits = [
        f"{name}:{n}: {line}"
        for name in tracked
        for n, line in enumerate((ROOT / name).read_text().splitlines(), 1)
        if BANNED.search(line.replace(PINNED_NAME, ""))
    ]
    assert hits == []
