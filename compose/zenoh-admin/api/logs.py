import asyncio
import os
import re
from pathlib import Path

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from .auth import consume_ws_ticket


router = APIRouter(tags=["logs"])
_LOG_DIR = Path(os.environ.get("EFDI_RUNTIME_LOG_DIR", "/runtime-logs")).resolve()
_SERVICE_RE = re.compile(r"^[a-z0-9-]+$")
_TAIL_BYTES = 256 * 1024
_SESSION_MAX_SECONDS = 30 * 60


def _log_path(service: str) -> Path | None:
    if not _SERVICE_RE.fullmatch(service):
        return None
    path = (_LOG_DIR / f"{service}.log").resolve()
    if path.parent != _LOG_DIR:
        return None
    return path


def _initial_lines(handle) -> list[str]:
    handle.seek(0, 2)
    end = handle.tell()
    handle.seek(max(0, end - _TAIL_BYTES))
    if handle.tell() > 0:
        handle.readline()
    return [line.rstrip("\r\n") for line in handle.readlines()[-100:]]


@router.websocket("/api/logs")
async def logs_ws(ws: WebSocket, service: str = Query(...), ticket: str = Query(...)):
    ticket_entry = consume_ws_ticket(ticket)
    if not ticket_entry or ticket_entry[1] not in {"admin", "superadmin"}:
        await ws.close(code=4401)
        return
    path = _log_path(service)
    if path is None:
        await ws.close(code=4400)
        return

    await ws.accept()
    loop = asyncio.get_running_loop()
    started = loop.time()

    async def stream_logs() -> None:
        handle = None
        try:
            while loop.time() - started < _SESSION_MAX_SECONDS:
                if handle is None:
                    try:
                        handle = path.open("r", encoding="utf-8", errors="replace")
                        for line in await asyncio.to_thread(_initial_lines, handle):
                            await ws.send_text(line)
                    except FileNotFoundError:
                        await asyncio.sleep(0.5)
                        continue

                line = await asyncio.to_thread(handle.readline)
                if line:
                    await ws.send_text(line.rstrip("\r\n"))
                else:
                    await asyncio.sleep(0.25)
                    try:
                        if path.stat().st_ino != os.fstat(handle.fileno()).st_ino:
                            handle.close()
                            handle = None
                    except (FileNotFoundError, OSError):
                        pass
        finally:
            if handle is not None:
                handle.close()

    async def watch_client() -> None:
        while True:
            await ws.receive()

    stream_task = asyncio.create_task(stream_logs())
    client_task = asyncio.create_task(watch_client())
    try:
        done, _ = await asyncio.wait(
            {stream_task, client_task},
            timeout=_SESSION_MAX_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            await ws.send_text("\r\n[session expired — re-authentication required]")
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        for task in (stream_task, client_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stream_task, client_task, return_exceptions=True)
