"""An explicit, short-lived desktop lease. No screenshot is taken before Start."""
import io
import queue
import secrets
import threading
import time

from solar.config import config


class Desktop:
    def __init__(self):
        self.lock = threading.RLock()
        self.lease = None
        self.frame = None
        self.frame_number = 0
        self.error = ""
        self.thread = None

    def start(self, user, session_key, monitor=1):
        if not config()["desktop"]["enabled"] or not user.remote_access or not session_key:
            raise PermissionError("Remote desktop is disabled")
        with self.lock:
            if self.lease:
                if self.lease["session"] == session_key:
                    return self.status(user, session_key)
                raise ValueError("Remote desktop is already in use")
            if self.thread and self.thread.is_alive():
                raise ValueError("The previous desktop stream is stopping. Try again in a moment.")
            now = time.monotonic()
            self.lease = {"id": secrets.token_urlsafe(32), "user": user.id, "session": session_key,
                          "started": now, "activity": now, "heartbeat": now, "connected": False,
                          "monitor": monitor, "commands": queue.Queue(maxsize=100)}
            self.error, self.frame = "", None
            lease = self.lease
            self.thread = threading.Thread(target=self.capture, args=(lease,), name="solar-desktop", daemon=True)
            self.thread.start()
        return self.status(user, session_key)

    def status(self, user, session_key):
        with self.lock:
            mine = bool(self.lease and self.lease["user"] == user.id and self.lease["session"] == session_key)
            return {"running": bool(self.lease), "mine": mine,
                    "lease_id": self.lease["id"] if mine else None, "error": self.error if not self.lease or mine else ""}

    def stop_for_session(self, session_key):
        with self.lock:
            if self.lease and self.lease["session"] == session_key:
                self.lease = None
                self.frame = None

    def verify(self, user_id, session_key, lease_id):
        with self.lock:
            lease = self.lease
            return bool(lease and lease["user"] == user_id and lease["session"] == session_key
                        and secrets.compare_digest(lease["id"], lease_id))

    def heartbeat(self, lease_id):
        with self.lock:
            if self.lease and self.lease["id"] == lease_id:
                self.lease["heartbeat"] = time.monotonic()
                self.lease["connected"] = True

    def input(self, lease_id, command):
        if command.get("type") not in ("move", "click", "button", "release", "scroll", "key", "text"):
            raise ValueError("Unsupported desktop command")
        with self.lock:
            if not self.lease or self.lease["id"] != lease_id:
                raise PermissionError("Desktop lease expired")
            self.lease["activity"] = time.monotonic()
            try:
                self.lease["commands"].put_nowait(command)
            except queue.Full:
                raise ValueError("Too many desktop inputs") from None

    def capture(self, lease):
        mouse = keyboard = None
        held_buttons = set()
        try:
            import mss
            from PIL import Image
            with mss.mss() as capture:
                index = lease["monitor"]
                if index >= len(capture.monitors):
                    raise ValueError("Display is unavailable")
                monitor = capture.monitors[index]
                while True:
                    with self.lock:
                        if self.lease is not lease:
                            break
                        now = time.monotonic()
                        cfg = config()["desktop"]
                        if (now - lease["heartbeat"] > 15 or now - lease["activity"] > cfg["idle_seconds"]
                                or now - lease["started"] > cfg["max_minutes"] * 60):
                            self.lease, self.frame = None, None
                            break
                    for _ in range(50):
                        try:
                            command = lease["commands"].get_nowait()
                        except queue.Empty:
                            break
                        if mouse is None:
                            from pynput import mouse as mouse_module, keyboard as keyboard_module
                            mouse, keyboard = mouse_module.Controller(), keyboard_module.Controller()
                        self.apply_input(command, mouse, keyboard, monitor, held_buttons)
                    screenshot = capture.grab(monitor)
                    img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
                    if img.width > cfg["max_width"]:
                        img.thumbnail((cfg["max_width"], cfg["max_width"]))
                    output = io.BytesIO()
                    img.save(output, "JPEG", quality=65)
                    with self.lock:
                        if self.lease is lease:
                            self.frame, self.frame_number = output.getvalue(), self.frame_number + 1
                    time.sleep(1 / max(1, min(10, cfg["fps"])))
        except Exception:
            with self.lock:
                self.error = "Desktop unavailable. Solar must run in an unlocked interactive desktop session with screen/input permissions."
        finally:
            if mouse:
                for button in held_buttons:
                    try:
                        mouse.release(button)
                    except Exception:
                        pass
            with self.lock:
                if self.lease is lease:
                    self.lease, self.frame = None, None

    @staticmethod
    def apply_input(command, mouse, keyboard, monitor, held_buttons=None):
        from pynput.mouse import Button
        from pynput.keyboard import Key
        kind = command["type"]
        if kind == "release":
            if held_buttons is not None:
                for button in tuple(held_buttons):
                    mouse.release(button)
                    held_buttons.discard(button)
        elif kind in ("move", "click", "button"):
            x, y = float(command.get("x", 0)), float(command.get("y", 0))
            if not (0 <= x <= 1 and 0 <= y <= 1):
                return
            mouse.position = (monitor["left"] + int(x * (monitor["width"] - 1)),
                              monitor["top"] + int(y * (monitor["height"] - 1)))
            button = {"right": Button.right, "middle": Button.middle}.get(command.get("button"), Button.left)
            if kind == "click":
                mouse.click(button, 2 if command.get("double") is True else 1)
            elif kind == "button":
                if command.get("down") is True:
                    mouse.press(button)
                    if held_buttons is not None:
                        held_buttons.add(button)
                elif command.get("down") is False:
                    mouse.release(button)
                    if held_buttons is not None:
                        held_buttons.discard(button)
        elif kind == "scroll":
            mouse.scroll(0, max(-10, min(10, int(command.get("amount", 0)))))
        elif kind == "text":
            value = command.get("text", "")
            if isinstance(value, str) and len(value) <= 1000 and "\0" not in value:
                keyboard.type(value)
        elif kind == "key":
            mapping = {"Enter": Key.enter, "Escape": Key.esc, "Tab": Key.tab, "Backspace": Key.backspace,
                       "Delete": Key.delete, "ArrowLeft": Key.left, "ArrowRight": Key.right,
                       "ArrowUp": Key.up, "ArrowDown": Key.down, "Home": Key.home, "End": Key.end,
                       "PageUp": Key.page_up, "PageDown": Key.page_down, " ": Key.space}
            mapping.update({f"F{i}": getattr(Key, f"f{i}") for i in range(1, 13)})
            value = command.get("key", "")
            key = mapping.get(value)
            if key is None and isinstance(value, str) and len(value) == 1 and value.isprintable():
                key = value
            if key is None:
                return
            modifiers = [k for field, k in (("ctrl", Key.ctrl), ("alt", Key.alt), ("shift", Key.shift), ("meta", Key.cmd)) if command.get(field) is True]
            try:
                for modifier in modifiers:
                    keyboard.press(modifier)
                keyboard.press(key)
                keyboard.release(key)
            finally:
                for modifier in reversed(modifiers):
                    keyboard.release(modifier)


desktop = Desktop()
