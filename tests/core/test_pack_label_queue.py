"""Execute the actual browser queue, including delayed printing and cancellation."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_browser_automatic_label_queue():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed to execute the browser print queue tests')
    result = subprocess.run([node, '--test', 'tests/js/test_pack_label_queue.cjs'],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
