"""Build CBDT and CBLC tables. CBDT Format 17 (small metrics + PNG), CBLC index format 1."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Sequence

from png_filter import filter_png_chunks, get_png_size


@dataclass
class FontMetrics:
    upem: int
    ascent: int
    descent: int


def _div_round(a: float, b: float) -> int:
    return int(round(a / b))


def _small_glyph_metrics(
    width: int,
    height: int,
    ppem: int,
    font: FontMetrics,
    target: str = "linux",
) -> bytes:
    r"""SmallGlyphMetrics, big-endian. bearingY from height, clamped to int8.
    Windows uses a higher y_bearing so emojis align correctly (they sit lower otherwise ¯\_(ツ)_/¯)."""
    if target == "windows":
        y_bearing = min(height - 1, 127)
    else:
        y_bearing = min(_div_round(height * 5, 6), 127)
    advance = width
    return struct.pack(">BBbbB", height, width, 0, y_bearing, advance)


def build_cbdt(
    glyphs: Sequence[tuple[int, str, bytes]],
    ppem: int,
    font_metrics: FontMetrics,
    target: str = "linux",
) -> tuple[bytes, list[tuple[int, int, int]]]:
    """Build CBDT v3, Format 17. glyphs = (gid, name, png_bytes); returns bytes + (gid, offset, length) for CBLC."""
    out = bytearray(struct.pack(">HH", 3, 0))
    locations: list[tuple[int, int, int]] = []

    for gid, _name, png_data in glyphs:
        png_data = filter_png_chunks(png_data)
        size = get_png_size(png_data)
        if size is None:
            raise ValueError(f"Invalid PNG for glyph id {gid}")
        width, height = size

        metrics = _small_glyph_metrics(width, height, ppem, font_metrics, target)
        offset = len(out)
        out += metrics
        out += struct.pack(">I", len(png_data))
        out += png_data
        locations.append((gid, offset, len(metrics) + 4 + len(png_data)))

    return bytes(out), locations


def _sbit_line_metrics(
    ppem: int,
    font_metrics: FontMetrics,
    width_max: int,
) -> bytes:
    """SbitLineMetrics (12 bytes), big-endian. Spec: ascender, descender, widthMax, caretSlope*, caretOffset, minOriginSB, minAdvanceSB, maxBeforeBL, minAfterBL, pad1, pad2."""
    line_height = _div_round((font_metrics.ascent + font_metrics.descent) * ppem, font_metrics.upem)
    ascender = min(_div_round(font_metrics.ascent * ppem, font_metrics.upem), 127)
    descender = -(line_height - ascender)
    descender = max(-128, min(127, descender))
    # SbitLineMetrics: 12 bytes (ascender, descender, widthMax, 9×pad)
    return struct.pack(">bbB", ascender, descender, width_max) + struct.pack(">9b", 0, 0, 0, 0, 0, 0, 0, 0, 0)


# Target glyphs per index subtable (uses multiple subtables; some stacks expect this).
INDEX_SUBTABLE_GLYPH_CHUNK = 256


def _runs_from_locations(
    locations: list[tuple[int, int, int]],
) -> list[list[tuple[int, int, int]]]:
    """Split locations into runs: contiguous GID runs, then chunk large runs so we get multiple index subtables."""
    if not locations:
        return []
    sorted_locs = sorted(locations, key=lambda x: x[0])
    # First split at GID gaps
    contiguous: list[list[tuple[int, int, int]]] = []
    run: list[tuple[int, int, int]] = [sorted_locs[0]]
    for gid, offset, length in sorted_locs[1:]:
        if gid == run[-1][0] + 1:
            run.append((gid, offset, length))
        else:
            contiguous.append(run)
            run = [(gid, offset, length)]
    contiguous.append(run)
    # Then chunk any run larger than INDEX_SUBTABLE_GLYPH_CHUNK so we emit multiple subtables
    runs: list[list[tuple[int, int, int]]] = []
    for run in contiguous:
        for i in range(0, len(run), INDEX_SUBTABLE_GLYPH_CHUNK):
            runs.append(run[i : i + INDEX_SUBTABLE_GLYPH_CHUNK])
    return runs


def build_cblc(
    cbdt_bytes: bytes,
    glyphs: Sequence[tuple[int, str, bytes]],
    locations: list[tuple[int, int, int]],
    ppem: int,
    font_metrics: FontMetrics,
) -> bytes:
    """Build CBLC v3, one strike, index format 1. Multiple index subtables per contiguous GID run."""
    if not glyphs or not locations:
        raise ValueError("No glyphs for CBLC")

    width_max = 0
    for _gid, _name, png_data in glyphs:
        size = get_png_size(filter_png_chunks(png_data))
        if size:
            width_max = max(width_max, size[0])

    runs = _runs_from_locations(locations)
    first_gid = min(loc[0] for loc in locations)
    last_gid = max(loc[0] for loc in locations)
    color_ref = 0
    hori = _sbit_line_metrics(ppem, font_metrics, width_max)
    vert = hori

    # Build one IndexSubTableFormat1 per run; then the IndexSubtableList (array + subtables).
    record_size = 8
    index_subtable_list = bytearray()
    subtable_chunks: list[bytes] = []

    for run in runs:
        run_first = run[0][0]
        run_last = run[-1][0]
        base_offset = run[0][1]
        run_end = run[-1][1] + run[-1][2]
        # Offsets relative to base_offset: one per GID in range + sentinel
        offsets_rel = [run[i][1] - base_offset for i in range(len(run))]
        offsets_rel.append(run_end - base_offset)

        subtable_data = bytearray()
        subtable_data += struct.pack(">HHL", 1, 17, base_offset)
        for o in offsets_rel:
            subtable_data += struct.pack(">I", o)
        subtable_chunks.append(bytes(subtable_data))

    # IndexSubtableList: array of (firstGlyphIndex, lastGlyphIndex, indexSubtableOffset)
    # offset is from start of IndexSubtableList
    array_size = len(runs) * record_size
    offset_into_list = array_size
    for i, run in enumerate(runs):
        index_subtable_list += struct.pack(
            ">HHL", run[0][0], run[-1][0], offset_into_list
        )
        offset_into_list += len(subtable_chunks[i])
    for chunk in subtable_chunks:
        index_subtable_list += chunk

    number_of_index_subtables = len(runs)
    index_tables_size = len(index_subtable_list)
    header_size = 8
    bitmap_size_record_size = 48
    index_subtable_list_offset = header_size + bitmap_size_record_size

    bitmap_size = bytearray()
    bitmap_size += struct.pack(
        ">IIII",
        index_subtable_list_offset,
        index_tables_size,
        number_of_index_subtables,
        color_ref,
    )
    bitmap_size += hori
    bitmap_size += vert
    bitmap_size += struct.pack(">HHBBBb", first_gid, last_gid, ppem, ppem, 32, 1)

    out = bytearray()
    out += struct.pack(">HHI", 3, 0, 1)
    out += bitmap_size
    out += index_subtable_list

    return bytes(out)
