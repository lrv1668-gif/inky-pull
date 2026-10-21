from conftest import gradient, ink_stripes
from PIL import Image

from inkypull.image import PURE_INKS, prepare, preview, to_ink_palette

PANEL = (1600, 1200)


def _inky_skips_dither(image: Image.Image) -> bool:
    """The condition inky's Spectra 6 `set_image` uses to choose Dither.NONE."""
    return image.mode == "P" and len(image.palette.colors) == 6


def test_to_ink_palette_maps_measured_inks_to_their_indices():
    out = to_ink_palette(ink_stripes())
    assert out is not None
    assert [out.getpixel((i * 266 + 10, 10)) for i in range(6)] == [0, 1, 2, 3, 4, 5]
    assert _inky_skips_dither(out)
    assert out.getpalette()[:18] == [c for ink in PURE_INKS for c in ink]


def test_to_ink_palette_accepts_pure_inks_and_palette_mode():
    img = Image.new("P", (10, 10))
    img.putpalette([c for ink in PURE_INKS for c in ink])
    img.paste(4, (0, 0, 5, 10))
    out = to_ink_palette(img)
    assert out.getpixel((0, 0)) == 4 and out.getpixel((9, 0)) == 0


def test_to_ink_palette_rejects_photos_and_off_palette_colours():
    assert to_ink_palette(gradient()) is None
    assert to_ink_palette(Image.new("RGB", (4, 4), (128, 128, 128))) is None


def test_prepare_passes_ink_images_through_at_panel_size():
    out = prepare(ink_stripes((800, 600)), PANEL)
    assert out.size == PANEL
    assert _inky_skips_dither(out)


def test_prepare_returns_rgb_for_photos():
    out = prepare(gradient(), PANEL)
    assert (out.mode, out.size) == ("RGB", PANEL)


def test_prepare_contain_pads_cover_crops():
    wide = gradient((400, 100))
    assert prepare(wide, PANEL, fit="contain").getpixel((800, 5)) == (255, 255, 255)
    assert prepare(wide, PANEL, fit="cover").getpixel((800, 5)) != (255, 255, 255)


def test_prepare_auto_rotates_portrait_clockwise():
    portrait = Image.new("RGB", (1200, 1600), "white")
    portrait.paste((0, 0, 0), (0, 0, 1200, 100))  # black band along the top
    out = prepare(portrait, PANEL, rotate="auto").convert("RGB")
    assert out.size == PANEL
    # Clockwise: the top edge ends up on the right.
    assert out.getpixel((1599, 600)) == (0, 0, 0)
    assert out.getpixel((0, 600)) == (255, 255, 255)


def test_prepare_explicit_rotation():
    img = Image.new("RGB", PANEL, "white")
    img.paste((0, 0, 0), (0, 0, 100, 1200))  # black band on the left
    out = prepare(img, PANEL, rotate=180).convert("RGB")
    assert out.getpixel((1599, 600)) == (0, 0, 0)


def test_preview_renders_both_paths_as_rgb():
    assert preview(prepare(ink_stripes(), PANEL)).mode == "RGB"
    assert preview(prepare(gradient(), PANEL)).size == PANEL
