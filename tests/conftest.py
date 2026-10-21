import io

import httpx
import pytest
from PIL import Image

from inkypull.image import MEASURED_INKS


def png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def ink_stripes(size=(1600, 1200)) -> Image.Image:
    """Six vertical stripes, one per measured ink, as an RGB image."""
    img = Image.new("RGB", size, "white")
    w = size[0] // 6
    for i, ink in enumerate(MEASURED_INKS):
        img.paste(ink, (i * w, 0, (i + 1) * w, size[1]))
    return img


def gradient(size=(320, 240)) -> Image.Image:
    img = Image.new("RGB", size)
    img.putdata([(x % 256, y % 256, (x + y) % 256) for y in range(size[1]) for x in range(size[0])])
    return img


@pytest.fixture
def client():
    with httpx.Client() as c:
        yield c
