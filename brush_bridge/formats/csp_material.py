"""Generate Clip Studio Paint image materials (brush tips / paper textures) from scratch.

A CSP image material is a folder (catalog.zip, info.zip, data/material_0.layer,
thumbnail/thumbnail.png, icedata/layerData.xml) that a .sut embeds as a tar in
``MaterialFile.FileData``. ``material_0.layer`` is a C2F container around a SQLite database
holding a one-layer canvas. CSP encrypts the first C2F data chunk, but every ``dATA`` chunk
carries a 2-byte flag (``01 00`` = encrypted, ``00 00`` = plain) and the remainder of each
file is already written plain, so we write every chunk plain. See docs/formats/csp.md.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import os
import secrets
import sqlite3
import struct
import tarfile
import tempfile
import zipfile
import zlib
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

from .csp_binary import C2F_MAGIC, MaterialRef

TILE = 256
PAGE_SIZE = 4096
FIRST_CHUNK = 5120  # CSP splits the database after page 1 + 1 KiB of page 2
MAX_SIDE = 4096
THUMB_SIDE = 300
TOOL_VERSION = "5.1.2"

KIND_TIP = "tip"
KIND_TEXTURE = "texture"


@lru_cache(maxsize=1)
def _template() -> dict:
    return json.loads((Path(__file__).with_name("csp_material_template.json")).read_text(encoding="utf-8"))


def new_uuid() -> str:
    """CSP style uuid: 32 hex digits grouped 10-4-4-4-10."""
    h = secrets.token_hex(16)
    return f"{h[:10]}-{h[10:14]}-{h[14:18]}-{h[18:22]}-{h[22:]}"


# ---------------------------------------------------------------------------------------------
# Offscreen blobs (tiled 8-bit pixel storage)
# ---------------------------------------------------------------------------------------------
def _ustr(s: str) -> bytes:
    return struct.pack(">I", len(s)) + s.encode("utf-16-be")


def _section(name: str, words: list[int]) -> bytes:
    return _ustr(name) + struct.pack(f">{len(words)}I", *words)


def _offscreen_params(w: int, h: int, color: bool) -> list[int]:
    bx, by = -(-w // TILE), -(-h // TILE)
    if color:  # 5-channel (BGRA + mask) render buffer of the root folder
        return [w, h, bx, by, 33, 1, 4, 5, TILE * TILE, 4, 1024, 1, TILE, TILE * TILE, TILE, TILE, 8, 8, 0, 0]
    return [w, h, bx, by, 1, 1, 0, 1, 0, 0, 0, 1, TILE, 0, TILE, TILE, 8, 8, 0, 0]


def offscreen_attribute(w: int, h: int, color: bool, block_sizes: list[int]) -> bytes:
    secs = [_section("Parameter", _offscreen_params(w, h, color)),
            _section("InitColor", [20, 0, 0, 0, 4]),
            _section("BlockSize", [12, len(block_sizes), 4, *block_sizes])]
    return struct.pack(">4I", 16, *(len(s) for s in secs)) + b"".join(secs)


_BEGIN, _END = _ustr("BlockDataBeginChunk"), _ustr("BlockDataEndChunk")


def offscreen_blocks(tiles: list[bytes | None], channels: int) -> tuple[bytes, list[int]]:
    """Encode tiles (raw TILExTILE bytes or None for an empty block) -> (BlockData, block sizes)."""
    out, sizes, status, sums = [], [], [], []
    for i, raw in enumerate(tiles):
        head = struct.pack(">5I", i, TILE * TILE * channels, TILE, TILE, 0 if raw is None else 1)
        body = b""
        if raw is not None:
            comp = zlib.compress(raw, 1)
            payload = struct.pack("<I", len(comp)) + comp
            body = struct.pack(">I", len(payload)) + payload
            sums.append(zlib.adler32(payload))
        else:
            sums.append(0)
        status.append(0 if raw is None else 1)
        rec = _BEGIN + head + body + _END
        rec = struct.pack(">I", 4 + len(rec)) + rec
        out.append(rec)
        sizes.append(len(rec))
    tail = (_section("BlockStatus", [12, len(tiles), 4, *status])
            + _section("BlockCheckSum", [12, len(tiles), 4, *sums]))
    return b"".join(out) + tail, sizes


def _tiles(ink: np.ndarray) -> list[bytes]:
    h, w = ink.shape
    bx, by = -(-w // TILE), -(-h // TILE)
    pad = np.zeros((by * TILE, bx * TILE), np.uint8)
    pad[:h, :w] = ink
    return [pad[y * TILE:(y + 1) * TILE, x * TILE:(x + 1) * TILE].tobytes()
            for y in range(by) for x in range(bx)]


def _offscreen(w: int, h: int, color: bool, tiles: list[bytes | None] | None = None) -> tuple[bytes, bytes]:
    n = (-(-w // TILE)) * (-(-h // TILE))
    data, sizes = offscreen_blocks(tiles or [None] * n, 5 if color else 1)
    return offscreen_attribute(w, h, color, sizes), data


def resizable_image_info(w: int, h: int, texture: bool) -> bytes:
    b = bytearray(bytes.fromhex(_template()["resizable_image_info"]))
    struct.pack_into(">II", b, 16, *((1, 3) if texture else (0, 0)))
    struct.pack_into(">II", b, 32, w, h)
    struct.pack_into(">4d", b, 64, w / 2, h / 2, w / 2, h / 2)
    struct.pack_into(">d", b, 136, float(w))
    struct.pack_into(">d", b, 160, float(h))
    struct.pack_into(">2d", b, 168, float(w), float(h))
    return bytes(b)


# ---------------------------------------------------------------------------------------------
# Material database
# ---------------------------------------------------------------------------------------------
def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def _insert(con: sqlite3.Connection, table: str, values: list) -> None:
    cols = _columns(con, table)
    values = list(values) + [None] * (len(cols) - len(values))
    con.execute(f"INSERT INTO {table} VALUES ({','.join('?' * len(cols))})", values[:len(cols)])


def build_layer_db(ink: np.ndarray, name: str, texture: bool) -> bytes:
    """``ink``: uint8 HxW, 255 = full ink (CSP stores grey materials as ink density)."""
    t = _template()
    h, w = ink.shape
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        con = sqlite3.connect(tmp)
        try:
            con.execute(f"PRAGMA page_size={PAGE_SIZE}")
            con.execute("PRAGMA journal_mode=DELETE")
            for sql in t["schema"]:
                con.execute(sql)
            _insert(con, "Material", [1, name, 0, 0, 1, 0, 0, 0, 600, 20, 0, 45, 10, 0, 1, 0, 0, 0])
            rows = t["rows"]
            canvas = list(rows["Canvas"][0])
            canvas[3:5] = [w, h]
            _insert(con, "Canvas", canvas)
            preview = Image.new("RGBA", (w, h), (255, 255, 255, 0))
            _insert(con, "CanvasPreview", [1, 1, 1, 1, w, h, _png(preview)])
            cols = _columns(con, "Layer")
            for r in rows["Layer"]:
                r = list(r) + [None] * (len(cols) - len(r))
                r[cols.index("LayerUuid")] = new_uuid()
                if r[cols.index("MainId")] == 3:
                    r[cols.index("LayerName")] = name
                    r[cols.index("ResizableImageInfo")] = resizable_image_info(w, h, texture)
                _insert(con, "Layer", r)
            for r in rows["Mipmap"]:
                _insert(con, "Mipmap", r)
            for r in rows["MipmapInfo"]:
                _insert(con, "MipmapInfo", r)
            blobs = {3: _offscreen(w, h, True), 5: _offscreen(w, h, False),
                     7: _offscreen(w, h, False, _tiles(ink))}
            for r in rows["Offscreen"]:
                r = list(r)
                r[4], r[5] = blobs[r[1]]
                _insert(con, "Offscreen", r)
            for i, r in enumerate(t["ElemScheme"], 1):
                _insert(con, "ElemScheme", [i, *r])
            for i, r in enumerate(t["ParamScheme"], 1):
                _insert(con, "ParamScheme", [i, *r])
            con.execute("DELETE FROM sqlite_sequence")
            con.executemany("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", t["sequence"])
            con.commit()
        finally:
            con.close()
        return Path(tmp).read_bytes()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------
# C2F container
# ---------------------------------------------------------------------------------------------
def _chunk(tag: bytes, payload: bytes) -> bytes:
    return struct.pack("<I", len(payload)) + tag + payload + struct.pack("<I", zlib.crc32(tag + payload))


def c2f_wrap(data: bytes, split: int | None = None) -> bytes:
    """Plain C2F: HEAD, dATA (flag 00 00) [, dATA], TAIL."""
    parts = [data[:split], data[split:]] if split and len(data) > split else [data]
    return (C2F_MAGIC + _chunk(b"HEAD", b"") + b"".join(_chunk(b"dATA", b"\x00\x00" + p) for p in parts)
            + _chunk(b"TAIL", b""))


def c2f_unwrap(data: bytes) -> bytes:
    """Concatenate the plain dATA payloads (raises ValueError for encrypted chunks)."""
    from .csp_binary import c2f_chunks

    out = []
    for tag, payload in c2f_chunks(data):
        if tag == b"dATA":
            if payload[:2] != b"\x00\x00":
                raise ValueError("encrypted C2F chunk")
            out.append(payload[2:])
    return b"".join(out)


def _zip_c2f(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, text.encode("utf-8"))
    return c2f_wrap(buf.getvalue())


# ---------------------------------------------------------------------------------------------
# XML documents
# ---------------------------------------------------------------------------------------------
def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'


def catalog_xml(name: str, cat_uuid: str, item_uuid: str, ids: dict[str, str], when: _dt.datetime) -> str:
    d = when.strftime("%Y/%m/%d %H:%M:%S")
    m = 'modify_description="0" modify_info="0" modify_name="0" modify_thumbnail="0"'
    tool = f'<tool company="celsys" name="paint" version="{TOOL_VERSION}"/>'
    n = _esc(name)
    return (_XML_HEAD + "<archive>\n\n  <version>2.0.8</version>\n\n"
            f'  <catalog {m} uuid="{cat_uuid}">\n    <type>material</type>\n    <readonly>0</readonly>\n'
            f"    <share_edit_flag>0</share_edit_flag>\n    <uploadable>1</uploadable>\n    {tool}\n"
            f"    <name>{n}</name>\n    <groups>\n"
            f'      <group {m} parentuuid="" uuid="{new_uuid()}">\n        {tool}\n'
            '        <type ver="">material</type>\n        <name></name>\n        <required>0</required>\n'
            "        <reserveId>0</reserveId>\n        <reserveWait>0</reserveWait>\n        <items>\n"
            f'          <item {m} parentuuid="" uuid="{item_uuid}">\n            {tool}\n'
            f'            <type ver="1.0.1">image</type>\n            <name>{n}</name>\n'
            f"            <created_date>{d}</created_date>\n            <modified_date>{d}</modified_date>\n"
            "            <required>0</required>\n            <permittool>\n              <cspdebut>1</cspdebut>\n"
            "            </permittool>\n            <reserveId>0</reserveId>\n            <reserveWait>0</reserveWait>\n"
            f'            <thumbnail>\n              <fileref idref="{ids["thumb"]}"/>\n            </thumbnail>\n'
            f'            <data>\n              <fileref idref="{ids["data"]}"/>\n            </data>\n'
            f'            <icedata>\n              <fileref idref="{ids["ice"]}"/>\n            </icedata>\n'
            "          </item>\n        </items>\n      </group>\n    </groups>\n    <region/>\n  </catalog>\n\n"
            "  <files>\n"
            f'    <file document="false" encrypt="false" id="{ids["data"]}" mime="application/octet-stream">\n'
            "      <path>data/material_0.layer</path>\n    </file>\n"
            f'    <file document="false" encrypt="false" id="{ids["ice"]}" mime="text/xml">\n'
            "      <path>icedata/layerData.xml</path>\n    </file>\n"
            f'    <file document="false" encrypt="false" id="{ids["thumb"]}" mime="image/png">\n'
            "      <path>thumbnail/thumbnail.png</path>\n    </file>\n  </files>\n\n</archive>\n")


def info_xml(item_uuid: str) -> str:
    return (_XML_HEAD + "<infolist>\n\n  <version>2.0.0</version>\n\n"
            f'  <info uuid="{item_uuid}">\n    <datalist key="lead">\n      <data></data>\n    </datalist>\n'
            '    <datalist key="tag"/>\n    <datalist key="updateHistory">\n      <data></data>\n'
            "    </datalist>\n  </info>\n\n</infolist>\n")


_LAYER_KEYS = [("scaling", "0"), ("isTiling", "0"), ("howTiling", "0"), ("directionTiling", "0"),
               ("superPosition", "-1"), ("tone", "0"), ("toneLines", "60.000000"), ("concentrationType", "0"),
               ("concentration", "40.000000"), ("toneKind", "1"), ("toneAngle", "45.000000"),
               ("noizeSize", "10.000000"), ("noizeCoefficient", "0.000000"), ("useLayerColor", "0"),
               ("mainColorRed", "0"), ("mainColorGreen", "0"), ("mainColorBlue", "0"),
               ("subColorRed", "4294967295"), ("subColorGreen", "4294967295"), ("subColorBlue", "4294967295"),
               ("isChangeThumbnail", "0"), ("isChangeTitle", "0"), ("csauuid", "")]


def layer_data_xml(item_uuid: str, texture: bool) -> str:
    tag, folder = ("PaperTexture", "単色パターン:テクスチャ") if texture else ("BrushPattern", "画像素材:ブラシ")
    items = [("systemtag", tag), ("foldertag", folder), *_LAYER_KEYS]
    body = "".join(f'    <datalist key="{k}">\n      <data>{v}</data>\n    </datalist>\n' for k, v in items)
    return (_XML_HEAD + "<infolist>\n\n  <version>2.0.0</version>\n\n"
            f'  <info uuid="{item_uuid}">\n{body}  </info>\n\n</infolist>\n')


# ---------------------------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------------------------
def _fit(im: Image.Image, side: int) -> Image.Image:
    if max(im.size) <= side:
        return im
    s = side / max(im.size)
    return im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)


def ink_from_image(im: Image.Image, kind: str) -> np.ndarray:
    """Neutral image -> CSP ink density. Tips: L 255 = paint. Textures: L 255 = white (no ink)."""
    g = _fit(im.convert("L"), MAX_SIDE)
    a = np.asarray(g, dtype=np.uint8)
    return a.copy() if kind == KIND_TIP else (255 - a).astype(np.uint8)


def thumbnail_png(ink: np.ndarray) -> bytes:
    h, w = ink.shape
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    im.putalpha(Image.fromarray(ink))
    return _png(_fit(im, THUMB_SIDE))


class GeneratedMaterial:
    """A generated material: its tar bytes plus the reference a .sut Variant uses."""

    def __init__(self, name: str, kind: str, catalog_uuid: str, folder: tuple[str, str], tar: bytes):
        self.name, self.kind, self.catalog_uuid, self.folder, self.tar = name, kind, catalog_uuid, folder, tar

    @property
    def catalog_path(self) -> str:
        return f".:{self.folder[0]}:{self.folder[1]}:{self.catalog_uuid}"

    @property
    def layer_path(self) -> str:
        return self.catalog_path + ":data:material_0.layer"

    def ref(self) -> MaterialRef:
        return MaterialRef(path=self.layer_path, name=self.name, catalog=self.catalog_path, kind=2)

    def material_row(self) -> dict:
        return {"InstallFolder": 0, "OriginalPath": self.layer_path, "OldMaterial": 0, "FileData": self.tar,
                "CatalogPath": self.catalog_path, "MaterialUuid": ""}


def build_material(image: Image.Image, name: str, kind: str = KIND_TIP, split: bool = True,
                   when: _dt.datetime | None = None) -> GeneratedMaterial:
    """Create a CSP image material (as embedded in a .sut) from a neutral tip / texture image."""
    texture = kind == KIND_TEXTURE
    when = when or _dt.datetime.now()
    ink = ink_from_image(image, kind)
    db = build_layer_db(ink, name, texture)
    cat_uuid, item_uuid = new_uuid(), new_uuid()
    ids = {"data": new_uuid(), "ice": new_uuid(), "thumb": new_uuid()}
    members = [
        ("catalog.zip", _zip_c2f("catalog.xml", catalog_xml(name, cat_uuid, item_uuid, ids, when))),
        ("info.zip", _zip_c2f("info.xml", info_xml(item_uuid))),
        ("data/material_0.layer", c2f_wrap(db, FIRST_CHUNK if split else None)),
        ("thumbnail/thumbnail.png", thumbnail_png(ink)),
        ("icedata/layerData.xml", ("\ufeff" + layer_data_xml(item_uuid, texture)).encode("utf-8")),
    ]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for mname, data in members:
            ti = tarfile.TarInfo(mname)
            ti.size, ti.mode, ti.uname, ti.gname, ti.mtime = len(data), 0o777, "compress", "", int(when.timestamp())
            tf.addfile(ti, io.BytesIO(data))
    folder = (f"{secrets.randbelow(100):02d}", f"{secrets.randbelow(100):02d}")
    return GeneratedMaterial(name, kind, cat_uuid, folder, buf.getvalue())


def read_material_ink(layer_c2f: bytes) -> np.ndarray | None:
    """Decode the original-pixel offscreen of a plain (generated) material .layer -> ink array."""
    try:
        db = c2f_unwrap(layer_c2f)
    except ValueError:
        return None
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        Path(tmp).write_bytes(db)
        con = sqlite3.connect(tmp)
        try:
            orig = con.execute("SELECT o.Attribute, o.BlockData FROM Layer l JOIN Mipmap m ON m.MainId = "
                               "l.ResizableOriginalMipmap JOIN MipmapInfo i ON i.MainId = m.BaseMipmapInfo "
                               "JOIN Offscreen o ON o.MainId = i.Offscreen WHERE l.ResizableOriginalMipmap IS NOT NULL"
                               ).fetchone()
        finally:
            con.close()
    finally:
        os.unlink(tmp)
    if not orig:
        return None
    attr, data = orig
    w, h, bx, by = struct.unpack_from(">4I", attr, 16 + 4 + 18)
    img = np.zeros((by * TILE, bx * TILE), np.uint8)
    p = 0
    begin_len = len(_BEGIN)
    while p < len(data) and data[p + 4:p + 4 + begin_len] == _BEGIN:
        rs = struct.unpack_from(">I", data, p)[0]
        q = p + 4 + begin_len
        idx, _unc, _tw, _th, exists = struct.unpack_from(">5I", data, q)
        if exists:
            n = struct.unpack_from("<I", data, q + 24)[0]
            raw = np.frombuffer(zlib.decompress(data[q + 28:q + 28 + n]), np.uint8).reshape(TILE, TILE)
            y, x = divmod(idx, bx)
            img[y * TILE:(y + 1) * TILE, x * TILE:(x + 1) * TILE] = raw
        p += rs
    return img[:h, :w]
