"""The SDK lives in sdk/python (published as assay-evals, which assay-server depends on): make it
importable in the tests without installing it."""
import sys
from pathlib import Path

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")
if SDK not in sys.path:
    sys.path.insert(0, SDK)
