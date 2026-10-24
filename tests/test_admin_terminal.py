"""Host shell authorization, revocation, reconnect, lifetime and static asset boundaries."""
import asyncio
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import Client, SimpleTestCase, TestCase, override_settings

from solar.config import config
from solar.core.admin_terminal import ShellManager, ShellSession, shell_command
from solar.core.models import User
from solar import viewer_assets

PASSWORD = "Terminal-test-passphrase-42!"


@override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver", "localhost"])
class TerminalAccessTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("terminal-admin", PASSWORD, must_change_password=False)
        self.member = User.objects.create_user("terminal-member", PASSWORD, host_access=True, must_change_password=False)
        self.client.force_login(self.admin)

    def test_status_never_starts_shell_and_members_are_denied(self):
        with patch("solar.core.terminal_views.shells") as manager:
            manager.get.return_value = None
            self.assertEqual(self.client.get("/api/admin-terminal/").json(), {"terminal": None})
            manager.start.assert_not_called()
            self.client.force_login(self.member)
            for method in ("get", "post", "delete"):
                self.assertEqual(getattr(self.client, method)("/api/admin-terminal/").status_code, 403)
            manager.start.assert_not_called()

    def test_admin_needs_host_permission_and_changed_password(self):
        for fields in ({"host_access": False}, {"host_access": True, "must_change_password": True}):
            User.objects.filter(pk=self.admin.pk).update(**fields)
            self.assertEqual(self.client.post("/api/admin-terminal/").status_code, 403)

    def test_start_stop_csrf_and_logout(self):
        with patch("solar.core.terminal_views.shells") as manager:
            session = Mock()
            session.id = "test-terminal"
            session.status.return_value = {"id": session.id, "running": True}
            manager.start.return_value = manager.get.return_value = session
            self.assertEqual(self.client.post("/api/admin-terminal/").status_code, 200)
            manager.start.assert_called_once()
            self.assertEqual(self.client.delete("/api/admin-terminal/").status_code, 200)
            session.stop.assert_called_once()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post("/api/admin-terminal/").status_code, 403)
        with patch("solar.core.admin_terminal.shells.stop_for_session") as stop:
            key = self.client.session.session_key
            self.client.post("/api/logout/")
            stop.assert_called_once_with(key)

    def test_revoking_host_access_stops_active_shell(self):
        other = User.objects.create_superuser("terminal-other-admin", PASSWORD, must_change_password=False)
        with patch("solar.core.admin_terminal.shells.stop_for_user") as stop:
            result = self.client.patch(f"/api/users/{other.id}/", json.dumps({"current_password": PASSWORD, "host_access": False}), content_type="application/json")
            self.assertEqual(result.status_code, 200, result.content)
            stop.assert_called_once_with(other.id)

    def test_session_check_revokes_password_change_logout_or_admin_demotion(self):
        session = ShellSession(self.admin, self.client.session.session_key, Mock(), "PowerShell")
        self.assertTrue(session.authorized())
        User.objects.filter(pk=self.admin.pk).update(is_admin=False)
        self.assertFalse(session.authorized())
        User.objects.filter(pk=self.admin.pk).update(is_admin=True)
        self.admin.set_password("Another-long-test-passphrase-42!")
        self.admin.save(update_fields=["password"])
        self.assertFalse(session.authorized())
        self.client.force_login(self.admin)
        session.session_key = self.client.session.session_key
        self.assertTrue(session.authorized())
        self.client.logout()
        self.assertFalse(session.authorized())

    def test_shell_is_bound_to_admin_and_sign_in_session(self):
        manager = ShellManager()
        with patch("solar.core.admin_terminal.terminal") as process, patch.object(ShellSession, "start"):
            first = manager.start(self.admin, "session-one")
            self.assertIs(manager.start(self.admin, "session-one"), first)
            self.assertIs(manager.get(self.admin, "session-one", first.id), first)
            self.assertIsNone(manager.get(self.admin, "session-two", first.id))
            self.assertIsNone(manager.get(self.member, "session-one", first.id))
            with self.assertRaises(ValueError):
                manager.start(self.admin, "session-two")
            with self.assertRaises(PermissionError):
                manager.start(self.member, "session-one")
            process.assert_called_once()
            first.stop()
            self.assertTrue(first.closed.is_set())
            process.return_value.close.assert_called_once()


