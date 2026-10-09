"""The SDK lives in sdk/python (published as assay-evals, which assay-server depends on): make it
importable in the tests without installing it."""
import os
import sys
from pathlib import Path

# Tests write the interpreter's path into assay.toml (command = "..."): on Windows its backslashes
# would be TOML escapes. Windows takes forward slashes in a path just as well.
if os.name == "nt":
    sys.executable = sys.executable.replace("\\", "/")

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")
if SDK not in sys.path:
    sys.path.insert(0, SDK)
