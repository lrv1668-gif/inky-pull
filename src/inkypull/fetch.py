"""Conditional image fetching: only return an image when it actually changed."""

from __future__ import annotations

import hashlib
import io
from dataclasses import asdict, dataclass

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
    it sent the same bytes again (for servers that don't send validators).
    """

    def __init__(
        self, client: httpx.Client, headers: dict[str, str] | None = None, timeout: float = 30.0
    ):
        self._client = client
        self._headers = dict(headers or {})
        self._timeout = timeout
        self._validators: dict[str, Validators] = {}

    def fetch(self, url: str, *, force: bool = False) -> Fetched | None:
        headers = dict(self._headers)
        known = self._validators.get(url)
        if known and not force:
            if known.etag:
                headers["If-None-Match"] = known.etag
            if known.last_modified:
                headers["If-Modified-Since"] = known.last_modified

        try:
            response = self._client.get(
                url, headers=headers, timeout=self._timeout, follow_redirects=True
            )
        except httpx.HTTPError as e:
            raise FetchError(f"couldn't reach {url}: {e}") from e

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
