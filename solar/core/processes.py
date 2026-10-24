"""Portable subprocess helpers; argv is always passed without a command shell."""
import os
import platform
import queue
from pathlib import Path
import shutil
import subprocess
import threading
import time

import psutil


def executable(name):
    found = shutil.which(name + (".exe" if os.name == "nt" else "")) or shutil.which(name)
    if found and Path(found).suffix.lower() == ".exe":
        return found
    if name == "codex" and os.name == "nt":
        # npm's .cmd/.ps1 wrappers need a shell. Resolve the packaged executable instead.
        roots = []
        for suffix in ("codex.cmd", "codex.ps1"):
            wrapper = shutil.which(suffix)
            if wrapper:
                roots.append(Path(wrapper).parent / "node_modules" / "@openai" / "codex")
        for root in roots:
            architecture = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
            package = root / "node_modules" / "@openai" / f"codex-win32-{architecture}"
            for base in (package, root):
                for pattern in ("vendor/*/bin/codex.exe", "vendor/*/codex/codex.exe"):
                    matches = sorted(base.glob(pattern))
                    if matches:
                        return str(matches[0])
    if not found or Path(found).suffix.lower() in (".cmd", ".bat", ".ps1"):
        raise ValueError(f"{name} executable is unavailable. Install its official CLI on this PC.")
    return found


def child_environment():
    # Do not pass Solar secrets or unrelated API keys to provider processes.
    names = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE",
             "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "LANG", "LC_ALL",
             "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"}
    env = {k: v for k, v in os.environ.items() if k.upper() in names}
    env.update(TERM="xterm-256color", COLORTERM="truecolor", PYTHONUNBUFFERED="1")
    return env


def kill_tree(pid):
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in reversed(children):
            try:
                child.kill()
            except psutil.Error:
                pass
        parent.kill()
        psutil.wait_procs(children + [parent], timeout=3)
    except psutil.Error:
        pass


class PipeProcess:
    def __init__(self, argv, cwd, env):
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} \
            if os.name == "nt" else {"start_new_session": True}
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, **options)
        self.pid = self.process.pid

    def stop(self):
        if self.process.poll() is None:
            kill_tree(self.pid)


def run_captured(argv, cwd, env, check, attach, timeout_seconds=60, max_output=65536):
    """A cancellable, bounded, noninteractive command for explicitly trusted roles."""
    process = PipeProcess(argv, cwd, env)
    finished = threading.Event()
    chunks = queue.Queue(maxsize=32)
    threads = []
    started = time.monotonic()

    def read(stream, source):
        try:
            while not finished.is_set():
                data = stream.read(4096)
                if not data:
                    break
                while not finished.is_set():
                    try:
                        chunks.put((source, data), timeout=.1)
                        break
                    except queue.Full:
                        pass
        finally:
            stream.close()
            while not finished.is_set():
                try:
                    chunks.put((source, None), timeout=.1)
                    break
                except queue.Full:
                    pass

    try:
        attach(process)
        process.process.stdin.close()
        for source, stream in (("stdout", process.process.stdout), ("stderr", process.process.stderr)):
            thread = threading.Thread(target=read, args=(stream, source), daemon=True)
            thread.start()
            threads.append(thread)
        output = {"stdout": bytearray(), "stderr": bytearray()}
        done, total = set(), 0
        while len(done) < 2 or process.process.poll() is None:
            check()
            if time.monotonic() - started > timeout_seconds:
                raise ValueError("Host command exceeded its time limit")
            try:
                source, data = chunks.get(timeout=.1)
            except queue.Empty:
                continue
            if data is None:
                done.add(source)
                continue
            total += len(data)
            if total > max_output:
                raise ValueError("Host command output exceeded 64 KB; write large results to a file")
            output[source].extend(data)
        return {"exit_code": process.process.returncode,
                **{source: bytes(data).decode("utf-8", errors="replace") for source, data in output.items()}}
    finally:
        finished.set()
        process.stop()
        for thread in threads:
            thread.join(timeout=2)
        for stream in (process.process.stdin, process.process.stdout, process.process.stderr):
            if stream and not stream.closed:
                stream.close()


class PosixTerminal:
    def __init__(self, argv, cwd, env, cols=100, rows=30):
        import pty
        master, slave = pty.openpty()
        self.fd = master
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                                        start_new_session=True)
        self.pid = self.process.pid
        os.close(slave)
        self.resize(cols, rows)

    def read(self):
        try:
            return os.read(self.fd, 16384)
        except OSError:
            return b""

    def write(self, data):
        os.write(self.fd, data.encode("utf-8"))

    def resize(self, cols, rows):
        import fcntl
        import struct
        import termios
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def poll(self):
        return self.process.poll()

    def stop(self):
        if self.process.poll() is None:
            kill_tree(self.pid)

    def close(self):
        self.stop()
        os.close(self.fd)


