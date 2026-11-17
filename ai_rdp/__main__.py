from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import signal
import sys
import threading
from pathlib import Path

from .backend import DemoBackend, NativeBackend
from .service import Controller, LocalServer, write_runtime
from .platform_support import lock_state, native_binary, prepare_state

ROOT = Path(__file__).resolve().parent.parent


def main():
    os.umask(0o077)
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Local C++ FreeRDP bridge with an observer and latched stop")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--demo", action="store_true")
    serve.add_argument("--profile", type=Path)
    serve.add_argument("--host")
    serve.add_argument("--port", type=int, default=3389)
    serve.add_argument("--username")
    serve.add_argument("--domain", default="")
    serve.add_argument("--cert-sha256", default="")
    serve.add_argument("--width", type=int, default=1600)
    serve.add_argument("--height", type=int, default=900)
    serve.add_argument("--listen-port", type=int, default=8765)
    serve.add_argument("--password-stdin", action="store_true", help="Read one password line from stdin, never argv/env/file")
    serve.add_argument("--state-dir", type=Path, default=ROOT / ".state")
    probe = sub.add_parser("probe")
    probe.add_argument("--host", required=True)
    probe.add_argument("--port", type=int, default=3389)
    probe.add_argument("--username", default="")
    probe.add_argument("--state-dir", type=Path, default=ROOT / ".state")
    mcp = sub.add_parser("mcp")
    mcp.add_argument("--state-dir", type=Path, default=ROOT / ".state")
    args = parser.parse_args()
    if args.command == "mcp":
        from .mcp_server import run
        run(args.state_dir)
        return
    state = args.state_dir.resolve()
    prepare_state(state)
    try: lock = lock_state(state / "service.lock")
    except BlockingIOError: parser.error("Another service is using this state directory")
    binary = native_binary(ROOT)
    if args.command == "probe":
        backend = NativeBackend(binary, state)
        try:
            backend.connect(host=args.host, port=args.port, username=args.username, password="",
                            domain="", width=1600, height=900, certificate="")
        except RuntimeError as exc:
            if backend.certificate:
                print(json.dumps({"host": args.host, "port": args.port, "sha256": backend.certificate,
                                  "trusted": False, "password_sent": False}))
            else:
                print(str(exc), file=sys.stderr)
                raise SystemExit(1)
        finally: backend.stop()
        return
    if not 640 <= args.width <= 3840 or not 480 <= args.height <= 2160:
        parser.error("Desktop size must be 640–3840 by 480–2160")
    if args.demo:
        backend, config = DemoBackend(args.width, args.height), {}
    else:
        profile = json.loads(args.profile.read_text(encoding="utf-8-sig")) if args.profile else {}
        host = args.host or profile.get("host")
        username = args.username or profile.get("username")
        certificate = args.cert_sha256 or profile.get("certificate", "")
        certificate = certificate.lower().replace(":", "")
        if not host or not username: parser.error("--host and --username (or a profile) are required")
        if not re.fullmatch(r"[a-f0-9]{64}", certificate):
            parser.error("Supply a verified SHA256 certificate pin; use 'probe' to inspect, not automatically trust")
        if not binary.is_file():
            parser.error("Native backend missing; use the release for this platform or follow the developer build instructions")
        password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Windows password (not saved): ")
        if not password: parser.error("An existing Windows password is required")
        backend = NativeBackend(binary, state)
        config = dict(host=host, port=profile.get("port", args.port), username=username,
                      password=password, domain=profile.get("domain", args.domain),
                      width=args.width, height=args.height, certificate=certificate)
        del password
    controller = Controller(backend, args.demo)
    server = LocalServer(("127.0.0.1", args.listen_port), controller, secrets.token_urlsafe(32), secrets.token_urlsafe(32))
    runtime = write_runtime(state, server.server_port, server.control_token, server.watch_token, controller.frames.session_id)
    print(f"Observer: {runtime['base_url']}/watch#{runtime['watch_token']}", flush=True)
    print("Press Ctrl+C or the observer's Stop button to disconnect. No AI resume endpoint exists.", flush=True)
    threading.Thread(target=controller.start, args=(config,), daemon=True).start()
    def terminate(*_):
        controller.stop("terminal")
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever(poll_interval=.1)
    except KeyboardInterrupt:
        controller.stop("terminal")
    finally:
        server.server_close()
        try:
            if json.loads((state / "runtime.json").read_text()).get("pid") == os.getpid():
                (state / "runtime.json").unlink()
        except (OSError, ValueError): pass


if __name__ == "__main__": main()
