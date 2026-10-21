"""Pull an image from a URL and show it on a Pimoroni Inky e-paper display."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("inkypull")
except PackageNotFoundError:
    __version__ = "0.0.0"

from .config import Config, ConfigError, load  # noqa: E402
from .display import FileDisplay, InkyDisplay, MemoryDisplay, open_display  # noqa: E402
from .frame import Frame  # noqa: E402

__all__ = [
    "Config",
    "ConfigError",
    "FileDisplay",
    "Frame",
    "InkyDisplay",
    "MemoryDisplay",
    "__version__",
    "load",
    "open_display",
]
