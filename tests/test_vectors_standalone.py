"""Runs the standalone re-computation of the test vectors.

``tests/vectors/check_vectors_standalone.py`` uses the standard library only and imports
nothing from the package. It recomputes the sum tree, conversion and coverage vectors from the
written specification (docs/hypothetical/W_CHANGEOVER_DESIGN.md), so the vectors the other
tests rely on are checked by code that shares nothing with the library.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

VECTORS = Path(__file__).parent / "vectors"


def test_standalone_recomputation_of_the_vectors_passes():
    result = subprocess.run(
        [sys.executable, str(VECTORS / "check_vectors_standalone.py"), str(VECTORS)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[-1] == "OK"
    assert lines[0] == "sum tree: 9 trees, 39 inclusion proofs recomputed"
    assert lines[1] == "conversion: 78 conversions recomputed"
    assert lines[2] == "coverage: 11 cases recomputed"


def test_the_standalone_script_imports_nothing_from_the_package():
    source = (VECTORS / "check_vectors_standalone.py").read_text(encoding="utf-8")
    assert "openreserve" not in source.replace('b"openreserve.sumtree.v1"', "")
