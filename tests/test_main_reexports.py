"""
Every name that ``dm20_protocol.main`` exposed before the tool modules were
split out must still be importable from it (tests and external callers do
``from dm20_protocol import main as m`` and use ``m.<name>``).
"""

import json
from pathlib import Path

from dm20_protocol import main as m

NAMES_PATH = Path(__file__).parent / "fixtures" / "main_module_names.json"


def test_main_still_exposes_all_names():
    expected = json.loads(NAMES_PATH.read_text(encoding="utf-8"))
    missing = [n for n in expected if not hasattr(m, n)]
    assert not missing, f"Names no longer importable from dm20_protocol.main: {missing}"
