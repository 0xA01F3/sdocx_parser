#!/usr/bin/env python3
"""Convert Samsung Notes .sdocx files to HTML, PDF and plain text.

Reproduces the character formatting (bold, italic, underline, strikethrough,
colour, highlight, font size) and the paragraph formatting (bullets, numbered
lists, checkboxes, indent, alignment), for both the note-level title/body and
any text boxes placed on a page - including their rotation. The page background
colour is reproduced too, so a note written in Samsung's dark theme exports
light-on-dark the way it was written.

Handwriting/ink strokes and images are deliberately NOT rendered. When a note
contains any, this is logged.

PDF output is produced by rendering the HTML export with WeasyPrint. WeasyPrint
is an optional dependency: it is imported only when --pdf is asked for.

Usage:
    python3 sdocx_parser.py NOTE.sdocx --all
    python3 sdocx_parser.py FOLDER/ --html --txt -o out/
    python3 sdocx_parser.py extracted/ -r --pdf
"""

from __future__ import annotations

import argparse
import html
import logging
import shutil
import struct
import sys
import textwrap
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from string import Template

LOGGER_NAME = "sdocx_parser"
DEFAULT_LOG_NAME = "sdocx_parser.log"

log = logging.getLogger(LOGGER_NAME)


# ---------------------------------------------------------------------------
# Binary reading primitives
# ---------------------------------------------------------------------------


