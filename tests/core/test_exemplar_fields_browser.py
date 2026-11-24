"""Run the actual per-unit form event handlers in Node."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_gtd_omission_form():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for the browser form tests')
    result = subprocess.run([node, '--test', 'tests/js/test_exemplar_fields.cjs'],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
