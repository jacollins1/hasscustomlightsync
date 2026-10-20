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
# Bulbs look washed out below this saturation; lift everything to at least it.
_TARGET_MIN_SAT = 0.65
# Spread used to invent neighbours when the source has too few distinct hues.
_ANALOGOUS_STEP = 0.07

FALLBACK_PALETTE: tuple[RGB, ...] = (
    (255, 64, 32),
    (255, 0, 128),
    (128, 0, 255),
    (0, 96, 255),
    (0, 200, 160),
)


def _to_hsv(rgb: RGB) -> tuple[float, float, float]:
    return colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)


def _to_rgb(h: float, s: float, v: float) -> RGB:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return (round(r * 255), round(g * 255), round(b * 255))


def _hue_distance(a: float, b: float) -> float:
    d = abs(a - b) % 1.0
    return min(d, 1.0 - d)


def build_palette(sources: Iterable[RGB | None], count: int) -> list[RGB]:
    """Return ``count`` vivid colors derived from ``sources`` (most important first).

    Near-black, near-white and grey sources are dropped, near-duplicate hues are
    merged, saturation is lifted so bulbs do not look pastel, and analogous hues
    are invented around the strongest color when the source is monochrome.
    """
    count = max(1, count)
    hues: list[tuple[float, float]] = []  # (hue, saturation)
    for rgb in sources:
        if rgb is None:
            continue
        h, s, v = _to_hsv(rgb)
        if v < _MIN_VALUE or s < _MIN_SAT:
            continue
        if any(_hue_distance(h, existing) < _HUE_DEDUP for existing, _ in hues):
            continue
        hues.append((h, s))

    if not hues:
        return [FALLBACK_PALETTE[i % len(FALLBACK_PALETTE)] for i in range(count)]

    # Invent analogous neighbours around the dominant hue until we have enough
    # distinct hues to give adjacent bulbs visibly different colors.
    base_h, base_s = hues[0]
    step = 1
    while len(hues) < min(count, 3):
        sign = 1 if step % 2 else -1
        candidate = (base_h + sign * _ANALOGOUS_STEP * ((step + 1) // 2)) % 1.0
        if all(_hue_distance(candidate, h) >= _HUE_DEDUP * 0.8 for h, _ in hues):
            hues.append((candidate, base_s))
        step += 1
        if step > 12:
            break

    vivid = [_to_rgb(h, max(s, _TARGET_MIN_SAT), 1.0) for h, s in hues]
    return [vivid[i % len(vivid)] for i in range(count)]


def palette_from_image(data: bytes, count: int, *, max_colors: int = 8) -> list[RGB]:
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
