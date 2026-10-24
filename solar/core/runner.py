import codecs
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import logging
import queue
import threading
import time
import uuid

from django.db import close_old_connections, transaction
from django.utils import timezone
import psutil

from solar.config import config
from solar.core.cron import next_cron
from solar.core.models import Chat, Event, Run, Schedule, ScheduleRun, User
from solar.core.processes import PipeProcess, kill_tree, terminal
from solar.core.providers import persist_cli_state, prompt_text, run_api, setup_cli
from solar.core.vault import collect_workspace, materialize, sync_manifests, workspace_path
from solar.core.projects import execution_directory, execution_guard

log = logging.getLogger(__name__)
HANDLES = {}
HANDLES_LOCK = threading.RLock()


class Cancelled(Exception):
    pass


class Handle:
    def __init__(self, run):
        self.run = run
        self.cancel = threading.Event()
        self.process = None
        self.started = time.monotonic()
        self.last_check = 0
        self.last_input = self.started
        self.buffer = []
        self.buffer_start = 0
        self.sequence = 0
        self.buffer_size = 0
        self.total_output = 0
        self.lock = threading.RLock()

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()
        if time.monotonic() - self.started > config()["execution"]["timeout_seconds"]:
            self.cancel.set()
            raise ValueError("Run exceeded its configured time limit")
        if time.monotonic() - self.last_check > 2:
            self.last_check = time.monotonic()
            user = User.objects.filter(pk=self.run.chat.owner_id, is_active=True).first()
            status = Run.objects.filter(pk=self.run.pk).values_list("status", flat=True).first()
            if not user or status == "cancelling" or ((self.run.chat.provider.kind != "openai" or self.process or self.run.chat.project_id) and not user.host_access):
                self.cancel.set()
                raise Cancelled()
            Run.objects.filter(pk=self.run.pk).update(heartbeat_at=timezone.now())

    def attach(self, process):
        self.process = process
        try:
            created = psutil.Process(process.pid).create_time()
        except psutil.NoSuchProcess:
            created = None  # a short command may already have exited
        Run.objects.filter(pk=self.run.pk).update(process_id=process.pid, process_started=created)
        if self.cancel.is_set():
            process.stop()

    def stop(self):
        self.cancel.set()
        if self.process:
            self.process.stop()

    def append(self, text):
        with self.lock:
            self.total_output += len(text)
            if self.total_output > 20 * 1024**2:
                self.stop()
                raise ValueError("Terminal output reached the 20 MB run limit")
            self.sequence += 1
            self.buffer.append((self.sequence, text))
            self.buffer_size += len(text)
            while self.buffer_size > 1024**2 and self.buffer:
                number, removed = self.buffer.pop(0)
                self.buffer_start = number
                self.buffer_size -= len(removed)

    def output_since(self, sequence):
        with self.lock:
            return self.sequence, "".join(text for number, text in self.buffer if number > sequence)


def event(run, role, kind, content):
    if not content:
        return
    Event.objects.create(chat=run.chat, run=run, role=role, kind=kind, content=str(content)[:250_000])
    Chat.objects.filter(pk=run.chat_id).update(updated_at=timezone.now())


def queue_run(chat, prompt=None, mode="chat"):
    if chat.archived:
        raise ValueError("Restore this chat before running it")
    if chat.provider.owner_id != chat.owner_id or (chat.agent and chat.agent.owner_id != chat.owner_id):
        raise ValueError("Invalid chat resources")
    if chat.project_id:
        execution_directory(chat)  # validate ownership, current host permission, and path
    if chat.approval == "on-request" and chat.provider.kind in ("codex", "claude") and mode == "chat":
        mode = "native"
    if chat.provider.kind != "openai" and not chat.owner.host_access:
        raise PermissionError("Host execution is not enabled for your account")
    if mode == "native" and chat.provider.kind == "openai":
        raise ValueError("Native terminal mode is available for Codex and Claude Code")
    with transaction.atomic():
        if Run.objects.filter(chat=chat, status__in=Run.ACTIVE).exists():
            raise ValueError("This chat already has an active run")
        if Run.objects.filter(chat__owner_id=chat.owner_id, status__in=Run.ACTIVE).count() >= 20:
            raise ValueError("Your queue has reached its 20-run limit")
        run = Run.objects.create(chat=chat, mode=mode)
        if prompt is not None:
            event(run, "user", "message", prompt)
        elif mode == "native":
            event(run, "system", "activity", "Native provider session started")
    sync_manifests(chat)
    return run


