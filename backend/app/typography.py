"""Scene-title layout.

Measures the font file FFmpeg will draw, wraps on spaces, and keeps every
line inside the side margin. The renderer turns the result into drawtext
and does not invent its own sizes.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# Eight percent of the frame on each side, on every export ratio.
SIDE_MARGIN = 0.08
# The lowest line ends here. Wrapped lines grow upward from this edge.
BOTTOM = 0.90
MAX_LINES = 2
TITLE_LINE_SPACING = 8
SUBTITLE_LINE_SPACING = 6


@dataclass(frozen=True)
class TextBlock:
    lines: tuple[str, ...]
    fontsize: int
    line_spacing: int
    y: str

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def max_line_width(frame_width: int) -> float:
    return frame_width * (1.0 - 2.0 * SIDE_MARGIN)


def preferred_title_size(frame_width: int) -> int:
    return max(30, round(frame_width / 30))


def preferred_subtitle_size(frame_width: int) -> int:
    return max(18, round(frame_width / 52))


def _minimum_title_size(frame_width: int) -> int:
    return max(20, round(frame_width / 54))


def _minimum_subtitle_size(frame_width: int) -> int:
    return max(14, round(frame_width / 72))


@lru_cache(maxsize=8)
def _font(path: str) -> dict:
    data = Path(path).read_bytes()
    if len(data) < 12 or data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"):
        raise ValueError(f"not a truetype font: {path}")
    count = struct.unpack_from(">H", data, 4)[0]
    tables: dict[str, int] = {}
    pos = 12
    for _ in range(count):
        tag, _checksum, offset, _length = struct.unpack_from(">4sIII", data, pos)
        tables[tag.decode("latin1")] = offset
        pos += 16
    if "head" not in tables or "hhea" not in tables or "hmtx" not in tables or "cmap" not in tables:
        raise ValueError(f"font is missing measurement tables: {path}")
    units = struct.unpack_from(">H", data, tables["head"] + 18)[0]
    n_metrics = struct.unpack_from(">H", data, tables["hhea"] + 34)[0]
    return {
        "data": data,
        "units": units or 1000,
        "n_metrics": n_metrics,
        "hmtx": tables["hmtx"],
        "cmap": _cmap(data, tables["cmap"]),
    }


def _cmap(data: bytes, offset: int):
    _version, n_tables = struct.unpack_from(">HH", data, offset)
    chosen: tuple[int, int, int] | None = None
    for index in range(n_tables):
        platform, encoding, relative = struct.unpack_from(">HHI", data, offset + 4 + index * 8)
        sub = offset + relative
        fmt = struct.unpack_from(">H", data, sub)[0]
        rank = 0
        if fmt == 12:
            rank = 3
        elif fmt == 4 and (platform, encoding) in ((3, 1), (0, 3)):
            rank = 2
        elif fmt == 4:
            rank = 1
        if rank and (chosen is None or rank > chosen[0]):
            chosen = (rank, fmt, sub)
    if chosen is None:
        return lambda _code: 0
    _rank, fmt, sub = chosen
    if fmt == 12:
        return _format12(data, sub)
    return _format4(data, sub)


def _format12(data: bytes, sub: int):
    groups = struct.unpack_from(">I", data, sub + 12)[0]
    spans: list[tuple[int, int, int]] = []
    pos = sub + 16
    for _ in range(groups):
        start, end, glyph = struct.unpack_from(">III", data, pos)
        spans.append((start, end, glyph))
        pos += 12

    def lookup(code: int) -> int:
        for start, end, glyph in spans:
            if start <= code <= end:
                return glyph + (code - start)
        return 0

    return lookup


def _format4(data: bytes, sub: int):
    seg_count = struct.unpack_from(">H", data, sub + 6)[0] // 2
    end_at = sub + 14
    ends = struct.unpack_from(f">{seg_count}H", data, end_at)
    start_at = end_at + seg_count * 2 + 2
    starts = struct.unpack_from(f">{seg_count}H", data, start_at)
    delta_at = start_at + seg_count * 2
    deltas = struct.unpack_from(f">{seg_count}h", data, delta_at)
    offset_at = delta_at + seg_count * 2
    offsets = struct.unpack_from(f">{seg_count}H", data, offset_at)

    def lookup(code: int) -> int:
        for index, end in enumerate(ends):
            if code > end:
                continue
            if code < starts[index]:
                return 0
            if offsets[index] == 0:
                return (code + deltas[index]) & 0xFFFF
            pos = offset_at + index * 2 + offsets[index] + (code - starts[index]) * 2
            glyph = struct.unpack_from(">H", data, pos)[0]
            if glyph == 0:
                return 0
            return (glyph + deltas[index]) & 0xFFFF
        return 0

    return lookup


def _advance(font: dict, glyph: int) -> int:
    data = font["data"]
    if glyph < font["n_metrics"]:
        return struct.unpack_from(">H", data, font["hmtx"] + glyph * 4)[0]
    last = font["n_metrics"] - 1
    if last < 0:
        return 0
    return struct.unpack_from(">H", data, font["hmtx"] + last * 4)[0]


def text_width(font_path: str, text: str, fontsize: int) -> float:
    """Pixel width of `text` at `fontsize`, from the font's advance table."""
    font = _font(font_path)
    total = 0
    for char in text:
        total += _advance(font, font["cmap"](ord(char)))
    return total * fontsize / font["units"]


