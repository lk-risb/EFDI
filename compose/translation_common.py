"""Compatibility shim for protocol modules imported as `protocols.*`.

The canonical helpers live in compose/protocols/translation_common.py, but a
few modules import `translation_common` as a top-level module because they are
also executable as standalone scripts from compose/protocols/.
"""

from protocols.translation_common import *  # noqa: F401,F403
