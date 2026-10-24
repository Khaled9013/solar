import asyncio
from http.cookies import SimpleCookie
import json
import mimetypes
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "solar.settings")
from django.core.asgi import get_asgi_application
django_application = get_asgi_application()

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user
from django.contrib.sessions.backends.db import SessionStore
from django.db import close_old_connections
from django.http import HttpRequest
from django.http.request import validate_host

from solar.core.desktop import desktop
from solar.core.admin_terminal import shells
from solar.core.models import Run
from solar.core.runner import HANDLES, HANDLES_LOCK


def socket_auth(scope):
    close_old_connections()
    try:
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("ascii")
        origin = headers.get(b"origin", b"").decode("ascii")
        parsed = urlsplit(origin)
        expected = "https" if settings.SESSION_COOKIE_SECURE else "http"
        if parsed.scheme != expected or parsed.netloc != host or parsed.path or parsed.query or parsed.fragment:
            return None, None
        domain = urlsplit(f"{expected}://{host}").hostname or ""
        if not validate_host(f"[{domain}]" if ":" in domain else domain, settings.ALLOWED_HOSTS):
            return None, None
        return session_auth(headers)
    except (ValueError, UnicodeError):
        return None, None
    finally:
        close_old_connections()


def session_auth(headers):
    close_old_connections()
    try:
        cookie = SimpleCookie()
        cookie.load(headers.get(b"cookie", b"").decode("latin1"))
        name = settings.SESSION_COOKIE_NAME
        if name not in cookie:
            return None, None
        session = SessionStore(session_key=cookie[name].value)
        request = HttpRequest()
        request.session = session
        user = get_user(request)
        if not user.is_authenticated or not user.is_active or user.must_change_password:
            return None, None
        return user, session.session_key
    finally:
        close_old_connections()


@sync_to_async
def allowed_run(run_id, user_id):
    return Run.objects.filter(id=run_id, chat__owner_id=user_id, mode="native", status__in=Run.ACTIVE).exists()


async def websocket(scope, receive, send):
    await receive()  # websocket.connect
    user, session_key = await sync_to_async(socket_auth)(scope)
    if user is None:
        await send({"type": "websocket.close", "code": 4401})
        return
    path = scope.get("path", "")
    terminal_id = lease_id = shell_id = None
    if path.startswith("/ws/admin-terminal/"):
        shell_id = path.rstrip("/").rsplit("/", 1)[-1]
        if not shells.get(user, session_key, shell_id):
            await send({"type": "websocket.close", "code": 4403})
            return
    elif path.startswith("/ws/terminal/") and user.host_access:
        try:
            terminal_id = str(uuid.UUID(path.rstrip("/").rsplit("/", 1)[-1]))
        except ValueError:
            pass
        if not terminal_id or not await allowed_run(terminal_id, user.id):
            await send({"type": "websocket.close", "code": 4404})
            return
    elif path.startswith("/ws/desktop/") and user.remote_access:
        lease_id = path.rstrip("/").rsplit("/", 1)[-1]
        if not desktop.verify(user.id, session_key, lease_id):
            await send({"type": "websocket.close", "code": 4403})
            return
    else:
        await send({"type": "websocket.close", "code": 4403})
        return
    await send({"type": "websocket.accept"})
    sequence, frame_number, checked, input_count, rate_start = 0, 0, 0, 0, time.monotonic()
    desired_size, applied_size = None, None
    connected_at = time.monotonic()
    pending = asyncio.create_task(receive())
    try:
        while True:
            now = time.monotonic()
            if now - checked > 3:
                fresh, key = await sync_to_async(socket_auth)(scope)
                if not fresh or key != session_key or (terminal_id and not fresh.host_access) or (lease_id and not fresh.remote_access) or (shell_id and not shells.get(fresh, key, shell_id)):
                    if shell_id:
                        await asyncio.to_thread(shells.stop_for_session, session_key)
                    break
                checked = now
            if terminal_id or shell_id:
                if shell_id:
                    handle = shells.get(user, session_key, shell_id)
                else:
                    with HANDLES_LOCK:
                        handle = HANDLES.get(terminal_id)
                if handle:
                    # The browser can send dimensions while the worker is still
                    # queued. Retain them and apply before forwarding TUI output.
                    if handle.process and desired_size and applied_size != desired_size:
                        try:
                            await asyncio.to_thread(handle.process.resize, *desired_size)
                        except OSError:
                            break
                        applied_size = desired_size
                    if applied_size or now - connected_at > 1:
                        sequence, output = handle.output_since(sequence)
                        if output:
                            await send({"type": "websocket.send", "text": json.dumps({"type": "output", "data": output})})
                    if shell_id and handle.closed.is_set():
                        await send({"type": "websocket.send", "text": json.dumps({"type": "exit", "reason": handle.reason})})
                        break
                elif shell_id or not await allowed_run(terminal_id, user.id):
                    await send({"type": "websocket.send", "text": json.dumps({"type": "exit"})})
                    break
            else:
                if not desktop.verify(user.id, session_key, lease_id):
                    break
                with desktop.lock:
                    frame, number = desktop.frame, desktop.frame_number
                if frame and number != frame_number:
                    frame_number = number
                    await send({"type": "websocket.send", "bytes": frame})
            done, _ = await asyncio.wait({pending}, timeout=0.08)
            if not done:
                continue
            incoming = pending.result()
            if incoming["type"] == "websocket.disconnect":
                break
            pending = asyncio.create_task(receive())
            raw = incoming.get("text", "")
            if len(raw) > 16_384:
                break
            if now - rate_start > 1:
                rate_start, input_count = now, 0
            input_count += 1
            if input_count > 150:
                break
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    break
                if terminal_id or shell_id:
                    if data.get("type") == "input" and isinstance(data.get("data"), str) and handle and handle.process:
                        handle.last_input = now
                        await asyncio.to_thread(handle.process.write, data["data"])
                    elif data.get("type") == "resize":
                        cols, rows = int(data["cols"]), int(data["rows"])
                        if 20 <= cols <= 300 and 5 <= rows <= 150:
                            desired_size = (cols, rows)
                elif lease_id:
                    desktop.heartbeat(lease_id)
                    if data.get("type") != "heartbeat":
                        desktop.input(lease_id, data)
            except (ValueError, TypeError, KeyError, OSError):
                break
    finally:
        pending.cancel()
        try:
            await pending
        except (asyncio.CancelledError, Exception):
            pass
        if lease_id:
            desktop.stop_for_session(session_key)
        await send({"type": "websocket.close", "code": 1000})


