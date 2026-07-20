import asyncio
import os
import socket

import docker
from docker.errors import DockerException
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from .auth import consume_shell_ticket
from .db import SessionLocal
from .models import AdminUser


router = APIRouter(tags=["shell"])
_ROUTER_CONTAINER = os.environ.get("ZENOH_ROUTER_CONTAINER", "efdi-pod-zenoh-router")
_SESSION_MAX_SECONDS = 5 * 60
_client = docker.from_env()


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
        exec_id = _client.api.exec_create(
            _ROUTER_CONTAINER,
            ["/bin/sh"],
            stdin=True,
            stdout=True,
            stderr=True,
            tty=True,
        )
        sock = _client.api.exec_start(exec_id["Id"], socket=True, tty=True)
    except DockerException as exc:
        await ws.send_text(f"[error] {exc}\r\n")
        await ws.close()
        return

    raw_sock = sock._sock

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
