"""Explicitly started, administrator-only host terminals with bounded lifetimes."""
from collections import deque
import codecs
import os
from pathlib import Path
import shutil
import threading
import time
import uuid

from django.contrib.sessions.backends.db import SessionStore
from django.db import close_old_connections
from django.utils.crypto import constant_time_compare

from solar.config import config
from solar.core.models import User
from solar.core.processes import child_environment, terminal


def allowed(user):
    return bool(user and user.is_active and user.is_admin and user.host_access and not user.must_change_password)


def shell_command():
    if os.name == "nt":
        program = shutil.which("pwsh.exe") or str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        if not Path(program).is_file():
            raise ValueError("PowerShell is not installed on this PC")
        return [program, "-NoLogo", "-NoProfile", "-NoExit"], "PowerShell"
    program = shutil.which("pwsh")
    if program:
        return [program, "-NoLogo", "-NoProfile", "-NoExit"], "PowerShell"
    program = shutil.which("bash") or shutil.which("sh")
    if not program:
        raise ValueError("No interactive shell is installed")
    return [program, "-i"], Path(program).name


class ShellSession:
    def __init__(self, user, session_key, process, name):
        self.id = str(uuid.uuid4())
        self.user_id, self.session_key = user.id, session_key
        self.process, self.name = process, name
        self.started = self.last_input = time.monotonic()
        self.lock = threading.RLock()
        self.stop_lock = threading.Lock()
        self.buffer, self.size, self.sequence = deque(), 0, 0
        self.closed = threading.Event()
        self.stopping = False
        self.reason = ""
        self.reader = threading.Thread(target=self.read, name="solar-admin-terminal-reader", daemon=True)
        self.monitor = threading.Thread(target=self.watch, name="solar-admin-terminal-monitor", daemon=True)

    def start(self):
        self.reader.start()
        self.monitor.start()

    def read(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                raw = self.process.read()
                if not raw:
                    break
                value = decoder.decode(raw)
                with self.lock:
                    self.sequence += 1
                    self.buffer.append((self.sequence, value))
                    self.size += len(value)
                    while self.size > 1024**2 and self.buffer:
                        self.size -= len(self.buffer.popleft()[1])
        except OSError:
            self.reason = self.reason or "Terminal disconnected"
        finally:
            # Closing a ConPTY must happen while its reader can still drain it.
            threading.Thread(target=self.stop, daemon=True).start()

    def authorized(self):
        close_old_connections()
        try:
            user = User.objects.filter(pk=self.user_id).first()
            if not allowed(user):
                return False
            session = SessionStore(session_key=self.session_key)
            saved_hash = session.get("_auth_user_hash", "")
            hashes = [user.get_session_auth_hash(), *user.get_session_auth_fallback_hash()]
            return str(session.get("_auth_user_id")) == str(user.pk) and any(constant_time_compare(saved_hash, value) for value in hashes)
        finally:
            close_old_connections()

    def watch(self):
        try:
            while not self.closed.wait(2):
                now = time.monotonic()
                if not self.authorized():
                    self.reason = "Access revoked or sign-in expired"
                    break
                if now - self.started > config()["execution"]["timeout_seconds"]:
                    self.reason = "Terminal reached its session time limit"
                    break
                if now - self.last_input > config()["execution"]["native_idle_seconds"]:
                    self.reason = "Terminal stopped after its idle timeout"
                    break
        finally:
            self.stop()

    def output_since(self, sequence):
        with self.lock:
            return self.sequence, "".join(text for number, text in self.buffer if number > sequence)

    def stop(self):
        with self.stop_lock:
            if self.stopping:
                return
            self.stopping = True
        try:
            self.process.close()
        except OSError:
            self.reason = self.reason or "Terminal ended"
        finally:
            self.closed.set()

    def status(self):
        return {"id": self.id, "name": self.name, "running": not (self.closed.is_set() or self.stopping), "reason": self.reason}


class ShellManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.sessions = {}

    def get(self, user, session_key, terminal_id=None):
        if not allowed(user) or not session_key:
            return None
        with self.lock:
            session = self.sessions.get(str(user.id))
            if session and session.session_key == session_key and (not terminal_id or session.id == terminal_id):
                return session
        return None

    def start(self, user, session_key):
        if not allowed(user) or not session_key:
            raise PermissionError("Administrator and trusted host execution permissions are required")
        with self.lock:
            previous = self.sessions.get(str(user.id))
            if previous and not previous.closed.is_set():
                if previous.session_key == session_key:
                    return previous
                raise ValueError("A terminal is already running in another sign-in session. Stop it there first.")
            argv, name = shell_command()
            process = terminal(argv, Path.home(), child_environment())
            session = ShellSession(user, session_key, process, name)
            self.sessions[str(user.id)] = session
            session.start()
            return session

    def stop_for_session(self, key):
        with self.lock:
            sessions = [item for item in self.sessions.values() if item.session_key == key]
        for session in sessions:
            session.stop()

    def stop_for_user(self, user_id):
        with self.lock:
            session = self.sessions.get(str(user_id))
        if session:
            session.stop()

    def stop_all(self):
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.stop()


shells = ShellManager()
