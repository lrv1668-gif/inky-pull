"""One thread owns the panel; requests that arrive during a refresh are coalesced."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PIL import Image

from .display import Display

log = logging.getLogger(__name__)

Done = Callable[[Exception | None], None]


class DisplayWorker:
    """Shows images one at a time. `submit` never blocks.

    A Spectra 6 refresh takes tens of seconds; if several images are submitted meanwhile,
    only the newest is shown next (the others' callbacks are never called).
    """

    def __init__(self, display: Display):
        self._display = display
        self._cond = threading.Condition()
        self._pending: tuple[Image.Image, Done | None] | None = None
        self._busy = False
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="inkypull-display", daemon=True)
        self._thread.start()

    def submit(self, image: Image.Image, on_done: Done | None = None) -> None:
        with self._cond:
            if self._pending is not None:
                log.debug("replacing a queued image that hadn't been shown yet")
            self._pending = (image, on_done)
            self._cond.notify_all()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until nothing is queued or showing. False on timeout."""
        with self._cond:
            return self._cond.wait_for(lambda: self._pending is None and not self._busy, timeout)

    def close(self, timeout: float | None = None) -> None:
        """Finish anything queued, then stop the thread."""
        self.wait_idle(timeout)
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._pending is not None or self._closed)
                if self._pending is None:
                    return
                image, on_done = self._pending
                self._pending = None
                self._busy = True
            error: Exception | None = None
            try:
                self._display.show(image)
            except Exception as e:  # keep the worker alive whatever the driver throws
                log.exception("display refresh failed")
                error = e
            if on_done:
                try:
                    on_done(error)
                except Exception:
                    log.exception("display callback failed")
            with self._cond:
                self._busy = False
                self._cond.notify_all()
