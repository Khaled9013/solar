"""Regression coverage for recovery, permissions, and interactive boundaries."""
import asyncio
import io
import json
from pathlib import Path
import tempfile
import tomllib
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase, override_settings

from solar.config import config
from solar.core.desktop import Desktop
from solar.core.models import Agent, Chat, Provider, Skill, StoredFile, User
from solar.core.providers import claude_tools, persist_cli_state, setup_cli
from solar.core.vault import collect_workspace, file_cipher, materialize, put_file, read_file, safe_path


PASSWORD = "Hardening-test-password-42!"


@override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
class BoundaryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("boundary-test", PASSWORD, must_change_password=False)
        self.provider = Provider.objects.create(owner=self.user, name="Test API", kind="openai")
        self.chat = Chat.objects.create(owner=self.user, provider=self.provider, title="Original private session")
        self.client.force_login(self.user)

    def test_password_whitespace_is_significant(self):
        password = "  Spaces-are-part-of-this-password-42!  "
        response = self.client.post("/api/password/", json.dumps({"current_password": PASSWORD, "password": password}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.client.logout()
        for value, status in ((password.strip(), 401), (password, 200)):
            result = self.client.post("/api/login/", json.dumps({"username": self.user.username, "password": value}),
                                      content_type="application/json")
            self.assertEqual(result.status_code, status)

    def test_provider_default_model_applies_to_chats_and_schedules(self):
        self.provider.model = "configured-default-model"
        self.provider.save()
        for endpoint, payload in (("chats", {"title": "Uses provider default"}),
                                  ("schedules", {"name": "Uses provider default", "prompt": "Do a task"})):
            result = self.client.post(f"/api/{endpoint}/", json.dumps({**payload, "provider_id": str(self.provider.id), "model": ""}),
                                       content_type="application/json")
            self.assertEqual(result.status_code, 201, result.content)
            self.assertEqual(result.json()["model"], "configured-default-model")

    def test_chat_pagination_search_archive_and_owner_filter(self):
        Chat.objects.bulk_create([Chat(owner=self.user, provider=self.provider, title=f"Session {i:03}") for i in range(202)])
        other = User.objects.create_user("boundary-other", PASSWORD)
        Chat.objects.create(owner=other, provider=self.provider, title="Original another user's session")
        archived = Chat.objects.create(owner=self.user, provider=self.provider, title="Original archived", archived=True)
        first = self.client.get("/api/chats/").json()
        self.assertEqual(first["total"], 203)
        self.assertEqual(len(first["chats"]), 200)
        second = self.client.get(f"/api/chats/?offset={first['next_offset']}").json()
        self.assertEqual(len(second["chats"]), 3)
        self.assertIsNone(second["next_offset"])
        self.assertFalse({r["id"] for r in first["chats"]} & {r["id"] for r in second["chats"]})
        self.assertEqual([r["id"] for r in self.client.get("/api/chats/?q=ORIGINAL").json()["chats"]], [str(self.chat.id)])
        self.assertEqual([r["id"] for r in self.client.get("/api/chats/?archived=true&q=original").json()["chats"]], [str(archived.id)])

    def test_file_folder_conflicts_and_unicode_normalization(self):
        original = put_file(self.chat, "user/files/folder/item.txt", b"one")
        with self.assertRaisesRegex(ValueError, "file and a folder"):
            put_file(self.chat, "user/files/FOLDER", b"two")
        with self.assertRaisesRegex(ValueError, "file and a folder"):
            put_file(self.chat, "user/files/folder/ITEM.txt/nested", b"two")
        self.assertEqual(read_file(original), b"one")
        first = put_file(self.chat, "user/files/caf\u00e9.txt", b"first")
        second = put_file(self.chat, "user/files/cafe\u0301.txt", b"second")
        self.assertEqual(first.id, second.id)
        self.assertEqual(read_file(second), b"second")
        with self.assertRaises(ValueError):
            safe_path("user/files/invalid\x7fname")
        for name in ("COM¹.txt", "LPT²", "CON .txt"):
            with self.assertRaises(ValueError):
                safe_path("user/files/" + name)

    def test_removing_assigned_role_cannot_silently_expand_permissions(self):
        role = Agent.objects.create(owner=self.user, name="Restricted", role="Read", instructions="Read only")
        self.chat.agent = role
        self.chat.save()
        self.assertEqual(self.client.delete(f"/api/agents/{role.id}/").status_code, 400)
        self.chat.agent = None
        self.chat.save()
        self.assertEqual(self.client.delete(f"/api/agents/{role.id}/").status_code, 200)

    def test_skill_export_preserves_resources_and_updated_instructions(self):
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("demo/SKILL.md", "---\nname: demo\n---\nOriginal")
            archive.writestr("demo/resources/example.txt", "Resource")
        response = self.client.post("/api/skills/", {"file": SimpleUploadedFile("skill.zip", content.getvalue())})
        self.assertEqual(response.status_code, 201)
        skill_id = response.json()["id"]
        result = self.client.patch(f"/api/skills/{skill_id}/", json.dumps({"content": "Updated instructions"}),
                                    content_type="application/json")
        self.assertEqual(result.status_code, 200)
        result = self.client.get(f"/api/skills/{skill_id}/?download=1")
        self.assertEqual(result.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
            self.assertEqual(archive.read("SKILL.md"), b"Updated instructions")
            self.assertEqual(archive.read("resources/example.txt"), b"Resource")

    def test_failed_database_write_preserves_committed_file_revision(self):
        original = put_file(self.chat, "user/files/important.txt", b"Last committed data")
        with patch.object(StoredFile, "save", side_effect=IntegrityError("simulated write failure")):
            with self.assertRaises(IntegrityError):
                put_file(self.chat, "user/files/important.txt", b"Uncommitted replacement")
        original.refresh_from_db()
        self.assertEqual(read_file(original), b"Last committed data")

    def test_anonymous_upload_rejected_before_reading_body(self):
        from solar.asgi import application
        messages = []
        async def receive():
            self.fail("Anonymous uploads must not be buffered")
        async def send(message):
            messages.append(message)
        scope = {"type": "http", "method": "POST", "path": f"/api/chats/{self.chat.id}/files/",
                 "headers": [(b"content-type", b"multipart/form-data; boundary=test")]}
        asyncio.run(application(scope, receive, send))
        self.assertEqual(messages[0]["status"], 401)

    def test_websocket_rejects_password_reset_and_revoked_accounts(self):
        from solar.asgi import socket_auth
        scope = {"headers": [(b"host", b"localhost:5557"), (b"origin", b"https://localhost:5557"),
                              (b"cookie", f"solar_session={self.client.session.session_key}".encode())]}
        self.assertEqual(socket_auth(scope)[0].id, self.user.id)
        self.user.must_change_password = True
        self.user.save()
        self.assertEqual(socket_auth(scope), (None, None))
        self.user.must_change_password = False
        self.user.is_active = False
        self.user.save()
        self.assertEqual(socket_auth(scope), (None, None))


class NativeConfigurationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("native-config-test", PASSWORD, host_access=True)
        self.provider = Provider.objects.create(owner=self.user, name="Test CLI", kind="codex")
        self.lead = Agent.objects.create(owner=self.user, name="Lead", role="Plan", instructions="Delegate precisely",
                                         permission="read-only", allow_shell=False, allow_network=False)
        self.child = Agent.objects.create(owner=self.user, name="Worker", role="Implement", instructions="Use resources",
                                          permission="workspace-write", allow_shell=True, allow_network=True)
        self.other = User.objects.create_user("other-native-config-test", PASSWORD)
        self.foreign = Agent.objects.create(owner=self.other, name="Foreign", role="Private", instructions="Private")
        self.lead.delegates.add(self.child, self.foreign)
        self.chat = Chat.objects.create(owner=self.user, provider=self.provider, agent=self.lead,
                                        permission="workspace-write")
        self.root = materialize(self.chat)
        self.home_patch = patch("solar.core.providers.Path.home", return_value=self.root / "nonexistent-os-home")
        self.home_patch.start()
        self.executable_patch = patch("solar.core.providers.executable", side_effect=lambda name: name)
        self.executable_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        self.executable_patch.stop()
        collect_workspace(self.chat)

    def test_codex_native_roles_inherit_restrictions_and_scope(self):
        argv, env, _ = setup_cli(self.chat, self.root)
        roles = list((self.root / ".codex" / "agents").glob("*.toml"))
        self.assertEqual(len(roles), 1)
        role = tomllib.loads(roles[0].read_text(encoding="utf-8"))
        self.assertEqual(role["sandbox_mode"], "read-only")
        self.assertFalse(role["features"]["shell_tool"])
        self.assertFalse(role["features"]["multi_agent"])
        self.assertFalse(role["sandbox_workspace_write"]["network_access"])
        self.assertNotIn("--ignore-user-config", argv)
        self.assertEqual(env["CODEX_HOME"], str(self.root / ".codex-home"))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("SOLAR_HOME", env)

    def test_codex_api_key_uses_stdin_and_never_copies_host_login(self):
        self.provider.configuration = json.dumps({"api_key": "synthetic-provider-key", "auth_json": "old-login"})
        with patch("solar.core.providers.subprocess.run", return_value=SimpleNamespace(returncode=0)) as login:
            argv, _, home = setup_cli(self.chat, self.root)
        args, kwargs = login.call_args
        self.assertNotIn("synthetic-provider-key", " ".join(args[0]))
        self.assertEqual(kwargs["input"], b"synthetic-provider-key")
        self.assertIn("--with-api-key", args[0])
        self.assertEqual(kwargs["env"]["CODEX_HOME"], str(home))
        self.assertFalse((home / "auth.json").exists())  # mocked login; old account was not copied
        self.assertIn('cli_auth_credentials_store="file"', argv)

    def test_claude_uses_only_selected_roles_and_tools(self):
        self.provider.kind = "claude"
        argv, _, _ = setup_cli(self.chat, self.root)
        roles = list((self.root / ".claude" / "agents").glob("*.md"))
        self.assertEqual(len(roles), 2)  # main role + one allowed delegate
        main = (self.root / ".claude" / "agents" / "solar_main.md").read_text(encoding="utf-8")
        self.assertIn("Agent(solar_" + self.child.id.hex + ")", main)
        self.assertNotIn(self.foreign.id.hex, main)
        self.assertNotIn("Bash", argv)
        self.assertNotIn("Write", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("dontAsk", argv)
        self.assertNotIn("Bash", claude_tools("read-only", True, False))

    def test_native_delegate_skill_resources_are_materialized(self):
        from solar.crypto import cipher
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("SKILL.md", "Bundled instructions")
            archive.writestr("resources/config.txt", "Private role resource")
        skill = Skill(owner=self.user, name="Delegate skill", content="Bundled instructions")
        skill.bundle = cipher(f"skill:{skill.id}").encrypt(bundle.getvalue())
        skill.save()
        self.child.skills.add(skill)
        setup_cli(self.chat, self.root)
        resource = self.root / ".agents" / "skills" / str(skill.id) / "resources" / "config.txt"
        self.assertEqual(resource.read_text(), "Private role resource")

    def test_native_state_is_encrypted_and_can_restore_compressible_history(self):
        _, _, home = setup_cli(self.chat, self.root)
        (home / "history.jsonl").write_bytes(b"x" * 200_000)
        (home / "sessions").mkdir()
        (home / "sessions" / "test.jsonl").write_text('{"type":"session_meta"}\n', encoding="utf-8")
        (home / "auth.json").write_text('{"token":"test-credential"}', encoding="utf-8")
        persist_cli_state(self.chat, home, native=True)
        self.chat.refresh_from_db()
        self.assertNotIn(b"test-credential", bytes(self.chat.native_state))
        with zipfile.ZipFile(io.BytesIO(file_cipher(self.chat).decrypt(bytes(self.chat.native_state)))) as archive:
            self.assertNotIn("auth.json", archive.namelist())
        (home / "history.jsonl").unlink()
        argv, _, _ = setup_cli(self.chat, self.root, native=True)
        self.assertEqual((home / "history.jsonl").read_bytes(), b"x" * 200_000)
        self.assertIn("resume", argv)


class DesktopBoundaryTests(SimpleTestCase):
    def test_lease_requires_explicit_start_and_is_bound_to_session(self):
        desktop = Desktop()
        user = SimpleNamespace(id=uuid.uuid4(), remote_access=True)
        self.assertFalse(desktop.status(user, "one")["running"])
        with patch("solar.core.desktop.threading.Thread") as thread:
            status = desktop.start(user, "one")
            thread.return_value.start.assert_called_once()
        self.assertTrue(desktop.verify(user.id, "one", status["lease_id"]))
        self.assertFalse(desktop.verify(user.id, "two", status["lease_id"]))
        self.assertIsNone(desktop.status(user, "two")["lease_id"])
        with self.assertRaises(ValueError):
            desktop.start(user, "two")
        desktop.stop_for_session("two")
        self.assertTrue(desktop.lease)
        desktop.stop_for_session("one")
        self.assertIsNone(desktop.lease)

    def test_drag_and_release_never_call_real_input_controllers(self):
        # Mock the modules themselves: this test must never inject an OS input event.
        buttons = SimpleNamespace(left="left", middle="middle", right="right")
        keys = SimpleNamespace(**{name: name for name in (
            "enter", "esc", "tab", "backspace", "delete", "left", "right", "up", "down", "home", "end",
            "page_up", "page_down", "space", "ctrl", "alt", "shift", "cmd", *(f"f{i}" for i in range(1, 13)))})
        modules = {"pynput.mouse": SimpleNamespace(Button=buttons), "pynput.keyboard": SimpleNamespace(Key=keys)}
        mouse, keyboard, held = Mock(), Mock(), set()
        monitor = {"left": -1920, "top": 0, "width": 1920, "height": 1080}
        with patch.dict("sys.modules", modules):
            Desktop.apply_input({"type": "button", "down": True, "x": 1, "y": 1}, mouse, keyboard, monitor, held)
            self.assertEqual(mouse.position, (-1, 1079))
            self.assertEqual(held, {"left"})
            Desktop.apply_input({"type": "release"}, mouse, keyboard, monitor, held)
            self.assertFalse(held)
            mouse.release.assert_called_with("left")
            Desktop.apply_input({"type": "key", "key": "a", "ctrl": True}, mouse, keyboard, monitor, held)
            self.assertEqual(keyboard.press.call_args_list[0].args, ("ctrl",))
            self.assertEqual(keyboard.release.call_args_list[-1].args, ("ctrl",))


class ConfigurationBoundaryTests(SimpleTestCase):
    def test_windows_npm_codex_discovery_supports_current_packaged_layout(self):
        from solar.core.processes import executable
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent / ".solar-test-suite") as directory:
            root = Path(directory)
            binary = root / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"Synthetic file; never execute")
            def which(name):
                return str(root / "codex.cmd") if name in ("codex", "codex.cmd") else None
            with patch("solar.core.processes.os", SimpleNamespace(name="nt")), \
                    patch("solar.core.processes.shutil.which", side_effect=which), \
                    patch("solar.core.processes.platform.machine", return_value="AMD64"):
                self.assertEqual(Path(executable("codex")), binary)

    def test_tls_cannot_be_disabled_on_lan_and_limits_are_bounded(self):
        config.cache_clear()
        try:
            with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent / ".solar-test-suite") as directory:
                path = Path(directory) / "solar.conf"
                with patch("solar.config.CONFIG_PATH", path):
                    for text in ('[server]\nhosts=["0.0.0.0"]\ntls=false', '[execution]\nmax_workers=1000',
                                 '[server]\nallowed_hosts=["*"]'):
                        config.cache_clear()
                        path.write_text(text, encoding="utf-8")
                        with self.assertRaises(ValueError):
                            config()
                    config.cache_clear()
                    path.write_text('[server]\nhosts=["127.0.0.1", "::1"]\ntls=false', encoding="utf-8")
                    self.assertFalse(config()["server"]["tls"])
        finally:
            config.cache_clear()