class TerminalLifecycleTests(SimpleTestCase):
    def user(self):
        return SimpleNamespace(id="owner", is_admin=True, is_active=True, host_access=True, must_change_password=False)

    def test_output_buffer_and_utf8_stream(self):
        process = Mock()
        process.read.side_effect = [b"prefix \xe2", b"\x82\xac", b"x" * 1048576, b""]
        session = ShellSession(self.user(), "key", process, "PowerShell")
        with patch.object(session, "stop"):
            session.read()
        sequence, output = session.output_since(0)
        self.assertEqual(sequence, 3)
        self.assertLessEqual(len(output), 1048576)
        self.assertEqual(session.output_since(sequence), (sequence, ""))

    def test_idle_and_revocation_end_terminal(self):
        for authorized, idle, expected in ((False, 0, "revoked"), (True, 100, "idle")):
            session = ShellSession(self.user(), "key", Mock(), "PowerShell")
            session.last_input = time.monotonic() - idle
            with patch.object(session.closed, "wait", return_value=False), patch.object(session, "authorized", return_value=authorized), \
                    patch("solar.core.admin_terminal.config", return_value={"execution": {"timeout_seconds": 1000, "native_idle_seconds": 1}}):
                session.watch()
            self.assertIn(expected, session.reason)
            self.assertTrue(session.closed.is_set())
            session.process.close.assert_called_once()

    def test_websocket_refuses_member_or_another_login_before_accept(self):
        from solar.asgi import websocket
        manager = ShellManager()
        owner = self.user()
        session = ShellSession(owner, "owner-login", Mock(), "PowerShell")
        manager.sessions[str(owner.id)] = session
        member = SimpleNamespace(**{**vars(owner), "is_admin": False})
        async def run(user, key):
            sent = []
            async def receive():
                return {"type": "websocket.connect"}
            async def send(message):
                sent.append(message)
            with patch("solar.asgi.socket_auth", return_value=(user, key)), patch("solar.asgi.shells", manager):
                await websocket({"path": f"/ws/admin-terminal/{session.id}/"}, receive, send)
            return sent
        for user, key in ((member, "owner-login"), (owner, "another-login")):
            self.assertEqual(asyncio.run(run(user, key)), [{"type": "websocket.close", "code": 4403}])

    def test_real_powershell_pty_roundtrip(self):
        from solar.core.processes import child_environment, terminal
        argv, name = shell_command()
        process = terminal(argv, config()["data_dir"], child_environment())
        chunks, found = [], threading.Event()
        def read():
            while True:
                chunk = process.read()
                if not chunk:
                    return
                chunks.append(chunk)
                if b"SOLAR_HOST_SHELL_OK" in b"".join(chunks):
                    found.set()
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        try:
            process.resize(100, 25)
            # Construct the marker to distinguish command execution from terminal echo.
            command = "Write-Output ('SOLAR_HOST_' + 'SHELL_OK')\r" if name == "PowerShell" else "printf 'SOLAR_HOST_%s\\n' 'SHELL_OK'\n"
            process.write(command)
            self.assertTrue(found.wait(12), "Shell did not return the expected marker")
        finally:
            process.close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_websocket_streams_resizes_and_writes_only_owned_terminal(self):
        from solar.asgi import websocket
        owner, manager = self.user(), ShellManager()
        session = ShellSession(owner, "login", Mock(), "PowerShell")
        session.buffer.append((1, "PowerShell ready\r\n"))
        session.sequence = 1
        manager.sessions[str(owner.id)] = session
        async def scenario():
            queue, sent = asyncio.Queue(), []
            for item in ({"type": "websocket.connect"},
                         {"type": "websocket.receive", "text": json.dumps({"type": "resize", "cols": 100, "rows": 25})},
                         {"type": "websocket.receive", "text": json.dumps({"type": "input", "data": "Get-Date\r"})},
                         {"type": "websocket.disconnect"}):
                queue.put_nowait(item)
            async def send(message):
                sent.append(message)
            with patch("solar.asgi.socket_auth", return_value=(owner, "login")), patch("solar.asgi.shells", manager):
                await websocket({"path": f"/ws/admin-terminal/{session.id}/"}, queue.get, send)
            return sent
        sent = asyncio.run(scenario())
        self.assertEqual(sent[0]["type"], "websocket.accept")
        self.assertTrue(any("PowerShell ready" in item.get("text", "") for item in sent))
        session.process.resize.assert_called_once_with(100, 25)
        session.process.write.assert_called_once_with("Get-Date\r")
        session.process.close.assert_not_called()  # navigation preserves the terminal for reconnect

    def test_websocket_rechecks_admin_role_and_stops_revoked_shell(self):
        from solar.asgi import websocket
        owner, manager = self.user(), ShellManager()
        session = ShellSession(owner, "login", Mock(), "PowerShell")
        manager.sessions[str(owner.id)] = session
        revoked = SimpleNamespace(**{**vars(owner), "is_admin": False})
        async def scenario():
            sent = []
            async def receive():
                return {"type": "websocket.connect"}
            async def send(message):
                sent.append(message)
            with patch("solar.asgi.socket_auth", side_effect=[(owner, "login"), (revoked, "login")]), patch("solar.asgi.shells", manager):
                await websocket({"path": f"/ws/admin-terminal/{session.id}/"}, receive, send)
            return sent
        sent = asyncio.run(scenario())
        self.assertEqual([item["type"] for item in sent], ["websocket.accept", "websocket.close"])
        session.process.close.assert_called_once()
        session.process.write.assert_not_called()

    def test_static_module_types_and_csp(self):
        from solar.asgi import static_file
        async def headers(path):
            sent = []
            async def send(message):
                sent.append(message)
            await static_file({"path": path, "method": "HEAD"}, send)
            self.assertEqual(sent[0]["status"], 200)
            return dict(sent[0]["headers"])
        module = asyncio.run(headers("/static/vendor/pdf.min.mjs"))
        self.assertEqual(module[b"content-type"], b"text/javascript")
        self.assertIn(b"wasm-unsafe-eval", module[b"content-security-policy"])
        self.assertNotIn(b"'unsafe-eval'", module[b"content-security-policy"])


