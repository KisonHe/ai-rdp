import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from ai_rdp.platform_support import native_binary

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(native_binary(ROOT).is_file(), "Native backend has not been built")
class NativeErrorTests(unittest.TestCase):
    def test_closed_loopback_port_has_actionable_probe_error(self):
        # A port bound by this test but not listening cannot be taken by another server.
        with socket.socket() as reservation, tempfile.TemporaryDirectory() as directory:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
            result = subprocess.run([sys.executable, "-m", "ai_rdp", "probe", "--host", "127.0.0.1",
                                     "--port", str(port), "--state-dir", directory],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        # Local firewall/stealth settings may turn an unavailable port into a timeout
        # rather than an immediate refusal. Both must keep their actual diagnosis.
        self.assertTrue(any(title in result.stderr for title in (
            "目标电脑拒绝了端口连接", "连接远程桌面端口超时", "无法连接到远程桌面端口")), result.stderr)
        self.assertIn("尚未进入账号验证阶段", result.stderr)
        self.assertIn("ERRCONNECT_CONNECT_FAILED / 0x00020006 / 131078", result.stderr)
        self.assertIn(f"127.0.0.1:{port}", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
