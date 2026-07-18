"""Single source of truth for this pod's runtime data namespace prefix.

The prefix (e.g. "LTU/CISB", "LTU/CISB/LTK", or empty for a slot-root
sandbox namespace) is deployment-chosen and variable depth. It is written to a
dedicated state file the admin can update live; bridges read it here at startup.
The legacy namespace-prefix state file remains the fallback for older installs.
"""
import os

_PREFIX_FILE = os.environ.get("NAMESPACE_PREFIX_FILE", "/namespace-prefix")
_DATA_PREFIX_FILE = os.environ.get("DATA_NAMESPACE_PREFIX_FILE", "/data-topic-prefix")
_DEFAULT = "LTU/CISB"


def prefix() -> str:
    try:
        with open(_DATA_PREFIX_FILE) as f:
            return f.read().strip().strip("/")
    except OSError:
        pass
    if "DATA_NAMESPACE_PREFIX" in os.environ:
        return os.environ["DATA_NAMESPACE_PREFIX"].strip().strip("/")
    try:
        with open(_PREFIX_FILE) as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    return os.environ.get("NAMESPACE_PREFIX", _DEFAULT)


def topic_root(org: str | None = None) -> str:
    """Return the complete runtime data root without introducing a stray slash."""
    organization = org if org is not None else os.environ.get("PARTNER_NAMESPACE", "")
    configured = prefix()
    return "/".join(part for part in (configured, organization.strip("/")) if part)


if __name__ == "__main__":
    import tempfile
    import importlib
    import namespace_prefix as np
    # default: no file, no env
    os.environ.pop("NAMESPACE_PREFIX", None)
    os.environ["NAMESPACE_PREFIX_FILE"] = "/nonexistent-namespace-prefix"
    importlib.reload(np)
    assert np.prefix() == "LTU/CISB", np.prefix()
    # env override
    os.environ["NAMESPACE_PREFIX"] = "LTU/GVB"
    importlib.reload(np)
    assert np.prefix() == "LTU/GVB", np.prefix()
    # file wins over env
    with tempfile.NamedTemporaryFile("w", suffix=".prefix", delete=False) as tf:
        tf.write("  LTU/CISB/LTK/LTTB  \n")
        path = tf.name
    os.environ["NAMESPACE_PREFIX_FILE"] = path
    importlib.reload(np)
    assert np.prefix() == "LTU/CISB/LTK/LTTB", np.prefix()
    # empty file falls through to env
    open(path, "w").close()
    importlib.reload(np)
    assert np.prefix() == "LTU/GVB", np.prefix()
    os.unlink(path)
    print("namespace_prefix self-check OK")
