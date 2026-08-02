"""Read-only view of compose/protocols/data_stats.py's per-process counters.

Each running protocol/codec process keeps its own in-memory byte/frame
counters and flushes them to compose/state/stats/<key>.json every 30s (see
protocols/data_stats.py). This endpoint just sums what's on disk right now —
it holds no state of its own.
"""

import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends

from .deps import require_role

router = APIRouter(prefix="/api/data-stats", tags=["data-stats"])

_STATS_DIR = Path(os.environ.get("EFDI_STATS_DIR", "/runtime-stats"))


def _read_snapshots() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not _STATS_DIR.is_dir():
        return out
    for path in sorted(_STATS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out[path.stem] = data
    return out


@router.get("")
async def get_data_stats(_=Depends(require_role("admin", "superadmin", "readonly"))):
    snapshots = _read_snapshots()
    ingress = {k: v for k, v in snapshots.items() if not k.startswith("egress-")}
    egress = {k: v for k, v in snapshots.items() if k.startswith("egress-")}
    return {
        "ingress": ingress,
        "egress": egress,
        "ingress_bytes_total": sum(v.get("bytes_in", 0) for v in ingress.values()),
        "ingress_frames_total": sum(v.get("frames_in", 0) for v in ingress.values()),
        "egress_bytes_total": sum(v.get("bytes_out", 0) for v in egress.values()),
        "egress_frames_total": sum(v.get("frames_out", 0) for v in egress.values()),
    }
