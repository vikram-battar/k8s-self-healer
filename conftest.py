"""Make the src/ package importable so plain `pytest` works from the repo root."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
