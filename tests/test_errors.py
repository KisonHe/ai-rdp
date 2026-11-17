import contextlib
import io
import queue
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from ai_rdp.backend import NativeBackend
from ai_rdp.errors import RdpError, native_error, network_fact
from ai_rdp.service import Controller


class ErrorTests(unittest.TestCase):
    def test_reported_decimal_code_identifies_network_stage(self):
        error = native_error("connect_failed_131078", target="192.0.2.10:3389", elapsed=10.01)
        self.assertEqual(error.detail["stage"], "network")
        self.assertEqual(error.detail["symbol"], "ERRCONNECT_CONNECT_FAILED")
        self.assertEqual(error.detail["code_hex"], "0x00020006")
        self.assertIn("尚未进入账号验证", str(error))
        self.assertIn("10.0 秒", str(error))
        self.assertNotIn("connection_refused", error.detail["reason"])

    def test_specific_network_cause_requires_explicit_log_fact(self):
        tail = "password=do-not-surface\nconnect to 192.0.2.10:3389 failed with error: Connection refused [61]"
        error = native_error("connect_failed_131078", network_reason=network_fact(tail))
        self.assertEqual(error.detail["reason"], "connection_refused")
        self.assertNotIn("do-not-surface", str(error))
        for phrase, expected in [("No route to host", "network_unreachable"), ("Operation timed out", "network_timeout")]:
            fact = network_fact("connect to target failed with error: " + phrase)
            self.assertEqual(native_error("connect_failed_131078", network_reason=fact).detail["reason"], expected)
        self.assertIsNone(network_fact("ConnectLayer target:3389 [10000ms] failed"))

    def test_authentication_failure_is_not_reported_as_definite_wrong_password(self):
        generic = native_error("connect_failed_131081")  # 0x20009
        specific = native_error("connect_failed_131093")  # 0x20015
        self.assertEqual(generic.detail["stage"], "authentication")
        self.assertIn("不能仅凭此码断定密码错误", str(generic))
        self.assertIn("明确报告", str(specific))
        self.assertEqual(specific.detail["symbol"], "ERRCONNECT_WRONG_PASSWORD")

    def test_account_lock_and_permission_have_distinct_advice(self):
        locked = native_error(f"connect_failed_{0x20018}")
        denied = native_error(f"connect_failed_{0x2001A}")
        self.assertIn("先停止重复尝试", locked.detail["hint"])
        self.assertIn("未被授予此登录类型", denied.detail["message"])

    def test_certificate_mismatch_is_distinct_from_generic_tls(self):
        generic = native_error(f"connect_failed_{0x20008}", observed_certificate="a"*64)
        self.assertEqual(generic.detail["reason"], "tls_connect_failed")
        error = native_error(f"connect_failed_{0x20008}", certificate_reason="certificate_mismatch",
                             observed_certificate="a"*64, expected_certificate="b"*64)
        self.assertEqual(error.detail["reason"], "certificate_mismatch")
        self.assertEqual(error.detail["expected_certificate"], "b"*64)
        self.assertIn("已停止登录", str(error))

    def test_unknown_numeric_errors_keep_code_without_inventing_reason(self):
        error = native_error("connect_failed_3735928559")
        self.assertEqual(error.detail["code_hex"], "0xDEADBEEF")
        self.assertEqual(error.detail["reason"], "unknown_rdp_error")
        self.assertIn("底层没有提供已知的具体原因", str(error))
        self.assertNotIn("sensitive-value", str(native_error("unexpected_sensitive-value")))

    def test_server_disconnect_reason_is_preserved(self):
        error = native_error(f"disconnected_{0x10005}")
        self.assertIn("另一个连接", error.detail["title"])
        self.assertEqual(error.detail["symbol"], "ERRINFO_DISCONNECTED_BY_OTHER_CONNECTION")

    def test_legacy_protocol_error_uses_same_human_message(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = NativeBackend(Path("unused"), Path(directory))
            with self.assertRaises(RdpError) as caught:
                backend._result(["ERR", "0", "connect_failed_131078"])
            self.assertEqual(caught.exception.detail["stage"], "network")

    def test_terminal_error_survives_eof_and_later_write(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = NativeBackend(Path("unused"), Path(directory))
            backend.process = SimpleNamespace(stdout=io.StringIO("ERR 0 disconnected_65541 ERRINFO_DISCONNECTED_BY_OTHER_CONNECTION\n"), poll=lambda: 2)
            pending = queue.Queue()
            backend.pending[3] = pending
            backend._read()
            with self.assertRaises(RdpError) as caught:
                backend._result(pending.get_nowait())
            self.assertEqual(caught.exception.detail["code"], 65541)
            with self.assertRaises(RdpError) as caught:
                backend._write("CAPTURE 4")
            self.assertEqual(caught.exception.detail["code"], 65541)

    def test_certificate_callbacks_reach_python_without_raw_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = NativeBackend(Path("unused"), Path(directory))
            backend.expected_certificate = "b"*64
            backend.process = SimpleNamespace(stdout=io.StringIO("CERT 0 " + "a"*64 + "\nCERT_ERROR 0 certificate_mismatch\nERR 0 connect_failed_131080 ERRCONNECT_TLS_CONNECT_FAILED\n"))
            pending = queue.Queue()
            backend.pending[0] = pending
            backend._read()
            with self.assertRaises(RdpError) as caught: backend._result(pending.get_nowait())
            self.assertEqual(caught.exception.detail["reason"], "certificate_mismatch")

    def test_command_timeout_survives_cleanup_and_background_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = NativeBackend(Path("unused"), Path(directory))
            backend._write = lambda _: None
            backend.stop = backend.stopped.set
            with patch("ai_rdp.backend.queue.Queue.get", side_effect=queue.Empty):
                with self.assertRaises(RdpError) as first:
                    backend.command("CAPTURE", -1)
            self.assertEqual(first.exception.detail["reason"], "command_timeout")
            with self.assertRaises(RdpError) as later:
                backend.command("CAPTURE", -1)
            self.assertIs(later.exception, first.exception)
            with self.assertRaises(RdpError) as eof:
                backend._result(["ERR", "0", "transport_closed"])
            self.assertIs(eof.exception, first.exception)

    def test_controller_exposes_human_error_to_terminal_and_status(self):
        error = native_error("connect_failed_131078", target="192.0.2.10:3389")
        class FailedBackend:
            def connect(self, **_): raise error
            def stop(self): pass
        controller = Controller(FailedBackend())
        config = {"password": "not-for-errors"}
        terminal = io.StringIO()
        with contextlib.redirect_stderr(terminal): controller.start(config)
        self.assertEqual(config, {})
        status = controller.status()
        self.assertEqual(status["error_detail"]["code"], 131078)
        self.assertIn("无法连接到远程桌面端口", terminal.getvalue())
        self.assertNotIn("not-for-errors", str(status) + terminal.getvalue())


if __name__ == "__main__": unittest.main()
