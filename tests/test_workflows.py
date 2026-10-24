import asyncio
from datetime import datetime, timedelta, timezone as dt_timezone
import io
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
from unittest.mock import patch
import zipfile

from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
import httpx

from solar.core.cron import next_cron, parse
from solar.core.models import Agent, Chat, Event, Provider, Run, Schedule, ScheduleRun, User
from solar.core.providers import run_api, setup_cli
from solar.core.processes import executable, terminal, run_captured, child_environment
from solar.core.runner import Handle, Worker, execute, queue_run, tick_schedules
from solar.core.vault import collect_workspace, materialize, put_file, read_file, workspace_path

PASSWORD = "Workflow-test-password-42!"


class CronTests(SimpleTestCase):
    def test_impossible_calendar_date_and_leap_year(self):
        after = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        with self.assertRaisesRegex(ValueError, "no occurrence"):
            next_cron("0 0 31 2 *", after)
        self.assertEqual(next_cron("0 0 29 2 *", after), datetime(2028, 2, 29, tzinfo=dt_timezone.utc))

    def test_intervals_lists_and_steps(self):
        after = datetime(2026, 1, 1, 0, 0, tzinfo=dt_timezone.utc)
        self.assertEqual(next_cron("*/15 * * * *", after), after + timedelta(minutes=15))
        self.assertEqual(next_cron("0 3,6 * * 1-5", after), after.replace(hour=3))

    def test_day_of_month_and_weekday_or_semantics(self):
        after = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)  # Thursday
        self.assertEqual(next_cron("0 0 2 * 0", after).day, 2)
        self.assertEqual(next_cron("0 0 * * 7", after).day, 4)

    def test_spring_forward_skips_nonexistent_local_time(self):
        after = datetime(2026, 3, 8, 5, 0, tzinfo=dt_timezone.utc)
        self.assertEqual(next_cron("30 2 * * *", after, "America/New_York"),
                         datetime(2026, 3, 9, 6, 30, tzinfo=dt_timezone.utc))

    def test_fall_back_occurs_in_both_repeated_hours(self):
        after = datetime(2026, 11, 1, 5, 30, tzinfo=dt_timezone.utc)
        self.assertEqual(next_cron("30 1 * * *", after, "America/New_York"),
                         datetime(2026, 11, 1, 6, 30, tzinfo=dt_timezone.utc))

    def test_invalid_fields(self):
        for expression in ("* * *", "61 * * * *", "*/0 * * * *", "* 25 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                parse(expression)


@override_settings(SECURE_SSL_REDIRECT=False)
class SchedulingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("scheduler-test", PASSWORD, must_change_password=False)
        self.provider = Provider.objects.create(owner=self.user, name="Test", kind="openai", model="test")
        self.now = timezone.now()

    def schedule(self, **kwargs):
        values = dict(owner=self.user, provider=self.provider, name="Hourly task", prompt="Do work", model="test",
                      starts_at=self.now, next_run=self.now, interval_seconds=3600)
        values.update(kwargs)
        return Schedule.objects.create(**values)

    def test_interval_count_and_history(self):
        schedule = self.schedule(max_runs=2)
        tick_schedules(self.now)
        schedule.refresh_from_db()
        self.assertEqual(schedule.run_count, 1)
        self.assertEqual(schedule.next_run, self.now + timedelta(hours=1))
        self.assertEqual(ScheduleRun.objects.count(), 1)
        Run.objects.update(status="completed")
        tick_schedules(self.now + timedelta(hours=1))
        schedule.refresh_from_db()
        self.assertFalse(schedule.enabled)
        self.assertEqual(schedule.run_count, 2)
        self.assertEqual(Chat.objects.count(), 1)
        self.assertEqual(schedule.history.values("run__chat_id").distinct().count(), 1)
        self.assertEqual(schedule.chat.events.filter(role="user", kind="message").count(), 2)
        self.assertTrue(all(c.owner_id == self.user.id for c in Chat.objects.all()))

    def test_idempotence_and_no_overlap(self):
        schedule = self.schedule()
        tick_schedules(self.now)
        tick_schedules(self.now)
        tick_schedules(self.now + timedelta(hours=1))
        self.assertEqual(ScheduleRun.objects.count(), 1)
        schedule.refresh_from_db()
        self.assertEqual(schedule.run_count, 1)
        self.assertEqual(schedule.next_run, self.now + timedelta(hours=2))

    def test_missed_intervals_coalesce(self):
        schedule = self.schedule()
        tick_schedules(self.now + timedelta(days=5))
        schedule.refresh_from_db()
        self.assertEqual(schedule.run_count, 1)
        self.assertEqual(schedule.next_run, self.now + timedelta(days=5, hours=1))

    def test_ended_schedule_does_not_run(self):
        schedule = self.schedule(ends_at=self.now - timedelta(seconds=1))
        tick_schedules(self.now)
        schedule.refresh_from_db()
        self.assertFalse(schedule.enabled)
        self.assertEqual(Run.objects.count(), 0)

    def test_once_schedule_stops(self):
        schedule = self.schedule(kind="once")
        tick_schedules(self.now)
        schedule.refresh_from_db()
        self.assertFalse(schedule.enabled)
        self.assertEqual(schedule.run_count, 1)

    def test_disabled_owner_does_not_run(self):
        schedule = self.schedule()
        self.user.is_active = False
        self.user.save()
        tick_schedules(self.now)
        self.assertEqual(Run.objects.count(), 0)

    def test_every_three_days_for_three_months_form(self):
        self.client.force_login(self.user)
        payload = {"name": "Quarterly routine", "prompt": "Do work", "provider_id": str(self.provider.id),
                   "kind": "interval", "interval_seconds": 3*86400, "duration_months": 3,
                   "timezone": "Asia/Riyadh", "starts_at": "2027-01-31T09:00:00+03:00"}
        response = self.client.post("/api/schedules/", data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        schedule = Schedule.objects.get(pk=response.json()["id"])
        self.assertEqual(schedule.ends_at.month, 4)
        self.assertEqual(schedule.ends_at.day, 30)
        self.assertEqual(schedule.interval_seconds, 259200)


class FakeHandle:
    def check(self):
        pass


def sse(*deltas):
    return "".join("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n" for delta in deltas) + "data: [DONE]\n\n"


class APIProviderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("api-test", PASSWORD, must_change_password=False)
        self.provider = Provider.objects.create(owner=self.user, name="Fake API", kind="openai", model="test",
                                                configuration=json.dumps({"base_url": "http://127.0.0.1:11434/v1", "tools": True}))
        self.chat = Chat.objects.create(owner=self.user, provider=self.provider, model="test", permission="workspace-write")
        Event.objects.create(chat=self.chat, role="user", content="Create a file")
        self.events = []

    def run_fake(self, handler, control=None, **kwargs):
        original = httpx.Client
        transport = httpx.MockTransport(handler)
        with patch("solar.core.providers.httpx.Client", side_effect=lambda **opts: original(transport=transport, **opts)):
            return run_api(self.chat, control or FakeHandle(), lambda *e: self.events.append(e), **kwargs)

    def test_trusted_local_model_can_run_program_and_collect_output(self):
        self.user.host_access = True
        self.user.save()
        role = Agent.objects.create(owner=self.user, name="Builder", role="Build", instructions="Use host tools",
                                     permission="workspace-write", allow_shell=True)
        self.chat.agent = role
        put_file(self.chat, "user/files/input.txt", b"source input")
        run = Run.objects.create(chat=self.chat, status="running")
        control = Handle(run)
        count = 0
        def handle(request):
            nonlocal count
            count += 1
            data = json.loads(request.content)
            if count == 1:
                self.assertIn("run_command", [tool["function"]["name"] for tool in data["tools"]])
                script = "from pathlib import Path; p=Path('user/files/input.txt').read_text(); Path('codex/files/built.txt').write_text(p+' compiled'); print('COMMAND_OK')"
                call = {"index": 0, "id": "host", "function": {"name": "run_command", "arguments": json.dumps({"argv": [sys.executable, "-c", script]})}}
                return httpx.Response(200, text=sse({"tool_calls": [call]}))
            result = json.loads(data["messages"][-1]["content"])
            self.assertEqual(result["exit_code"], 0, result)
            self.assertIn("COMMAND_OK", result["stdout"])
            return httpx.Response(200, text=sse({"content": "Built successfully"}))
        self.assertEqual(self.run_fake(handle, control=control), "Built successfully")
        file = next(file for file in self.chat.files.all() if file.path == "codex/files/built.txt")
        self.assertEqual(read_file(file), b"source input compiled")
        self.assertFalse(workspace_path(self.chat).exists())

    def test_untrusted_and_read_only_models_cannot_invoke_host_programs(self):
        role = Agent.objects.create(owner=self.user, name="Restricted builder", role="Build", instructions="Build",
                                     permission="workspace-write", allow_shell=True)
        self.chat.agent = role
        for trusted, permission in ((False, "workspace-write"), (True, "read-only")):
            self.user.host_access = trusted
            self.user.save()
            self.chat.permission = permission
            count = 0
            def handle(request):
                nonlocal count
                count += 1
                data = json.loads(request.content)
                if count == 1:
                    self.assertNotIn("run_command", [tool["function"]["name"] for tool in data["tools"]])
                    call = {"index": 0, "id": "denied", "function": {"name": "run_command", "arguments": '{"argv":["not-permitted"]}'}}
                    return httpx.Response(200, text=sse({"tool_calls": [call]}))
                self.assertIn("not permitted", data["messages"][-1]["content"])
                return httpx.Response(200, text=sse({"content": "Denied"}))
            with patch("solar.core.providers.run_captured") as command:
                self.assertEqual(self.run_fake(handle), "Denied")
                command.assert_not_called()

    def test_delegate_cannot_add_host_execution_missing_from_parent(self):
        self.user.host_access = True
        self.user.save()
        lead = Agent.objects.create(owner=self.user, name="No shell lead", role="Plan", instructions="Delegate",
                                     permission="workspace-write", allow_shell=False)
        child = Agent.objects.create(owner=self.user, name="Shell child", role="Build", instructions="Build",
                                      permission="workspace-write", allow_shell=True)
        lead.delegates.add(child)
        self.chat.agent = lead
        count = 0
        def handle(request):
            nonlocal count
            count += 1
            data = json.loads(request.content)
            self.assertNotIn("run_command", [tool["function"]["name"] for tool in data["tools"]])
            if count == 1:
                call = {"index": 0, "id": "child", "function": {"name": "delegate_agent", "arguments": json.dumps({"agent_id": str(child.id), "task": "Review"})}}
                return httpx.Response(200, text=sse({"tool_calls": [call]}))
            return httpx.Response(200, text=sse({"content": "Reviewed"}))
        self.assertEqual(self.run_fake(handle), "Reviewed")
        self.assertEqual(count, 3)

    def test_streaming_and_file_tool_round_trip(self):
        requests = []
        def handle(request):
            data = json.loads(request.content)
            requests.append(data)
            if len(requests) == 1:
                tool = {"index": 0, "id": "call-1", "function": {"name": "write_file", "arguments": json.dumps({"path": "codex/files/hello.txt", "content": "hello from model"})}}
                return httpx.Response(200, text=sse({"tool_calls": [tool]}))
            self.assertEqual(data["messages"][-1]["role"], "tool")
            return httpx.Response(200, text=sse({"content": "File "}, {"content": "created."}))
        result = self.run_fake(handle)
        self.assertEqual(result, "File created.")
        file = self.chat.files.get()
        self.assertEqual(read_file(file), b"hello from model")
        self.assertIn(("assistant", "delta", "created."), self.events)

    def test_read_only_model_cannot_write(self):
        self.chat.permission = "read-only"
        calls = []
        def handle(request):
            data = json.loads(request.content)
            calls.append(data)
            if len(calls) == 1:
                names = [t["function"]["name"] for t in data["tools"]]
                self.assertNotIn("write_file", names)
                return httpx.Response(200, text=sse({"tool_calls": [{"index":0,"id":"c","function":{"name":"write_file","arguments":json.dumps({"path":"codex/files/secret","content":"bad"})}}]}))
            self.assertIn("not permitted", data["messages"][-1]["content"])
            return httpx.Response(200, text=sse({"content": "Denied."}))
        self.run_fake(handle)
        self.assertEqual(self.chat.files.count(), 0)

    def test_path_traversal_tool_rejected(self):
        count = 0
        def handle(request):
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(200, text=sse({"tool_calls": [{"index":0,"id":"c","function":{"name":"write_file","arguments":json.dumps({"path":"codex/files/../../outside","content":"bad"})}}]}))
            self.assertIn("Unsafe file path", json.loads(request.content)["messages"][-1]["content"])
            return httpx.Response(200, text=sse({"content":"Blocked."}))
        self.run_fake(handle)
        self.assertEqual(self.chat.files.count(), 0)

    def test_allowed_delegation_and_inherited_read_only(self):
        root = Agent.objects.create(owner=self.user, name="Lead", role="Lead", instructions="Delegate", permission="read-only")
        child = Agent.objects.create(owner=self.user, name="Reviewer", role="Review", instructions="Review", permission="workspace-write")
        root.delegates.add(child)
        self.chat.agent = root
        count = 0
        def handle(request):
            nonlocal count
            count += 1
            data = json.loads(request.content)
            if count == 1:
                return httpx.Response(200, text=sse({"tool_calls":[{"index":0,"id":"delegate","function":{"name":"delegate_agent","arguments":json.dumps({"agent_id":str(child.id),"task":"Review"})}}]}))
            if count == 2:
                self.assertIn("Reviewer", data["messages"][0]["content"])
                self.assertNotIn("write_file", [tool["function"]["name"] for tool in data["tools"]])
                return httpx.Response(200, text=sse({"content":"Review complete"}))
            self.assertIn("Review complete", data["messages"][-1]["content"])
            return httpx.Response(200, text=sse({"content":"Done"}))
        self.assertEqual(self.run_fake(handle), "Done")
        self.assertEqual(count, 3)

    def test_cross_user_delegation_rejected(self):
        other = User.objects.create_user("other-api-test", PASSWORD)
        root = Agent.objects.create(owner=self.user, name="Lead", role="Lead", instructions="Delegate")
        child = Agent.objects.create(owner=other, name="Private", role="Private", instructions="Private")
        # A corrupt relationship must still be filtered at execution, not just in HTTP validation.
        root.delegates.add(child)
        self.chat.agent = root
        count = 0
        def handle(request):
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(200, text=sse({"tool_calls":[{"index":0,"id":"delegate","function":{"name":"delegate_agent","arguments":json.dumps({"agent_id":str(child.id),"task":"Read private files"})}}]}))
            self.assertIn("not allowed", json.loads(request.content)["messages"][-1]["content"])
            return httpx.Response(200, text=sse({"content":"Denied"}))
        self.run_fake(handle)
        self.assertEqual(count, 2)

    def test_provider_error_does_not_echo_secrets(self):
        with self.assertRaisesRegex(ValueError, "HTTP 401") as caught:
            self.run_fake(lambda request: httpx.Response(401, text="super-secret-token"))
        self.assertNotIn("super-secret-token", str(caught.exception))

    def test_redirect_not_followed(self):
        with self.assertRaises(ValueError):
            self.run_fake(lambda request: httpx.Response(302, headers={"Location":"http://169.254.169.254/"}))


class ExecutionTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user("worker-test", PASSWORD, host_access=True, must_change_password=False)
        self.provider = Provider.objects.create(owner=self.user, name="Mock CLI", kind="codex")
        self.chat = Chat.objects.create(owner=self.user, provider=self.provider)

    def test_real_subprocess_output_and_encrypted_artifact_collection(self):
        run = queue_run(self.chat, "Say hello")
        Run.objects.filter(pk=run.pk).update(status="running")
        script = "import sys,json,pathlib; prompt=sys.stdin.read(); pathlib.Path('codex/files/result.txt').write_text('result artifact'); print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Hello from CLI'}}))"
        def setup(chat, root):
            home = root / ".codex-home"
            home.mkdir()
            return [sys.executable, "-c", script], os.environ.copy(), home
        with patch("solar.core.runner.setup_cli", side_effect=setup):
            execute(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(self.chat.events.filter(role="assistant", kind="message").get().content, "Hello from CLI")
        self.assertEqual(read_file(self.chat.files.get()), b"result artifact")
        self.assertFalse(workspace_path(self.chat).exists())

    def test_workspace_recovery(self):
        put_file(self.chat, "user/files/input.txt", b"input")
        root = materialize(self.chat)
        (root / "codex" / "files" / "output.txt").write_bytes(b"output")
        collect_workspace(self.chat)
        self.assertEqual(self.chat.files.count(), 2)
        self.assertFalse(root.exists())

    def test_stop_during_api_read_remains_cancelled(self):
        self.provider.kind = "openai"
        self.provider.save()
        run = queue_run(self.chat, "Test cancellation")
        Run.objects.filter(pk=run.pk).update(status="running")
        def cancelled(chat, handle, emit):
            handle.stop()
            raise httpx.ReadTimeout("Synthetic pending read timeout")
        with patch("solar.core.runner.run_api", side_effect=cancelled):
            execute(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(run.error, "")

    def test_timeout_is_enforced(self):
        run = Run.objects.create(chat=self.chat, status="running")
        handle = Handle(run)
        handle.started -= 100_000
        with self.assertRaisesRegex(ValueError, "time limit"):
            handle.check()

    def test_native_terminal_real_io(self):
        root = workspace_path(self.chat)
        root.mkdir(parents=True, exist_ok=True)
        script = "import sys; print('SOLAR_PTY_READY',flush=True); value=input(); print('SOLAR_PTY_ECHO:'+value,flush=True)"
        proc = terminal([sys._base_executable, "-u", "-c", script], root, os.environ.copy())
        output = bytearray()
        finished = threading.Event()
        def read():
            try:
                while True:
                    data = proc.read()
                    if not data:
                        break
                    output.extend(data)
            finally:
                finished.set()
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            deadline = time.monotonic() + 10
            while b"SOLAR_PTY_READY" not in output and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertIn(b"SOLAR_PTY_READY", output)
            proc.resize(100, 30)
            proc.write("test-input\r")
            while b"SOLAR_PTY_ECHO:test-input" not in output and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertIn(b"SOLAR_PTY_ECHO:test-input", output)
        finally:
            proc.close()
            finished.wait(3)
            collect_workspace(self.chat)

    def test_host_command_limits_and_cancel_kill_the_process(self):
        root = materialize(self.chat)
        processes = []
        def attach(process):
            processes.append(process)
        try:
            with self.assertRaisesRegex(ValueError, "time limit"):
                run_captured([sys.executable, "-c", "import time; time.sleep(30)"], root, child_environment(),
                             lambda: None, attach, timeout_seconds=.25)
            self.assertIsNotNone(processes[-1].process.poll())
            with self.assertRaisesRegex(ValueError, "output exceeded"):
                run_captured([sys.executable, "-c", "print('x'*100000)"], root, child_environment(), lambda: None, attach)
            self.assertIsNotNone(processes[-1].process.poll())
            def cancel():
                raise ValueError("simulated cancellation")
            with self.assertRaisesRegex(ValueError, "cancellation"):
                run_captured([sys.executable, "-c", "import time; time.sleep(30)"], root, child_environment(), cancel, attach)
            self.assertIsNotNone(processes[-1].process.poll())
        finally:
            collect_workspace(self.chat)
