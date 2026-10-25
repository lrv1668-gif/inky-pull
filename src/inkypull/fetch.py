"""Conditional image fetching: only return an image when it actually changed."""

from __future__ import annotations

import hashlib
import io
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from PIL import Image, UnidentifiedImageError


class FetchError(Exception):
    """The URL couldn't be fetched or didn't return a usable image."""


@dataclass
class Validators:
    etag: str | None = None
    last_modified: str | None = None
    digest: str | None = None


@dataclass
class Fetched:
    url: str
    image: Image.Image
    digest: str


class Poller:
    """Fetches image URLs with If-None-Match / If-Modified-Since.

    `fetch` returns None when the image is unchanged: either the server answered 304, or
    it sent the same bytes again (for servers that don't send validators). After each
    fetch, `retry_after` is the response's Retry-After in seconds (None without one) —
    the server saying when it's worth checking again.
    """

    def __init__(
        self, client: httpx.Client, headers: dict[str, str] | None = None, timeout: float = 30.0
    ):
        self._client = client
        self._headers = dict(headers or {})
        self._timeout = timeout
        self._validators: dict[str, Validators] = {}
        self.retry_after: float | None = None

    def fetch(self, url: str, *, force: bool = False, wait: float | None = None) -> Fetched | None:
        """`wait` (seconds) asks the server to long-poll: a conditional request gets `?wait=`,
        and a server that supports it holds the request until the image changes."""
        headers = dict(self._headers)
        target = httpx.URL(url)
        known = self._validators.get(url)
        if known and not force:
            if known.etag:
                headers["If-None-Match"] = known.etag
            if known.last_modified:
                headers["If-Modified-Since"] = known.last_modified
            if wait and (known.etag or known.last_modified):
                # Merged, not passed as params=, which would replace the URL's own query
                target = target.copy_merge_params({"wait": int(wait)})

        self.retry_after = None
        try:
            response = self._client.get(
                target, headers=headers, timeout=self._timeout, follow_redirects=True
            )
        except httpx.HTTPError as e:
            raise FetchError(f"couldn't reach {url}: {e}") from e
        self.retry_after = parse_retry_after(response.headers.get("retry-after"))

        if response.status_code == 304:
            if known is None:
                raise FetchError(f"{url} answered 304 to an unconditional request")
            return None
        if response.status_code >= 400:
            raise FetchError(f"{url} returned HTTP {response.status_code}")

        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            raise FetchError(f"{url} returned {content_type or 'no content type'}, not an image")

        digest = hashlib.sha256(response.content).hexdigest()
        validators = Validators(
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            digest=digest,
        )
        if known and not force and known.digest == digest:
            self._validators[url] = validators
            return None

        try:
            image = Image.open(io.BytesIO(response.content))
            image.load()
        except (UnidentifiedImageError, OSError) as e:
            raise FetchError(f"{url} returned an image Pillow can't read: {e}") from e

        self._validators[url] = validators
        return Fetched(url=url, image=image, digest=digest)

    def forget(self, url: str) -> None:
        """Drop what we know about `url`, so the next fetch downloads it in full."""
        self._validators.pop(url, None)

    def export(self) -> dict[str, dict]:
        return {url: asdict(v) for url, v in self._validators.items()}

    def restore(self, data: dict[str, dict]) -> None:
        self._validators = {
            url: Validators(**{k: v.get(k) for k in ("etag", "last_modified", "digest")})
            for url, v in data.items()
            if isinstance(v, dict)
        }


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """A Retry-After header in seconds: delay-seconds or an HTTP date. None if absent or bad."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())
