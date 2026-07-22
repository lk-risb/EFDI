"""Protocol modules and their generated protobuf bindings.

The checked-in generated bindings live under compose/generated/protocols,
while the runtime imports them as `protocols.*_pb2`.  Extend the package path
so both locations resolve through the same namespace regardless of whether the
caller sets PYTHONPATH manually.
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
