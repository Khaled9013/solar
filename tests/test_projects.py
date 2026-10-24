"""Workspace and policy regression tests. All paths, accounts and provider replies are synthetic."""
import asyncio
from datetime import timedelta
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import uuid

import httpx

from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, TransactionTestCase, SimpleTestCase, override_settings
from django.utils import timezone

from solar.config import config
from solar.core.models import Agent, Chat, Event, Project, Provider, Run, Schedule, ScheduleRun, User
from solar.core.projects import execution_directory, project_lock
from solar.core.providers import instructions, persist_cli_state, prompt_text, run_api, setup_cli
from solar.core.runner import execute, queue_run, tick_schedules
from solar.core.vault import collect_workspace, materialize, put_file, read_file, workspace_path
from solar.crypto import digest

PASSWORD = "Synthetic-project-passphrase-42!"


class ProjectFixture:
    def setUp(self):
        self.user = User.objects.create_user("project-operator", PASSWORD, host_access=True, must_change_password=False)
        self.provider = Provider.objects.create(owner=self.user, kind="codex", name="Synthetic Codex")
        parent = Path(__file__).resolve().parent.parent / "artifacts"
        parent.mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(prefix="project-tests-", dir=parent)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.project = Project.objects.create(owner=self.user, name="Sample project", root_path=str(self.root),
                                               path_hash=digest(os.path.normcase(str(self.root)), "project-path"))
        self.chat = Chat.objects.create(owner=self.user, provider=self.provider, project=self.project, permission="workspace-write")
        self.client.force_login(self.user)

    def request(self, path, data, method="post"):
        return getattr(self.client, method)(path, json.dumps(data), content_type="application/json")