class Reader:
    """Little-endian binary reader. Raises on any read past the end."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data, self.pos = data, pos

    def read(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise EOFError(
                f"wanted {n} bytes at offset {self.pos}, "
                f"only {len(self.data) - self.pos} left"
            )
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.read(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.read(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.read(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.read(4))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self.read(8))[0]

    def bitfield(self) -> int:
        """u8 byte-count (0-4), then that many bytes, little-endian."""
        n = self.u8()
        if n > 4:
            raise ValueError(f"bitfield size {n} > 4 at offset {self.pos - 1}")
        return int.from_bytes(self.read(n), "little") if n else 0

    def short_u16_string(self) -> str:  # u16 char count, UTF-16LE
        return self.read(2 * self.u16()).decode("utf-16-le")

    def long_u16_string(self) -> str:  # u32 char count, UTF-16LE
        return self.read(2 * self.u32()).decode("utf-16-le")

    def short_u8_string(self) -> str:  # u16 byte count, UTF-8
        return self.read(self.u16()).decode("utf-8")


def open_object_frame(r: Reader, expected_data_type: int):
    """Enter a length-prefixed frame. Returns (start, end, flex_offset, prop,
    fields); flex_offset is relative to the frame start."""
    frame_start = r.pos
    size = r.u32()  # inclusive of these 4 bytes
    frame_end = frame_start + size
    data_type = r.u16()
    if data_type != expected_data_type:
        raise ValueError(
            f"expected frame type {expected_data_type}, got {data_type} "
            f"at offset {frame_start}"
        )
    flex_offset = r.u32()
    prop = r.bitfield()
    fields = r.bitfield()
    return frame_start, frame_end, flex_offset, prop, fields


def skip_frame(r: Reader) -> None:
    start = r.pos
    size = r.u32()
    r.pos = start + size


# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

SPAN_TYPES = {
    0: "None", 1: "ForegroundColour", 3: "FontSize", 4: "FontName", 5: "Bold",
    6: "Italic", 7: "Underline", 9: "Hypertext", 15: "ComposingBackgroundColour",
    16: "Composing", 17: "BackgroundColour", 18: "ComposingTag", 19: "TimeStamp",
    20: "Strikethrough", 21: "Suggestion", 22: "SpellCorrection", 23: "Formula",
}
# Attributes whose payload carries an on/off flag rather than a value.
BOOLEAN_SPAN_TYPES = {5: "Bold", 6: "Italic", 7: "Underline", 20: "Strikethrough"}

PARAGRAPH_TYPES = {2: "IndentLevel", 3: "Alignment", 4: "LineSpacing",
                   5: "Bullet", 6: "ParsingState"}
BULLET_STYLES = {0: "None", 8: "Bullet", 4: "Numbered", 2: "Checkbox"}
ALIGNMENTS = {0: "left", 1: "right", 2: "center"}

OBJECT_TYPE_NAMES = {
    1: "Stroke", 2: "Text", 3: "Image", 4: "Container", 7: "Shape", 8: "Line",
    10: "Audio", 11: "Formula", 13: "Web", 14: "Painting", 17: "Link",
    19: "Generic", 20: "Plot", 21: "Maths", 22: "Table", 23: "CodeBlock",
}

INLINE_OBJECT_MARKER = "￼"    # Android's object-replacement character
DEFAULT_PT = 12.0
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".heic"}

# Samsung's own defaults for its two note themes: a light note stores #fcfcfc
# paper with #252525 text, a dark one #010101 paper with #dadada text. The
# background is read from the file (see page_background); these are what the
# text colour and the exported chrome have to match so they do not fight it.
FG_LIGHT = "#252525"
FG_DARK = "#dadada"
BG_LIGHT = "#fcfcfc"          # the fallback when the file's colour is unreadable

THEMES = {
    "light": dict(
        fg=FG_LIGHT, border="#e2e2e2",
        notice_fg="#6b4d12", notice_bg="#fdf5e6", notice_border="#c98a24",
        print_notice_fg="#333333", print_notice_bg="#f4f4f4",
        print_notice_border="#999999",
    ),
    "dark": dict(
        fg=FG_DARK, border="#3a3a3a",
        notice_fg="#e8cf9a", notice_bg="#241d0e", notice_border="#c98a24",
        print_notice_fg="#e8cf9a", print_notice_bg="#241d0e",
        print_notice_border="#c98a24",
    ),
}

# Samsung's page canvas corresponds to a page this many points wide. Used to
# convert a stored point size into a share of the canvas, so text inside a
# page scales with it.
PAGE_WIDTH_PT = 600.0

# Stored font sizes are in Samsung's own units, not points: the app lays a note
# out against a narrow phone-width column and scales that up on export. Reading
# them as points renders every note about two-thirds the size Samsung draws it.
#
# Measured off the reference exports in samples/pdf-exports/ (all 600 x 849pt
# pages): body text stored as 12 comes out ~20pt with a 27pt line pitch, in the
# note flow and in page text boxes alike. Hence 20/12 here, and the 1.35 line
# height in the stylesheet below - the same 1.35 the page canvas already used.
SAMSUNG_PT_SCALE = 20.0 / 12.0
LINE_HEIGHT = 1.35

# Samsung's own exports are 600 x 849pt pages with a ~27pt margin. Matching
# that on A4 keeps our pagination close to theirs now that the type matches;
# a wider margin pushes a note that Samsung fits on one page onto two.
A4_WIDTH_PT = 595.28
PDF_PAGE_MARGIN_PT = 28.0

# The A4 content width. WeasyPrint supports neither container queries nor
# container-query units, so for PDF the page canvas gets a concrete width and
# its text is scaled to it in points instead.
PDF_CANVAS_WIDTH_PT = A4_WIDTH_PT - 2 * PDF_PAGE_MARGIN_PT
PDF_CANVAS_SCALE = PDF_CANVAS_WIDTH_PT / PAGE_WIDTH_PT


# ---------------------------------------------------------------------------
# Object model
# ---------------------------------------------------------------------------


@dataclass
class Span:
    span_type: int
    start: int
    end: int
    interval_type: int
    extra: bytes

    @property
    def type_name(self) -> str:
        return SPAN_TYPES.get(self.span_type, f"Unknown({self.span_type})")


@dataclass
class Paragraph:
    paragraph_type: int
    start: int          # LINE index, not a character offset
    end: int
    extra: bytes


@dataclass
class TextObject:
    uuid: str
    rect: tuple
    format_version: int
    text: str = ""
    spans: list = field(default_factory=list)
    paragraphs: list = field(default_factory=list)
    margins: tuple = None
    gravity: int = None
    angle: float = 0.0      # degrees, clockwise
    pivot: tuple = None     # rotation centre; None => rect centre


@dataclass(frozen=True)
class Included:
    """Which of the parser's own additions go into the exports. Both are off
    by default, so an export carries the note and nothing else; what is left
    out of it is reported in the run log instead of on the page."""

    title: bool = False       # the note's title, as a heading
    warning: bool = False     # the notice listing content that is not rendered


@dataclass
class Note:
    """One parsed .sdocx, independent of any output format."""

    source: Path
    name: str               # the stem used for output filenames
    title: TextObject
    body: TextObject
    pages: list
    background: str
    skipped: list           # human-readable descriptions of unrendered content

    @property
    def doc_title(self) -> str:
        return self.title.text.strip() or self.name

    @property
    def has_text(self) -> bool:
        """Whether there is any typed text for an export to carry. False for a
        note that is nothing but handwriting."""
        return bool(self.body.text.strip()
                    or any(t.text.strip()
                           for p in self.pages for t in p["texts"]))


# ---------------------------------------------------------------------------
# Archives: a .sdocx zip, or a directory it was already extracted into
# ---------------------------------------------------------------------------


class NoteArchive:
    """The two accessors the parser needs, over a zip or a directory."""

    def read(self, name: str) -> bytes:
        raise NotImplementedError

    def namelist(self) -> list:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class ZipNoteArchive(NoteArchive):
    """A .sdocx file, which is an ordinary zip archive."""

    def __init__(self, path: Path):
        self._zf = zipfile.ZipFile(path)

    def read(self, name: str) -> bytes:
        return self._zf.read(name)

    def namelist(self) -> list:
        return self._zf.namelist()

    def close(self) -> None:
        self._zf.close()


class DirNoteArchive(NoteArchive):
    """A directory a .sdocx was unzipped into - member names are the same
    slash-separated paths the zip used."""

    def __init__(self, root: Path):
        self.root = root

    def read(self, name: str) -> bytes:
        member = self.root / name
        if not member.is_file():
            raise KeyError(f"there is no item named {name!r} in {self.root}")
        return member.read_bytes()

    def namelist(self) -> list:
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file()
        )


def is_extracted_sdocx(path: Path) -> bool:
    """A directory holding an unzipped note. Named `<note>.sdocx/` by every
    tool that does the unzipping, but the note.note member is what actually
    makes it readable, so that is what is tested."""
    return path.is_dir() and (path / "note.note").is_file()


def open_archive(path: Path) -> NoteArchive:
    return DirNoteArchive(path) if path.is_dir() else ZipNoteArchive(path)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_page_id_info(data: bytes) -> list:
    """Reading order of pages: 32-byte note hash, u16 count, then per page a
    short u16 string UUID and a 32-byte hash."""
    pos = 32
    count = struct.unpack_from("<H", data, pos)[0]
    pos += 2
    order = []
    for _ in range(count):
        n = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        order.append(data[pos : pos + 2 * n].decode("utf-16-le"))
        pos += 2 * n + 32
    return order


def bgra_colour(payload: bytes):
    """Colour payloads are BGRA. Alpha 0 means the colour is not applied
    (Samsung writes a transparent black for "no highlight")."""
    if len(payload) < 4 or payload[3] == 0:
        return None
    return f"#{payload[2]:02x}{payload[1]:02x}{payload[0]:02x}"


# Sizes of the page-header flex fields that sit BELOW the background colour
# (bit 5), so its offset can be computed. Bit 0 is a 4 x f64 rect, bit 4 a u32.
# Bits 1-3 have never been seen set; if one is, its size is unknown.
PAGE_FLEX_SIZES_BELOW_BG = {0: 32, 4: 4}
PAGE_BG_FIELD_BIT = 5


def page_background(data: bytes, flex_offset: int, page_end: int, fields: int):
    """The page's background colour, or None if the file does not say.

    Bit 5 of the page header's field flags is a BGRA background colour: #fcfcfc
    on Samsung's light theme, #010101 on its dark one. Flex fields are written
    in ascending bit order, so the colour's offset is the sum of the sizes of
    the set bits below it - bail out rather than guess if one of those is a
    field whose size we do not know."""
    if not flex_offset or not (fields >> PAGE_BG_FIELD_BIT & 1):
        return None
    off = flex_offset
    for bit in range(PAGE_BG_FIELD_BIT):
        if not (fields >> bit & 1):
            continue
        if bit not in PAGE_FLEX_SIZES_BELOW_BG:
            return None
        off += PAGE_FLEX_SIZES_BELOW_BG[bit]
    if off + 4 > min(page_end, len(data)):
        return None
    return bgra_colour(data[off : off + 4])


def parse_page_header(r: Reader):
    """Returns (uuid, width, height, format_version, background) and leaves the
    reader at the start of the layer data."""
    page_end_offset = r.u32()
    flex_offset = r.u32()
    r.bitfield()
    fields = r.bitfield()
    r.u32()                      # orientation
    width = r.u32()
    height = r.u32()
    r.u32()                      # offset x
    r.u32()                      # offset y
    uuid = r.short_u16_string()
    r.i64()                      # modified time
    format_version = r.u32()
    r.u32()                      # min format version
    background = page_background(r.data, flex_offset, page_end_offset, fields)
    r.pos = page_end_offset      # skip the rest of the optional fields
    return uuid, width, height, format_version, background


def iter_layer_objects(r: Reader):
    """Yield (object_type, object_bytes) for every object on the page."""
    layer_count = r.u16()
    r.u16()                                  # current layer index
    for _ in range(layer_count):
        skip_frame(r)                        # layer header
        for _ in range(r.u32()):             # object count
            obj_type = r.u8()
            child_count = r.u16()
            if child_count:
                raise ValueError(f"object with {child_count} children unsupported")
            yield obj_type, r.read(r.u32())  # frames + 32-byte hash
        r.read(32)                           # layer hash


def parse_object_base(r: Reader):
    """Fixed fields plus the two flex fields we need: rotation and its pivot."""
    frame_start, frame_end, flex_offset, _prop, fields = open_object_frame(r, 0)
    format_version = r.u32()
    uuid = r.short_u8_string()
    r.i64()                                            # modified time
    rect = (r.f64(), r.f64(), r.f64(), r.f64())
    r.u32()                                            # timestamp
    r.u8()                                             # resize mode

    # Bit 0 is the rotation angle (f32 degrees) and, like the Shape frame's
    # text payload, bit 0's field is always FIRST in the flex area.
    angle = 0.0
    if flex_offset and (fields & 1):
        angle = Reader(r.data, frame_start + flex_offset).f32()

    # Bit 18 is the pivot (2 x f64). It sits after lower-bit fields whose
    # sizes we do not all know, but it is the last field written, so read it
    # off the tail. If any higher bit is set something follows it and that
    # assumption breaks - bail out rather than return a wrong pivot.
    pivot = None
    if flex_offset and (fields >> 18 & 1) and not (fields >> 19):
        tail = Reader(r.data, frame_end - 16)
        pivot = (tail.f64(), tail.f64())

    r.pos = frame_end
    return format_version, uuid, rect, angle, pivot


def parse_common(r: Reader) -> dict:
    """The text payload: flat string plus span and paragraph run arrays."""
    size = r.u32()                     # exclusive length prefix
    end = r.pos + size
    sub = Reader(r.data, r.pos)

    text = sub.long_u16_string()

    spans = []
    for _ in range(sub.u32()):
        rec_size = sub.u16()
        if rec_size < 16:
            raise ValueError(f"span record size {rec_size} < 16")
        spans.append(
            Span(sub.u32(), sub.u32(), sub.u32(), sub.u32(), sub.read(rec_size - 16))
        )

    paragraphs = []
    for _ in range(sub.u32()):
        rec_size = sub.u16()
        if rec_size < 12:
            raise ValueError(f"paragraph record size {rec_size} < 12")
        paragraphs.append(
            Paragraph(sub.u32(), sub.u32(), sub.u32(), sub.read(rec_size - 12))
        )

    margins = (sub.f32(), sub.f32(), sub.f32(), sub.f32())
    gravity = sub.u8()

    r.pos = end
    return dict(text=text, spans=spans, paragraphs=paragraphs,
                margins=margins, gravity=gravity)


def parse_text_object(obj_bytes: bytes) -> TextObject:
    """A type-2 layer object: ObjectBase -> ShapeBase -> Shape -> Text."""
    r = Reader(obj_bytes)
    fmt_v, uuid, rect, angle, pivot = parse_object_base(r)
    skip_frame(r)                                   # ShapeBase

    frame_start, frame_end, flex_offset, _prop, fields = open_object_frame(r, 7)
    obj = TextObject(uuid=uuid, rect=rect, format_version=fmt_v,
                     angle=angle, pivot=pivot)
    if flex_offset != 0 and (fields & 1):           # bit 0 => text present
        r.pos = frame_start + flex_offset
        common = parse_common(r)
        obj.text = common["text"]
        obj.spans = common["spans"]
        obj.paragraphs = common["paragraphs"]
        obj.margins = common["margins"]
        obj.gravity = common["gravity"]
    r.pos = frame_end
    return obj


def parse_note_title_body(note_bytes: bytes):
    """note.note holds the note's title and body as two ordinary text objects."""
    r = Reader(note_bytes)
    r.u32()                       # flex offset
    r.bitfield()
    r.bitfield()
    r.u32()                       # format version
    r.short_u16_string()          # id
    r.u32()                       # revision
    r.i64()                       # created
    r.i64()                       # modified
    for _ in range(5):            # width, height, 2 paddings, min format
        r.u32()
    title_blob = r.read(r.u32())
    body_blob = r.read(r.u32())
    return parse_text_object(title_blob), parse_text_object(body_blob)


