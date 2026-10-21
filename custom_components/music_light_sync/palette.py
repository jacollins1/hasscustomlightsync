"""Palette helpers: turn a few source colors into one vivid color per bulb.

Pure Python (Pillow only for album-art extraction, imported lazily) so the
logic can be unit-tested without Home Assistant.
"""

from __future__ import annotations

import colorsys
import io
from collections.abc import Iterable

RGB = tuple[int, int, int]

# Colors darker / less saturated than this carry no hue worth showing on a bulb.
_MIN_VALUE = 0.12
_MIN_SAT = 0.18
# Hues closer than this (0..1 wheel) count as the same color.
_HUE_DEDUP = 0.06
# Minimum spacing between colors in the scheme, so every entry reads as distinct.
_SCHEME_SPACING = 0.07
# Album art rarely spans the wheel, so the scheme adds color-wheel harmonies of
# the cover's dominant hue: complement, triad, split complements, then the gaps.
_HARMONIES = (0.5, 1 / 3, -1 / 3, 0.5 + 0.09, 0.5 - 0.09, 1 / 6, -1 / 6, 0.25, -0.25, 0.09, -0.09, 0.42, -0.42)

SCHEME_SIZE = 8

# Full saturation around the wheel (no pastels: they read as white on a bulb).
FALLBACK_PALETTE: tuple[RGB, ...] = (
    (255, 0, 40),
    (0, 120, 255),
    (255, 170, 0),
    (140, 0, 255),
    (0, 255, 90),
    (255, 0, 200),
    (0, 230, 255),
    (255, 80, 0),
)


def _to_hsv(rgb: RGB) -> tuple[float, float, float]:
    return colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)


def _to_rgb(h: float, s: float, v: float) -> RGB:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return (round(r * 255), round(g * 255), round(b * 255))


def hue_of(rgb: RGB) -> float:
    """Hue on a 0..1 wheel."""
    return _to_hsv(rgb)[0]


def hue_distance(a: float, b: float) -> float:
    """Shortest distance between two hues on a 0..1 wheel."""
    d = abs(a - b) % 1.0
    return min(d, 1.0 - d)


def build_palette(sources: Iterable[RGB | None], count: int = SCHEME_SIZE) -> list[RGB]:
    """Return ``count`` fully saturated colors anchored on ``sources`` (album art).

    The cover's own hues come first, in order of importance; then harmonies of
    the dominant one (complement, triad, split complements...) so the scheme
    spans the color wheel instead of staying in the cover's corner of it.
    Greys, near-black and near-white sources are ignored.
    """
    count = max(1, count)
    anchors: list[float] = []
    for rgb in sources:
        if rgb is None:
            continue
        h, s, v = _to_hsv(rgb)
        if v < _MIN_VALUE or s < _MIN_SAT:
            continue
        if any(hue_distance(h, existing) < _HUE_DEDUP for existing in anchors):
            continue
        anchors.append(h)

    if not anchors:
        return [FALLBACK_PALETTE[i % len(FALLBACK_PALETTE)] for i in range(count)]

    base = anchors[0]
    hues: list[float] = []
    for candidate in [*anchors, *((base + offset) % 1.0 for offset in _HARMONIES)]:
        if all(hue_distance(candidate, h) >= _SCHEME_SPACING for h in hues):
            hues.append(candidate)
        if len(hues) == count:
            break
    vivid = [_to_rgb(h, 1.0, 1.0) for h in hues]
    return [vivid[i % len(vivid)] for i in range(count)]


def palette_from_image(data: bytes, count: int = SCHEME_SIZE, *, max_colors: int = 8) -> list[RGB]:
    """Extract a bulb palette from encoded image bytes (album art).

    Colors are ranked by how much of the image they cover, weighted by
    saturation so a small vivid accent beats a large grey background.
    """
    from PIL import Image  # noqa: PLC0415 - heavy import, only needed here

    with Image.open(io.BytesIO(data)) as img:
        small = img.convert("RGB").resize((64, 64))
    quant = small.quantize(colors=max_colors, method=Image.Quantize.MEDIANCUT)
    raw_palette = quant.getpalette() or []
    counts = quant.getcolors() or []
    ranked: list[tuple[float, RGB]] = []
    for pixels, index in counts:
        rgb = (raw_palette[index * 3], raw_palette[index * 3 + 1], raw_palette[index * 3 + 2])
        _, s, v = _to_hsv(rgb)
        ranked.append((pixels * (0.25 + s) * (0.25 + v), rgb))
    ranked.sort(reverse=True)
    return build_palette((rgb for _, rgb in ranked), count)
