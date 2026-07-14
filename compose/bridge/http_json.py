"""Bounded JSON response reader shared by native HTTP bridge scripts."""

import json

MAX_JSON_BYTES = 10_000_000


def read_json_response(response, max_bytes: int = MAX_JSON_BYTES):
    """Read and decode JSON without allowing an unbounded remote response."""
    content_length = response.headers.get("Content-Length")
    try:
        declared_length = int(content_length) if content_length is not None else None
    except (TypeError, ValueError):
        # A malformed Content-Length is not trusted; the bounded read below is.
        declared_length = None
    if declared_length is not None and declared_length > max_bytes:
        raise json.JSONDecodeError("HTTP JSON response exceeds size limit", "", 0)
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise json.JSONDecodeError("HTTP JSON response exceeds size limit", "", 0)
    return json.loads(body.decode("utf-8"))