class ViewerInstallerTests(SimpleTestCase):
    def test_pinned_download_rejects_corruption_and_keeps_existing_file(self):
        with tempfile.TemporaryDirectory(dir=config()["data_dir"]) as directory:
            path = Path(directory) / "runtime.wasm"
            path.write_bytes(b"old")
            with patch.object(viewer_assets, "CAD_PATH", path), patch.object(viewer_assets.urllib.request, "urlopen", return_value=io.BytesIO(b"corrupt")):
                with self.assertRaisesRegex(ValueError, "pinned"):
                    viewer_assets.ensure_cad_runtime()
            self.assertEqual(path.read_bytes(), b"old")

    def test_verified_asset_is_atomic_and_reused_offline(self):
        data = b"\0asm\x01\0\0\0"
        digest = hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest()
        with tempfile.TemporaryDirectory(dir=config()["data_dir"]) as directory, patch.object(viewer_assets, "CAD_SIZE", len(data)), patch.object(viewer_assets, "CAD_BLOB", digest):
            path = Path(directory) / "runtime.wasm"
            with patch.object(viewer_assets, "CAD_PATH", path), patch.object(viewer_assets.urllib.request, "urlopen", return_value=io.BytesIO(data)) as download:
                viewer_assets.ensure_cad_runtime()
                self.assertEqual(path.read_bytes(), data)
                viewer_assets.ensure_cad_runtime()
                download.assert_called_once()
                self.assertEqual(download.call_args.args[0].full_url, viewer_assets.CAD_URL)
            self.assertEqual(list(Path(directory).iterdir()), [path])
