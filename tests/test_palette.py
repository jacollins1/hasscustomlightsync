"""Palette building."""

from __future__ import annotations

import colorsys
import io
import itertools

from PIL import Image

from custom_components.music_light_sync.palette import (
    FALLBACK_PALETTE,
    build_palette,
    palette_from_image,
)


def hue(rgb: tuple[int, int, int]) -> float:
    return colorsys.rgb_to_hsv(*(c / 255 for c in rgb))[0]


def test_greys_and_blacks_fall_back() -> None:
    assert build_palette([(0, 0, 0), (128, 128, 128), (250, 250, 250), None], 5) == list(FALLBACK_PALETTE[:5])


def test_red_cover_still_spans_the_wheel() -> None:
    """Jon's complaint: a red/pink cover gave only reds, pinks, purples and whites."""
    pal = build_palette([(220, 30, 60), (200, 40, 120), (240, 220, 225)])
    assert len(pal) == 8 and len(set(pal)) == 8
    hues = sorted(hue(c) for c in pal)
    gaps = [b - a for a, b in itertools.pairwise(hues)] + [1 - hues[-1] + hues[0]]
    assert max(gaps) < 0.25  # no quarter of the wheel left unused
    assert any(abs(hue(c) - 0.5) < 0.12 for c in pal)  # cyan/teal, the complement, is in
    assert all(colorsys.rgb_to_hsv(*(x / 255 for x in c))[1] > 0.99 for c in pal)  # no pastels


def test_monochrome_cover_gets_analogous_neighbours() -> None:
    pal = build_palette([(30, 60, 200)], 5)
    assert len(pal) == 5
    assert len({round(hue(c), 2) for c in pal}) >= 3


def test_vivid_and_deduplicated() -> None:
    pal = build_palette([(200, 30, 30), (210, 40, 35), (30, 200, 30)], 2)
    assert len(set(pal)) == 2
    for color in pal:
        assert colorsys.rgb_to_hsv(*(c / 255 for c in color))[1] >= 0.65


def test_album_art_extraction() -> None:
    img = Image.new("RGB", (100, 100), (20, 20, 20))
    for x in range(40):
        for y in range(100):
            img.putpixel((x, y), (220, 40, 60))
    for x in range(40, 55):
        for y in range(100):
            img.putpixel((x, y), (30, 180, 220))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    pal = palette_from_image(buf.getvalue(), 5)
    hues = {round(hue(c), 1) for c in pal}
    assert round(hue((220, 40, 60)), 1) in hues
    assert round(hue((30, 180, 220)), 1) in hues
