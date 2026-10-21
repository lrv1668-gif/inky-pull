"""Display backends: the real Inky panel, a PNG file, or memory (for tests)."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Protocol

from PIL import Image

from . import image as images

log = logging.getLogger(__name__)

# Named drivers for when EEPROM detection fails: module, and resolution if it needs one.
DRIVERS: dict[str, tuple[str, tuple[int, int] | None]] = {
    "el133uf1": ("inky.inky_el133uf1", (1600, 1200)),  # Impression 13.3" (Spectra 6)
    "e673": ("inky.inky_e673", None),  # Impression 7.3" (Spectra 6)
    "e640": ("inky.inky_e640", None),  # Impression 4" (Spectra 6)
    "ac073tc1a": ("inky.inky_ac073tc1a", (800, 480)),  # Impression 7.3" (2023, 7-colour)
    "uc8159": ("inky.inky_uc8159", (600, 448)),  # Impression 5.7"
}


class DisplayError(Exception):
    """The display couldn't be opened."""


class Display(Protocol):
    resolution: tuple[int, int]

    def show(self, image: Image.Image) -> None:
        """Draw `image` (already `resolution` in size). May block while the panel refreshes."""


class InkyDisplay:
    """A Pimoroni Inky board, via the `inky` library."""

    def __init__(self, driver: str = "auto", saturation: float = 0.5):
        try:
            importlib.import_module("inky")
        except ImportError as e:
            raise DisplayError(
                "the `inky` library isn't installed in this Python environment — install it "
                "with Pimoroni's installer, or run inkypull from its venv"
            ) from e

        if driver == "auto":
            from inky.auto import auto

            try:
                self._inky = auto()
            except RuntimeError as e:
                raise DisplayError(
                    f"couldn't detect an Inky board ({e}); set `driver` in the config, "
                    f'e.g. driver = "el133uf1"'
                ) from e
        else:
            if driver not in DRIVERS:
                raise DisplayError(f"unknown driver {driver!r} (use auto, {', '.join(DRIVERS)})")
            module, resolution = DRIVERS[driver]
            cls = importlib.import_module(module).Inky
            self._inky = cls(resolution=resolution) if resolution else cls()
        self._saturation = saturation
        self.resolution = tuple(self._inky.resolution)
        log.info("using %s at %dx%d", type(self._inky).__module__, *self.resolution)

    def show(self, image: Image.Image) -> None:
        try:
            self._inky.set_image(image, saturation=self._saturation)
        except TypeError:  # older drivers have no saturation parameter
            self._inky.set_image(image)
        self._inky.show()


class FileDisplay:
    """Writes a preview of what the panel would show to a PNG file."""

    def __init__(
        self, path: Path | str, resolution: tuple[int, int] = (1600, 1200), saturation: float = 0.5
    ):
        self.path = Path(path)
        self.resolution = resolution
        self._saturation = saturation
        self.count = 0

    def show(self, image: Image.Image) -> None:
        images.preview(image, self._saturation).save(self.path)
        self.count += 1
        log.info("wrote %s", self.path)


class MemoryDisplay:
    """Keeps every image it's given. Useful in tests and for embedding."""

    def __init__(self, resolution: tuple[int, int] = (1600, 1200)):
        self.resolution = resolution
        self.shown: list[Image.Image] = []

    def show(self, image: Image.Image) -> None:
        self.shown.append(image)


def open_display(
    spec: str,
    *,
    driver: str = "auto",
    saturation: float = 0.5,
    resolution: tuple[int, int] = (1600, 1200),
) -> Display:
    """`inky` for the panel, or `file:<path>` to write PNG previews instead."""
    if spec == "inky":
        return InkyDisplay(driver=driver, saturation=saturation)
    if spec.startswith("file:"):
        return FileDisplay(spec.removeprefix("file:"), resolution, saturation)
    raise DisplayError(f"unknown display {spec!r} (use inky or file:<path.png>)")
