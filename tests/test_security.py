import asyncio
from datetime import timedelta
import io
import json
from pathlib import Path
import stat
from unittest.mock import patch
import uuid
import zipfile

from cryptography.fernet import InvalidToken
from django.contrib.auth import SESSION_KEY, BACKEND_SESSION_KEY, HASH_SESSION_KEY
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from solar.core.models import Agent, Chat, Event, Provider, Run, Schedule, Skill, User
from solar.core import vault
from solar.core.providers import validate_endpoint
from solar.core.runner import queue_run, cancel_run
from solar.crypto import cipher

PASSWORD = "Test-only-passphrase-42!"


@override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
class SecurityTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("alice-test", PASSWORD, must_change_password=False)
        self.other = User.objects.create_user("bob-test", PASSWORD, must_change_password=False)
        self.admin = User.objects.create_superuser("admin-test", PASSWORD, must_change_password=False)
        self.provider = Provider.objects.create(owner=self.owner, name="Private test provider", kind="openai",
                                                model="test-model", configuration=json.dumps({"base_url": "http://127.0.0.1:11434/v1", "api_key": "private-test-key"}))
        self.chat = Chat.objects.create(owner=self.owner, provider=self.provider, title="Confidential project")
        self.client.force_login(self.owner)

    def post(self, path, data, method="post"):
        return getattr(self.client, method)(path, data=json.dumps(data), content_type="application/json")

    def test_credentials_and_content_are_encrypted_in_database(self):
        Event.objects.create(chat=self.chat, role="user", content="sensitive-message-unique")
        with connection.cursor() as cursor:
            cursor.execute("SELECT username, password FROM core_user WHERE login_id=%s", [self.owner.login_id])
            name, password = cursor.fetchone()
            self.assertTrue(name.startswith("enc:v1:"))
            self.assertNotIn("alice", name)
            self.assertTrue(password.startswith("argon2$"))
            self.assertNotIn(PASSWORD, password)
            cursor.execute("SELECT title FROM core_chat")
            self.assertNotIn("Confidential", cursor.fetchone()[0])
            cursor.execute("SELECT content FROM core_event")
            self.assertNotIn("sensitive-message", cursor.fetchone()[0])
            cursor.execute("SELECT configuration FROM core_provider")
            self.assertNotIn("private-test-key", cursor.fetchone()[0])
        self.assertTrue(self.owner.check_password(PASSWORD))

    def test_login_and_logout(self):
        self.client.logout()
        result = self.post("/api/login/", {"username": "ALICE-TEST", "password": PASSWORD})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["user"]["id"], str(self.owner.id))
        self.assertTrue(result.cookies["solar_session"]["httponly"])
        self.assertTrue(result.cookies["solar_session"]["secure"])
        self.assertEqual(result.cookies["solar_session"]["samesite"], "Strict")
        self.assertEqual(self.post("/api/logout/", {}).status_code, 200)
        self.assertEqual(self.client.get("/api/chats/").status_code, 401)

    def test_login_throttle(self):
        self.client.logout()
        for _ in range(10):
            response = self.post("/api/login/", {"username": self.owner.username, "password": "wrong"})
            self.assertEqual(response.status_code, 401)
        response = self.post("/api/login/", {"username": self.owner.username, "password": PASSWORD})
        self.assertEqual(response.status_code, 429)

    def test_csrf_is_required_including_login(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post("/api/login/", data=json.dumps({"username": "x", "password": "y"}),
                                     content_type="application/json").status_code, 403)
        client.force_login(self.owner)
        self.assertEqual(client.post("/api/chats/", data="{}", content_type="application/json").status_code, 403)

    def test_temporary_password_gate(self):
        self.owner.must_change_password = True
        self.owner.save()
        self.assertEqual(self.client.get("/api/chats/").status_code, 403)
        self.assertEqual(self.client.get("/api/me/").status_code, 200)
        with patch("solar.core.views.save_config"):
            changed = self.post("/api/password/", {"current_password": PASSWORD, "password": "A-new-long-test-password-123"})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.client.get("/api/chats/").status_code, 200)

    def test_password_change_invalidates_other_sessions(self):
        second = Client()
        second.force_login(self.owner)
        self.post("/api/password/", {"current_password": PASSWORD, "password": "Changed-test-passphrase-123!"})
        self.assertEqual(second.get("/api/chats/").status_code, 401)
        self.assertEqual(self.client.get("/api/chats/").status_code, 200)

    def test_user_cannot_read_other_chat_even_if_administrator(self):
        file = vault.put_file(self.chat, "user/files/test.txt", b"a secret")
        run = Run.objects.create(chat=self.chat)
        for user in (self.other, self.admin):
            self.client.force_login(user)
            self.assertEqual(self.client.get("/api/chats/").json()["chats"], [])
            for path in (f"/api/chats/{self.chat.id}/", f"/api/chats/{self.chat.id}/events/",
                         f"/api/chats/{self.chat.id}/files/", f"/api/chats/{self.chat.id}/export/",
                         f"/api/files/{file.id}/", f"/api/providers/{self.provider.id}/models/"):
                with self.subTest(path=path, user=user.id):
                    self.assertEqual(self.client.get(path).status_code, 404)
            self.assertEqual(self.post(f"/api/runs/{run.id}/stop/", {}).status_code, 404)
            self.assertEqual(self.client.delete(f"/api/files/{file.id}/").status_code, 404)

    def test_cross_owner_resource_assignment_rejected(self):
        agent = Agent.objects.create(owner=self.other, name="Other", role="Other", instructions="Other")
        skill = Skill.objects.create(owner=self.other, name="Other skill", content="Private")
        provider = Provider.objects.create(owner=self.other, name="Other provider", kind="openai")
        for data in ({"agent_id": str(agent.id)}, {"skills": [str(skill.id)]}, {"provider_id": str(provider.id)}):
            self.assertEqual(self.post(f"/api/chats/{self.chat.id}/", data, "patch").status_code, 404)
        result = self.post("/api/agents/", {"name": "Test", "role": "Reviewer", "instructions": "Review", "delegates": [str(agent.id)]})
        self.assertEqual(result.status_code, 404)

    def test_api_never_returns_provider_secrets(self):
        result = self.client.get("/api/providers/")
        self.assertNotIn("private-test-key", result.content.decode())
        self.assertTrue(result.json()["providers"][0]["has_api_key"])

    def test_member_cannot_manage_users_or_run_host(self):
        self.assertEqual(self.client.get("/api/users/").status_code, 403)
        provider = Provider.objects.create(owner=self.owner, name="Codex", kind="codex")
        chat = Chat.objects.create(owner=self.owner, provider=provider)
        self.assertEqual(self.post(f"/api/chats/{chat.id}/messages/", {"message": "Hi"}).status_code, 403)
        self.assertEqual(self.post(f"/api/chats/{chat.id}/native/", {}).status_code, 403)
        self.assertEqual(self.client.get("/api/desktop/").status_code, 403)

    def test_admin_actions_require_password_and_keep_last_admin(self):
        self.client.force_login(self.admin)
        data = {"username": "new-user", "password": PASSWORD, "current_password": "bad"}
        self.assertEqual(self.post("/api/users/", data).status_code, 403)
        data["current_password"] = PASSWORD
        self.assertEqual(self.post("/api/users/", data).status_code, 201)
        self.assertEqual(self.post(f"/api/users/{self.admin.id}/", {"is_admin": False, "current_password": PASSWORD}, "patch").status_code, 400)

    def test_disabled_user_loses_session(self):
        self.owner.is_active = False
        self.owner.save()
        self.assertEqual(self.client.get("/api/chats/").status_code, 401)

    def test_malformed_requests_are_rejected(self):
        self.assertEqual(self.client.post("/api/chats/", data="[]", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post("/api/chats/", data="not json", content_type="application/json").status_code, 400)
        self.assertEqual(self.post(f"/api/chats/{self.chat.id}/", {"archived": "false"}, "patch").status_code, 400)
        self.assertEqual(self.client.put("/api/chats/").status_code, 405)

    def test_security_headers_and_no_debug(self):
        result = self.client.get("/")
        self.assertEqual(result.status_code, 200)
        self.assertIn("script-src 'self'", result["Content-Security-Policy"])
        self.assertNotIn("'unsafe-eval'", result["Content-Security-Policy"])
        self.assertEqual(result["Cache-Control"], "no-store")
        self.assertEqual(result["X-Frame-Options"], "DENY")
        self.assertEqual(result["X-Content-Type-Options"], "nosniff")

    def test_file_encryption_and_cross_chat_key(self):
        file = vault.put_file(self.chat, "user/files/deep/report.txt", b"private test bytes")
        raw = vault.blob_path(file).read_bytes()
        self.assertNotIn(b"private test bytes", raw)
        self.assertEqual(vault.read_file(file), b"private test bytes")
        other = Chat.objects.create(owner=self.owner, provider=self.provider)
        with self.assertRaises(InvalidToken):
            vault.file_cipher(other).decrypt(raw)
        tampered = bytearray(raw)
        tampered[len(tampered)//2] ^= 1
        vault.blob_path(file).write_bytes(tampered)
        with self.assertRaises(InvalidToken):
            vault.read_file(file)

    def test_files_upload_and_folder_export(self):
        files = [SimpleUploadedFile("note.txt", b"hello"), SimpleUploadedFile("next.txt", b"next")]
        result = self.client.post(f"/api/chats/{self.chat.id}/files/", {"files": files, "paths": ["folder/note.txt", "folder/nested/next.txt"]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json()["files"]), 2)
        result = self.client.get(f"/api/chats/{self.chat.id}/export/?prefix=user/files/folder")
        with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["user/files/folder/nested/next.txt", "user/files/folder/note.txt"])
        file_id = self.chat.files.first().id
        result = self.client.get(f"/api/files/{file_id}/")
        self.assertTrue(result["Content-Disposition"].startswith("attachment"))

    def test_traversal_and_windows_names_rejected(self):
        for path in ("../escape", "/absolute", "C:\\escape", "a/../../b", "a\\..\\b", "x:secret", "CON.txt", "NUL", "a/.", "a//b", "x\0y", "file.", "trailing "):
            with self.subTest(path=path), self.assertRaises(ValueError):
                vault.safe_path(path)
        self.assertEqual(vault.safe_path("folder/مرحبا.txt"), "folder/مرحبا.txt")

    def test_zip_slip_and_links_rejected(self):
        for name, mode in (("../outside", 0), ("C:/outside", 0), ("link", stat.S_IFLNK | 0o777)):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                archive.writestr(info, b"target")
            with self.assertRaises(ValueError):
                vault.zip_members(buffer.getvalue())

    def test_zip_expansion_limit(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("large.txt", b"0"*1_000_000)
        with self.assertRaises(ValueError):
            vault.zip_members(buffer.getvalue())

    def test_duplicate_case_paths_overwrite_single_record(self):
        vault.put_file(self.chat, "user/files/Name.txt", b"one")
        file = vault.put_file(self.chat, "user/files/name.txt", b"two")
        self.assertEqual(self.chat.files.count(), 1)
        self.assertEqual(vault.read_file(file), b"two")

    def test_svg_cannot_preview_as_active_content(self):
        file = vault.put_file(self.chat, "user/files/evil.svg", b'<svg onload="alert(1)"/>')
        self.assertEqual(self.client.get(f"/api/files/{file.id}/?preview=true").status_code, 400)
        self.assertIn("attachment", self.client.get(f"/api/files/{file.id}/")["Content-Disposition"])

    def test_uploads_blocked_during_runs(self):
        Run.objects.create(chat=self.chat)
        result = self.client.post(f"/api/chats/{self.chat.id}/files/", {"files": SimpleUploadedFile("x.txt", b"x")})
        self.assertEqual(result.status_code, 400)

    def test_quota_is_enforced(self):
        from solar.config import config
        with patch.dict(config()["storage"], {"quota_mb": 0}):
            with self.assertRaises(ValueError):
                vault.put_file(self.chat, "user/files/full.txt", b"x")

    def test_manifests_encrypted_and_exported(self):
        Event.objects.create(chat=self.chat, role="user", content="Unique-secret-history")
        vault.sync_manifests(self.chat)
        root = vault.chat_directory(self.chat)
        self.assertTrue((root / "chat_history.txt.enc").exists())
        self.assertNotIn(b"Unique-secret-history", (root / "chat_history.txt.enc").read_bytes())
        with zipfile.ZipFile(io.BytesIO(vault.export_zip(self.chat))) as archive:
            self.assertIn("codex/agents/", archive.namelist())
            self.assertIn(b"Unique-secret-history", archive.read("chat_history.txt"))

    def test_queue_has_one_active_run_and_cancel(self):
        run = queue_run(self.chat, "Task")
        with self.assertRaises(ValueError):
            queue_run(self.chat, "Second task")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Run.objects.create(chat=self.chat)
        cancel_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")
        self.assertIsNotNone(queue_run(self.chat, "New task"))

    def test_slash_commands_do_not_call_a_model(self):
        for command in ("/effort high", "/permissions workspace-write", "/model test-model", "/help", "/status"):
            result = self.post(f"/api/chats/{self.chat.id}/messages/", {"message": command})
            self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(Run.objects.count(), 0)
        self.chat.refresh_from_db()
        self.assertEqual(self.chat.effort, "high")
        self.assertEqual(self.chat.permission, "workspace-write")
        self.assertEqual(self.post(f"/api/chats/{self.chat.id}/messages/", {"message": "/unknown"}).status_code, 400)

    def test_endpoint_allowlist_blocks_ssrf(self):
        for url in ("http://169.254.169.254/latest/meta-data", "http://localhost:22", "file:///etc/passwd",
                    "http://127.0.0.1:11434@evil.test/v1", "http://127.0.0.1:11434/v1?x=y",
                    "http://127.0.0.1:11434/v1/../admin", "http://127.0.0.1:11434/%2e%2e", "https://api.openai.com.evil.test"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_endpoint(url)
        self.assertEqual(validate_endpoint("http://127.0.0.1:11434/v1/"), "http://127.0.0.1:11434/v1")

    def test_skill_import_does_not_execute_resources(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("test/SKILL.md", "---\nname: test-skill\n---\nInstructions")
            archive.writestr("test/script.py", "raise RuntimeError('must not execute')")
        result = self.client.post("/api/skills/", {"file": SimpleUploadedFile("skill.zip", buffer.getvalue())})
        self.assertEqual(result.status_code, 201)
        skill = Skill.objects.get(pk=result.json()["id"])
        self.assertTrue(skill.bundle)
        self.assertNotIn(b"RuntimeError", bytes(skill.bundle))

    def test_desktop_requires_explicit_start(self):
        self.client.force_login(self.admin)
        with patch("solar.core.desktop.desktop.start") as start, patch("solar.core.desktop.desktop.status", return_value={"running": False}):
            self.assertEqual(self.client.get("/api/desktop/").status_code, 200)
            start.assert_not_called()

    def test_websocket_origin_and_session_checks(self):
        from solar.asgi import socket_auth
        session = self.client.session
        scope = {"headers": [(b"host", b"localhost:5557"), (b"origin", b"https://localhost:5557"),
                             (b"cookie", f"solar_session={session.session_key}".encode())]}
        user, key = socket_auth(scope)
        self.assertEqual(user.id, self.owner.id)
        self.assertEqual(key, session.session_key)
        for origin in (b"https://evil.test", b"http://localhost:5557", b"null", b"https://localhost:5557@evil.test"):
            scope["headers"][1] = (b"origin", origin)
            self.assertEqual(socket_auth(scope), (None, None))

    def test_oversized_body_rejected_before_django(self):
        from solar.asgi import application
        messages = []
        async def receive():
            return {"type": "http.request", "body": b""}
        async def send(message):
            messages.append(message)
        asyncio.run(application({"type": "http", "path": "/api/chats/", "headers": [(b"content-length", b"200000000")]}, receive, send))
        self.assertEqual(messages[0]["status"], 413)
