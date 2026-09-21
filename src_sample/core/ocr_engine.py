"""
ocr_engine.py
=============

Reference implementation: a thin, local (offline) CAPTCHA recognizer built on a
lightweight ONNX model (via ``ddddocr``) with optional OpenCV preprocessing.

Highlights of the pattern
-------------------------
* The model is loaded lazily, once per process, behind a lock.
* ``warmup()`` moves the one-time load off the critical path.
* Every call reports its own latency so it can be logged and monitored.

Everything runs on the client machine; no image ever leaves the process.

This is a *reference sample*, not production source. Dependencies:
``pip install ddddocr opencv-python-headless numpy``
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

_ALNUM_ONLY = re.compile(r"[^A-Za-z0-9]")


@dataclass(frozen=True)
class OcrResult:
    text: str
    elapsed_ms: float


class OcrEngine:
    """Local CAPTCHA recognizer."""

    def __init__(self, preprocess: bool = True) -> None:
        self._preprocess = preprocess
        self._model = None
        self._lock = threading.Lock()

    def warmup(self) -> float:
        """Load the model ahead of time and return how long it took (ms)."""
        started = time.perf_counter()
        self._get_model()
        return (time.perf_counter() - started) * 1000

    def recognize(self, image_bytes: bytes) -> OcrResult:
        """Recognize the characters in a CAPTCHA image given as raw bytes."""
        started = time.perf_counter()
        prepared = self._prepare(image_bytes) if self._preprocess else image_bytes
        raw = self._get_model().classification(prepared)
        text = _ALNUM_ONLY.sub("", raw or "")
        return OcrResult(text=text, elapsed_ms=(time.perf_counter() - started) * 1000)

    # ------------------------------------------------------------- internals

    def _get_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:  # double-checked: load exactly once
                    import ddddocr  # imported lazily so importing this module stays cheap

                    self._model = ddddocr.DdddOcr(show_ad=False)
        return self._model

    @staticmethod
    def _prepare(image_bytes: bytes) -> bytes:
        """Grayscale + Otsu threshold: a common, general-purpose cleanup step."""
        image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("image bytes could not be decoded")
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        ok, encoded = cv2.imencode(".png", binary)
        if not ok:
            raise ValueError("image could not be re-encoded")
        return encoded.tobytes()


_default_engine: Optional[OcrEngine] = None


def solve(image_bytes: bytes) -> OcrResult:
    """Convenience wrapper around a shared, process-wide engine."""
    global _default_engine
    if _default_engine is None:
        _default_engine = OcrEngine()
    return _default_engine.recognize(image_bytes)


if __name__ == "__main__":
    import sys

    engine = OcrEngine()
    print(f"model warm-up: {engine.warmup():.0f} ms")
    for path in sys.argv[1:]:
        with open(path, "rb") as handle:
            result = engine.recognize(handle.read())
        print(f"{path}: {result.text!r} in {result.elapsed_ms:.1f} ms")
