"""Shared ECS-compatible ndJSON logging (pvarki best-practices' logging.md:
"All applications MUST output their logs in ECS-compatible ndJSON format by
default", with an env var escape hatch for local development).

This is the reference implementation, not yet wired into every service —
this repo's ~50 bridges/protocols/layers currently use plain
`print(..., flush=True)` (548 call sites at last count). Converting all of
them is a separate, deliberate follow-up: doing it in one pass here would be
a huge, unreviewed diff across every live service. video_zenoh_bridge.py and
gst_zenoh_config.py use this module as the pilot; copy that pattern when
converting another service.

Usage:
    from ecs_log import get_logger
    log = get_logger("video-zenoh-bridge")
    log.info("pipeline running", extra={"pid": proc.pid})
    log.error("gst-launch exited", extra={"rc": rc})

Format (compose/.env):
    EFDI_LOG_FORMAT=ndjson   # default — one ECS-shaped JSON object per line
    EFDI_LOG_FORMAT=text     # human-readable for local dev, per logging.md's
                             # "MAY additionally have ENV variable support
                             # for changing to a format developers consider
                             # better for local development"
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys

_ECS_VERSION = "8.11.0"


class _ECSFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "@timestamp": datetime.datetime.fromtimestamp(
                record.created, tz=datetime.UTC
            ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "ecs.version": _ECS_VERSION,
            "log.level": record.levelname.lower(),
            "log.logger": record.name,
            "message": record.getMessage(),
            "service.name": record.name,
        }
        if record.exc_info:
            doc["error.stack_trace"] = self.formatException(record.exc_info)
        # Anything passed via logging's own `extra={...}` becomes additional
        # top-level fields — the same mechanism callers already reach for.
        reserved = logging.LogRecord(
            "", 0, "", 0, "", (), None
        ).__dict__.keys()
        for key, value in record.__dict__.items():
            if key not in reserved and key not in doc:
                doc[key] = value
        return json.dumps(doc, default=str, separators=(",", ":"))


def get_logger(service_name: str) -> logging.Logger:
    """One configured logger per service, ndJSON to stdout by default.

    Idempotent — calling this twice for the same service_name returns the
    same logger without adding a second handler (Python's logging module
    already deduplicates by name, this just guards the handler setup)."""
    logger = logging.getLogger(service_name)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get("EFDI_LOG_FORMAT", "ndjson").strip().lower() == "text":
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(name)s %(levelname)s: %(message)s"
        ))
    else:
        handler.setFormatter(_ECSFormatter())
    logger.addHandler(handler)
    logger.setLevel(os.environ.get("EFDI_LOG_LEVEL", "INFO").strip().upper())
    logger.propagate = False
    return logger