def cancel_run(run):
    with transaction.atomic():
        current = Run.objects.get(pk=run.pk)
        if current.status == "queued":
            current.status, current.finished_at = "cancelled", timezone.now()
            current.save(update_fields=["status", "finished_at"])
        elif current.status in ("running", "cancelling"):
            current.status = "cancelling"
            current.save(update_fields=["status"])
    with HANDLES_LOCK:
        handle = HANDLES.get(str(run.pk))
    if handle:
        handle.stop()


def stream_cli(run, handle, root):
    argv, env, home = setup_cli(run.chat, root)
    process = PipeProcess(argv, execution_directory(run.chat), env)
    handle.attach(process)
    lines = queue.Queue(maxsize=1000)

    def reader(stream, source):
        try:
            while True:
                # A single oversized provider record must not exhaust memory.
                line = stream.readline(1024 * 1024)
                if not line:
                    break
                while not handle.cancel.is_set():
                    try:
                        lines.put((source, line), timeout=0.25)
                        break
                    except queue.Full:
                        continue
        finally:
            stream.close()
            try:
                lines.put((source, None), timeout=5)
            except queue.Full:
                pass  # a cancelled consumer no longer needs an end-of-stream marker

    readers = [threading.Thread(target=reader, args=(stream, source), daemon=True)
               for stream, source in ((process.process.stdout, "out"), (process.process.stderr, "err"))]
    for thread in readers:
        thread.start()
    prompt = prompt_text(run.chat)
    def writer():
        try:
            process.process.stdin.write(prompt.encode("utf-8"))
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
    threading.Thread(target=writer, daemon=True).start()
    done, errors, answer, partial, total = set(), [], [], False, 0
    try:
        while len(done) < 2:
            handle.check()
            try:
                source, line = lines.get(timeout=0.25)
            except queue.Empty:
                continue
            if line is None:
                done.add(source)
                continue
            total += len(line)
            if total > 20 * 1024**2:
                raise ValueError("Provider output exceeded 20 MB")
            text = line.decode("utf-8", errors="replace").strip()
            if source == "err":
                errors.append(text)
                errors = errors[-12:]
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                event(run, "tool", "activity", text[:2000])
                continue
            kind = message.get("type", "")
            if run.chat.provider.kind == "codex":
                if kind == "thread.started" and message.get("thread_id"):
                    run.chat.provider_session = str(uuid.UUID(message["thread_id"]))
                item = message.get("item", {})
                if kind == "item.completed" and item.get("type") == "agent_message":
                    answer.append(item.get("text", ""))
                    event(run, "assistant", "delta", ("\n\n" if len(answer) > 1 else "") + item.get("text", ""))
                elif kind in ("item.started", "item.completed"):
                    label = item.get("command") or item.get("text") or item.get("type", "working")
                    event(run, "tool", "activity", label[:8000])
                elif kind in ("error", "turn.failed"):
                    errors.append(str(message.get("message") or message.get("error") or "Provider error"))
            else:
                if kind == "system" and message.get("session_id"):
                    run.chat.provider_session = str(uuid.UUID(message["session_id"]))
                if kind == "stream_event":
                    delta = message.get("event", {}).get("delta", {})
                    if delta.get("type") == "text_delta":
                        fragment = delta.get("text", "")
                        partial = True
                        answer.append(fragment)
                        event(run, "assistant", "delta", fragment)
                elif kind == "assistant":
                    for block in message.get("message", {}).get("content", []):
                        if block.get("type") == "tool_use":
                            event(run, "tool", "activity", f"{block.get('name', 'tool')}: {json.dumps(block.get('input', {}))[:4000]}")
                        elif block.get("type") == "text" and not partial:
                            answer.append(block.get("text", ""))
                            event(run, "assistant", "delta", block.get("text", ""))
                elif kind == "result":
                    if message.get("is_error"):
                        errors.append("Claude reported a failed turn; check credentials or denied tool permissions")
                    if not answer and message.get("result"):
                        answer.append(message["result"])
        code = process.process.wait(timeout=5)
        if code or not answer:
            # Own-provider diagnostics are useful, but remove common credential patterns.
            import re
            diagnostic = "\n".join(errors)[-3000:]
            diagnostic = re.sub(r"(?i)(?:sk-[\w-]+|bearer\s+\S+)", "[redacted]", diagnostic)
            raise ValueError(f"{run.chat.provider.name} exited with code {code}. {diagnostic}".strip())
        return ("\n\n" if run.chat.provider.kind == "codex" else "").join(answer)
    finally:
        if process.process.poll() is None:
            process.stop()
        persist_cli_state(run.chat, home, native=False)


