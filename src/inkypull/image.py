"""Turn a fetched image into something the panel can show.

Two paths:

* An image that already uses only the panel's inks (at most six colours, each close to
  one ink — e.g. a frame a server has dithered itself) is mapped to inky's pure palette
  so `set_image` passes it through untouched (`Dither.NONE`).
* Anything else is returned as RGB, and inky Floyd–Steinberg-dithers it.
"""

from __future__ import annotations

from PIL import Image, ImageColor, ImageOps

# Spectra 6 inks in inky's colour order: black, white, yellow, red, blue, green.
# Measured on a real panel and white-balanced so paper white is #fff.
MEASURED_INKS = [
    (0x00, 0x00, 0x00),
    (0xFF, 0xFF, 0xFF),
    (0xFF, 0xFF, 0x6E),
    (0xF1, 0x6F, 0x74),
    (0x5E, 0x5B, 0x91),
    (0x5A, 0x8D, 0x6C),
]

# What inky's drivers quantize palette images against (their DESATURATED_PALETTE).
PURE_INKS = [
    (0, 0, 0),
    (255, 255, 255),
    (255, 255, 0),
    (255, 0, 0),
    (0, 0, 255),
    (0, 255, 0),
]

# inky's SATURATED_PALETTE: what the panel really looks like at saturation 1.
SATURATED_INKS = [
    (0, 0, 0),
    (161, 164, 165),
    (208, 190, 71),
    (156, 72, 75),
    (61, 59, 94),
    (58, 91, 70),
]

# A colour this close (Euclidean RGB) to an ink counts as that ink.
INK_TOLERANCE = 24


def prepare(
    image: Image.Image,
    size: tuple[int, int],
    *,
    fit: str = "contain",
    background: str = "white",
    rotate: int | str = 0,
    portrait_rotate: int = 90,
) -> Image.Image:
    """Rotate and fit `image` to `size`; return an ink-palette image or RGB."""
    image = ImageOps.exif_transpose(image)
    image = _rotate(image, size, rotate, portrait_rotate)
    rgb = image.convert("RGB")
    inky = _ink_map(rgb) is not None
    resample = Image.Resampling.NEAREST if inky else Image.Resampling.LANCZOS
    fitted = _fit(rgb, size, fit, ImageColor.getrgb(background), resample)
    return to_ink_palette(fitted) or fitted


def to_ink_palette(image: Image.Image) -> Image.Image | None:
    """Map an image that only uses ink colours to inky's pure palette, else None."""
    rgb = image.convert("RGB")
    mapping = _ink_map(rgb)
    if mapping is None:
        return None
    # Build a palette of the colours actually used, so each maps to exactly one index.
    used = list(mapping)
    lookup = Image.new("P", (1, 1))
    lookup.putpalette([c for colour in used for c in colour])
    indexed = rgb.quantize(palette=lookup, dither=Image.Dither.NONE)
    lut = bytes(mapping[used[i]] if i < len(used) else 0 for i in range(256))
    out = Image.frombytes("P", rgb.size, indexed.tobytes().translate(lut))
    out.putpalette([c for colour in PURE_INKS for c in colour])
    return out


def preview(image: Image.Image, saturation: float = 0.5) -> Image.Image:
    """Approximate what the panel will look like, for writing to a PNG."""
    if image.mode == "P" and _ink_map(image.convert("RGB")) is not None:
        out = image.copy()
        out.putpalette([c for colour in MEASURED_INKS for c in colour])
        return out.convert("RGB")
    palette = [
        round(s * saturation + d * (1 - saturation))
        for sat, des in zip(SATURATED_INKS, PURE_INKS, strict=True)
        for s, d in zip(sat, des, strict=True)
    ]
    lookup = Image.new("P", (1, 1))
    lookup.putpalette(palette)
    dithered = image.convert("RGB").quantize(palette=lookup, dither=Image.Dither.FLOYDSTEINBERG)
    dithered.putpalette([c for colour in MEASURED_INKS for c in colour])
    return dithered.convert("RGB")


def _ink_map(rgb: Image.Image) -> dict[tuple[int, int, int], int] | None:
    """{colour: ink index} if every colour in `rgb` is an ink, else None."""
    colours = rgb.getcolors(maxcolors=len(MEASURED_INKS))
    if colours is None:
        return None
    mapping = {}
    for _, colour in colours:
        index = _nearest_ink(colour)
        if index is None:
            return None
        mapping[colour] = index
    return mapping


def _nearest_ink(colour: tuple[int, int, int]) -> int | None:
    best, best_dist = None, INK_TOLERANCE**2 + 1
    for i, inks in enumerate(zip(MEASURED_INKS, PURE_INKS, strict=True)):
        for ink in inks:
            dist = sum((a - b) ** 2 for a, b in zip(colour, ink, strict=True))
            if dist < best_dist:
                best, best_dist = i, dist
    return best


def _rotate(image: Image.Image, size: tuple[int, int], rotate: int | str, portrait: int):
    if rotate == "auto":
        panel_landscape = size[0] >= size[1]
        image_landscape = image.width >= image.height
        rotate = 0 if panel_landscape == image_landscape else portrait
    # Config rotations are clockwise; PIL's transposes are counter-clockwise.
    transpose = {
        90: Image.Transpose.ROTATE_270,
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_90,
    }.get(rotate)
    return image.transpose(transpose) if transpose is not None else image


def _fit(image, size, fit, background, resample):
    if image.size == size:
        return image
    if fit == "stretch":
        return image.resize(size, resample)
    if fit == "cover":
        return ImageOps.fit(image, size, resample)
    return ImageOps.pad(image, size, resample, color=background)