@override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver"])
class ProjectAccessTests(ProjectFixture, TestCase):
    def test_register_create_select_and_unregister_preserves_folder(self):
        target = self.root / "New project"
        result = self.request("/api/projects/", {"name":"New project", "path":str(target), "create":True})
        self.assertEqual(result.status_code, 201, result.content)
        project_id = result.json()["id"]
        self.assertTrue(target.is_dir())
        result = self.request(f"/api/chats/{self.chat.id}/", {"project_id":project_id}, "patch")
        self.assertEqual(result.status_code, 200, result.content)
        self.chat.refresh_from_db()
        self.assertEqual(execution_directory(self.chat), target)
        self.assertEqual(self.client.delete(f"/api/projects/{project_id}/").status_code, 400)
        self.request(f"/api/chats/{self.chat.id}/", {"project_id":None}, "patch")
        self.assertEqual(self.client.delete(f"/api/projects/{project_id}/").status_code, 200)
        self.assertTrue(target.is_dir())

    def test_project_identity_and_path_are_encrypted(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT name, root_path FROM core_project WHERE id=%s", [self.project.id.hex])
            name, path = cursor.fetchone()
        self.assertNotIn(self.project.name, name)
        self.assertNotIn(str(self.root), path)

    def test_host_access_and_ownership_apply_to_all_project_endpoints(self):
        other = User.objects.create_user("other-project-user", PASSWORD, host_access=True, must_change_password=False)
        self.client.force_login(other)
        for suffix in ("tree/", "files/?path=anything.txt"):
            self.assertEqual(self.client.get(f"/api/projects/{self.project.id}/{suffix}").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/projects/{self.project.id}/").status_code, 404)
        self.assertEqual(self.client.get("/api/projects/").json()["projects"], [])
        self.client.force_login(self.user)
        User.objects.filter(pk=self.user.pk).update(host_access=False)
        for url in ("/api/workspace-folders/", f"/api/projects/{self.project.id}/tree/", f"/api/projects/{self.project.id}/files/?path=x"):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.request(f"/api/chats/{self.chat.id}/", {"project_id":str(self.project.id)}, "patch").status_code, 403)

    def test_traversal_private_data_and_drive_roots_are_denied(self):
        for value in ("../outside.txt", "D:/outside.txt", "nested/../../outside", "file.txt:stream"):
            response = self.client.get(f"/api/projects/{self.project.id}/files/", {"path":value})
            self.assertIn(response.status_code, (400,403), (value,response.content))
        for value in (config()["data_dir"], Path(self.root.anchor)):
            result = self.request("/api/projects/", {"name":"Denied", "path":str(value)})
            self.assertEqual(result.status_code, 400)

    def test_project_upload_download_and_tree_never_overwrite(self):
        url = f"/api/projects/{self.project.id}/files/"
        data = {"files":SimpleUploadedFile("input.txt", b"original"), "paths":"source/input.txt"}
        self.assertEqual(self.client.post(url, data).status_code, 200)
        self.assertEqual(self.client.get(url, {"path":"source/input.txt"}).content, b"original")
        tree = self.client.get(f"/api/projects/{self.project.id}/tree/").json()["entries"]
        self.assertEqual(tree[0]["name"], "source")
        data["files"] = SimpleUploadedFile("input.txt", b"replacement")
        self.assertEqual(self.client.post(url, data).status_code, 400)
        self.assertEqual((self.root / "source/input.txt").read_bytes(), b"original")
        duplicate = {"files":[SimpleUploadedFile("x", b"1"),SimpleUploadedFile("x", b"2")], "paths":["duplicate.txt","duplicate.txt"]}
        self.assertEqual(self.client.post(url, duplicate).status_code, 400)
        self.assertFalse((self.root / "duplicate.txt").exists())

    def test_active_project_lock_prevents_conflicting_uploads(self):
        with project_lock(self.project):
            result = self.client.post(f"/api/projects/{self.project.id}/files/", {
                "files":SimpleUploadedFile("busy.txt", b"x"), "paths":"busy.txt"})
        self.assertEqual(result.status_code, 400)
        self.assertFalse((self.root / "busy.txt").exists())

    def test_project_links_are_not_followed(self):
        (self.root / "linked").mkdir()
        with patch("solar.core.projects.is_link_or_junction", side_effect=lambda path: Path(path).name=="linked"):
            self.assertEqual(self.client.get(f"/api/projects/{self.project.id}/tree/", {"path":"linked"}).status_code, 400)
            self.assertEqual(self.client.get(f"/api/projects/{self.project.id}/tree/").json()["entries"], [])

    def test_remote_folder_browser_and_duplicate_registration(self):
        (self.root / "child").mkdir()
        result = self.client.get("/api/workspace-folders/", {"path":str(self.root)})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["directories"][0]["name"], "child")
        result = self.request("/api/projects/", {"name":"Duplicate", "path":str(self.root)})
        self.assertEqual(result.status_code, 409)

    def test_new_folder_validation_and_write_failure_are_reported(self):
        result = self.request("/api/projects/", {"name":"Invalid", "path":str(self.root / "CON"), "create":True})
        self.assertEqual(result.status_code, 400)
        with patch("solar.core.projects.Path.mkdir", side_effect=PermissionError("Synthetic denied write")):
            result = self.request("/api/projects/", {"name":"Denied", "path":str(self.root / "cannot-create"), "create":True})
        self.assertEqual(result.status_code, 400)
        self.assertIn("write access", result.json()["error"])