def run_native(run, handle, root):
    argv, env, home = setup_cli(run.chat, root, native=True)
    prompt = run.chat.events.filter(run=run, role="user", kind="message").first()
    if prompt:
        # A short launch prompt directs the interactive provider to a private text
        # file, avoiding Windows command-line limits and keeping approvals in TUI.
        request_file = root / ".solar-request.txt"
        request_file.write_text(prompt_text(run.chat), encoding="utf-8")
        argv += [f"Read {request_file} and carry out the latest user request in that conversation."]
    process = terminal(argv, execution_directory(run.chat), env)
    handle.attach(process)
    ended = threading.Event()
    failure = []

    def reader():
        close_old_connections()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                raw = process.read()
                if not raw:
                    break
                text = decoder.decode(raw)
                handle.append(text)
                event(run, "tool", "terminal", text)
        except Exception as exc:
            failure.append(exc)
        finally:
            ended.set()
            close_old_connections()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        while not ended.wait(0.25):
            handle.check()
            if time.monotonic() - handle.last_input > config()["execution"]["native_idle_seconds"]:
                raise ValueError("Native terminal stopped after its idle timeout")
            if process.poll() is not None:
                break
        if failure:
            raise ValueError("Native terminal output could not be read") from failure[0]
    finally:
        process.close()
        thread.join(timeout=5)
        persist_cli_state(run.chat, home, native=True)


def execute(run_id):
    close_old_connections()
    run = Run.objects.select_related("chat__owner", "chat__provider", "chat__agent").get(pk=run_id)
    handle = Handle(run)
    with HANDLES_LOCK:
        HANDLES[str(run_id)] = handle
    status, error, root = "completed", "", None
    guard, guarded = execution_guard(run.chat, handle.check), False
    try:
        handle.check()
        guard.__enter__()
        guarded = True
        if run.chat.provider.kind == "openai":
            answer = run_api(run.chat, handle, lambda role, kind, text: event(run, role, kind, text))
            event(run, "assistant", "message", answer)
        else:
            root = materialize(run.chat)
            if run.mode == "native":
                run_native(run, handle, root)
            else:
                answer = stream_cli(run, handle, root)
                event(run, "assistant", "message", answer)
    except Cancelled:
        status = "cancelled"
        event(run, "system", "activity", "Run cancelled")
    except Exception as exc:
        if handle.cancel.is_set() and not isinstance(exc, ValueError):
            # An in-flight HTTP read can end with a network error after Stop. Keep
            # the user-requested cancellation, while preserving deliberate limit errors.
            status = "cancelled"
            event(run, "system", "activity", "Run cancelled")
        else:
            status = "failed"
            # Detailed provider errors stay encrypted and visible only to this chat's owner.
            error = str(exc)[:4000] if isinstance(exc, (ValueError, OSError, RuntimeError)) else "Run failed. Check provider setup and local server diagnostics."
            event(run, "system", "error", error)
            log.warning("Run %s failed (%s)", run_id, type(exc).__name__)
    finally:
        if handle.process:
            handle.process.stop()
        if root is not None:
            try:
                collect_workspace(run.chat)
            except Exception:
                status = "failed"
                error = "Output collection failed. The protected runtime workspace was retained for local recovery."
                event(run, "system", "error", error)
        try:
            sync_manifests(run.chat)
            Run.objects.filter(pk=run_id).update(status=status, error=error, finished_at=timezone.now(),
                                                 process_id=None, process_started=None)
        finally:
            with HANDLES_LOCK:
                HANDLES.pop(str(run_id), None)
            if guarded:
                guard.__exit__(None, None, None)
            close_old_connections()