def parse_pages(archive: NoteArchive) -> list:
    """Every page in reading order, with its text boxes and an object census."""
    pages = []
    for uuid in parse_page_id_info(archive.read("pageIdInfo.dat")):
        r = Reader(archive.read(f"{uuid}.page"))
        page_uuid, w, h, fmt_v, background = parse_page_header(r)
        texts, census = [], {}
        for obj_type, blob in iter_layer_objects(r):
            census[obj_type] = census.get(obj_type, 0) + 1
            if obj_type == 2:
                texts.append(parse_text_object(blob))
        texts.sort(key=lambda t: (t.rect[1], t.rect[0]))     # top, then left
        pages.append(dict(uuid=page_uuid, width=w, height=h,
                          format_version=fmt_v, texts=texts, census=census,
                          background=background))
    return pages


def parse_sdocx(path: Path) -> Note:
    """Read one note, from a .sdocx file or from a directory it was unzipped
    into. Everything the output formats need is on the returned Note."""
    with open_archive(path) as archive:
        title, body = parse_note_title_body(archive.read("note.note"))
        pages = parse_pages(archive)
        all_text = [title.text, body.text] + [
            t.text for p in pages for t in p["texts"]
        ]
        skipped = skipped_content(archive, pages, all_text)

    # The note's own paper colour, from the first page that states one. Pages
    # each get their own below; this is what the document flow around them uses.
    background = next((p["background"] for p in pages if p["background"]),
                      BG_LIGHT)
    return Note(source=path, name=path.stem, title=title, body=body,
                pages=pages, background=background, skipped=skipped)


# ---------------------------------------------------------------------------
# Formatting decode
# ---------------------------------------------------------------------------


def span_is_on(s: Span) -> bool:
    """Bold/Italic/Underline/Strikethrough spans are written for a range even
    when the attribute is OFF - the payload's first u16 is the flag. Treating
    a span's existence as "apply it" renders whole documents bold."""
    return len(s.extra) >= 2 and int.from_bytes(s.extra[:2], "little") == 1


def span_colour(s: Span):
    return bgra_colour(s.extra)