@override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver"])
class SessionPolicyTests(ProjectFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.runtime = materialize(self.chat)
        self.addCleanup(lambda: collect_workspace(self.chat))
        self.home_patch = patch("solar.core.providers.Path.home", return_value=self.root / "synthetic-home")
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.exec_patch = patch("solar.core.providers.executable", side_effect=lambda name:name)
        self.exec_patch.start()
        self.addCleanup(self.exec_patch.stop)

    def test_codex_max_full_access_and_approval_are_real_cli_controls(self):
        result = self.request(f"/api/chats/{self.chat.id}/", {"effort":"max", "permission":"danger-full-access",
                              "approval":"never", "allow_network":True, "windows_sandbox":"unelevated"}, "patch")
        self.assertEqual(result.status_code, 200, result.content)
        self.chat.refresh_from_db()
        argv, _, _ = setup_cli(self.chat, self.runtime)
        self.assertEqual(argv[argv.index("-s")+1], "danger-full-access")
        self.assertIn('model_reasoning_effort="max"', argv)
        self.assertIn('approval_policy="never"', argv)
        self.assertIn("sandbox_workspace_write.network_access=true", argv)
        if os.name == "nt":
            self.assertIn('windows.sandbox="unelevated"', argv)
        self.assertNotIn("--ephemeral", argv)

    def test_role_restrictions_cap_session_policy(self):
        self.chat.permission = "danger-full-access"
        self.chat.allow_shell = self.chat.allow_network = True
        self.chat.agent = Agent.objects.create(owner=self.user, name="Restricted", permission="read-only", allow_shell=False)
        argv, _, _ = setup_cli(self.chat, self.runtime)
        self.assertEqual(argv[argv.index("-s")+1], "read-only")
        self.assertIn("features.shell_tool=false", argv)
        self.assertIn("sandbox_workspace_write.network_access=false", argv)
        self.chat.approval = "auto-review"
        with self.assertRaisesRegex(ValueError, "workspace-write"):
            setup_cli(self.chat, self.runtime)

    def test_interactive_approval_opens_native_and_schedule_cannot_wait(self):
        self.chat.approval = "on-request"
        self.chat.save()
        response = self.request(f"/api/chats/{self.chat.id}/messages/", {"message":"Do synthetic work"})
        self.assertEqual(response.status_code, 202, response.content)
        self.assertTrue(response.json()["native"])
        result = self.request("/api/schedules/", {"name":"Cannot wait", "prompt":"Task", "provider_id":str(self.provider.id), "approval":"on-request"})
        self.assertEqual(result.status_code, 400)

    def test_auto_review_requires_workspace_write_and_is_forwarded(self):
        self.chat.approval = "auto-review"
        argv, _, _ = setup_cli(self.chat, self.runtime)
        self.assertIn("--approve-for-me", argv)
        self.assertNotIn('approval_policy="never"', argv)

    def test_untrusted_account_cannot_escalate_to_full_access(self):
        User.objects.filter(pk=self.user.pk).update(host_access=False)
        self.provider.kind = "openai"
        self.provider.model = "synthetic"
        self.provider.save()
        for data in ({"permission":"danger-full-access"}, {"allow_network":True}, {"windows_sandbox":"elevated"}):
            self.assertEqual(self.request(f"/api/chats/{self.chat.id}/", data, "patch").status_code, 403)

    def test_project_instructions_are_preserved_and_private_config_is_restored(self):
        for name in ("AGENTS.md", "CLAUDE.md"):
            (self.root / name).write_text("Existing project instructions", encoding="utf-8")
        argv, _, home = setup_cli(self.chat, self.runtime)
        self.assertIn("--add-dir", argv)
        self.assertEqual(argv[argv.index("--add-dir")+1], str(self.runtime))
        self.assertIn(str(self.root), tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))["projects"])
        self.provider.kind = "claude"
        argv, _, _ = setup_cli(self.chat, self.runtime)
        self.assertIn("--append-system-prompt-file", argv)
        for name in ("AGENTS.md", "CLAUDE.md"):
            self.assertEqual((self.root / name).read_text(), "Existing project instructions")

    def test_selected_folder_is_the_output_destination_in_provider_instructions(self):
        text, _ = instructions(self.chat)
        self.assertIn(f"workspace and output folder is {self.root}", text)
        self.assertIn("Save requested files and deliverables in this folder", text)
        self.assertNotIn("Store deliverables in codex/files", text)
        self.chat.project = None
        text, _ = instructions(self.chat)
        self.assertIn("Store deliverables in codex/files", text)

    def test_structured_state_resumes_exact_session_with_only_latest_prompt(self):
        session_id = str(uuid.uuid4())
        _, _, home = setup_cli(self.chat, self.runtime)
        (home / "history.jsonl").write_text("Synthetic persisted history", encoding="utf-8")
        self.chat.provider_session = session_id
        persist_cli_state(self.chat, home, native=False)
        self.chat.refresh_from_db()
        self.assertTrue(self.chat.native_started)
        argv, _, _ = setup_cli(self.chat, self.runtime)
        self.assertIn("resume", argv)
        self.assertIn(session_id, argv)
        self.assertNotIn("--ephemeral", argv)
        Event.objects.create(chat=self.chat, role="user", content="Old task")
        Event.objects.create(chat=self.chat, role="user", content="Current task")
        self.assertEqual(prompt_text(self.chat), "Current task")
        self.provider.kind = "claude"
        self.chat.provider = self.provider
        argv, _, _ = setup_cli(self.chat, self.runtime)
        self.assertIn("--resume", argv)
        self.assertIn(session_id, argv)
        self.assertNotIn("--no-session-persistence", argv)

    def test_opening_then_closing_terminal_without_conversation_does_not_resume_missing_session(self):
        _, _, home = setup_cli(self.chat, self.runtime, native=True)
        persist_cli_state(self.chat, home, native=True)
        self.chat.refresh_from_db()
        self.assertFalse(self.chat.native_started)
        argv, _, _ = setup_cli(self.chat, self.runtime, native=True)
        self.assertNotIn("resume", argv)

    def test_installed_codex_accepts_generated_resume_options_without_inference(self):
        from solar.core.processes import executable
        try:
            program = executable("codex")
        except ValueError:
            self.skipTest("Codex CLI is not installed")
        self.chat.native_started = True
        self.chat.provider_session = str(uuid.uuid4())
        self.chat.effort = "max"
        argv, env, _ = setup_cli(self.chat, self.runtime)
        argv[0] = program
        result = subprocess.run([*argv[:-1], "--help"], env=env, cwd=self.root, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertIn(b"Resume", result.stdout)

    def test_project_api_tools_obey_permissions_and_reject_path_escape(self):
        self.provider.kind = "openai"
        self.provider.configuration = json.dumps({"base_url":"http://127.0.0.1:11434/v1", "tools":True})
        self.chat.model = "synthetic"
        for permission, target, succeeds in (("workspace-write", "api.txt", True),
                                             ("read-only", "denied.txt", False),
                                             ("workspace-write", "../escape.txt", False)):
            self.chat.permission = permission
            count = 0
            def handler(request, target=target, succeeds=succeeds):
                nonlocal count
                count += 1
                data = json.loads(request.content)
                if count == 1:
                    delta = {"tool_calls":[{"index":0,"id":"write","function":{"name":"write_workspace",
                        "arguments":json.dumps({"path":target,"content":"API project output"})}}]}
                else:
                    result = json.loads(data["messages"][-1]["content"])
                    self.assertEqual("saved" in result, succeeds, result)
                    delta = {"content":"Done"}
                return httpx.Response(200, text="data: "+json.dumps({"choices":[{"delta":delta}]})+"\n\ndata: [DONE]\n\n")
            original = httpx.Client
            with patch("solar.core.providers.httpx.Client", side_effect=lambda original=original, handler=handler, **kwargs:original(transport=httpx.MockTransport(handler),**kwargs)):
                self.assertEqual(run_api(self.chat, Mock(), lambda *args:None), "Done")
        self.assertEqual((self.root / "api.txt").read_text(), "API project output")
        self.assertFalse((self.root / "denied.txt").exists())


class PersistentAutomationTests(ProjectFixture, TestCase):
    def test_task_continues_files_history_and_session_after_reload(self):
        now = timezone.now()
        schedule = Schedule.objects.create(owner=self.user, provider=self.provider, project=self.project, name="Continuing task",
            prompt="Next iteration", next_run=now, starts_at=now, max_runs=2, approval="never", effort="max")
        tick_schedules(now)
        schedule.refresh_from_db()
        first_chat_id = schedule.chat_id
        run = schedule.chat.runs.get()
        Event.objects.create(chat=schedule.chat, run=run, role="assistant", content="First result")
        put_file(schedule.chat, "codex/files/checkpoint.txt", b"checkpoint")
        Run.objects.filter(pk=run.pk).update(status="completed")
        tick_schedules(now+timedelta(hours=1))
        schedule.refresh_from_db()
        self.assertEqual(schedule.chat_id, first_chat_id)
        self.assertEqual(schedule.chat.events.filter(role="user").count(), 2)
        self.assertEqual(schedule.chat.events.filter(role="assistant").get().content, "First result")
        self.assertEqual(read_file(schedule.chat.files.get()), b"checkpoint")
        self.assertEqual(schedule.chat.effort, "max")
        self.assertEqual(schedule.chat.project_id, self.project.id)
        self.assertEqual(schedule.history.count(), 2)

    def test_upgrade_adopts_latest_chat_and_retains_older_history(self):
        now = timezone.now()
        schedule = Schedule.objects.create(owner=self.user, provider=self.provider, name="Legacy task", prompt="Continue", next_run=now)
        old = Chat.objects.create(owner=self.user, provider=self.provider)
        for index, chat in enumerate((old, self.chat)):
            run = Run.objects.create(chat=chat, status="completed")
            ScheduleRun.objects.create(schedule=schedule, run=run, due_at=now-timedelta(hours=2-index))
        migrate = importlib.import_module("solar.core.migrations.0005_reuse_automation_chats")
        migrate.adopt_chats(apps, None)
        schedule.refresh_from_db()
        self.assertEqual(schedule.chat_id, self.chat.id)
        self.assertTrue(Chat.objects.filter(pk=old.pk).exists())
        self.assertEqual(schedule.history.count(), 2)

    def test_manual_run_in_automation_chat_prevents_overlapping_tick(self):
        now = timezone.now()
        schedule = Schedule.objects.create(owner=self.user, provider=self.provider, chat=self.chat, name="Busy task", prompt="Task", next_run=now)
        queue_run(self.chat, "Manual task")
        tick_schedules(now)
        schedule.refresh_from_db()
        self.assertEqual(schedule.run_count, 0)
        self.assertEqual(schedule.history.count(), 0)


class LinkedProjectExecutionTests(ProjectFixture, TransactionTestCase):
    def test_actual_subprocess_works_in_project_and_cleanup_keeps_source(self):
        (self.root / "AGENTS.md").write_text("Keep project instructions", encoding="utf-8")
        put_file(self.chat, "user/files/input.txt", b"input")
        run = queue_run(self.chat, "Edit the project")
        script = "import sys,json,pathlib; sys.stdin.read(); pathlib.Path('generated.txt').write_text('project result'); pathlib.Path(sys.argv[1]).write_text('chat result'); print(json.dumps({'type':'thread.started','thread_id':sys.argv[2]})); print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'First message'}})); print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Second message'}}))"
        session_id = str(uuid.uuid4())
        def setup(chat, root):
            home = root / ".codex-home"
            home.mkdir()
            return [sys.executable, "-c", script, str(root/"codex/files/result.txt"), session_id], os.environ.copy(), home
        with patch("solar.core.runner.setup_cli", side_effect=setup):
            execute(run.pk)
        run.refresh_from_db()
        self.chat.refresh_from_db()
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(self.chat.provider_session, session_id)
        self.assertEqual((self.root / "generated.txt").read_text(), "project result")
        self.assertEqual((self.root / "AGENTS.md").read_text(), "Keep project instructions")
        self.assertEqual(self.chat.events.filter(role="assistant", kind="message").get().content, "First message\n\nSecond message")
        self.assertFalse(workspace_path(self.chat).exists())
        self.assertEqual(read_file(self.chat.files.get(path_hash=digest("codex/files/result.txt", f"path:{self.chat.id}"))), b"chat result")


class TerminalStartupTests(SimpleTestCase):
    def test_early_resize_is_retained_until_worker_attaches_process(self):
        from solar.asgi import websocket
        run_id = str(uuid.uuid4())
        user = SimpleNamespace(id=uuid.uuid4(), host_access=True)
        process = Mock()
        handle = SimpleNamespace(process=None, output_since=lambda sequence:(0,""))
        sent=[]
        async def scenario():
            count=0
            async def receive():
                nonlocal count
                count+=1
                if count==1:
                    return {"type":"websocket.connect"}
                if count==2:
                    return {"type":"websocket.receive","text":json.dumps({"type":"resize","cols":137,"rows":42})}
                await asyncio.sleep(.12)
                handle.process=process
                await asyncio.sleep(.2)
                return {"type":"websocket.disconnect"}
            async def send(message):sent.append(message)
            with patch("solar.asgi.socket_auth", return_value=(user,"synthetic-session")), patch("solar.asgi.allowed_run", new=AsyncMock(return_value=True)), patch.dict("solar.asgi.HANDLES", {run_id:handle}, clear=True):
                await websocket({"path":f"/ws/terminal/{run_id}/"}, receive, send)
        asyncio.run(scenario())
        process.resize.assert_called_with(137,42)
        self.assertEqual(sent[0]["type"], "websocket.accept")
