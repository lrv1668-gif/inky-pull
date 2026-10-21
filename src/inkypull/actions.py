"""What buttons do: the configured `Action`s, run off the GPIO thread one at a time."""

from __future__ import annotations

import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

import httpx

from .config import Action

log = logging.getLogger(__name__)


class Target(Protocol):
    """The parts of `Frame` actions can drive."""

    url: str

    def request_refresh(self) -> None: ...
    def poll_soon(self) -> None: ...
    def show_status(self) -> None: ...
    def set_url(self, url: str) -> None: ...


class ActionRunner:
    def __init__(self, target: Target, client: httpx.Client):
        self._target = target
        self._client = client
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inkypull-action")

    def submit(self, action: Action, label: str = "") -> None:
        self._executor.submit(self._run_logged, action, label)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _run_logged(self, action: Action, label: str) -> None:
        try:
            self.run(action)
        except Exception as e:
            log.error("%s: %s action failed: %s", label or "button", action.kind, e)

    def run(self, action: Action) -> None:
        """Run one action now, raising on failure."""
        t = self._target
        match action.kind:
            case "refresh":
                t.request_refresh()
            case "status":
                t.show_status()
            case "none":
                pass
            case "cycle":
                urls = action.urls
                i = urls.index(t.url) if t.url in urls else -1
                t.set_url(urls[(i + 1) % len(urls)])
            case "http":
                response = self._client.request(
                    action.method,
                    action.url,
                    json=action.json,
                    headers=action.headers,
                    timeout=action.timeout,
                )
                response.raise_for_status()
                log.info("%s %s → %d", action.method, action.url, response.status_code)
            case "command":
                result = subprocess.run(
                    list(action.run), timeout=action.timeout, capture_output=True, text=True
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"{action.run[0]} exited {result.returncode}: {result.stderr.strip()}"
                    )
            case _:
                raise ValueError(f"unknown action {action.kind!r}")
        if action.then == "refresh":
            # Not forced: the server may still be re-rendering, and redrawing the old image
            # would cost a full panel refresh for nothing. The image is shown once it changes.
            t.poll_soon()
