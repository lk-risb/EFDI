"""Single source of truth for this pod's runtime data namespace prefix.

The prefix (e.g. "EFDI", "ORG/UNIT", or empty for a slot-root
sandbox namespace) is deployment-chosen and variable depth. It is written to a
dedicated state file the admin can update live; bridges read it here at startup.
The legacy namespace-prefix state file remains the fallback for older installs.

The state files are located here, not supplied by whoever launches the process.
A process must land in the same namespace whether it was started by start.sh,
by the admin control agent, by systemd or by hand — resolving the location from
inherited environment made the namespace a property of the launcher, and a
process started any other way silently fell back to the built-in default and
published into a namespace nobody was subscribed to.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT = "EFDI"


def _state_candidates(basename: str) -> list[str]:
    """Locations to search for a prefix state file, most specific first."""
    paths = []
    state_dir = os.environ.get("POD_STATE_DIR")
    if state_dir:
        paths.append(os.path.join(state_dir, basename))
    paths.append(os.path.join(_HERE, "state", basename))   # host default
    paths.append("/" + basename)                           # container mount
    return paths


def _read_state(env_var: str, basename: str) -> str | None:
    """Contents of the first state file that exists, or None if none do.

    An existing but empty file is a valid value — it selects the slot-root
    namespace — so existence, not content, decides which candidate wins.
    """
    override = os.environ.get(env_var)
    candidates = [override] if override else _state_candidates(basename)
    for path in candidates:
        try:
            with open(path) as f:
                return f.read().strip().strip("/")
        except OSError:
            continue
    return None


def prefix() -> str:
    data = _read_state("DATA_NAMESPACE_PREFIX_FILE", "data-topic-prefix")
    if data is not None:
        return data
    if "DATA_NAMESPACE_PREFIX" in os.environ:
        return os.environ["DATA_NAMESPACE_PREFIX"].strip().strip("/")
    legacy = _read_state("NAMESPACE_PREFIX_FILE", "namespace-prefix")
    if legacy:
        return legacy
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
    # Pin both state files away from the real ones so the checks below do not
    # depend on how this checkout happens to be configured.
    os.environ["DATA_NAMESPACE_PREFIX_FILE"] = "/nonexistent-data-topic-prefix"
    # default: no file, no env
    os.environ.pop("NAMESPACE_PREFIX", None)
    os.environ["NAMESPACE_PREFIX_FILE"] = "/nonexistent-namespace-prefix"
    importlib.reload(np)
    assert np.prefix() == "EFDI", np.prefix()
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
    # an existing data-topic-prefix file wins outright, empty content included:
    # empty selects the slot-root namespace and must not fall through.
    with tempfile.NamedTemporaryFile("w", suffix=".data", delete=False) as tf:
        data_path = tf.name
    os.environ["DATA_NAMESPACE_PREFIX_FILE"] = data_path
    importlib.reload(np)
    assert np.prefix() == "", repr(np.prefix())
    os.unlink(data_path)
    os.environ["DATA_NAMESPACE_PREFIX_FILE"] = "/nonexistent-data-topic-prefix"
    # discovery is launcher-independent: with no env override at all, the file
    # under POD_STATE_DIR is found without anyone exporting its location.
    del os.environ["DATA_NAMESPACE_PREFIX_FILE"]
    state = tempfile.mkdtemp()
    with open(os.path.join(state, "data-topic-prefix"), "w") as f:
        f.write("LTU/CISB/LTK\n")
    os.environ["POD_STATE_DIR"] = state
    importlib.reload(np)
    assert np.prefix() == "LTU/CISB/LTK", np.prefix()
    del os.environ["POD_STATE_DIR"]
    os.unlink(path)
    print("namespace_prefix self-check OK")