def is_dark(colour: str) -> bool:
    """Whether text on this background has to be light. A rough sRGB luminance
    is plenty for a two-way decision - the stored backgrounds are near-white or
    near-black, never borderline."""
    r, g, b = (int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.5


def theme_for(background: str) -> dict:
    return THEMES["dark" if is_dark(background) else "light"]


def decode_bullet_payload(extra: bytes) -> dict:
    """16-byte payload, four u32s. The second is polymorphic: checked-state
    for checkboxes, ordinal for numbered items."""
    if len(extra) < 16:
        return {"style": 0, "value": 0}
    style, value, _reserved, group = struct.unpack_from("<4I", extra)
    return dict(style=style, value=value, group=group,
                checked=bool(value) if style == 2 else None,
                ordinal=value if style == 4 else None)


def decode_u32(extra: bytes) -> int:
    return struct.unpack_from("<I", extra)[0] if len(extra) >= 4 else 0


def bullet_marker(bullet, indent: int = 0) -> str:
    """The glyph Samsung draws. Empty when the line is not a list item -
    style 0 is a Bullet run that draws no marker at all."""
    if not bullet:
        return ""
    style = bullet["style"]
    if style == 0:
        return ""
    if style == 8:
        return "•"
    if style == 2:
        return "☑" if bullet["checked"] else "☐"
    if style == 4:
        n = bullet["ordinal"] or 1
        if indent <= 0:
            return f"{n}."
        alphabet = "A" if indent == 1 else "a"
        return f"{chr(ord(alphabet) + n - 1)}."
    return ""


def lines_with_paragraph_info(t: TextObject) -> list:
    """Split into lines, attaching bullet, indent, alignment and marker.
    Paragraph run offsets are LINE indices, not character offsets."""
    lines = t.text.split("\n")
    info = [{"text": l, "bullet": None, "indent": 0, "align": 0, "marker": ""}
            for l in lines]
    for p in t.paragraphs:
        lo, hi = p.start, min(p.end, len(lines))
        if p.paragraph_type == 5:
            decoded = decode_bullet_payload(p.extra)
            for i in range(lo, hi):
                info[i]["bullet"] = decoded
        elif p.paragraph_type == 2:
            level = decode_u32(p.extra)
            for i in range(lo, hi):
                info[i]["indent"] = level
        elif p.paragraph_type == 3:
            align = decode_u32(p.extra)
            for i in range(lo, hi):
                info[i]["align"] = align
    for row in info:
        row["marker"] = bullet_marker(row["bullet"], row["indent"])
    return info


def char_attributes(t: TextObject):
    """Per-character formatting. Span offsets are CHARACTER offsets."""
    n = len(t.text)
    sets = [set() for _ in range(n)]
    fg, bg, size = [None] * n, [None] * n, [None] * n
    for s in t.spans:
        lo, hi = max(0, s.start), min(s.end, n)
        if s.span_type in BOOLEAN_SPAN_TYPES:
            if not span_is_on(s):
                continue
            name = BOOLEAN_SPAN_TYPES[s.span_type]
            for i in range(lo, hi):
                sets[i].add(name)
        elif s.type_name == "ForegroundColour":
            col = span_colour(s)
            for i in range(lo, hi):
                fg[i] = col
        elif s.type_name == "BackgroundColour":
            col = span_colour(s)
            for i in range(lo, hi):
                bg[i] = col
        elif s.type_name == "FontSize" and len(s.extra) >= 4:
            pt = struct.unpack_from("<f", s.extra)[0]
            for i in range(lo, hi):
                size[i] = pt
    attrs = [frozenset(s) for s in sets]
    return attrs, fg, bg, size


def skipped_content(archive: NoteArchive, pages: list, texts: list) -> list:
    """Identify skipped content. Ink, images, and PDFs are out of scope by design."""
    strokes = sum(p["census"].get(1, 0) for p in pages)
    other_objs = {}
    for p in pages:
        for t, n in p["census"].items():
            if t not in (1, 2):
                other_objs[t] = other_objs.get(t, 0) + n

    pdfs, images = [], []
    for name in archive.namelist():
        if not name.startswith("media/") or name.endswith("mediaInfo.dat"):
            continue
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if ext == ".pdf":
            pdfs.append(name)
        elif ext in IMAGE_EXTS:
            images.append(name)

    inline = sum(t.count(INLINE_OBJECT_MARKER) for t in texts)

    bits = []
    if strokes:
        bits.append(f"{strokes:,} ink stroke{'s' if strokes != 1 else ''} "
                    "(handwriting or drawing)")
    for t, n in sorted(other_objs.items()):
        bits.append(f"{n} {OBJECT_TYPE_NAMES.get(t, f'type-{t}')} object"
                    f"{'s' if n != 1 else ''} on the page canvas")
    if images:
        bits.append(f"{len(images)} embedded image{'s' if len(images) != 1 else ''}")
    if pdfs:
        bits.append(f"{len(pdfs)} embedded PDF{'s' if len(pdfs) != 1 else ''} "
                    "— this note annotates a document")
    if inline:
        bits.append(f"{inline} inline object{'s' if inline != 1 else ''} "
                    "inside the text")
    return bits


# ---------------------------------------------------------------------------
# HTML emission
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderStyle:
    """How one text object is to be typeset."""

    unit: str = "pt"                 # "pt" (absolute) or "cqw" (share of canvas)
    scale: float = 1.0               # multiplies point sizes, for the PDF canvas
    default_fg: str = FG_LIGHT       # the colour the stylesheet already applies
    indent_em: float = 2.0


def font_size_css(stored: float, style: RenderStyle) -> str:
    """A stored size as CSS. Absolute in the document flow, but inside a page
    canvas it must scale with the canvas - expressed either as a share of its
    width via container query units, or, for PDF, in points off a fixed one."""
    pt = stored * SAMSUNG_PT_SCALE
    if style.unit == "cqw":
        return f"{pt / PAGE_WIDTH_PT * 100:.3f}cqw"
    return f"{pt * style.scale:g}pt"


def runs_to_html(text: str, attrs, fg, bg, size, lo: int, hi: int,
                 style: RenderStyle) -> str:
    """Merge adjacent characters with identical formatting into runs."""
    if lo >= hi:
        return ""
    pieces, run_start = [], lo
    for i in range(lo + 1, hi + 1):
        same = (
            i < hi
            and attrs[i] == attrs[run_start]
            and fg[i] == fg[run_start]
            and bg[i] == bg[run_start]
            and size[i] == size[run_start]
        )
        if same:
            continue
        pieces.append(
            _run_html(text[run_start:i], attrs[run_start], fg[run_start],
                      bg[run_start], size[run_start], style)
        )
        run_start = i
    return "".join(pieces)


def _run_html(s: str, attrs, fg, bg, size, style: RenderStyle) -> str:
    # An inline object (image in the text flow) is not rendered.
    # html.escape() leaves U+FFFC alone, so substituting after escaping
    # is safe.
    out = html.escape(s).replace(
        INLINE_OBJECT_MARKER,
        '<span class="inline-obj" title="inline object not rendered">&#9634;</span>',
    )
    if "Bold" in attrs:
        out = f"<strong>{out}</strong>"
    if "Italic" in attrs:
        out = f"<em>{out}</em>"
    if "Underline" in attrs:
        out = f"<u>{out}</u>"
    if "Strikethrough" in attrs:
        out = f"<s>{out}</s>"
    styles = []
    if fg and fg != style.default_fg:  # the stylesheet already sets the default
        styles.append(f"color:{fg}")
    if bg:
        styles.append(f"background-color:{bg}")
    if size is not None and abs(size - DEFAULT_PT) > 1e-6:
        styles.append(f"font-size:{font_size_css(size, style)}")
    if styles:
        out = f'<span style="{";".join(styles)}">{out}</span>'
    return out


def trim_trailing_blanks(rows: list) -> list:
    """Drop the empty lines a note ends with. Samsung does not draw them, and
    at full size each one is a line of blank paper - enough of them push an
    otherwise single-page note onto a second, empty, PDF page. A blank line
    that still carries a bullet or checkbox is an empty list item, so it
    stays."""
    while rows and not rows[-1]["text"].strip() and not rows[-1]["marker"]:
        rows.pop()
    return rows


def text_object_to_html(t: TextObject, style: RenderStyle = RenderStyle()) -> str:
    """Render one text object's lines, markers and alignment."""
    if not t.text:
        return ""
    attrs, fg, bg, size = char_attributes(t)
    out, start = [], 0
    for row in trim_trailing_blanks(lines_with_paragraph_info(t)):
        end = start + len(row["text"])
        body = runs_to_html(t.text, attrs, fg, bg, size, start, end, style)

        classes = ["line"]
        styles = []
        if row["indent"]:
            styles.append(f"margin-left:{row['indent'] * style.indent_em:g}em")
        align = ALIGNMENTS.get(row["align"])
        if align and align != "left":
            styles.append(f"text-align:{align}")
        bullet = row["bullet"]
        if bullet and bullet["style"] == 2 and bullet["checked"]:
            classes.append("checked")      # Samsung fades and strikes these

        style_attr = f' style="{";".join(styles)}"' if styles else ""
        if row["marker"]:
            out.append(
                f'<div class="{" ".join(classes)} listline"{style_attr}>'
                f'<span class="marker">{html.escape(row["marker"])}</span>'
                f'<span class="content">{body or "&nbsp;"}</span></div>'
            )
        else:
            out.append(
                f'<div class="{" ".join(classes)}"{style_attr}>'
                f'{body or "&nbsp;"}</div>'
            )
        start = end + 1                    # skip the newline
    return "\n".join(out)


def page_canvas_html(page: dict, doc_background: str, for_pdf: bool) -> str:
    """Absolutely-positioned text boxes for one page, preserving rotation.
    Positions are percentages so the canvas can be any width; ink and images
    on the page are not drawn.

    For PDF the canvas is given a concrete size in points and its text is
    scaled to match, because WeasyPrint resolves neither container query units
    nor a percentage top against an aspect-ratio height."""
    if not page["texts"]:
        return ""
    w, h = page["width"], page["height"]

    # Each page carries its own background, so a page may be darker or lighter
    # than the note around it. Set both colours on the canvas and tell the run
    # emitter which text colour it can leave to the stylesheet.
    background = page["background"] or doc_background
    fg = theme_for(background)["fg"]
    style = (RenderStyle(unit="pt", scale=PDF_CANVAS_SCALE, default_fg=fg)
             if for_pdf else RenderStyle(unit="cqw", default_fg=fg))

    boxes = []
    for t in page["texts"]:
        left, top, right, bottom = t.rect
        bw, bh = max(right - left, 1.0), max(bottom - top, 1.0)
        box_style = [
            f"left:{left / w * 100:.3f}%",
            f"top:{top / h * 100:.3f}%",
            f"width:{bw / w * 100:.3f}%",
        ]
        if t.angle:
            px, py = t.pivot if t.pivot else (left + bw / 2, top + bh / 2)
            box_style.append(f"transform:rotate({t.angle:g}deg)")
            box_style.append(
                f"transform-origin:{(px - left) / bw * 100:.2f}% "
                f"{(py - top) / bh * 100:.2f}%"
            )
        boxes.append(
            f'<div class="textbox" style="{";".join(box_style)}">'
            f"{text_object_to_html(t, style)}</div>"
        )

    if for_pdf:
        size_css = (f"width:{PDF_CANVAS_WIDTH_PT:g}pt;"
                    f"height:{PDF_CANVAS_WIDTH_PT * h / w:.1f}pt")
    else:
        size_css = f"aspect-ratio:{w} / {h}"

    return (
        f'<div class="page" style="{size_css};'
        f'background:{background};color:{fg}">'
        + "".join(boxes)
        + "</div>"
    )


CSS = Template("""
/* Deliberately not viewer-themed. This reproduces a document whose colours are
   absolute - Samsung stores the paper colour and opaque highlights - so the
   palette comes from the note itself, via the :root block above. */
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2.5rem 1.5rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Helvetica Neue", Arial, sans-serif;
  font-size: ${body_pt}pt; line-height: ${line_height};
  color: var(--fg); background: var(--bg);
}
/* The chrome is sized in em so it stays in proportion to the note's own text. */
.doc { max-width: 46rem; margin: 0 auto; }
h1.note-title { font-size: 1.667em; font-weight: 700; margin: 0 0 1.5rem; }
.line { white-space: pre-wrap; word-wrap: break-word; min-height: ${line_height}em; }
.listline { display: flex; align-items: baseline; gap: .55em; }
.listline .marker {
  flex: 0 0 auto; min-width: 1.2em; text-align: right;
  font-variant-numeric: tabular-nums;
}
.listline .content { flex: 1 1 auto; white-space: pre-wrap; }
.checked { opacity: .55; text-decoration: line-through; }
.inline-obj { opacity: .45; }
.notice {
  max-width: 46rem; margin: 0 auto 1.75rem; padding: .7rem .9rem;
  border-left: 3px solid var(--notice-border); background: var(--notice-bg);
  font-size: .875em; line-height: 1.45; color: var(--notice-fg);
}
.notice strong { display: block; margin-bottom: .2rem; }
.pages { max-width: 46rem; margin: 2.5rem auto 0; }
.pages h2 {
  font-size: .833em; text-transform: uppercase; letter-spacing: .08em;
  color: #8a8a8a; font-weight: 600; margin: 0 0 .75rem;
}
.page {
  position: relative; width: 100%; margin: 0 auto 1.5rem;
  container-type: inline-size;             /* text below scales in cqw */
  border: 1px solid var(--border); border-radius: 2px;
  overflow: hidden;
  /* its own background and text colour are set inline, per page */
}
.textbox {
  position: absolute;
  font-size: ${textbox_cqw}cqw;            /* the default size, of 600pt */
  line-height: ${line_height};
}
.textbox .line { min-height: 0; }
@media print {
  /* A light note prints on the paper's own white; a dark one has to keep its
     background or the light text prints invisibly. */
  body { background: var(--print-bg); padding: 0;
         print-color-adjust: var(--print-adjust); }
  .notice { border-left-color: var(--print-notice-border);
            background: var(--print-notice-bg); color: var(--print-notice-fg); }
}
""").substitute(
    body_pt=f"{DEFAULT_PT * SAMSUNG_PT_SCALE:g}",
    line_height=f"{LINE_HEIGHT:g}",
    textbox_cqw=f"{DEFAULT_PT * SAMSUNG_PT_SCALE / PAGE_WIDTH_PT * 100:.3f}",
)

# Appended only for PDF. WeasyPrint paginates the document itself, so the page
# box, and the sizes the canvas was laid out against, are pinned here. The
# paper colour goes on the page box, not just on body, so a dark note's margins
# are dark too instead of framing it in white.
PDF_CSS = f"""
@page {{ size: A4; margin: {PDF_PAGE_MARGIN_PT:g}pt; background: var(--print-bg); }}
.doc, .notice, .pages {{ max-width: none; }}
.page {{
  container-type: normal;                  /* WeasyPrint has no container queries */
  width: {PDF_CANVAS_WIDTH_PT:g}pt;
  break-inside: avoid; page-break-inside: avoid;
}}
.textbox {{ font-size: {DEFAULT_PT * SAMSUNG_PT_SCALE * PDF_CANVAS_SCALE:g}pt; }}
"""


def theme_css(background: str) -> str:
    """The palette block for one note: the paper colour out of the file, and
    the text and chrome colours that go with it."""
    theme = theme_for(background)
    dark = is_dark(background)
    decls = {
        "color-scheme": "dark" if dark else "light",
        "--bg": background,
        "--fg": theme["fg"],
        "--border": theme["border"],
        "--notice-fg": theme["notice_fg"],
        "--notice-bg": theme["notice_bg"],
        "--notice-border": theme["notice_border"],
        "--print-bg": background if dark else "#fff",
        "--print-adjust": "exact" if dark else "economy",
        "--print-notice-fg": theme["print_notice_fg"],
        "--print-notice-bg": theme["print_notice_bg"],
        "--print-notice-border": theme["print_notice_border"],
    }
    body = "".join(f"\n  {k}: {v};" for k, v in decls.items())
    return f":root {{{body}\n}}\n"


def shows_warning(note: Note, included: Included) -> bool:
    """Whether the not-rendered notice goes into this note's export. Normally
    that is what --include-warning asks for, but a note with no typed text at
    all is exported from its notice alone - without it the file would be empty
    with nothing on it to say why."""
    return bool(note.skipped) and (included.warning or not note.has_text)


def notice_html(items: list) -> str:
    if not items:
        return ""
    lis = "".join(f"<li>{html.escape(b)}</li>" for b in items)
    return (
        '<div class="notice"><strong>Text-only export — some content '
        "was not rendered</strong>"
        f"<ul>{lis}</ul></div>"
    )


def render_html(note: Note, for_pdf: bool = False,
                included: Included = Included()) -> str:
    """The complete HTML document for one note. This is also the input the PDF
    renderer works from, with `for_pdf` swapping in the paginated stylesheet.

    The heading and the not-rendered notice are only written when `included`
    asks for them - Samsung's own export prints neither. The <title> in the
    head stays regardless: it names the browser tab and the PDF's metadata
    rather than appearing on the page."""
    doc_fg = theme_for(note.background)["fg"]
    stylesheet = theme_css(note.background) + CSS + (PDF_CSS if for_pdf else "")

    heading = (f'<h1 class="note-title">{html.escape(note.doc_title)}</h1>'
               if included.title else "")
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{html.escape(note.doc_title)}</title>",
        f"<style>{stylesheet}</style></head><body>",
        notice_html(note.skipped) if shows_warning(note, included) else "",
        '<div class="doc">',
        heading,
        text_object_to_html(note.body, RenderStyle(default_fg=doc_fg)),
        "</div>",
    ]

    canvases = [page_canvas_html(p, note.background, for_pdf) for p in note.pages]
    canvases = [c for c in canvases if c]
    if canvases:
        parts.append('<div class="pages"><h2>Text boxes placed on pages</h2>')
        parts.extend(canvases)
        parts.append("</div>")

    parts.append("</body></html>")
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Plain text emission
# ---------------------------------------------------------------------------

