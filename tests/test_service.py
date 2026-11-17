import http.client
import json
import threading
import time
import unittest
from ai_rdp.backend import DemoBackend
from ai_rdp.service import Controller, LocalServer


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.backend = DemoBackend()
        self.controller = Controller(self.backend, demo=True)
        self.controller.state = "connected"
        self.controller.frames.publish(self.backend.capture())
        self.fid = self.controller.observe()["frame_id"]

    def test_all_actions_are_validated_before_input(self):
        with self.assertRaises(ValueError):
            self.controller.act(self.fid, [{"type": "click", "x": 20, "y": 20}, {"type": "text", "text": "bad\n"}])
        self.assertEqual(self.backend.events, [])

    def test_batch_text_limit_is_checked_before_events(self):
        with self.assertRaises(ValueError):
            self.controller.act(self.fid, [{"type":"text","text":"a"*600},{"type":"text","text":"b"*600}])
        self.assertEqual(self.backend.events, [])

    def test_chord_releases_keys_in_reverse_order(self):
        self.controller.act(self.fid, [{"type": "key", "keys": ["ctrl", "shift", "a"]}])
        self.assertEqual(self.backend.events, [("key", 29, True), ("key", 42, True), ("key", 30, True), ("key", 30, False), ("key", 42, False), ("key", 29, False)])

    def test_unicode_is_not_logged(self):
        self.controller.act(self.fid, [{"type": "text", "text": "hello中文🙂"}])
        self.assertEqual(self.backend.value, "hello中文🙂")
        self.assertNotIn("hello", json.dumps(self.controller.status()))

    def test_lone_surrogate_is_rejected_before_any_events(self):
        with self.assertRaises(ValueError):
            self.controller.act(self.fid, [{"type": "click", "x": 20, "y": 20}, {"type": "text", "text": "hello\ud800"}])
        self.assertEqual(self.backend.events, [])

    def test_stop_interrupts_drag_and_is_latched(self):
        errors = []
        def drag():
            try: self.controller.act(self.fid, [{"type": "drag", "x": 10, "y": 10, "to_x": 100, "to_y": 100}, {"type": "text", "text": "must never happen"}])
            except RuntimeError as exc: errors.append(str(exc))
        thread = threading.Thread(target=drag)
        thread.start()
        time.sleep(.07)
        start = time.monotonic()
        self.controller.stop()
        thread.join(timeout=.5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic()-start, .5)
        count = len(self.backend.events)
        time.sleep(.04)
        self.assertEqual(count, len(self.backend.events))
        self.assertEqual(self.backend.value, "")
        self.assertTrue(errors)
        with self.assertRaises(RuntimeError): self.controller.act(self.fid, [{"type": "key", "keys": ["enter"]}])

    def test_outside_desktop_is_rejected_before_event(self):
        with self.assertRaises(ValueError): self.controller.act(self.fid, [{"type": "click", "x": 2000, "y": 20}])
        self.assertFalse(self.backend.events)

    def test_immediate_and_settled_have_independent_image_modes(self):
        self.assertEqual(self.controller.observe(mode="crop", region=[0,0,100,100], timing="immediate")["mode"], "crop")
        result = self.controller.observe(mode="full", timing="settled", timeout_ms=0)
        self.assertEqual(result["mode"], "full")
        self.assertFalse(result["timing"]["quiet"])


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.backend = DemoBackend()
        self.controller = Controller(self.backend, demo=True)
        self.controller.state = "connected"
        self.controller.frames.publish(self.backend.capture())
        self.server = LocalServer(("127.0.0.1", 0), self.controller, "control-token", "watch-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, role=None, data=None, extra=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Authorization": f"Bearer {role}-token"} if role else {}
        headers.update(extra or {})
        conn.request(method, path, json.dumps(data) if data is not None else None, headers)
        r = conn.getresponse()
        payload = r.read()
        conn.close()
        return r.status, payload

    def test_frames_and_status_require_auth(self):
        self.assertEqual(self.request("GET", "/v1/frame.png")[0], 401)
        self.assertEqual(self.request("GET", "/v1/status")[0], 401)
        self.assertEqual(self.request("GET", "/v1/frame.png", "watch")[0], 200)

    def test_observer_cannot_type_but_can_stop(self):
        self.assertEqual(self.request("POST", "/v1/act", "watch", {})[0], 403)
        self.assertEqual(self.request("POST", "/v1/stop", "watch", {})[0], 200)
        self.assertTrue(self.controller.stop_event.is_set())
        self.assertEqual(self.request("POST", "/v1/resume", "control", {})[0], 404)

    def test_cross_origin_and_dns_rebinding_are_blocked(self):
        for header in ({"Origin": "https://evil.example"}, {"Host": "rebind.example"}):
            self.assertEqual(self.request("POST", "/v1/stop", "watch", {}, header)[0], 403)
        self.assertFalse(self.controller.stop_event.is_set())

    def test_no_change_has_no_base64_image(self):
        first = json.loads(self.request("POST", "/v1/observe", "control", {})[1])
        second = json.loads(self.request("POST", "/v1/observe", "control", {})[1])
        self.assertEqual(first["mode"], "full")
        self.assertEqual(second["patches"], [])


if __name__ == "__main__": unittest.main()
