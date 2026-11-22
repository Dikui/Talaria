"""data-access/04: Notebook 取数可脱离工作台和 FastAPI。"""

import subprocess
import sys

import pytest


@pytest.mark.small
@pytest.mark.spec
def test_sdk_reads_without_importing_the_workbench_or_fastapi(tmp_path):
    program = """
import sys
from importlib.abc import MetaPathFinder

class RejectWorkbench(MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.partition(".")[0] == "fastapi" or fullname.startswith("talaria.workbench_api"):
            raise AssertionError(f"SDK imports {fullname}")

sys.meta_path.insert(0, RejectWorkbench())
from talaria import sdk
assert not sdk.bars("BTC-USDT-PERP", "1m", start=0, data_dir=sys.argv[1], offline=True)
"""
    subprocess.run([sys.executable, "-c", program, str(tmp_path)], check=True)
