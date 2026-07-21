import asyncio
import hashlib
import hmac
import os
import socket

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from .auth import consume_shell_ticket
from .db import SessionLocal
from .models import AdminUser


router = APIRouter(tags=["shell"])
_SESSION_MAX_SECONDS = 5 * 60
_SHELL_HOST = os.environ.get("EFDI_SHELL_CONTROL_HOST", "127.0.0.1")
_SHELL_PORT = int(os.environ.get("EFDI_SHELL_CONTROL_PORT", "18897"))
_EXPLICIT_CONTROL_TOKEN = os.environ.get("EFDI_CONTROL_TOKEN", "")
_ADMIN_SECRET = os.environ.get("ZENOH_ADMIN_SECRET_KEY", "")
_CONTROL_TOKEN = _EXPLICIT_CONTROL_TOKEN or (
    hashlib.sha256(f"efdi-control-v1:{_ADMIN_SECRET}".encode()).hexdigest()
    if _ADMIN_SECRET else ""
)
_SHELL_ENABLED = os.environ.get("EFDI_SHELL_ENABLED", "true").lower() in {"1", "true", "yes"}


def _read_line(raw_sock: socket.socket) -> bytes | None:
    data = bytearray()
    while len(data) < 64:
        chunk = raw_sock.recv(1)
        if not chunk:
            return None
        if chunk == b"\n":
            return bytes(data)
        data.extend(chunk)
    return None


def _open_shell_socket() -> socket.socket:
    if not _SHELL_ENABLED or not _CONTROL_TOKEN:
        raise OSError("EFDI_CONTROL_TOKEN is required for shell access")
    raw_sock = socket.create_connection((_SHELL_HOST, _SHELL_PORT), timeout=8)
    raw_sock.sendall(f"EFDI-SHELL/1 {_CONTROL_TOKEN}\n".encode())
    response = _read_line(raw_sock)
    if response is None or not hmac.compare_digest(response, b"OK"):
        raw_sock.close()
        raise OSError("shell helper authorization failed")
    raw_sock.settimeout(None)
    return raw_sock


@router.websocket("/api/shell/ws")
async def shell_ws(ws: WebSocket, t: str = Query(...)):
    user_id = consume_shell_ticket(t)
    if not user_id:
        await ws.close(code=4001)
        return

    async with SessionLocal() as db:
        result = await db.execute(
            select(AdminUser).where(
                AdminUser.id == user_id,
                AdminUser.is_active.is_(True),
                AdminUser.role == "superadmin",
                AdminUser.auth_provider == "local",
            )
        )
        if result.scalar_one_or_none() is None:
            await ws.close(code=4001)
            return

    await ws.accept()
    loop = asyncio.get_running_loop()
    try:
        raw_sock = await loop.run_in_executor(None, _open_shell_socket)
    except OSError as exc:
        await ws.send_text(f"[error] {exc}\r\n")
        await ws.close()
        return

    async def read_container():
        while True:
            try:
                data = await loop.run_in_executor(None, raw_sock.recv, 4096)
                if not data:
                    return
                await ws.send_bytes(data)
            except Exception:
                return

    async def read_client():
        while True:
            try:
                message = await ws.receive()
                if "bytes" in message:
                    await loop.run_in_executor(None, raw_sock.sendall, message["bytes"])
                elif "text" in message:
                    await loop.run_in_executor(None, raw_sock.sendall, message["text"].encode())
            except (WebSocketDisconnect, RuntimeError, OSError):
                return

    container_task = asyncio.create_task(read_container())
    client_task = asyncio.create_task(read_client())
    try:
        done, _ = await asyncio.wait(
            {container_task, client_task},
            timeout=_SESSION_MAX_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            try:
                await ws.send_text("\r\n[session expired — re-authentication required]\r\n")
            except Exception:
                pass
    finally:
        try:
            raw_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            raw_sock.close()
        except OSError:
            pass
        for task in (container_task, client_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(container_task, client_task, return_exceptions=True)