def wrap_words(text: str, measure, max_width: float) -> tuple[str, ...]:
    """Break on spaces only. A single word that does not fit stays whole."""
    words = text.split()
    if not words:
        return ()
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
            continue
        trial = f"{current} {word}"
        if measure(trial) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return tuple(lines)


def fit_block(
    text: str, font_path: str, max_width: float, start: int, minimum: int,
) -> tuple[tuple[str, ...], int]:
    """Shrink until the words fit on two lines inside `max_width`."""
    words = " ".join(text.split())
    size = max(start, minimum)
    while size >= minimum:
        measure = lambda sample, px=size: text_width(font_path, sample, px)
        lines = wrap_words(words, measure, max_width)
        widest = max((measure(line) for line in lines), default=0.0)
        if len(lines) <= MAX_LINES and widest <= max_width + 0.51:
            return lines, size
        size -= 1
    measure = lambda sample: text_width(font_path, sample, minimum)
    return wrap_words(words, measure, max_width), minimum


def block_height(fontsize: int, line_count: int, line_spacing: int) -> int:
    """Room reserved under the title so it cannot sit on the subtitle."""
    if line_count <= 0:
        return 0
    return int(round(fontsize * 1.25 * line_count + line_spacing * max(0, line_count - 1)))


def _anchor(reserve_below: int) -> str:
    if reserve_below <= 0:
        return f"h*{BOTTOM:.3f}-text_h"
    return f"h*{BOTTOM:.3f}-text_h-{reserve_below}"


def compose(
    title: str | None,
    subtitle: str | None,
    title_font: str | None,
    subtitle_font: str | None,
    width: int,
    height: int,
) -> tuple[TextBlock | None, TextBlock | None]:
    """Title and subtitle blocks for one frame size.

    The subtitle sits on the bottom anchor. The title sits above it, and a
    second line of either block moves up rather than down into the margin.
    """
    limit = max_line_width(width)
    gap = max(12, round(height * 0.012))
    subtitle_block = None
    if subtitle and subtitle.strip() and subtitle_font:
        lines, size = fit_block(
            subtitle, subtitle_font, limit,
            preferred_subtitle_size(width), _minimum_subtitle_size(width),
        )
        subtitle_block = TextBlock(lines, size, SUBTITLE_LINE_SPACING, _anchor(0))
    reserve = 0
    if subtitle_block is not None:
        reserve = block_height(
            subtitle_block.fontsize, len(subtitle_block.lines), subtitle_block.line_spacing,
        ) + gap
    title_block = None
    if title and title.strip() and title_font:
        lines, size = fit_block(
            title, title_font, limit,
            preferred_title_size(width), _minimum_title_size(width),
        )
        title_block = TextBlock(lines, size, TITLE_LINE_SPACING, _anchor(reserve))
    return title_block, subtitle_block
