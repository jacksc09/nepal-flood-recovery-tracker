"""Let the tests import the scripts in src/ directly (import weeks, ...)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
