"""The frame: poll a URL, keep the panel showing the latest image, react to buttons."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx

from . import __version__, screens
from .actions import ActionRunner
from .buttons import Buttons, NoButtons
from .config import Config, in_quiet_hours
from .display import Display
from .fetch import FetchError, Poller
from .image import prepare
from .worker import DisplayWorker

log = logging.getLogger(__name__)


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "inkypull"


class Frame:
    """Glue between a URL, a display and its buttons.

    What's on the panel is tracked as a key — `url:<url>#<sha256>` or `screen:<name>` — and
    persisted with the HTTP validators, so a restart doesn't redraw an unchanged image
    (e-paper keeps its picture without power).
    """

    def __init__(
        self,
        config: Config,
        display: Display,
        *,
        buttons: Buttons | None = None,
        client: httpx.Client | None = None,
        state_dir: Path | None | bool = True,
        now: Callable[[], datetime] = datetime.now,
    ):
        self.config = config
        self.display = display
        self.size = tuple(display.resolution)
        self._owns_client = client is None
        self._client = client or httpx.Client(headers={"User-Agent": f"inkypull/{__version__}"})
        self._poller = Poller(self._client, config.headers, config.timeout)
        self._worker = DisplayWorker(display)
        self._buttons = buttons or NoButtons()
        self._actions = ActionRunner(self, self._client)
        self._now = now

        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._force = False
        self._long_polling = False
        self._server_holds = False  # a long poll was held once, so ?wait= is honoured
        self._failures = 0
        self._on_panel: str | None = None
        self._queued: str | None = None
        self.url = config.url
        self.last_error: str | None = None
        self.last_update: datetime | None = None

        if state_dir is True:
            state_dir = config.state_dir or default_state_dir()
        self._state_path = Path(state_dir) / "state.json" if state_dir else None
        self._load_state()

    # -- polling -------------------------------------------------------------------------

    def poll_once(
        self, *, force: bool = False, show_errors: bool = True, long_poll: bool = False
    ) -> bool:
        """Fetch the current URL and queue it if it changed. True if something was queued.

        `force` downloads in full and redraws even an unchanged image. `long_poll` (what
        `run` passes) asks the server to hold the request until the image changes, for
        up to the configured `long_poll`.
        """
        url = self.url
        full = force or self._source_on_panel() != url
        wait = self.config.long_poll if long_poll and not full else None
        if wait is not None:
            # Only the loop's own long poll touches the flag: a forced fetch from a button
            # while it's held mustn't clear it
            with self._lock:
                self._long_polling = True
        started = time.monotonic()
        try:
            fetched = self._poller.fetch(url, force=full, wait=wait)
        except FetchError as e:
            self._failed(e, show_errors)
            return False
        finally:
            if wait is not None:
                with self._lock:
                    self._long_polling = False
        if wait is not None and time.monotonic() - started >= wait / 2:
            with self._lock:
                self._server_holds = True
        if self.url != url:
            log.debug("source changed while fetching %s; dropping it", url)
            return False

        with self._lock:
            self._failures = 0
            self.last_error = None
        if fetched is None:
            log.debug("unchanged: %s", url)
            return False

        key = f"url:{url}#{fetched.digest}"
        if not force and key in (self._on_panel, self._queued):
            return False
        c = self.config
        image = prepare(
            fetched.image,
            self.size,
            fit=c.fit,
            background=c.background,
            rotate=c.rotate,
            portrait_rotate=c.portrait_rotate,
        )
        log.info("new image from %s", url)
        self._submit(image, key, on_fail=lambda: self._poller.forget(url))
        return True

    def run(self) -> None:
        """Poll until `stop()`; buttons and refresh requests wake the loop early."""
        self._buttons.start(self._on_button)
        c = self.config
        if c.long_poll:
            log.info("showing %s, long-polling up to %gs", self.url, c.long_poll)
        else:
            log.info("showing %s, checking at least every %gs", self.url, c.poll_interval)
        while not self._stop.is_set():
            with self._lock:
                force, self._force = self._force, False
            delay = c.poll_interval
            if force or not in_quiet_hours(c.quiet_hours, self._now().time()):
                started = time.monotonic()
                changed = self.poll_once(force=force, long_poll=bool(c.long_poll))
                delay = self.next_delay(time.monotonic() - started, changed)
            if delay > 0:
                self._wake.wait(delay)
            self._wake.clear()

    def next_delay(self, elapsed: float, changed: bool) -> float:
        """How long to sleep after a poll that took `elapsed` seconds.

        With `long_poll`, a request the server held goes straight back to waiting, as does
        a new image from a server that has held one before (the next request is a long
        poll again). One answered at once — a server that ignores `?wait=`, or sends no
        validators so `?wait=` is never sent — falls back to the interval, so it can't
        spin. Otherwise the server's Retry-After, capped at `poll_interval` (it may be
        hours away overnight) and at least a second.
        """
        c = self.config
        with self._lock:
            ok = self._failures == 0
            holds = self._server_holds
        if ok and c.long_poll and (elapsed >= c.long_poll / 2 or (changed and holds)):
            return 0.0
        retry = self._poller.retry_after
        return c.poll_interval if retry is None else max(1.0, min(retry, c.poll_interval))

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._worker.wait_idle(timeout)

    def close(self) -> None:
        self.stop()
        self._buttons.close()
        self._actions.close()
        self._worker.close()
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Frame:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- what actions call ---------------------------------------------------------------

    def request_refresh(self) -> None:
        """Fetch now and redraw, even if the image hasn't changed (e.g. to clear ghosting)."""
        with self._lock:
            long_polling = self._long_polling
            self._force = not long_polling
        if long_polling:
            self.poll_once(force=True)  # the loop is held in a long poll; don't wait for it
        self._wake.set()

    def poll_soon(self) -> None:
        """Check for a new image now; redraw only if it changed."""
        self._wake.set()

    def set_url(self, url: str) -> None:
        with self._lock:
            self.url = url
            self._save_state()
        log.info("source is now %s", url)
        with self._lock:
            long_polling = self._long_polling
        if long_polling:
            self.poll_once()  # the loop is held in a long poll on the old URL
        self.poll_soon()  # a different source is never "unchanged", so this redraws

    def show_status(self) -> None:
        lines = {
            "Showing": self.url,
            "Host": _hostname_and_ip(),
            "Last update": self.last_update.strftime("%Y-%m-%d %H:%M")
            if self.last_update
            else "never",
            "Last error": self.last_error or "none",
            "inkypull": __version__,
        }
        self._submit(screens.status(self.size, lines), "screen:status")

    def press(self, name: str, long: bool = False) -> None:
        """Run the action configured for a button (what the GPIO listener calls)."""
        self._on_button(name, long)

    # -- internals -----------------------------------------------------------------------

    def _on_button(self, name: str, long: bool) -> None:
        button = self.config.buttons.get(name)
        if button is None:
            log.info("button %s has no action", name)
            return
        action = (button.long_press if long else None) or button.press
        if action is None:
            return
        self._actions.submit(action, f"button {name}{' (long)' if long else ''}")

    def _failed(self, error: FetchError, show_errors: bool) -> None:
        with self._lock:
            self._failures += 1
            self.last_error = str(error)
            failures = self._failures
            nothing_shown = self._on_panel is None and self._queued is None
        log.warning("%s (failure %d)", error, failures)
        if not show_errors:
            return
        if failures >= self.config.error_after:
            self._submit_once(screens.unreachable(self.size, self.url, str(error)), "screen:error")
        elif nothing_shown:
            self._submit_once(screens.welcome(self.size, self.url), "screen:welcome")

    def _submit_once(self, image, key: str) -> None:
        if key not in (self._on_panel, self._queued):
            self._submit(image, key)

    def _submit(self, image, key: str, on_fail: Callable[[], None] | None = None) -> None:
        def done(error: Exception | None) -> None:
            with self._lock:
                if self._queued == key:
                    self._queued = None
                if error is not None:
                    self._on_panel = None
                    self.last_error = f"display: {error}"
                    if on_fail:
                        on_fail()
                else:
                    self._on_panel = key
                    if key.startswith("url:"):
                        self.last_update = self._now()
                self._save_state()

        with self._lock:
            self._queued = key
        self._worker.submit(image, done)

    def _source_on_panel(self) -> str | None:
        key = self._on_panel
        if key and key.startswith("url:"):
            return key.removeprefix("url:").rsplit("#", 1)[0]
        return None

    def _load_state(self) -> None:
        if not self._state_path or not self._state_path.is_file():
            return
        try:
            data = json.loads(self._state_path.read_text())
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable state file %s: %s", self._state_path, e)
            return
        if data.get("url") in self.config.sources:
            self.url = data["url"]
        self._on_panel = data.get("on_panel")
        self._poller.restore(data.get("validators", {}))

    def _save_state(self) -> None:
        if not self._state_path:
            return
        data = {"url": self.url, "on_panel": self._on_panel, "validators": self._poller.export()}
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._state_path)
        except OSError as e:
            log.warning("couldn't save state to %s: %s", self._state_path, e)


def _hostname_and_ip() -> str:
    name = socket.gethostname()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET; nothing is sent, it just picks a route
            return f"{name} ({s.getsockname()[0]})"
    except OSError:
        return name
