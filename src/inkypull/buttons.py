"""Button input via libgpiod v2 (the same stack `inky` uses)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Protocol

log = logging.getLogger(__name__)

# (button name, long press?)
OnPress = Callable[[str, bool], None]

DEBOUNCE_NS = 30_000_000


class Buttons(Protocol):
    def start(self, on_press: OnPress) -> None: ...
    def close(self) -> None: ...


class NoButtons:
    """For displays without buttons, or when running off a Pi."""

    def start(self, on_press: OnPress) -> None:
        pass

    def close(self) -> None:
        pass


class GpioButtons:
    """Watches active-low buttons on BCM GPIO pins.

    A press is reported on release: `long` is True if it was held for `long_press` seconds.
    """

    def __init__(self, pins: dict[str, int], long_press: float = 1.0):
        try:
            import gpiod
            import gpiodevice
            from gpiod.line import Bias, Direction, Edge
        except ImportError as e:
            raise RuntimeError(
                "button support needs `gpiod` and `gpiodevice` (installed alongside inky)"
            ) from e

        self._gpiod = gpiod
        self._long_ns = int(long_press * 1e9)
        chip = gpiodevice.find_chip_by_platform()
        self._names = {chip.line_offset_from_id(pin): name for name, pin in pins.items()}
        settings = gpiod.LineSettings(
            direction=Direction.INPUT, bias=Bias.PULL_UP, edge_detection=Edge.BOTH
        )
        self._request = chip.request_lines(
            consumer="inkypull", config={tuple(self._names): settings}
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_press: OnPress) -> None:
        self._thread = threading.Thread(
            target=self._run, args=(on_press,), name="inkypull-buttons", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        self._request.release()

    def _run(self, on_press: OnPress) -> None:
        falling = self._gpiod.EdgeEvent.Type.FALLING_EDGE
        pressed_at: dict[int, int] = {}
        last_edge: dict[int, int] = {}
        while not self._stop.is_set():
            if not self._request.wait_edge_events(timedelta(seconds=0.5)):
                continue
            for event in self._request.read_edge_events():
                offset, ts = event.line_offset, event.timestamp_ns
                if ts - last_edge.get(offset, 0) < DEBOUNCE_NS:
                    continue
                last_edge[offset] = ts
                if event.event_type == falling:
                    pressed_at[offset] = ts
                elif (start := pressed_at.pop(offset, None)) is not None:
                    name = self._names[offset]
                    held = ts - start >= self._long_ns
                    log.info("button %s %s", name, "long press" if held else "press")
                    try:
                        on_press(name, held)
                    except Exception:
                        log.exception("button handler failed")
