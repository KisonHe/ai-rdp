from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .frames import Frames, png

KEYS = {"esc": 1, "escape": 1, "backspace": 14, "tab": 15, "enter": 28,
        "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "space": 57,
        "win": 0x15B, "windows": 0x15B, "delete": 0x153, "insert": 0x152,
        "home": 0x147, "end": 0x14F, "pageup": 0x149, "pagedown": 0x151,
        "up": 0x148, "down": 0x150, "left": 0x14B, "right": 0x14D,
        "capslock": 58}
KEYS.update(dict(zip("1234567890", range(2, 12))))
KEYS.update(dict(zip("qwertyuiop", range(16, 26))))
KEYS.update(dict(zip("asdfghjkl", range(30, 39))))
KEYS.update(dict(zip("zxcvbnm", range(44, 51))))
KEYS.update({f"f{i}": 58 + i for i in range(1, 11)})
KEYS.update({"f11": 87, "f12": 88})


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


class Controller:
    def __init__(self, backend, demo=False):
        self.backend = backend
        self.demo = demo
        self.frames = Frames(uuid.uuid4().hex[:12])
        self.stop_event = threading.Event()
        self.action_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.state = "connecting"
        self.error = None
        self.last_action = None
        self.pointer = None
        self.stop_reason = None
        self.stop_at = None
        self.action_started = 0.0
        self.action_finished = 0.0

    def start(self, config):
        try:
            self.backend.connect(**config)
            config.clear()
            with self.state_lock:
                if self.stop_event.is_set():
                    self.backend.stop()
                    return
                self.state = "connected"
            while not self.stop_event.is_set():
                image = self.backend.capture()
                if not self.stop_event.is_set():
                    if image is None: self.frames.touch()
                    else: self.frames.publish(image)
                self.stop_event.wait(.25)
        except Exception as exc:
            config.clear()
            with self.state_lock:
                if not self.stop_event.is_set():
                    self.state, self.error = "failed", str(exc)
            self.backend.stop()

    def status(self):
        with self.state_lock:
            try:
                im, _, age = self.frames.current()
                size = list(im.size)
            except ValueError:
                size, age = None, None
            return {"session_id": self.frames.session_id, "state": self.state,
                    "demo": self.demo, "desktop_size": size,
                    "frame_age_ms": round(age * 1000) if age is not None else None,
                    "last_action": self.last_action, "pointer": self.pointer,
                    "error": self.error, "stop_reason": self.stop_reason,
                    "stopped": self.stop_event.is_set(),
                    "restart_required": self.stop_event.is_set()}

    def stop(self, reason="operator"):
        # This path never waits for action_lock or a screenshot/network request.
        with self.state_lock:
            if not self.stop_event.is_set():
                self.stop_event.set()
                self.stop_at = time.monotonic()
                self.stop_reason = reason
                self.state = "stopped"
        self.backend.stop()
        return self.status()

    def _running(self):
        if self.stop_event.is_set():
            raise RuntimeError("Operator stopped this session; only a manual service restart can reconnect")
        if self.state != "connected":
            raise RuntimeError("RDP is not connected")

    def _pause(self, seconds):
        if self.stop_event.wait(seconds):
            self._running()

    def _point(self, action, prefix=""):
        return (integer(action.get(prefix + "x"), prefix + "x", 0, 3839),
                integer(action.get(prefix + "y"), prefix + "y", 0, 2159))

    def _validate(self, action):
        if not isinstance(action, dict): raise ValueError("Each action must be an object")
        kind = action.get("type")
        if kind == "click":
            point = self._point(action)
            if action.get("button", "left") not in {"left", "right", "middle"}:
                raise ValueError("button must be left, right or middle")
            integer(action.get("count", 1), "count", 1, 2)
            return [point]
        if kind == "scroll":
            point = self._point(action)
            if action.get("direction") not in {"up", "down", "left", "right"}:
                raise ValueError("Invalid scroll direction")
            integer(action.get("steps", 3), "steps", 1, 20)
            return [point]
        if kind == "drag":
            return [self._point(action), self._point(action, "to_")]
        if kind == "key":
            chord = action.get("keys")
            if not isinstance(chord, list) or not 1 <= len(chord) <= 5 or any(
                not isinstance(k, str) or k.lower() not in KEYS for k in chord):
                raise ValueError("keys must contain 1–5 supported key names")
            if len(set(k.lower() for k in chord)) != len(chord):
                raise ValueError("Duplicate keys are not allowed")
            return []
        if kind == "text":
            text = action.get("text")
            if not isinstance(text, str) or not 1 <= len(text) <= 1000 or any(ord(c) < 32 or ord(c) == 127 for c in text):
                raise ValueError("text must contain 1–1000 printable characters; send Enter/Tab separately")
            text.encode("utf-16-le")  # Reject lone surrogates before sending any event.
            return []
        raise ValueError("Action type must be click, key, text, scroll or drag")

    def act(self, frame_id, actions):
        if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
            raise ValueError("actions must contain 1–8 actions")
        # Validate the whole request before the first potentially irreversible event.
        points = [p for action in actions for p in self._validate(action)]
        if sum(len(action.get("text", "")) for action in actions if action["type"] == "text") > 1000:
            raise ValueError("A batch may contain at most 1000 text characters in total")
        with self.action_lock:
            self._running()
            self.frames.validate_action(frame_id, points)
            self.action_started = time.monotonic()
            completed = 0
            try:
                for action in actions:
                    self._running()
                    kind = action["type"]
                    if kind == "text":
                        self.backend.text(action["text"])
                        summary = f"Text input ({len(action['text'])} characters)"
                    elif kind == "key":
                        codes = [KEYS[k.lower()] for k in action["keys"]]
                        for code in codes:
                            self._running()
                            self.backend.key(code, True)
                        for code in reversed(codes):
                            self._running()
                            self.backend.key(code, False)
                        summary = "Keyboard shortcut"
                    elif kind == "click":
                        x, y = self._point(action)
                        button = {"left": 0x1000, "right": 0x2000, "middle": 0x4000}[action.get("button", "left")]
                        self.backend.mouse(x, y, 0x0800)
                        for _ in range(action.get("count", 1)):
                            self._running()
                            self.backend.mouse(x, y, button | 0x8000)
                            self._pause(.04)
                            self.backend.mouse(x, y, button)
                            self._pause(.06)
                        self.pointer = [x, y]
                        summary = "Mouse click"
                    elif kind == "scroll":
                        x, y = self._point(action)
                        direction = action["direction"]
                        flags = 0x0400 if direction in {"left", "right"} else 0x0200
                        flags |= (512 - 120) if direction in {"down", "left"} else 120
                        for _ in range(action.get("steps", 3)):
                            self._running()
                            self.backend.mouse(x, y, flags)
                            self._pause(.025)
                        self.pointer = [x, y]
                        summary = "Scroll"
                    elif kind == "drag":
                        x, y = self._point(action)
                        tx, ty = self._point(action, "to_")
                        self.backend.mouse(x, y, 0x1000 | 0x8000)
                        for step in range(1, 21):
                            self._running()
                            self.backend.mouse(round(x + (tx-x)*step/20), round(y + (ty-y)*step/20), 0x0800)
                            self._pause(.025)
                        self.backend.mouse(tx, ty, 0x1000)
                        self.pointer = [tx, ty]
                        summary = "Mouse drag"
                    completed += 1
                    self.last_action = {"summary": summary, "at": time.time(), "completed": completed}
                    self._pause(.08)
                return {"completed": completed, "session_id": self.frames.session_id}
            finally:
                if not self.stop_event.is_set():
                    self.backend.release()
                self.action_finished = time.monotonic()

    def observe(self, **options):
        timing = options.pop("timing", "immediate")
        timeout_ms = integer(options.pop("timeout_ms", 1500), "timeout_ms", 0, 5000)
        if timing not in {"immediate", "changed", "settled"}:
            raise ValueError("timing must be immediate, changed or settled")
        timing_result = {"policy": timing}
        if timing == "changed":
            timing_result.update(self.frames.wait_changed(options.get("observer", "default"), timeout_ms / 1000, self.stop_event))
        elif timing == "settled":
            now = time.monotonic()
            timing_result.update(self.frames.settle(self.action_started or now, self.action_finished or now,
                                                   timeout_ms / 1000, cancel=self.stop_event))
        result = self.frames.observe(**options)
        result["timing"] = timing_result
        result["status"] = self.status()
        return result


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, controller, control_token, watch_token):
        if address[0] != "127.0.0.1": raise ValueError("Only IPv4 loopback binding is supported")
        self.controller = controller
        self.control_token = control_token
        self.watch_token = watch_token
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-rdp/0.1"
    web_root = Path(__file__).parent / "web"

    def log_message(self, *_): pass  # URLs/tokens/input never enter HTTP access logs.

    def _send(self, code, data, content_type="application/json"):
        if not isinstance(data, bytes): data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass

    def _origin(self):
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed: return False
        origin = self.headers.get("Origin")
        return not origin or origin in {f"http://{host}" for host in allowed}

    def _role(self):
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer "): return None
        value = value[7:]
        if hmac.compare_digest(value, self.server.control_token): return "control"
        if hmac.compare_digest(value, self.server.watch_token): return "watch"
        return None

    def do_GET(self):
        if not self._origin(): return self._send(403, {"error": "Invalid Host or Origin"})
        path = urlsplit(self.path).path
        static = {"/watch": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "application/javascript"),
                  "/style.css": ("style.css", "text/css")}
        if path in static:
            name, mime = static[path]
            return self._send(200, (self.web_root / name).read_bytes(), mime)
        if not self._role(): return self._send(401, {"error": "Authentication required"})
        if path == "/v1/status": return self._send(200, self.server.controller.status())
        if path == "/v1/frame.png":
            try:
                image, _, _ = self.server.controller.frames.current()
                return self._send(200, png(image), "image/png")
            except ValueError as exc: return self._send(409, {"error": str(exc)})
        return self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self._origin(): return self._send(403, {"error": "Invalid Host or Origin"})
        role = self._role()
        if not role: return self._send(401, {"error": "Authentication required"})
        path = urlsplit(self.path).path
        if path == "/v1/stop":
            return self._send(200, self.server.controller.stop("operator" if role == "watch" else "agent"))
        if role != "control": return self._send(403, {"error": "Observer has no input permission"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536: raise ValueError("Request size must be 1–65536 bytes")
            self.connection.settimeout(5)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict): raise ValueError("Request must be an object")
            controller = self.server.controller
            if path == "/v1/observe": return self._send(200, controller.observe(**data))
            if path == "/v1/act": return self._send(200, controller.act(data.get("frame_id"), data.get("actions")))
            return self._send(404, {"error": "Not found"})
        except (ValueError, TypeError, UnicodeError) as exc:
            return self._send(400, {"error": str(exc)})
        except RuntimeError as exc:
            return self._send(409, {"error": str(exc)})
        except Exception:
            return self._send(500, {"error": "Internal service error"})


def write_runtime(state_dir, port, control_token, watch_token, session_id):
    data = {"pid": os.getpid(), "base_url": f"http://127.0.0.1:{port}",
            "control_token": control_token, "watch_token": watch_token, "session_id": session_id}
    path = state_dir / "runtime.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file: json.dump(data, file)
    return data