TEXT_INDENT = "    "


# The attributes plain text keeps, as short tags, outermost first. Colour,
# highlight and size have no equivalent and are dropped.
TEXT_TAGS = (("Strikethrough", "s"), ("Bold", "b"), ("Italic", "i"),
             ("Underline", "u"))


def mark_formatting(text: str, attrs, lo: int, hi: int) -> str:
    """One line's text with its character formatting wrapped in <s>, <b>, <i>
    and <u>. Tags are opened and closed within the line, so every line stands
    on its own, and they always nest in TEXT_TAGS order so identical formatting
    always comes out written the same way."""
    pieces, i = [], lo
    while i < hi:
        current = attrs[i]
        j = i
        while j < hi and attrs[j] == current:
            j += 1
        chunk = text[i:j]
        for name, tag in reversed(TEXT_TAGS):      # innermost wrapped first
            if name in current:
                chunk = f"<{tag}>{chunk}</{tag}>"
        pieces.append(chunk)
        i = j
    return "".join(pieces)


def strike_line(line: str) -> str:
    """A ticked checkbox's text, struck out. Samsung draws a checked item
    crossed off, and that is the state of the item rather than a flourish, so
    plain text says so too - outside the tick, which is not itself struck."""
    if not line.strip():
        return line
    if line.startswith("<s>") and line.endswith("</s>"):
        return line                                # already struck end to end
    return f"<s>{line}</s>"


