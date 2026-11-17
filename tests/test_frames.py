import unittest
import threading
import time
from PIL import Image, ImageDraw
from ai_rdp.frames import Frames


class FrameTests(unittest.TestCase):
    def setUp(self):
        self.frames = Frames("fixture")
        self.image = Image.new("RGB", (1600, 900), "white")
        self.frames.publish(self.image)

    def test_initial_full_then_no_image_when_unchanged(self):
        first = self.frames.observe()
        self.assertEqual(first["mode"], "full")
        second = self.frames.observe()
        self.assertEqual(second["reason"], "unchanged")
        self.assertEqual(second["patches"], [])

    def test_single_pixel_change_is_not_lost(self):
        first = self.frames.observe()
        self.image.putpixel((103, 211), (254, 255, 255))
        self.frames.publish(self.image)
        result = self.frames.observe()
        self.assertEqual(result["mode"], "changes")
        self.assertEqual(result["base_content_id"], first["content_id"])
        patch = result["patches"][0]
        x, y = patch["origin"]
        w, h = patch["source_size"]
        self.assertTrue(x <= 103 < x+w and y <= 211 < y+h)
        self.assertLess(result["returned_pixels"], result["full_desktop_pixels"] * .1)

    def test_watching_and_cropping_do_not_advance_delta_baseline(self):
        self.frames.observe()
        self.image.putpixel((1450, 800), (0, 0, 0))
        self.frames.publish(self.image)
        self.frames.current()  # Human viewer.
        cropped = self.frames.observe(mode="crop", region=[0, 0, 200, 100])
        with self.assertRaises(ValueError):
            self.frames.validate_action(cropped["frame_id"], [(1450, 800)])
        delta = self.frames.observe()
        self.assertEqual(delta["reason"], "changed")
        self.assertGreater(delta["patches"][0]["origin"][0], 1000)

    def test_large_change_and_resize_force_full_context(self):
        self.frames.observe()
        self.frames.publish(Image.new("RGB", (1600, 900), "black"))
        self.assertEqual(self.frames.observe()["reason"], "large_change")
        old = self.frames.observe()["frame_id"]
        self.frames.publish(Image.new("RGB", (1280, 720), "black"))
        with self.assertRaises(ValueError): self.frames.validate_action(old)
        self.assertEqual(self.frames.observe()["mode"], "full")

    def test_periodic_full_and_scale_contract(self):
        self.frames.observe()
        self.frames.baselines["default"].full_at -= 61
        result = self.frames.observe(max_width=800)
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["patches"][0]["scale"], [.5, .5])
        self.assertEqual(result["patches"][0]["image_size"], [800, 450])

    def test_moving_window_includes_erased_and_new_areas(self):
        d = ImageDraw.Draw(self.image)
        d.rectangle((100, 100, 200, 200), fill="black")
        self.frames.publish(self.image)
        self.frames.observe()
        d.rectangle((100, 100, 200, 200), fill="white")
        d.rectangle((1100, 500, 1200, 600), fill="black")
        self.frames.publish(self.image)
        result = self.frames.observe()
        self.assertEqual(len(result["patches"]), 2)

    def test_distinct_observers_and_observation_expiry(self):
        result = self.frames.observe(observer="a")
        self.assertEqual(self.frames.observe(observer="b")["mode"], "full")
        stamp, size, bounds = self.frames.issued[result["frame_id"]]
        self.frames.issued[result["frame_id"]] = (stamp-31, size, bounds)
        with self.assertRaises(ValueError): self.frames.validate_action(result["frame_id"])

    def test_invalid_crop_and_boolean_dimensions_are_rejected(self):
        for region in ([0, 0, 0, 20], [1599, 899, 5, 5], [True, 0, 10, 10]):
            with self.assertRaises(ValueError): self.frames.observe(mode="crop", region=region)
        with self.assertRaises(ValueError): self.frames.observe(max_width=True)

    def test_video_cannot_block_bounded_settling(self):
        stop = threading.Event()
        def animate():
            n = 0
            while not stop.wait(.015):
                image = self.image.copy()
                image.putpixel((100, 100), (n % 255, 0, 0))
                self.frames.publish(image)
                n += 1
        thread = threading.Thread(target=animate)
        thread.start()
        start = time.monotonic()
        try:
            result = self.frames.settle(start, start, timeout=.18, quiet=.1)
            self.assertFalse(result["quiet"])
            self.assertLess(time.monotonic() - start, .5)
        finally:
            stop.set(); thread.join()

    def test_change_wait_returns_existing_new_frame_without_waiting_for_next(self):
        self.frames.observe()
        self.image.putpixel((10, 10), (0, 0, 0))
        self.frames.publish(self.image)
        start = time.monotonic()
        self.assertTrue(self.frames.wait_changed("default", .5)["changed"])
        self.assertLess(time.monotonic()-start, .1)


if __name__ == "__main__": unittest.main()
