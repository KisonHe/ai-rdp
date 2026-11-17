import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ai_rdp.platform_support import (IS_WINDOWS, check_private, lock_state,
                                     native_binary, platform_tag, prepare_state,
                                     write_private_json)


class PlatformTests(unittest.TestCase):
    def test_private_runtime_atomic_replacement_and_unicode_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / '测试 state'
            prepare_state(state)
            runtime = state / 'runtime.json'
            for version in (1, 2):
                write_private_json(runtime, {'version': version})
                check_private(runtime)
                self.assertEqual(json.loads(runtime.read_text(encoding='utf-8')), {'version': version})
            self.assertFalse(list(state.glob('.runtime-*')))

    def test_runtime_rejects_permissions_for_other_users(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'private'
            prepare_state(state)
            runtime = state / 'runtime.json'
            write_private_json(runtime, {})
            if IS_WINDOWS:
                subprocess.run(['icacls.exe', str(runtime), '/grant', '*S-1-5-32-545:R'],
                               check=True, capture_output=True)
            else:
                runtime.chmod(0o644)
            with self.assertRaises(RuntimeError):
                check_private(runtime)

    def test_lock_rejects_second_process_and_releases_after_close(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'service.lock'
            script = ('import sys; from pathlib import Path; '
                      'from ai_rdp.platform_support import lock_state; lock_state(Path(sys.argv[1]))')
            lock = lock_state(path)
            try:
                result = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'Another service', result.stderr)
            finally:
                lock.close()
            result = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_backend_precedes_development_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = 'ai-rdp-native.exe' if IS_WINDOWS else 'ai-rdp-native'
            built = root / 'build' / 'native' / name
            bundled = root / 'bin' / platform_tag() / name
            for path in (built, bundled):
                path.parent.mkdir(parents=True)
                path.touch()
            self.assertEqual(native_binary(root), bundled)
            bundled.unlink()
            self.assertEqual(native_binary(root), built)