def text_object_lines(t: TextObject) -> list:
    """One text object as plain lines, keeping bullets, checkboxes, indentation
    and character formatting - as much as plain text can carry."""
    if not t.text:
        return []
    attrs, _fg, _bg, _size = char_attributes(t)
    out, start = [], 0
    for row in lines_with_paragraph_info(t):
        end = start + len(row["text"])
        line = mark_formatting(t.text, attrs, start, end)
        line = line.replace(INLINE_OBJECT_MARKER, "[object]")
        bullet = row["bullet"]
        if bullet and bullet["style"] == 2 and bullet["checked"]:
            line = strike_line(line)
        if row["marker"]:
            line = f"{row['marker']} {line}".rstrip()
        out.append(TEXT_INDENT * row["indent"] + line if line else "")
        start = end + 1                    # skip the newline
    return out


def extract_text(note: Note, included: Included = Included()) -> str:
    """The note as plain UTF-8 text: the body, then any page text boxes, with
    the title and the not-rendered notice heading it only when `included` asks
    for them."""
    out = []
    if included.title:
        out += [note.doc_title, "=" * len(note.doc_title), ""]

    if shows_warning(note, included):
        out.append("[Text-only export — some content was not rendered]")
        out.extend(f"  - {item}" for item in note.skipped)
        out.append("")

    out.extend(text_object_lines(note.body))

    numbered = [(i, p) for i, p in enumerate(note.pages, 1) if p["texts"]]
    for i, page in numbered:
        out.extend(["", f"--- Page {i}: text boxes placed on the page ---", ""])
        for j, t in enumerate(page["texts"]):
            if j:
                out.append("")
            out.extend(text_object_lines(t))

    return "\n".join(out).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# PDF emission
# ---------------------------------------------------------------------------


class PdfBackendMissing(RuntimeError):
    """WeasyPrint is not importable. Carries the instructions to fix that."""


PDF_INSTALL_HELP = textwrap.dedent(
    """\
    PDF output needs WeasyPrint, which is not installed (or cannot load its
    native libraries).

        pip install weasyprint

    WeasyPrint also needs Pango, cairo and GDK-PixBuf on the system:
        macOS          brew install pango libffi
        Debian/Ubuntu  sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libffi-dev
        Fedora         sudo dnf install pango
        Windows        see https://doc.courtbouillon.org/weasyprint/stable/first_steps.html

    Everything else (--html, --txt) works without it."""
)


def load_pdf_backend():
    """Import WeasyPrint on demand. Kept out of module import so the parser,
    HTML and text paths have no hard dependency on it - and so a missing or
    half-installed WeasyPrint reports how to fix itself instead of a traceback.

    OSError is caught alongside ImportError because WeasyPrint imports fine and
    then fails on its native libraries when Pango/cairo are absent."""
    try:
        from weasyprint import HTML  # noqa: PLC0415  (deliberately lazy)
    except (ImportError, OSError) as exc:
        raise PdfBackendMissing(f"{PDF_INSTALL_HELP}\n\n(import failed: {exc})")
    return HTML


