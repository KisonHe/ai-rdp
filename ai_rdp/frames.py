from __future__ import annotations

import base64
import hashlib
import io
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from PIL import Image, ImageChops


def png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def changed_rects(before: Image.Image, after: Image.Image, tile: int = 64):
    """Exact RGB tile comparison. No threshold that can hide small text changes."""
    diff = ImageChops.difference(before, after)
    w, h = after.size
    dirty = set()
    for y in range(0, h, tile):
        for x in range(0, w, tile):
            if diff.crop((x, y, min(x + tile, w), min(y + tile, h))).getbbox():
                dirty.add((x // tile, y // tile))
    rects = []
    while dirty:
        seed = dirty.pop()
        group, pending = [seed], [seed]
        while pending:
            x, y = pending.pop()
            for p in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if p in dirty:
                    dirty.remove(p)
                    pending.append(p)
                    group.append(p)
        # Small context margin makes text changes easier to interpret.
        rects.append((max(0, min(x for x, _ in group) * tile - 12),
                      max(0, min(y for _, y in group) * tile - 12),
                      min(w, (max(x for x, _ in group) + 1) * tile + 12),
                      min(h, (max(y for _, y in group) + 1) * tile + 12)))
    return sorted(rects, key=lambda r: (r[1], r[0]))


@dataclass
class Baseline:
    image: Image.Image
    frame_id: str
    full_at: float
    deltas: int = 0


class Frames:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.image = None
        self.frame_id = None
        self.received_at = 0.0
        self.changed_at = 0.0
        self.baselines: OrderedDict[str, Baseline] = OrderedDict()
        self.issued: OrderedDict[str, tuple] = OrderedDict()

    def publish(self, image: Image.Image):
        image = image.convert("RGB")
        digest = hashlib.sha256(image.tobytes() + str(image.size).encode()).hexdigest()[:20]
        with self.condition:
            self.received_at = time.monotonic()
            if self.frame_id != f"{self.session_id}:{digest}":
                self.image = image.copy()
                self.frame_id = f"{self.session_id}:{digest}"
                self.changed_at = self.received_at
            self.condition.notify_all()

    def touch(self):
        with self.condition:
            self.received_at = time.monotonic()
            self.condition.notify_all()

    def settle(self, started, finished, timeout=3.0, quiet=.6, cancel=None):
        """A bounded visual heuristic, not confirmation of application success."""
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                now = time.monotonic()
                if cancel and cancel.is_set(): return {"quiet": False, "cancelled": True}
                changed = self.changed_at >= started
                fresh = self.received_at >= finished + .3
                if fresh and now - self.changed_at >= quiet and (changed or now - finished >= 1.5):
                    return {"quiet": True, "changed_after_action": changed}
                if now >= deadline:
                    return {"quiet": False, "changed_after_action": changed}
                self.condition.wait(min(.1, deadline - now))

    def wait_changed(self, observer, timeout, cancel=None):
        deadline = time.monotonic() + timeout
        with self.condition:
            baseline = self.baselines.get(observer)
            if baseline is None: return {"changed": True, "timed_out": False}
            while self.frame_id == baseline.frame_id:
                if cancel and cancel.is_set(): return {"changed": False, "cancelled": True}
                left = deadline - time.monotonic()
                if left <= 0: return {"changed": False, "timed_out": True}
                self.condition.wait(min(.1, left))
            return {"changed": True, "timed_out": False}

    def current(self):
        with self.lock:
            if self.image is None:
                raise ValueError("No desktop frame is available yet")
            return self.image.copy(), self.frame_id, time.monotonic() - self.received_at

    def wait(self, seconds: float):
        with self.condition:
            old = self.frame_id
            self.condition.wait_for(lambda: self.frame_id != old, min(max(seconds, 0), 5))

    def validate_action(self, frame_id: str, points=()):
        with self.lock:
            issued = self.issued.get(frame_id)
            if not issued or self.image is None:
                raise ValueError("Observe this session before acting")
            when, size, bounds = issued
            if time.monotonic() - when > 30 or size != self.image.size:
                raise ValueError("Observation expired or desktop resized; observe again")
            for x, y in points:
                if not bounds[0] <= x < bounds[2] or not bounds[1] <= y < bounds[3]:
                    raise ValueError("Coordinates are outside the observed desktop region")

    def observe(self, mode="auto", observer="default", region=None, max_width=1600):
        if mode not in {"auto", "full", "changes", "crop"}:
            raise ValueError("mode must be auto, full, changes or crop")
        if not isinstance(observer, str) or not 1 <= len(observer) <= 64:
            raise ValueError("observer must contain 1–64 characters")
        if not isinstance(max_width, int) or isinstance(max_width, bool) or not 320 <= max_width <= 3840:
            raise ValueError("max_width must be between 320 and 3840")
        with self.lock:
            image, fid, age = self.current()
            w, h = image.size
            now = time.monotonic()
            prior = self.baselines.get(observer)
            reason, actual = "requested", mode
            if mode == "crop":
                if not isinstance(region, list) or len(region) != 4 or any(type(n) is not int for n in region):
                    raise ValueError("crop requires [x,y,width,height] in desktop pixels")
                x, y, rw, rh = region
                if x < 0 or y < 0 or rw < 1 or rh < 1 or x + rw > w or y + rh > h:
                    raise ValueError("crop is outside the desktop")
                rects = [(x, y, x + rw, y + rh)]
            elif (mode == "full" or not prior or prior.image.size != image.size or
                  now - prior.full_at >= 60 or prior.deltas >= 8):
                actual, rects = "full", [(0, 0, w, h)]
                reason = "requested" if mode == "full" else "context_refresh"
            else:
                rects = changed_rects(prior.image, image)
                area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects)
                if len(rects) > 4 or area > w * h * .45:
                    actual, rects, reason = "full", [(0, 0, w, h)], "large_change"
                else:
                    actual, reason = "changes", "changed" if rects else "unchanged"
            patches = []
            for left, top, right, bottom in rects:
                part = image.crop((left, top, right, bottom))
                original = part.size
                if part.width > max_width:
                    part = part.resize((max_width, max(1, round(part.height * max_width / part.width))), Image.Resampling.LANCZOS)
                data = png(part)
                patches.append({"origin": [left, top], "source_size": list(original),
                                "image_size": list(part.size),
                                "scale": [part.width / original[0], part.height / original[1]],
                                "mime_type": "image/png", "bytes": len(data),
                                "data": base64.b64encode(data).decode()})
            # A crop does not acknowledge unseen changes elsewhere on screen.
            if actual != "crop":
                baseline = Baseline(image, fid, now if actual == "full" else prior.full_at,
                                    0 if actual == "full" else prior.deltas + bool(rects))
                self.baselines[observer] = baseline
                self.baselines.move_to_end(observer)
                while len(self.baselines) > 16:
                    self.baselines.popitem(last=False)
            bounds = rects[0] if actual == "crop" else (0, 0, w, h)
            # Unique observation handle avoids conflating crops of the same underlying frame.
            observation_id = f"{fid}:{time.monotonic_ns()}"
            self.issued[observation_id] = (now, image.size, bounds)
            while len(self.issued) > 32:
                self.issued.popitem(last=False)
            return {"session_id": self.session_id, "frame_id": observation_id,
                    "content_id": fid, "base_content_id": prior.frame_id if prior else None,
                    "desktop_size": [w, h], "frame_age_ms": round(age * 1000),
                    "mode": actual, "reason": reason, "patches": patches,
                    "coordinate_space": "Use original desktop pixels: desktop = origin + image_pixel / scale",
                    "returned_pixels": sum(p["image_size"][0] * p["image_size"][1] for p in patches),
                    "full_desktop_pixels": w * h}