class WindowsTerminal:
    """ConPTY (Windows 10 1809+), without a shell, window, or optional native package."""
    def __init__(self, argv, cwd, env, cols=100, rows=30):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        k = self.k = c.WinDLL("kernel32", use_last_error=True)

        class COORD(c.Structure):
            _fields_ = [("X", c.c_short), ("Y", c.c_short)]

        class STARTUPINFO(c.Structure):
            _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
                        ("lpTitle", w.LPWSTR), ("dwX", w.DWORD), ("dwY", w.DWORD),
                        ("dwXSize", w.DWORD), ("dwYSize", w.DWORD), ("dwXCountChars", w.DWORD),
                        ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD), ("dwFlags", w.DWORD),
                        ("wShowWindow", w.WORD), ("cbReserved2", w.WORD), ("lpReserved2", c.c_void_p),
                        ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]

        class STARTUPINFOEX(c.Structure):
            _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", c.c_void_p)]

        class PROCESS_INFORMATION(c.Structure):
            _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE), ("dwProcessId", w.DWORD),
                        ("dwThreadId", w.DWORD)]

        self.COORD = COORD
        k.CreatePipe.argtypes = [c.POINTER(w.HANDLE), c.POINTER(w.HANDLE), c.c_void_p, w.DWORD]
        k.CreatePipe.restype = w.BOOL
        k.CreatePseudoConsole.argtypes = [COORD, w.HANDLE, w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
        k.CreatePseudoConsole.restype = c.c_long
        k.ResizePseudoConsole.argtypes = [w.HANDLE, COORD]
        k.ResizePseudoConsole.restype = c.c_long
        k.ClosePseudoConsole.argtypes = [w.HANDLE]
        k.ClosePseudoConsole.restype = None
        k.InitializeProcThreadAttributeList.argtypes = [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)]
        k.UpdateProcThreadAttribute.argtypes = [c.c_void_p, w.DWORD, c.c_size_t, c.c_void_p,
                                              c.c_size_t, c.c_void_p, c.c_void_p]
        k.DeleteProcThreadAttributeList.argtypes = [c.c_void_p]
        k.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD,
                                    c.c_void_p, w.LPCWSTR, c.c_void_p, c.POINTER(PROCESS_INFORMATION)]
        k.CreateProcessW.restype = w.BOOL
        k.ReadFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
        k.WriteFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
        k.CloseHandle.argtypes = [w.HANDLE]
        k.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
        read_in, self.write_in = w.HANDLE(), w.HANDLE()
        self.read_out, write_out = w.HANDLE(), w.HANDLE()
        self.hpc, self.process_handle = w.HANDLE(), None
        self.closed = False
        self.write_lock = threading.Lock()
        try:
            if not k.CreatePipe(c.byref(read_in), c.byref(self.write_in), None, 0):
                raise c.WinError(c.get_last_error())
            if not k.CreatePipe(c.byref(self.read_out), c.byref(write_out), None, 0):
                raise c.WinError(c.get_last_error())
            hr = k.CreatePseudoConsole(COORD(cols, rows), read_in, write_out, 0, c.byref(self.hpc))
            if hr < 0:
                raise OSError(f"CreatePseudoConsole failed: {hr:#x}")
            size = c.c_size_t()
            k.InitializeProcThreadAttributeList(None, 1, 0, c.byref(size))
            attribute_buffer = c.create_string_buffer(size.value)
            attrs = c.cast(attribute_buffer, c.c_void_p)
            if not k.InitializeProcThreadAttributeList(attrs, 1, 0, c.byref(size)):
                raise c.WinError(c.get_last_error())
            try:
                if not k.UpdateProcThreadAttribute(attrs, 0, 0x00020016, self.hpc, c.sizeof(w.HANDLE), None, None):
                    raise c.WinError(c.get_last_error())
                startup = STARTUPINFOEX()
                startup.StartupInfo.cb = c.sizeof(STARTUPINFOEX)
                # Null explicit standard handles prevent Windows from duplicating the
                # server's redirected stdout instead of attaching the pseudoconsole.
                startup.StartupInfo.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
                startup.lpAttributeList = attrs
                info = PROCESS_INFORMATION()
                command = c.create_unicode_buffer(subprocess.list2cmdline(argv))
                env_block = c.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0")
                if not k.CreateProcessW(argv[0], command, None, None, False, 0x00080000 | 0x00000400,
                                        env_block, str(cwd), c.byref(startup), c.byref(info)):
                    raise c.WinError(c.get_last_error())
                self.process_handle, self.pid = info.hProcess, info.dwProcessId
                k.CloseHandle(info.hThread)
            finally:
                k.DeleteProcThreadAttributeList(attrs)
        except Exception:
            if self.hpc:
                k.ClosePseudoConsole(self.hpc)
            for handle in (self.write_in, self.read_out):
                if handle:
                    k.CloseHandle(handle)
            raise
        finally:
            for handle in (read_in, write_out):
                if handle:
                    k.CloseHandle(handle)

    def read(self):
        buffer, count = self.c.create_string_buffer(16384), self.w.DWORD()
        if not self.k.ReadFile(self.read_out, buffer, 16384, self.c.byref(count), None):
            return b""
        return buffer.raw[:count.value]

    def write(self, data):
        raw, count = data.encode("utf-8"), self.w.DWORD()
        with self.write_lock:
            if not self.k.WriteFile(self.write_in, raw, len(raw), self.c.byref(count), None):
                raise self.c.WinError(self.c.get_last_error())

    def resize(self, cols, rows):
        self.k.ResizePseudoConsole(self.hpc, self.COORD(cols, rows))

    def poll(self):
        code = self.w.DWORD()
        self.k.GetExitCodeProcess(self.process_handle, self.c.byref(code))
        return None if code.value == 259 else code.value

    def stop(self):
        if not self.closed and self.poll() is None:
            kill_tree(self.pid)

    def close(self):
        if self.closed:
            return
        self.stop()
        self.closed = True
        # A concurrent reader must continue draining while ClosePseudoConsole runs.
        self.k.ClosePseudoConsole(self.hpc)
        for handle in (self.read_out, self.write_in, self.process_handle):
            self.k.CloseHandle(handle)


def terminal(argv, cwd, env, cols=100, rows=30):
    return (WindowsTerminal if os.name == "nt" else PosixTerminal)(argv, cwd, env, cols, rows)