def render_pdf(markup: str, dest: Path, base_url: Path = None) -> Path:
    """Write `markup` to `dest` as PDF. The HTML export is the single source of
    layout; nothing here draws the document a second time."""
    HTML = load_pdf_backend()
    dest.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=markup, base_url=str(base_url or Path.cwd())).write_pdf(dest)
    return dest


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_inputs(root: Path, recursive: bool) -> list:
    """Every note under `root`, covering the three input shapes:

      * a single NOTE.sdocx file, or a single already-unzipped NOTE.sdocx/ dir
      * a flat folder of .sdocx files
      * a folder of unzipped NOTE.sdocx/ directories

    An unzipped note is never descended into, so its media/ and .page members
    are not mistaken for further inputs."""
    if root.is_file():
        return [root]
    if is_extracted_sdocx(root):
        return [root]

    found = []

    def scan(directory: Path) -> None:
        for entry in sorted(directory.iterdir()):
            if entry.is_dir():
                if is_extracted_sdocx(entry):
                    found.append(entry)
                elif recursive:
                    scan(entry)
            elif entry.is_file() and entry.suffix.lower() == ".sdocx":
                found.append(entry)

    scan(root)
    return found


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class _ConsoleFilter(logging.Filter):
    """Lets records marked file_only through to the log file alone."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "file_only", False)


class _PrefixFilter(logging.Filter):
    """Names the library a borrowed record came from."""

    def __init__(self, prefix: str):
        super().__init__()
        self.prefix = prefix

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = f"{self.prefix}{record.msg}"
        return True


# Declarations WeasyPrint is known to ignore, and which PDF_CSS exists to
# replace: the container-query layout the HTML export uses for page canvases,
# and a colour-adjust hint the @page background makes unnecessary. Warning
# about these on every note would only train the reader to ignore the log.
EXPECTED_WEASYPRINT_WARNINGS = ("container-type", "cqw", "print-color-adjust")


class _ExpectedCssFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not (
            message.startswith("Ignored")
            and any(t in message for t in EXPECTED_WEASYPRINT_WARNINGS)
        )


def adopt_weasyprint_logger() -> None:
    """Send WeasyPrint's own warnings - unsupported CSS, unreachable resources -
    to the run log instead of letting them escape to stderr unrecorded."""
    wp = logging.getLogger("weasyprint")
    wp.setLevel(logging.WARNING)
    wp.handlers = list(log.handlers)
    wp.filters = [_ExpectedCssFilter(), _PrefixFilter("weasyprint: ")]
    wp.propagate = False


def setup_logging(log_file: Path, console_level: int) -> Path:
    """Console gets plain messages; the log file gets timestamps, levels and
    full tracebacks, appended so a run's history survives the next run."""
    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter("%(message)s"))
    console.addFilter(_ConsoleFilter())
    log.addHandler(console)

    if log_file is None:
        return None

    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-8s %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
    )
    log.addHandler(handler)
    return log_file


def log_run_header(argv: list, log_file: Path) -> None:
    started = datetime.now().isoformat(sep=" ", timespec="seconds")
    log.info("=" * 72, extra={"file_only": True})
    log.info("run started %s", started, extra={"file_only": True})
    log.info("command: %s", " ".join([Path(sys.argv[0]).name] + list(argv)),
             extra={"file_only": True})
    if log_file:
        log.debug("logging to %s", log_file)


# ---------------------------------------------------------------------------
# Conversion driver
# ---------------------------------------------------------------------------


@dataclass
class Tally:
    converted: int = 0
    skipped: int = 0
    failed: int = 0
    written: int = 0


class _AlreadyDone(Exception):
    """Every output for this note exists and --skip-existing was given."""


def output_paths(note_path: Path, out_dir: Path, formats: list) -> dict:
    """Where each requested format is written for one input. The extension is
    appended rather than substituted, so `2024.05.trip.sdocx` keeps its whole
    name instead of becoming `2024.05.html`."""
    directory = out_dir or note_path.parent
    return {fmt: directory / f"{note_path.stem}.{fmt}" for fmt in formats}


def convert_one(note_path: Path, out_dir: Path, formats: list,
                skip_existing: bool, included: Included = Included()) -> list:
    """Parse one note and write every requested format. Returns the paths
    written. Raises on a parse failure; the caller decides whether that ends
    the batch (it does not)."""
    dests = output_paths(note_path, out_dir, formats)

    if skip_existing and all(d.exists() for d in dests.values()):
        raise _AlreadyDone(", ".join(d.name for d in dests.values()))

    note = parse_sdocx(note_path)
    # Neither the title nor the list of unrendered content goes into the
    # exports unless asked for, so both are reported here - a note whose
    # filename differs from its title, or one that is mostly handwriting,
    # would otherwise leave no trace of what the export does not contain.
    log.info("  title: %s%s", note.title.text.strip() or "(none)",
             "" if included.title else " (not written to the output)")
    if note.skipped:
        log.info("  not rendered: %s%s", "; ".join(note.skipped),
                 "" if shows_warning(note, included)
                 else " (not flagged in the output)")
    if not note.has_text:
        log.info("  no typed text in this note - exporting its notice only")
    log.debug("  parsed %s: %d page(s), %d character(s) of body text",
              note_path.name, len(note.pages), len(note.body.text))

    written = []
    for fmt, dest in dests.items():
        if skip_existing and dest.exists():
            log.info("  skip  %s (exists)", dest.name)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "html":
            dest.write_text(render_html(note, included=included),
                            encoding="utf-8")
        elif fmt == "txt":
            dest.write_text(extract_text(note, included=included),
                            encoding="utf-8")
        elif fmt == "pdf":
            render_pdf(render_html(note, for_pdf=True, included=included),
                       dest, base_url=note_path.parent)
        written.append(dest)
        log.info("  wrote %s", dest)
    return written


def run_batch(targets: list, out_dir: Path, formats: list,
              skip_existing: bool, included: Included = Included()) -> Tally:
    """Convert every target. A file that fails is logged with its traceback
    and the batch carries on to the next one."""
    tally = Tally()
    total = len(targets)
    for n, path in enumerate(targets, 1):
        kind = "directory" if path.is_dir() else "file"
        log.info("[%d/%d] %s (%s)", n, total, path, kind)
        try:
            written = convert_one(path, out_dir, formats, skip_existing,
                                  included)
        except _AlreadyDone as exc:
            tally.skipped += 1
            log.info("  skip  all outputs exist (%s)", exc)
        except Exception as exc:
            tally.failed += 1
            # Console gets the one-line reason, the log file the traceback.
            log.error("  FAIL  %s: %s: %s", path.name, type(exc).__name__, exc)
            log.debug("traceback for %s", path, exc_info=True)
        else:
            tally.converted += 1
            tally.written += len(written)
    return tally


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

