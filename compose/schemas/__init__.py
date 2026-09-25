"""Third-party vendor schemas and their generated protobuf bindings.

Source .proto contracts (reconstructed from a vendor's own upload — see each
file's own provenance/SHA-256 comment) live under compose/schemas/vendors;
generated bindings live under compose/generated/schemas. Extend the package
path the same way compose/protocols/__init__.py does, so
`schemas.vendors.<vendor>.*_pb2` resolves regardless of PYTHONPATH order.

Kept apart from compose/protocols/proto/, which holds EFDI's own /v2 envelope
contracts, not reverse-sourced third-party ones.
"""

from __future__ import annotations

import sys
from pathlib import Path
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)  # type: ignore[name-defined]
_generated = Path(__file__).resolve().parents[1] / "generated" / "schemas"
if _generated.is_dir():
    generated_path = str(_generated)
    if generated_path not in __path__:
        __path__.append(generated_path)
    if generated_path not in sys.path:
        sys.path.insert(0, generated_path)
