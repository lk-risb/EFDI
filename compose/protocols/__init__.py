"""Protocol modules and their generated protobuf bindings.

Generated bindings live under compose/generated/protocols while source
contracts and translators live under compose/protocols. Extend the package
path so `protocols.random.*_pb2` and `protocols.vendors.*_pb2` resolve beside
their source modules regardless of whether the caller sets PYTHONPATH manually.
"""

from __future__ import annotations

import sys
from pathlib import Path
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)  # type: ignore[name-defined]
_generated = Path(__file__).resolve().parents[1] / "generated" / "protocols"
if _generated.is_dir():
    generated_path = str(_generated)
    if generated_path not in __path__:
        __path__.append(generated_path)
    if generated_path not in sys.path:
        sys.path.insert(0, generated_path)