STATIC_ROOT = Path(__file__).parent / "static"
STATIC_FILES = {"/static/" + p.relative_to(STATIC_ROOT).as_posix(): p for p in STATIC_ROOT.rglob("*") if p.is_file()}


async def static_file(scope, send):
    path = STATIC_FILES.get(scope["path"])
    if not path or scope["method"] not in ("GET", "HEAD"):
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"Not found"})
        return
    content = await asyncio.to_thread(path.read_bytes)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix in (".js", ".mjs"):
        mime = "text/javascript"
    elif path.suffix == ".wasm":
        mime = "application/wasm"
    await send({"type": "http.response.start", "status": 200, "headers": [
        (b"content-type", mime.encode()), (b"content-length", str(len(content)).encode()),
        (b"x-content-type-options", b"nosniff"), (b"cache-control", b"public, max-age=3600"),
        (b"content-security-policy", b"default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self' blob: data:; worker-src 'self'; object-src 'none'"),
        (b"cross-origin-resource-policy", b"same-origin"),
    ]})
    await send({"type": "http.response.body", "body": content if scope["method"] == "GET" else b""})


class BodyTooLarge(Exception):
    pass


async def _application(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    elif scope["type"] == "websocket":
        await websocket(scope, receive, send)
    elif scope["type"] == "http":
        if scope["path"].startswith("/static/"):
            await static_file(scope, send)
            return
        headers = dict(scope.get("headers", []))
        is_upload = (scope.get("method") == "POST" and
                     (scope["path"].endswith("/files/") or scope["path"] == "/api/skills/") and
                     headers.get(b"content-type", b"").startswith(b"multipart/form-data"))
        maximum, size = (100 * 1024**2 if is_upload else 1024**2), 0
        if scope["path"] == "/api/login/":
            maximum = 8192
        if is_upload:
            user, _ = await sync_to_async(session_auth)(headers)
            if not user:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"Sign in before uploading"}'})
                return

        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > maximum:
                raise BodyTooLarge()
            return message
        try:
            try:
                length = int(headers.get(b"content-length", b"0"))
            except ValueError:
                length = maximum + 1
            if length > maximum:
                raise BodyTooLarge()
            await django_application(scope, limited_receive, send)
        except BodyTooLarge:
            await send({"type": "http.response.start", "status": 413,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": json.dumps({"error": f"Request exceeds {maximum} bytes"}).encode()})


# Hold the slot through ASGI response transmission, not just view execution: slow
# clients must not leave dozens of complete decrypted exports buffered in memory.
_LARGE_REQUEST_SLOTS = asyncio.Semaphore(2)


async def application(scope, receive, send):
    path = scope.get("path", "")
    large_request = scope["type"] == "http" and (
        path.startswith("/api/files/") or
        (path.startswith(("/api/chats/", "/api/projects/")) and path.endswith(("/files/", "/export/"))) or
        (path == "/api/skills/" and scope.get("method") == "POST") or
        (path.startswith("/api/skills/") and b"download=" in scope.get("query_string", b""))
    )
    if large_request:
        async with _LARGE_REQUEST_SLOTS:
            await _application(scope, receive, send)
    else:
        await _application(scope, receive, send)
