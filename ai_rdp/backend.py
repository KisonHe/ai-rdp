from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw
from .errors import native_error, network_fact


class NativeBackend:
    def __init__(self, binary: Path, state_dir: Path):
        self.binary = binary
        self.state_dir = state_dir
        self.process = None
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending = {}
        self.next_id = 1
        self.stopped = threading.Event()
        self.certificate = None
        self.generation = -1
        self.target = None
        self.expected_certificate = None
        self.certificate_reason = None
        self.terminal_error = None
        self.connect_elapsed = None
        self.failure = None

    def connect(self, host, port, username, password, domain, width, height, certificate):
        self.target = f"{host}:{port}"
        self.expected_certificate = certificate
        log_fd = os.open(self.state_dir / "native.log", os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(log_fd, "wb") as log:
            try:
                environment = os.environ.copy()
                modules = self.binary.parent / "ossl-modules"
                if os.name == "nt" and modules.is_dir():
                    environment["OPENSSL_MODULES"] = str(modules.resolve())
                self.process = subprocess.Popen([str(self.binary)], stdin=subprocess.PIPE,
                                                stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8",
                                                bufsize=1, env=environment,
                                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except FileNotFoundError:
                raise native_error("native_missing") from None
            except OSError:
                raise native_error("native_start_failed") from None
        ready = queue.Queue()
        self.pending[0] = ready
        threading.Thread(target=self._read, daemon=True).start()
        def hx(s): return str(s).encode().hex() or "-"
        config = " ".join(["INIT", hx(host), str(port), hx(username), hx(domain), hx(password),
                           str(width), str(height), hx(self.state_dir / "native-frame.ppm"), hx(certificate)])
        started = time.monotonic()
        try:
            self._write(config)
            del config, password
            parts = ready.get(timeout=25)
            self.connect_elapsed = time.monotonic() - started
            self._result(parts)
        except queue.Empty:
            self.failure = native_error("connect_timeout", target=self.target)
            self.stop()
            raise self.failure from None
        finally:
            with self.pending_lock:
                self.pending.pop(0, None)

    def _write(self, line):
        with self.write_lock:
            if not self.process or self.process.poll() is not None:
                if self.failure: raise self.failure
                if self.terminal_error: self._result(self.terminal_error)
                raise native_error("native_not_running", target=self.target)
            try:
                self.process.stdin.write(line + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                if self.terminal_error: self._result(self.terminal_error)
                raise native_error("transport_closed", target=self.target) from None

    def _read(self):
        try:
            for line in self.process.stdout:
                parts = line.strip().split()
                if len(parts) < 2:
                    continue
                if parts[0] == "CERT":
                    self.certificate = parts[2]
                    continue
                if parts[0] == "CERT_ERROR":
                    self.certificate_reason = parts[2]
                    continue
                if parts[0] in {"OK", "ERR"}:
                    if parts[0] == "ERR" and parts[1] == "0": self.terminal_error = parts
                    with self.pending_lock:
                        pending = self.pending.get(int(parts[1]))
                    if pending:
                        pending.put(parts)
        finally:
            with self.pending_lock:
                for q in self.pending.values():
                    q.put(self.terminal_error or ["ERR", "0", "transport_closed"])

    def _network_fact(self):
        try:
            with (self.state_dir / "native.log").open("rb") as log:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 32768))
                return network_fact(log.read().decode(errors="replace"))
        except OSError:
            return None

    def _result(self, parts):
        if self.failure: raise self.failure
        if parts[0] != "OK":
            token = "stopped" if self.stopped.is_set() else parts[2]
            raise native_error(token, target=self.target,
                               elapsed=self.connect_elapsed if token.startswith("connect_failed_") else None,
                               certificate_reason=self.certificate_reason,
                               observed_certificate=self.certificate,
                               expected_certificate=self.expected_certificate,
                               network_reason=self._network_fact())
        return parts[2:]

    def command(self, op, *args):
        with self.lock:
            if self.stopped.is_set():
                raise self.failure or native_error("stopped", target=self.target)
            q = queue.Queue()
            ident = self.next_id
            self.next_id += 1
            with self.pending_lock:
                self.pending[ident] = q
            try:
                self._write(" ".join([op, str(ident), *map(str, args)]))
                try:
                    return self._result(q.get(timeout=10))
                except queue.Empty:
                    self.failure = native_error("command_timeout", target=self.target)
                    self.stop()
                    raise self.failure from None
            finally:
                with self.pending_lock:
                    self.pending.pop(ident, None)

    def capture(self):
        result = self.command("CAPTURE", self.generation)
        self.generation = int(result[2])
        if len(result) > 3 and result[3] == "unchanged": return None
        with Image.open(self.state_dir / "native-frame.ppm") as image:
            return image.convert("RGB")

    def key(self, code, down): self.command("KEY", code, int(down))
    def mouse(self, x, y, flags): self.command("MOUSE", x, y, flags)
    def text(self, text): self.command("TEXT", text.encode("utf-16-le").hex() or "-")
    def release(self): self.command("RELEASE")

    def stop(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        try:
            self._write("STOP 0")
        except (RuntimeError, BrokenPipeError, OSError):
            pass
        # Never waits for the normal command lock. Escalate if the native event loop hangs.
        def reap():
            if not self.process:
                return
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        threading.Thread(target=reap, daemon=True).start()


class DemoBackend:
    """Explicitly simulated desktop for protocol/UI tests, never an RDP success claim."""
    def __init__(self, width=1280, height=720):
        self.size = width, height
        self.value = ""
        self.count = 0
        self.stopped = threading.Event()
        self.events = []
        self.lock = threading.Lock()

    def connect(self, **_): pass
    def capture(self):
        with self.lock:
            im = Image.new("RGB", self.size, "#112039")
            d = ImageDraw.Draw(im)
            d.text((40, 32), "SIMULATED TEST DESKTOP — NOT WINDOWS / NOT RDP", fill="#f9bb62", font_size=24)
            d.rounded_rectangle((40, 100, 1000, 300), 14, fill="#ffffff")
            d.text((64, 122), "ai-rdp local test fixture", fill="#14283f", font_size=24)
            d.text((64, 180), f"Input: {self.value}", fill="#14283f", font_size=20)
            d.text((64, 240), f"Actions: {self.count}", fill="#14283f", font_size=20)
            return im
    def _event(self, *event):
        with self.lock:
            if self.stopped.is_set(): raise RuntimeError("stopped")
            self.events.append(event)
            self.count += 1
    def key(self, code, down): self._event("key", code, down)
    def mouse(self, x, y, flags): self._event("mouse", x, y, flags)
    def text(self, text):
        self._event("text", len(text))
        with self.lock: self.value += text
    def release(self): pass
    def stop(self): self.stopped.set()
