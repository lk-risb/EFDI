"""Compatibility shim for protocol modules imported as `protocols.*`.

The canonical gateway lives in compose/protocols/gateway.py, but a few
modules import `gateway` as a top-level module because they are also
executable as standalone scripts from compose/protocols/.
"""

from protocols.gateway import *  # noqa: F401,F403
