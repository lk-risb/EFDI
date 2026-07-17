"""Single source of truth for this pod's org namespace prefix.

The prefix (e.g. "LTU/CISB", "LTU/CISB/LTK") is deployment-chosen and variable
depth; only the first segment is fixed by convention. It is written to a state
file the admin API can update live; bridges read it here at startup. Falls back
to the NAMESPACE_PREFIX env var, then the historical default "LTU/CISB".
"""
import os

_PREFIX_FILE = os.environ.get("NAMESPACE_PREFIX_FILE", "/namespace-prefix")
_DEFAULT = "LTU/CISB"


def prefix() -> str:
    try:
        with open(_PREFIX_FILE) as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    return os.environ.get("NAMESPACE_PREFIX", _DEFAULT)


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
