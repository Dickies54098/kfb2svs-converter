#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch convert KFB to QuPath-compatible SVS.

KFbioConverter writes an Aperio-like TIFF, but its baseline IFD normally has
NewSubfileType=2 and its reduced-resolution IFDs omit that tag.  Bio-Formats
(used by QuPath) can interpret both the non-zero and missing values as possible
label/macro images, splitting valid pyramid levels into separate series.

This program fixes that metadata error by writing an explicit
NewSubfileType=0 on every retained pyramid IFD and excludes non-pyramid IFDs
from the reachable TIFF directory chain.  It uses only Python's standard
library and supports both Classic TIFF and BigTIFF.

The original input is never edited by the normal commands.  A new SVS is
written into the requested output folder.
"""

from __future__ import annotations

import argparse
import csv
import errno
import os
import shutil
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable
from uuid import uuid4


APERIO_PREFIX = b"Aperio Image"
DEFAULT_MIN_PYRAMID_MAX_DIM = 0
# Generic placeholder paths so the script is usable out of the box without
# leaking any user-specific directory. Replace these with your own absolute
# paths when running directly, or pass --input-dir / --output-dir on the CLI.
DEFAULT_INPUT_DIR = Path(r"C:\path\to\your\kfb_input")
SCRIPT_DIR = Path(__file__).resolve().parent
BUNDLED_CONVERTER = SCRIPT_DIR / "converter" / "x86" / "KFbioConverter.exe"
# If the bundled converter is missing, fall back to a generic placeholder so
# the module still imports cleanly; the CLI will surface a clear error.
DEFAULT_CONVERTER = BUNDLED_CONVERTER if BUNDLED_CONVERTER.is_file() else (
    Path(r"C:\path\to\KFbioConverter.exe")
)

# TIFF field type -> byte width (TIFF 6.0 + BigTIFF's LONG8/SLONG8/IFD8).
TYPE_SIZES = {
    1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4,
    10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8,
}
TYPE_FORMATS = {
    1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i",
    13: "I", 16: "Q", 17: "q", 18: "Q",
}

TAG_NEW_SUBFILE_TYPE = 254
TAG_IMAGE_WIDTH = 256
TAG_IMAGE_HEIGHT = 257
TAG_IMAGE_DESCRIPTION = 270
TAG_STRIP_OFFSETS = 273
TAG_STRIP_BYTE_COUNTS = 279
TAG_TILE_OFFSETS = 324
TAG_TILE_BYTE_COUNTS = 325


@dataclass(frozen=True)
class Layout:
    endian: str
    bigtiff: bool
    first_ifd_offset: int
    header_first_ifd_pointer_offset: int

    @property
    def count_size(self) -> int:
        return 8 if self.bigtiff else 2

    @property
    def entry_size(self) -> int:
        return 20 if self.bigtiff else 12

    @property
    def offset_size(self) -> int:
        return 8 if self.bigtiff else 4

    @property
    def entry_format(self) -> str:
        return self.endian + ("HHQQ" if self.bigtiff else "HHII")

    @property
    def offset_format(self) -> str:
        return self.endian + ("Q" if self.bigtiff else "I")


@dataclass(frozen=True)
class Tag:
    tag: int
    field_type: int
    count: int
    value_or_offset: int
    entry_offset: int

    def byte_count(self) -> int | None:
        size = TYPE_SIZES.get(self.field_type)
        return None if size is None else size * self.count


@dataclass
class Ifd:
    offset: int
    entries: list[Tag]
    next_pointer_offset: int
    next_offset: int


@dataclass
class CleanupResult:
    source: Path
    destination: Path
    kept_ifds: int
    removed_ifds: int
    removed_associated_ifds: int
    removed_small_pyramid_ifds: int
    original_bytes: int
    output_bytes: int
    physically_truncated: bool
    baseline_subfile_type_before: int | None
    baseline_subfile_type_after: int | None


def _read_exact(f: BinaryIO, count: int, label: str) -> bytes:
    data = f.read(count)
    if len(data) != count:
        raise ValueError(f"Unexpected end of file while reading {label} ({len(data)}/{count} bytes)")
    return data


def _unpack_from_file(f: BinaryIO, fmt: str, label: str) -> tuple:
    return struct.unpack(fmt, _read_exact(f, struct.calcsize(fmt), label))


def read_layout(f: BinaryIO) -> Layout:
    f.seek(0)
    order = _read_exact(f, 2, "byte order")
    if order == b"II":
        endian = "<"
    elif order == b"MM":
        endian = ">"
    else:
        raise ValueError("Not a TIFF/BigTIFF file: invalid byte order")

    magic = _unpack_from_file(f, endian + "H", "TIFF magic")[0]
    if magic == 42:
        first = _unpack_from_file(f, endian + "I", "first IFD offset")[0]
        return Layout(endian, False, first, 4)
    if magic == 43:
        offset_size, reserved, first = _unpack_from_file(f, endian + "HHQ", "BigTIFF header")
        if offset_size != 8 or reserved != 0:
            raise ValueError("Unsupported BigTIFF header")
        return Layout(endian, True, first, 8)
    raise ValueError(f"Not a TIFF/BigTIFF file: magic={magic}")


def read_ifd(f: BinaryIO, layout: Layout, offset: int) -> Ifd:
    if offset <= 0:
        raise ValueError("Invalid IFD offset")
    f.seek(offset)
    count_fmt = layout.endian + ("Q" if layout.bigtiff else "H")
    entry_count = _unpack_from_file(f, count_fmt, "IFD entry count")[0]
    # A valid WSI never needs millions of IFD fields; this also avoids damage
    # causing the reader to allocate an unreasonable amount of memory.
    if entry_count > 10000:
        raise ValueError(f"Implausible IFD entry count {entry_count} at offset {offset}")
    entries: list[Tag] = []
    for index in range(entry_count):
        entry_offset = offset + layout.count_size + index * layout.entry_size
        tag, field_type, count, value = _unpack_from_file(f, layout.entry_format, "IFD entry")
        entries.append(Tag(tag, field_type, count, value, entry_offset))
    next_pointer_offset = offset + layout.count_size + entry_count * layout.entry_size
    next_offset = _unpack_from_file(f, layout.offset_format, "next IFD offset")[0]
    return Ifd(offset, entries, next_pointer_offset, next_offset)


def read_ifd_chain(f: BinaryIO, layout: Layout) -> list[Ifd]:
    pages: list[Ifd] = []
    visited: set[int] = set()
    offset = layout.first_ifd_offset
    while offset:
        if offset in visited:
            raise ValueError(f"Circular IFD chain at offset {offset}")
        visited.add(offset)
        page = read_ifd(f, layout, offset)
        pages.append(page)
        offset = page.next_offset
        if len(pages) > 10000:
            raise ValueError("Too many TIFF IFDs")
    if not pages:
        raise ValueError("TIFF contains no IFDs")
    return pages


def find_tag(page: Ifd, tag_id: int) -> Tag | None:
    return next((tag for tag in page.entries if tag.tag == tag_id), None)


def read_tag_bytes(f: BinaryIO, layout: Layout, tag: Tag) -> bytes | None:
    byte_count = tag.byte_count()
    if byte_count is None:
        return None
    if byte_count <= layout.offset_size:
        return struct.pack(layout.offset_format, tag.value_or_offset)[:byte_count]
    f.seek(tag.value_or_offset)
    return _read_exact(f, byte_count, f"tag {tag.tag} data")


def read_tag_values(f: BinaryIO, layout: Layout, page: Ifd, tag_id: int) -> list[int] | None:
    tag = find_tag(page, tag_id)
    if tag is None:
        return None
    fmt = TYPE_FORMATS.get(tag.field_type)
    if fmt is None:
        return None
    raw = read_tag_bytes(f, layout, tag)
    if raw is None:
        return None
    expected = struct.calcsize(layout.endian + fmt) * tag.count
    if len(raw) != expected:
        return None
    return list(struct.unpack(layout.endian + fmt * tag.count, raw))


def read_tag_scalar(f: BinaryIO, layout: Layout, page: Ifd, tag_id: int) -> int | None:
    values = read_tag_values(f, layout, page, tag_id)
    return values[0] if values else None


def read_description(f: BinaryIO, layout: Layout, page: Ifd) -> bytes:
    tag = find_tag(page, TAG_IMAGE_DESCRIPTION)
    if tag is None or tag.field_type != 2:
        return b""
    raw = read_tag_bytes(f, layout, tag)
    return b"" if raw is None else raw.split(b"\x00", 1)[0]


def is_aperio_pyramid_page(f: BinaryIO, layout: Layout, page: Ifd) -> bool:
    return read_description(f, layout, page).startswith(APERIO_PREFIX)


def page_dimensions(f: BinaryIO, layout: Layout, page: Ifd) -> tuple[int, int]:
    width = read_tag_scalar(f, layout, page, TAG_IMAGE_WIDTH)
    height = read_tag_scalar(f, layout, page, TAG_IMAGE_HEIGHT)
    if width is None or height is None or width <= 0 or height <= 0:
        raise ValueError(f"IFD {page.offset}: invalid or missing image dimensions")
    return width, height


def page_data_ranges(f: BinaryIO, layout: Layout, page: Ifd) -> list[tuple[int, int]]:
    """Return image-data ranges referenced by a tiled or stripped IFD."""
    for offset_tag, count_tag in (
        (TAG_TILE_OFFSETS, TAG_TILE_BYTE_COUNTS),
        (TAG_STRIP_OFFSETS, TAG_STRIP_BYTE_COUNTS),
    ):
        offsets = read_tag_values(f, layout, page, offset_tag)
        byte_counts = read_tag_values(f, layout, page, count_tag)
        if offsets is None or byte_counts is None:
            continue
        if len(offsets) != len(byte_counts):
            raise ValueError(f"IFD {page.offset}: image offset/count array length mismatch")
        return [(offset, offset + size) for offset, size in zip(offsets, byte_counts)]
    return []


def page_referenced_end(f: BinaryIO, layout: Layout, page: Ifd) -> int:
    """Largest byte endpoint required by this IFD, its tag payloads, and pixels."""
    end = page.next_pointer_offset + layout.offset_size
    for tag in page.entries:
        byte_count = tag.byte_count()
        if byte_count is None:
            continue
        if byte_count > layout.offset_size:
            end = max(end, tag.value_or_offset + byte_count)
    for _start, data_end in page_data_ranges(f, layout, page):
        end = max(end, data_end)
    return end


def page_min_data_start(f: BinaryIO, layout: Layout, page: Ifd) -> int | None:
    starts = [start for start, _end in page_data_ranges(f, layout, page)]
    return min(starts) if starts else None


def write_offset(f: BinaryIO, layout: Layout, pointer_offset: int, value: int) -> None:
    f.seek(pointer_offset)
    f.write(struct.pack(layout.offset_format, value))


def rebuild_ifd_chain_with_explicit_subfile_type_zero(
    f: BinaryIO,
    layout: Layout,
    pages: list[Ifd],
) -> int | None:
    """Append a replacement IFD chain with NewSubfileType=0 on every page.

    KFbioConverter omits tag 254 on its reduced-resolution IFDs.  Bio-Formats
    treats that missing value as non-zero while identifying label/macro images,
    which splits valid pyramid levels into additional QuPath series.  An IFD
    cannot safely grow in place, so a tiny replacement directory chain is
    appended; all original tag payload and pixel offsets remain unchanged.
    """
    baseline_before = read_tag_scalar(f, layout, pages[0], TAG_NEW_SUBFILE_TYPE)
    rebuilt_entries: list[list[Tag]] = []
    for page in pages:
        entries: list[Tag] = []
        found_nst = False
        for tag in page.entries:
            if tag.tag == TAG_NEW_SUBFILE_TYPE:
                if found_nst:
                    raise ValueError(f"IFD {page.offset}: duplicate NewSubfileType tags")
                found_nst = True
                byte_count = tag.byte_count()
                if tag.count != 1 or byte_count is None or byte_count > layout.offset_size:
                    raise ValueError(
                        f"IFD {page.offset}: unsupported NewSubfileType layout"
                    )
                entries.append(
                    Tag(tag.tag, tag.field_type, tag.count, 0, entry_offset=0)
                )
            else:
                entries.append(
                    Tag(
                        tag.tag,
                        tag.field_type,
                        tag.count,
                        tag.value_or_offset,
                        entry_offset=0,
                    )
                )
        if not found_nst:
            # TIFF LONG, count 1, value 0 (stored inline).
            entries.append(
                Tag(TAG_NEW_SUBFILE_TYPE, 4, 1, 0, entry_offset=0)
            )
        entries.sort(key=lambda tag: tag.tag)
        rebuilt_entries.append(entries)

    f.seek(0, os.SEEK_END)
    old_end = f.tell()
    alignment = 8 if layout.bigtiff else 2
    chain_start = (old_end + alignment - 1) // alignment * alignment
    if chain_start > old_end:
        f.write(b"\x00" * (chain_start - old_end))

    offsets: list[int] = []
    cursor = chain_start
    for entries in rebuilt_entries:
        offsets.append(cursor)
        cursor += (
            layout.count_size
            + len(entries) * layout.entry_size
            + layout.offset_size
        )
    if not layout.bigtiff and cursor > 0xFFFFFFFF:
        raise ValueError(
            "Classic TIFF replacement IFD chain would exceed the 4 GiB offset limit"
        )

    count_format = layout.endian + ("Q" if layout.bigtiff else "H")
    for index, entries in enumerate(rebuilt_entries):
        f.seek(offsets[index])
        f.write(struct.pack(count_format, len(entries)))
        for tag in entries:
            f.write(
                struct.pack(
                    layout.entry_format,
                    tag.tag,
                    tag.field_type,
                    tag.count,
                    tag.value_or_offset,
                )
            )
        next_offset = offsets[index + 1] if index + 1 < len(offsets) else 0
        f.write(struct.pack(layout.offset_format, next_offset))

    # Make only the replacement pyramid reachable from the TIFF header.
    write_offset(
        f,
        layout,
        layout.header_first_ifd_pointer_offset,
        offsets[0],
    )
    f.flush()
    return baseline_before


def validate_cleaned_svs(
    path: Path,
    min_pyramid_max_dim: int = DEFAULT_MIN_PYRAMID_MAX_DIM,
) -> tuple[int, int | None]:
    """Validate the final reachable IFD chain; returns (page_count, baseline_nst)."""
    with path.open("rb") as f:
        layout = read_layout(f)
        pages = read_ifd_chain(f, layout)
        if len(pages) < 2:
            raise ValueError("SVS needs a baseline plus at least one pyramid level")
        baseline_nst = read_tag_scalar(f, layout, pages[0], TAG_NEW_SUBFILE_TYPE)
        if baseline_nst not in (None, 0):
            raise ValueError(f"Baseline NewSubfileType is still {baseline_nst}, expected 0")
        bad_pages = [page.offset for page in pages if not is_aperio_pyramid_page(f, layout, page)]
        if bad_pages:
            raise ValueError(f"Non-Aperio IFD(s) still reachable: {bad_pages}")
        for index, page in enumerate(pages):
            # Forces checks of tile/strip arrays without decoding a single tile.
            page_referenced_end(f, layout, page)
            nst = read_tag_scalar(f, layout, page, TAG_NEW_SUBFILE_TYPE)
            if nst != 0:
                raise ValueError(
                    f"Pyramid IFD {index} has NewSubfileType={nst}, expected explicit 0"
                )
            if index:
                width, height = page_dimensions(f, layout, page)
                if (
                    min_pyramid_max_dim > 0
                    and max(width, height) < min_pyramid_max_dim
                ):
                    raise ValueError(
                        f"Overview-sized pyramid IFD is still reachable: "
                        f"{width}x{height} (< {min_pyramid_max_dim})"
                    )
        return len(pages), baseline_nst


def clean_svs_in_place(
    path: Path,
    min_pyramid_max_dim: int = DEFAULT_MIN_PYRAMID_MAX_DIM,
) -> CleanupResult:
    """Remove associated IFDs from *path* after the caller created a safe copy."""
    original_bytes = path.stat().st_size
    with path.open("r+b") as f:
        layout = read_layout(f)
        pages = read_ifd_chain(f, layout)
        # The first IFD is the full-resolution baseline.  KFbioConverter emits
        # all genuine pyramid levels with an "Aperio Image..." description;
        # thumbnail/label/macro IFDs do not have that description.
        if min_pyramid_max_dim < 0:
            raise ValueError("--min-pyramid-max-dim cannot be negative")
        aperio_levels = [
            page for page in pages[1:]
            if is_aperio_pyramid_page(f, layout, page)
        ]
        small_levels = [
            page for page in aperio_levels
            if max(page_dimensions(f, layout, page)) < min_pyramid_max_dim
        ]
        kept = [pages[0]] + [
            page for page in aperio_levels
            if page not in small_levels
        ]
        associated = [
            page for page in pages[1:]
            if page not in aperio_levels
        ]
        removed = [page for page in pages if page not in kept]
        if len(kept) < 2:
            raise ValueError(
                "Could not identify an Aperio pyramid (need baseline plus >=1 level); "
                "refusing to edit this file"
            )

        before = rebuild_ifd_chain_with_explicit_subfile_type_zero(
            f,
            layout,
            kept,
        )
        after = 0
        physically_truncated = False

    kept_count, validated_nst = validate_cleaned_svs(path, min_pyramid_max_dim)
    return CleanupResult(
        source=path,
        destination=path,
        kept_ifds=kept_count,
        removed_ifds=len(removed),
        removed_associated_ifds=len(associated),
        removed_small_pyramid_ifds=len(small_levels),
        original_bytes=original_bytes,
        output_bytes=path.stat().st_size,
        physically_truncated=physically_truncated,
        baseline_subfile_type_before=before,
        baseline_subfile_type_after=validated_nst if after is not None else before,
    )


def process_existing_svs(
    source: Path,
    destination: Path,
    force: bool,
    min_pyramid_max_dim: int,
) -> CleanupResult | None:
    if destination.exists() and not force:
        print(f"SKIP existing: {destination.name}")
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    # copy2 creates a separate, recoverable source-preserving output before
    # any TIFF bytes are changed.
    shutil.copy2(source, destination)
    try:
        result = clean_svs_in_place(destination, min_pyramid_max_dim)
    except Exception:
        # The source is intact.  Keep the copied failed output for inspection
        # rather than silently deleting forensic evidence.
        raise
    return result


def run_converter(converter: Path, source: Path, destination: Path, layers: int) -> None:
    command = [str(converter), str(source), str(destination), str(layers)]
    completed = subprocess.run(
        command,
        cwd=str(converter.parent),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0 or "OK" not in output or not destination.exists():
        raise RuntimeError(
            f"KFbioConverter failed for {source.name} (exit {completed.returncode}):\n"
            f"{output[-1200:]}"
        )


def _publish_svs(staged: Path, destination: Path) -> None:
    """Move a cleaned staged SVS to its final destination.

    os.replace is atomic only when both paths share a volume.  When the staging
    folder lives on a different drive than the output folder (e.g. E: stage,
    F: output), Windows raises ERROR_NOT_SAME_DEVICE (WinError 17) and POSIX
    raises EXDEV.  In that case fall back to copying into a sibling temp file
    on the destination volume and then atomically replacing it, so the final
    output is never left half-written.
    """
    try:
        os.replace(staged, destination)
        return
    except OSError as exc:
        is_cross_device = getattr(exc, "winerror", None) == 17 or exc.errno == errno.EXDEV
        if not is_cross_device:
            raise
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".__publish_{uuid4().hex}.svs")
    try:
        shutil.copy2(staged, tmp)
        os.replace(tmp, destination)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    try:
        staged.unlink()
    except OSError:
        pass


def convert_and_clean_one(
    source: Path,
    destination: Path,
    converter: Path,
    layers: int,
    stage_dir: Path,
    min_pyramid_max_dim: int,
) -> CleanupResult:
    """Convert one KFB through an isolated temporary file, then publish it.

    The unique staging name is deliberate: a previous Ctrl+C can leave a
    partial file in the staging folder, but it must never block a safe resume.
    The final publish is atomic when the stage folder and the output folder
    share a volume; otherwise it transparently falls back to a copy+replace
    on the destination volume (see _publish_svs).
    """
    staged = stage_dir / f"{source.stem}.__stage_{uuid4().hex}.svs"
    run_converter(converter, source, staged, layers)
    result = clean_svs_in_place(staged, min_pyramid_max_dim)
    _publish_svs(staged, destination)
    result.source = source
    result.destination = destination
    return result


def result_row(result: CleanupResult) -> dict[str, object]:
    return {
        "file": result.destination.name,
        "kept_ifds": result.kept_ifds,
        "removed_ifds": result.removed_ifds,
        "removed_associated_ifds": result.removed_associated_ifds,
        "removed_small_pyramid_ifds": result.removed_small_pyramid_ifds,
        "input_mib": f"{result.original_bytes / 1024**2:.1f}",
        "output_mib": f"{result.output_bytes / 1024**2:.1f}",
        "physically_truncated": result.physically_truncated,
        "baseline_nst_before": result.baseline_subfile_type_before,
        "baseline_nst_after": result.baseline_subfile_type_after,
    }


def write_report(output_dir: Path, rows: Iterable[dict[str, object]]) -> Path:
    report = output_dir / f"qupath_svs_report_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    fieldnames = [
        "file", "kept_ifds", "removed_ifds", "removed_associated_ifds",
        "removed_small_pyramid_ifds", "input_mib", "output_mib",
        "physically_truncated", "baseline_nst_before", "baseline_nst_after",
    ]
    with report.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return report


def add_common_io_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--input-dir", type=Path, default=DEFAULT_INPUT_DIR,
        help=f"Folder containing source files (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_INPUT_DIR / "svs_qupath_clean",
        help="New output folder; input files are never edited",
    )
    parser.add_argument("--force", action="store_true", help="Replace an already-existing output file")
    parser.add_argument(
        "--min-pyramid-max-dim",
        type=int,
        default=DEFAULT_MIN_PYRAMID_MAX_DIM,
        help=(
            "Optional: drop reduced-resolution IFDs whose longest edge is smaller "
            f"than this (default: {DEFAULT_MIN_PYRAMID_MAX_DIM}, disabled)"
        ),
    )


def add_convert_staging_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--stage-dir",
        type=Path,
        default=None,
        help=(
            "Temporary ASCII-only folder used by KFbioConverter before the cleaned "
            "SVS is moved to --output-dir. Default: <output drive>:\\kfb2svs_stage"
        ),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert KFB to SVS and remove QuPath-visible thumbnail/label/macro series safely."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    convert = sub.add_parser("convert", help="Convert every KFB, then clean the new SVS")
    add_common_io_arguments(convert)
    add_convert_staging_argument(convert)
    convert.add_argument("--converter", type=Path, default=DEFAULT_CONVERTER)
    convert.add_argument("--layers", type=int, default=4, choices=range(2, 10))
    convert.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Number of KFB files to convert concurrently (default: 1). "
            "For a USB/external HDD, use 2; higher values usually slow the disk down."
        ),
    )

    clean = sub.add_parser("clean", help="Clean already-converted SVS files into a new folder")
    add_common_io_arguments(clean)
    clean.set_defaults(output_dir=DEFAULT_INPUT_DIR / "svs_qupath_clean")

    inspect = sub.add_parser("inspect", help="Report TIFF/IFD structure without changing a file")
    inspect.add_argument("file", type=Path)
    return parser.parse_args()


def inspect(path: Path) -> None:
    with path.open("rb") as f:
        layout = read_layout(f)
        pages = read_ifd_chain(f, layout)
        print(f"{path}\nformat={'BigTIFF' if layout.bigtiff else 'Classic TIFF'}; IFDs={len(pages)}")
        for index, page in enumerate(pages):
            width = read_tag_scalar(f, layout, page, TAG_IMAGE_WIDTH)
            height = read_tag_scalar(f, layout, page, TAG_IMAGE_HEIGHT)
            nst = read_tag_scalar(f, layout, page, TAG_NEW_SUBFILE_TYPE)
            description = read_description(f, layout, page).decode("latin-1", "replace").replace("\r", " ").replace("\n", " ")
            print(f"  {index}: ifd={page.offset}, {width}x{height}, NewSubfileType={nst}, desc={description[:90]!r}")


def main() -> int:
    args = parse_args()
    if args.command == "inspect":
        inspect(args.file)
        return 0

    if args.command == "convert" and args.workers < 1:
        raise SystemExit("--workers must be at least 1")

    input_dir: Path = args.input_dir
    output_dir: Path = args.output_dir
    if not input_dir.is_dir():
        raise SystemExit(f"Input folder does not exist: {input_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    # KFbioConverter accepts Chinese input paths, but some builds fail when the
    # *output* path contains a long Chinese component.  Convert into an
    # ASCII-only folder on the same drive, clean there, then atomically move
    # the finished file into the requested output folder.
    stage_dir: Path | None = None
    if args.command == "convert":
        if args.stage_dir is not None:
            stage_dir = args.stage_dir
        else:
            drive_root = Path(output_dir.anchor)
            if not output_dir.anchor:
                raise SystemExit(
                    "--output-dir must be an absolute path when using convert; "
                    "or provide --stage-dir explicitly"
                )
            stage_dir = drive_root / "kfb2svs_stage"
        try:
            stage_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SystemExit(f"Cannot create staging folder {stage_dir}: {exc}") from exc

    if args.command == "convert":
        if not args.converter.is_file():
            raise SystemExit(f"KFbioConverter.exe not found: {args.converter}")
        sources = sorted(input_dir.glob("*.kfb"))
    else:
        sources = sorted(input_dir.glob("*.svs"))
    if not sources:
        raise SystemExit(f"No {'KFB' if args.command == 'convert' else 'SVS'} files found in: {input_dir}")

    rows: list[dict[str, object]] = []
    failures: list[tuple[Path, str]] = []

    # Pre-scan: split sources into already-converted (skipped) and pending.
    # This avoids flooding the log with "SKIP existing" lines interleaved with
    # real progress, so the user can actually see which files still need work.
    pending: list[tuple[int, Path, Path]] = []
    skipped_count = 0
    for index, source in enumerate(sources, 1):
        destination = output_dir / f"{source.stem}.svs"
        if destination.exists() and not args.force:
            skipped_count += 1
            continue
        pending.append((index, source, destination))

    pending_count = len(pending)
    print(
        f"Scanned {len(sources)} file(s): {skipped_count} already converted "
        f"(skipped), {pending_count} pending conversion."
    )
    if skipped_count:
        print("  Existing outputs will be skipped; use --force to re-convert them.")
    if pending_count == 0:
        print("Nothing to do.")
        return 0
    print(f"\nPending conversion ({pending_count}):")
    for seq, (_orig, source, _dest) in enumerate(pending, 1):
        print(f"  [{seq}/{pending_count}] {source.name}")
    print()

    def record_success(seq: int, source: Path, result: CleanupResult) -> None:
        rows.append(result_row(result))
        optional_drop = (
            f"; dropped {result.removed_small_pyramid_ifds} optional tiny level(s)"
            if result.removed_small_pyramid_ifds
            else ""
        )
        print(f"[{seq}/{pending_count}] {source.name}")
        print(
            f"  OK: kept {result.kept_ifds} pyramid IFDs; "
            f"excluded {result.removed_associated_ifds} associated IFDs"
            f"{optional_drop}; NewSubfileType=0 on every kept IFD "
            f"(baseline was {result.baseline_subfile_type_before}); "
            f"{result.output_bytes / 1024**2:.1f} MiB"
        )

    if args.command == "convert" and args.workers > 1:
        assert stage_dir is not None
        future_info = {}
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for seq, (_orig, source, destination) in enumerate(pending, 1):
                future = executor.submit(
                    convert_and_clean_one,
                    source,
                    destination,
                    args.converter,
                    args.layers,
                    stage_dir,
                    args.min_pyramid_max_dim,
                )
                future_info[future] = (seq, source)
            for future in as_completed(future_info):
                seq, source = future_info[future]
                try:
                    record_success(seq, source, future.result())
                except Exception as exc:
                    failures.append((source, str(exc)))
                    print(f"[{seq}/{pending_count}] {source.name}\n  FAILED: {exc}", file=sys.stderr)
    else:
        for seq, (_orig, source, destination) in enumerate(pending, 1):
            print(f"[{seq}/{pending_count}] {source.name}")
            try:
                if args.command == "convert":
                    assert stage_dir is not None
                    result = convert_and_clean_one(
                        source,
                        destination,
                        args.converter,
                        args.layers,
                        stage_dir,
                        args.min_pyramid_max_dim,
                    )
                else:
                    result = process_existing_svs(
                        source,
                        destination,
                        args.force,
                        args.min_pyramid_max_dim,
                    )
                    if result is None:
                        continue
                record_success(seq, source, result)
            except Exception as exc:
                failures.append((source, str(exc)))
                print(f"  FAILED: {exc}", file=sys.stderr)

    if rows:
        report = write_report(output_dir, rows)
        print(f"Report: {report}")
    print(
        f"Finished: {len(rows)} succeeded, {len(failures)} failed, "
        f"{skipped_count} skipped"
    )
    if failures:
        for source, message in failures:
            print(f"  - {source.name}: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