def tick_schedules(now=None):
    now = now or timezone.now()
    for schedule_id in Schedule.objects.filter(enabled=True, next_run__lte=now).values_list("id", flat=True)[:100]:
        with transaction.atomic():
            schedule = Schedule.objects.select_related("owner", "provider", "agent").get(pk=schedule_id)
            if not schedule.enabled or schedule.next_run > now:
                continue
            if (not schedule.owner.is_active or schedule.owner.must_change_password
                    or (schedule.ends_at and now > schedule.ends_at)
                    or (schedule.max_runs is not None and schedule.run_count >= schedule.max_runs)):
                schedule.enabled = False
                schedule.save(update_fields=["enabled"])
                continue
            due = schedule.next_run
            if schedule.kind == "once":
                schedule.enabled = False
            elif schedule.kind == "cron":
                schedule.next_run = next_cron(schedule.cron, now, schedule.timezone)
            else:
                missed = max(1, int((now - due).total_seconds()) // schedule.interval_seconds + 1)
                schedule.next_run = due + timedelta(seconds=missed * schedule.interval_seconds)
            # Never overlap the same recurring task. Missed times coalesce to one occurrence.
            overlapping = schedule.history.filter(run__status__in=Run.ACTIVE).exists() or (
                schedule.chat_id and schedule.chat.runs.filter(status__in=Run.ACTIVE).exists())
            if not overlapping:
                if not schedule.chat_id:
                    # Preserve the latest existing task conversation when upgrading.
                    previous = schedule.history.filter(run__chat__owner_id=schedule.owner_id).select_related("run__chat").first()
                    chat = previous.run.chat if previous and previous.run_id else Chat.objects.create(
                        owner=schedule.owner, title=schedule.name, provider=schedule.provider, agent=schedule.agent,
                        project=schedule.project, model=schedule.model, effort=schedule.effort, permission=schedule.permission,
                        approval=schedule.approval, allow_shell=schedule.allow_shell, allow_network=schedule.allow_network,
                        windows_sandbox=schedule.windows_sandbox)
                    schedule.chat = chat
                else:
                    chat = schedule.chat
                if chat.owner_id != schedule.owner_id:
                    schedule.enabled = False
                    schedule.save(update_fields=["enabled"])
                    continue
                try:
                    run = queue_run(chat, schedule.prompt)
                except (ValueError, PermissionError) as exc:
                    run = Run.objects.create(chat=chat, status="failed", error=str(exc), finished_at=now)
                    event(run, "system", "error", str(exc))
                ScheduleRun.objects.create(schedule=schedule, due_at=due, run=run)
                schedule.run_count += 1
            if ((schedule.max_runs is not None and schedule.run_count >= schedule.max_runs)
                    or (schedule.ends_at and schedule.next_run > schedule.ends_at)):
                schedule.enabled = False
            schedule.save()


def recover():
    for run in Run.objects.filter(status__in=["running", "cancelling"]).select_related("chat"):
        if run.process_id and run.process_started:
            try:
                process = psutil.Process(run.process_id)
                expected = run.chat.project.root_path if run.chat.project_id else str(workspace_path(run.chat))
                if abs(process.create_time() - run.process_started) < 0.1 and process.cwd() == expected:
                    kill_tree(run.process_id)
            except psutil.Error:
                pass
        run.status, run.error, run.finished_at = "interrupted", "Server restarted during this run", timezone.now()
        run.save(update_fields=["status", "error", "finished_at"])
        event(run, "system", "error", run.error)
    runtime = config()["data_dir"] / "runtime"
    for root in runtime.iterdir():
        if root.is_file() and root.name.startswith("tmp") and root.suffix == ".upload":
            root.unlink()  # incomplete request spool from a terminated server
            continue
        if not root.is_dir() or len(root.name) != 36:
            continue
        try:
            chat = Chat.objects.filter(pk=root.name).first()
            if chat:
                latest = chat.runs.order_by("-created_at").first()
                if latest and chat.provider.kind in ("codex", "claude"):
                    home = root / (".codex-home" if chat.provider.kind == "codex" else ".claude-home")
                    if home.exists():
                        persist_cli_state(chat, home, native=latest.mode == "native")
                collect_workspace(chat)
        except Exception:
            log.warning("A runtime workspace requires local recovery")


class Worker:
    def __init__(self):
        self.stop_event = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=config()["execution"]["max_workers"], thread_name_prefix="solar-run")
        self.thread = threading.Thread(target=self.loop, name="solar-scheduler", daemon=True)
        self.futures = []

    def start(self):
        recover()
        self.thread.start()

    def loop(self):
        while not self.stop_event.is_set():
            close_old_connections()
            try:
                tick_schedules()
                self.futures = [future for future in self.futures if not future.done()]
                slots = config()["execution"]["max_workers"] - len(self.futures)
                for _ in range(max(slots, 0)):
                    with transaction.atomic():
                        run = Run.objects.filter(status="queued").order_by("created_at").first()
                        if not run:
                            break
                        run.status, run.started_at = "running", timezone.now()
                        run.save(update_fields=["status", "started_at"])
                    self.futures.append(self.pool.submit(execute, run.id))
            except Exception as exc:
                log.warning("Scheduler cycle failed (%s)", type(exc).__name__)
            finally:
                close_old_connections()
            self.stop_event.wait(1)

    def stop(self):
        self.stop_event.set()
        from solar.core.admin_terminal import shells
        shells.stop_all()
        self.thread.join(timeout=5)
        with HANDLES_LOCK:
            handles = list(HANDLES.values())
        for handle in handles:
            handle.stop()
        self.pool.shutdown(wait=True, cancel_futures=True)