EPILOG = """\
examples:
  # one note, every format, written next to the input
  sdocx_parser.py notes/raven.sdocx --all

  # a flat folder of .sdocx files -> HTML and plain text in out/
  sdocx_parser.py samples/sdocx/ --html --txt -o out/

  # a folder of already-unzipped NOTE.sdocx/ directories -> PDF
  sdocx_parser.py extracted/ --pdf -o out/

  # one already-unzipped note
  sdocx_parser.py extracted/raven.sdocx/ --html

  # search subfolders too, and do not redo notes already converted
  sdocx_parser.py archive/ -r --all -o out/ --skip-existing

  # head each output with the note's title and what could not be rendered
  sdocx_parser.py notes/ --html --include-title --include-warning

An export carries the note itself: no title heading, and no notice about the
ink strokes, images and embedded PDFs that are never rendered. Pass
--include-title and --include-warning to put those on the page - the run log
records both on every run regardless, so nothing is dropped silently. PDF
output requires WeasyPrint (pip install weasyprint).
"""


def wrap_for_help(text: str) -> str:
    """RawDescriptionHelpFormatter keeps the epilog's example commands laid out
    as written, but it would also leave the description as one long line."""
    width = max(40, min(shutil.get_terminal_size().columns, 100) - 2)
    return textwrap.fill(text, width)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="sdocx_parser.py",
        description=wrap_for_help(
            "Convert Samsung Notes .sdocx files to HTML, PDF and/or plain "
            "text, preserving character formatting (bold, italic, underline, "
            "strikethrough, colour, highlight, size), list formatting "
            "(bullets, numbering, checkboxes, indent, alignment) and the "
            "note's own page colours. Accepts a single note, a folder of "
            "notes, or folders that .sdocx archives were already unzipped "
            "into. Every run is logged."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "input", type=Path,
        help="a .sdocx file, a folder of .sdocx files, or a folder holding "
             "unzipped NOTE.sdocx/ directories (the unzipped directory "
             "itself also works)",
    )

    fmt = ap.add_argument_group(
        "output formats",
        "Pick any combination. With none given, --html is assumed.",
    )
    fmt.add_argument("--html", action="store_true",
                     help="write a styled .html file per note (the default, "
                          "and the layout the PDF is rendered from)")
    fmt.add_argument("--pdf", action="store_true",
                     help="write a paginated .pdf per note by rendering the "
                          "HTML with WeasyPrint; needs `pip install weasyprint`")
    fmt.add_argument("--txt", action="store_true",
                     help="write a plain-text .txt per note, keeping bullets, "
                          "checkboxes and indentation. Bold, italic, underline "
                          "and strikethrough are marked with <b>, <i>, <u> and "
                          "<s> tags, and a ticked checkbox's text is struck "
                          "out the way Samsung draws it; colour, highlight and "
                          "font size have no plain-text equivalent and are "
                          "dropped")
    fmt.add_argument("--all", action="store_true",
                     help="shorthand for --html --pdf --txt")

    out = ap.add_argument_group("output and traversal")
    out.add_argument("-o", "--output-dir", type=Path, default=None, metavar="DIR",
                     help="write every output here (created if needed); by "
                          "default each output sits beside its input")
    out.add_argument("-r", "--recursive", action="store_true",
                     help="descend into subfolders when the input is a folder; "
                          "unzipped NOTE.sdocx/ directories are treated as "
                          "notes, never traversed")
    out.add_argument("--skip-existing", action="store_true",
                     help="leave outputs that already exist alone instead of "
                          "overwriting them; useful to resume a long batch")
    out.add_argument("--include-title", action="store_true",
                     help="put the note's title at the top of each output. "
                          "Off by default, matching Samsung's own export, "
                          "which prints the body only; the title is written "
                          "to the log either way, and stays in the HTML "
                          "<title> tag so the browser tab and the PDF "
                          "metadata still name the note")
    out.add_argument("--include-warning", action="store_true",
                     help="put the notice listing what was not rendered - ink "
                          "strokes, images, embedded PDFs - at the top of each "
                          "output. Off by default, except for a note with no "
                          "typed text at all, which always keeps its notice "
                          "rather than exporting as an empty file; the same "
                          "list goes to the log on every run, so nothing is "
                          "dropped silently either way")

    logging_group = ap.add_argument_group("logging")
    logging_group.add_argument(
        "--log-file", type=Path, default=Path(DEFAULT_LOG_NAME), metavar="PATH",
        help=f"append this run's log here (default: ./{DEFAULT_LOG_NAME}); the "
             "log records every file parsed, skipped or failed, with full "
             "tracebacks for failures",
    )
    logging_group.add_argument("--no-log-file", action="store_true",
                               help="log to the console only, writing no log file")
    logging_group.add_argument("-v", "--verbose", action="store_true",
                               help="also print the detail that normally only "
                                    "goes to the log file")
    logging_group.add_argument("-q", "--quiet", action="store_true",
                               help="print warnings and errors only; the log "
                                    "file still gets everything")
    return ap


def selected_formats(args) -> list:
    """The requested formats, in the order they are written. Defaults to HTML
    so that the bare `sdocx_parser.py NOTE.sdocx` still does something useful."""
    if args.all:
        return ["html", "pdf", "txt"]
    picked = [f for f in ("html", "pdf", "txt") if getattr(args, f)]
    return picked or ["html"]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)

    console_level = (logging.DEBUG if args.verbose
                     else logging.WARNING if args.quiet
                     else logging.INFO)
    log_file = None if args.no_log_file else args.log_file
    try:
        log_file = setup_logging(log_file, console_level)
    except OSError as exc:
        print(f"error: cannot open log file {args.log_file}: {exc}",
              file=sys.stderr)
        return 2
    log_run_header(argv, log_file)

    formats = selected_formats(args)

    if not args.input.exists():
        log.error("error: %s does not exist", args.input)
        return 2

    if args.input.is_file() and args.input.suffix.lower() != ".sdocx":
        log.error("error: %s is not a .sdocx file", args.input)
        return 2

    # Fail on a missing PDF backend before any work is done - but only give up
    # entirely if PDF was the only thing asked for.
    pdf_unavailable = False
    if "pdf" in formats:
        try:
            load_pdf_backend()
        except PdfBackendMissing as exc:
            log.error("error: %s", exc)
            if formats == ["pdf"]:
                return 2
            formats = [f for f in formats if f != "pdf"]
            pdf_unavailable = True
            log.warning("continuing without PDF output; writing %s",
                        ", ".join(formats))
        else:
            adopt_weasyprint_logger()

    targets = discover_inputs(args.input, args.recursive)
    if not targets:
        hint = "" if args.recursive else " (try --recursive)"
        log.error("error: no .sdocx files or unzipped .sdocx directories "
                  "in %s%s", args.input, hint)
        return 2

    log.info("%d note(s) to convert; formats: %s",
             len(targets), ", ".join(formats))
    tally = run_batch(targets, args.output_dir, formats, args.skip_existing,
                      Included(title=args.include_title,
                               warning=args.include_warning))

    summary = (f"done: {tally.converted} converted, {tally.skipped} skipped, "
               f"{tally.failed} failed; {tally.written} file(s) written")
    (log.warning if tally.failed else log.info)(summary)
    if pdf_unavailable:
        log.warning("no PDFs were written: WeasyPrint is not installed")
    if log_file:
        log.info("log written to %s", log_file)

    # Non-zero whenever something asked for did not get produced.
    return 1 if tally.failed or pdf_unavailable else 0


if __name__ == "__main__":
    raise SystemExit(main())
