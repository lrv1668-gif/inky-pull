"""Screens inkypull draws itself: welcome, can't-reach, status.

Drawn in greyscale and thresholded to pure black and white, so they reach the panel as
ink-only images and never get dithered.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from .image import to_ink_palette


def welcome(size: tuple[int, int], url: str) -> Image.Image:
    return _screen(size, "inkypull", ["Waiting for the first image from", url])


def unreachable(size: tuple[int, int], url: str, error: str) -> Image.Image:
    return _screen(
        size, "Can't reach the image", [url, "", error, "", "Retrying in the background."]
    )


def status(size: tuple[int, int], lines: dict[str, str]) -> Image.Image:
    return _screen(size, "Status", [f"{k}: {v}" for k, v in lines.items()])


def _screen(size: tuple[int, int], title: str, lines: list[str]) -> Image.Image:
    width, height = size
    unit = min(width, height) // 30
    margin = unit * 3
    canvas = Image.new("L", size, 255)
    draw = ImageDraw.Draw(canvas)
    title_font = _font(unit * 2)
    body_font = _font(unit)

    y = margin
    draw.text((margin, y), title, font=title_font, fill=0)
    y += unit * 3
    draw.rectangle((margin, y, width - margin, y + max(2, unit // 6)), fill=0)
    y += unit * 2
    for line in lines:
        for wrapped in _wrap(draw, line, body_font, width - 2 * margin):
            draw.text((margin, y), wrapped, font=body_font, fill=0)
            y += int(unit * 1.5)
        if y > height - margin:
            break

    return to_ink_palette(canvas.point(lambda v: 0 if v < 128 else 255).convert("RGB"))


def _font(px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default(size=px)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    if not text:
        return [""]
    lines, current = [], ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines
