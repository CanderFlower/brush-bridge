"""Binary sub-structures used inside Clip Studio Paint tool databases (.sut / .todb).

Everything here was reverse-engineered from files produced by CSP itself; see
``docs/formats/csp.md`` for the full write-up. All integers are big-endian.
"""
from __future__ import annotations

import io
import struct
import tarfile
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image

# ---------------------------------------------------------------------------------------------
# Effector blobs (BrushSizeEffector, BrushOpacityEffector, BrushFlowEffector, ...)
# ---------------------------------------------------------------------------------------------
# Layout (u32 words):
#   [0] header size in bytes (0x28 in old files, 0x2c in CSP 1.x-4.x exports, 0x30 in CSP 5.x)
#   [1] mask of inputs offered in the UI (0xf0 or 0x1f0)
#   [2] enabled inputs: 0x10 pen pressure, 0x20 tilt, 0x40 velocity, 0x80 random
#   [3] pressure minimum %   [4] tilt minimum %   [5] velocity minimum %   [6] random minimum %
#   [7] unknown (0)
#   [8] byte size of the pressure curve block that follows the header (0 = none)
#   [9] byte size of the tilt curve block that follows the pressure block (0 = none)
#   [10] (header >= 0x2c) tilt maximum % (100; size effectors use 150..500)
#   [11] (header >= 0x30) unknown, 0
# Curve block: u32 0x0c, u32 point count, u32 0x10 (bytes per point), then (x, y) float64 pairs.
# CSP dereferences the curve of every enabled curve input: an effector with pressure or tilt
# enabled but no matching curve block crashes CSP (5.x) as soon as the sub tool is imported.
# The tilt input is 0 with the pen lying flat and 1 with it upright.
EFF_PRESSURE, EFF_TILT, EFF_VELOCITY, EFF_RANDOM = 0x10, 0x20, 0x40, 0x80


@dataclass
class Effector:
    enabled: int = 0
    pressure_min: int = 0
    tilt_min: int = 0
    velocity_min: int = 0
    random_min: int = 0
    pressure_curve: list[tuple[float, float]] = field(default_factory=list)
    tilt_curve: list[tuple[float, float]] = field(default_factory=list)

    def influence(self, bit: int) -> float:
        """Neutral-model influence (0..1) of one input: 1 - minimum%."""
        if not self.enabled & bit:
            return 0.0
        mn = {EFF_PRESSURE: self.pressure_min, EFF_TILT: self.tilt_min,
              EFF_VELOCITY: self.velocity_min, EFF_RANDOM: self.random_min}[bit]
        return max(0.0, min(1.0, 1.0 - mn / 100.0))


def _curve(data: bytes, off: int, size: int) -> list[tuple[float, float]]:
    if size < 12 or off + size > len(data):
        return []
    _hdr, count, step = struct.unpack_from(">III", data, off)
    pts = []
    p = off + 12
    for _ in range(count):
        if step < 16 or p + 16 > off + size:
            break
        pts.append(struct.unpack_from(">dd", data, p))
        p += step
    return pts


def decode_effector(blob) -> Optional[Effector]:
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 40:
        return None
    hdr = struct.unpack_from(">I", blob, 0)[0]
    if hdr < 40 or hdr > len(blob):
        return None
    w = struct.unpack_from(">10i", blob, 0)
    e = Effector(enabled=w[2], pressure_min=w[3], tilt_min=w[4], velocity_min=w[5], random_min=w[6])
    if w[8] > 0:
        e.pressure_curve = _curve(blob, hdr, w[8])
    if w[9] > 0:
        e.tilt_curve = _curve(blob, hdr + max(w[8], 0), w[9])
    return e


def _curve_block(points: Optional[list[tuple[float, float]]]) -> bytes:
    pts = sorted((max(0.0, min(1.0, float(x))), max(0.0, min(1.0, float(y)))) for x, y in (points or []))
    if len(pts) < 2:
        pts = [(0.0, 0.0), (1.0, 1.0)]
    return struct.pack(">III", 0x0C, len(pts), 0x10) + b"".join(struct.pack(">dd", x, y) for x, y in pts)


def encode_effector(pressure: float = 0.0, tilt: float = 0.0, velocity: float = 0.0,
                    jitter: float = 0.0, *, pressure_curve: Optional[list[tuple[float, float]]] = None,
                    tilt_curve: Optional[list[tuple[float, float]]] = None,
                    random_min: Optional[int] = None) -> bytes:
    """Build a 0x2c-header effector (the layout CSP 1.x .. 5.x all read).

    ``random_min`` overrides the random input's minimum in percent (-100..100; CSP's colour
    jitter uses -100, i.e. a two-sided range). ``pressure_curve`` and ``tilt_curve`` default to a
    straight line; a curve block is always written for every enabled curve input (CSP crashes
    without it).
    """
    enabled = 0
    mins = []
    for bit, val in ((EFF_PRESSURE, pressure), (EFF_TILT, tilt), (EFF_VELOCITY, velocity), (EFF_RANDOM, jitter)):
        if val > 0:
            enabled |= bit
        mins.append(int(round(100 * (1.0 - max(0.0, min(1.0, val))))) if val > 0 else 0)
    if random_min is not None and enabled & EFF_RANDOM:
        mins[3] = int(max(-100, min(100, round(random_min))))
    pblock = _curve_block(pressure_curve) if enabled & EFF_PRESSURE else b""
    tblock = _curve_block(tilt_curve) if enabled & EFF_TILT else b""
    head = struct.pack(">11i", 0x2C, 0xF0, enabled, *mins, 0, len(pblock), len(tblock), 100)
    return head + pblock + tblock


# ---------------------------------------------------------------------------------------------
# Rotation effector (BrushRotationEffector, an INTEGER bit field, not a blob)
# ---------------------------------------------------------------------------------------------
# 0x01 always set; 0x02 follow line direction; 0x10 pressure; 0x20 tilt; 0x40 velocity; 0x80 random
ROT_BASE, ROT_DIRECTION, ROT_RANDOM = 0x01, 0x02, 0x80


# ---------------------------------------------------------------------------------------------
# Material references (BrushPatternImageArray, TextureImage, DualPatternImageArray, ...)
# ---------------------------------------------------------------------------------------------
# u32 8, u32 count, then per item:
#   u32 item size, u32 nbytes + UTF-16LE path (".:12:69:<uuid>:data:material_0.layer"),
#   u32 kind (2 = user material, 3 = bundled), u32 nbytes + UTF-16LE display name,
#   u32 1, u32 nbytes + UTF-16LE catalog path (".:12:69:<uuid>" / ".:Install:Paint019:<uuid>"),
#   u32 0
@dataclass
class MaterialRef:
    path: str
    name: str = ""
    catalog: str = ""
    kind: int = 2


def _ustr(data: bytes, p: int) -> tuple[str, int]:
    n = struct.unpack_from(">I", data, p)[0]
    p += 4
    return data[p:p + n].decode("utf-16-le", "replace").rstrip("\x00"), p + n


def decode_material_refs(blob) -> list[MaterialRef]:
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 8:
        return []
    out: list[MaterialRef] = []
    try:
        hdr, count = struct.unpack_from(">II", blob, 0)
        p = hdr
        for _ in range(count):
            size = struct.unpack_from(">I", blob, p)[0]
            end = p + size
            path, q = _ustr(blob, p + 4)
            ref = MaterialRef(path=path)
            if q + 8 <= end:
                ref.kind = struct.unpack_from(">I", blob, q)[0]
                ref.name, q = _ustr(blob, q + 4)
            if q + 8 <= end:
                ref.catalog, q = _ustr(blob, q + 4)
            out.append(ref)
            if size <= 0:
                break
            p = end
    except (struct.error, IndexError):
        pass
    return out


def encode_material_refs(refs: list[MaterialRef]) -> bytes:
    items = []
    for r in refs:
        def s(x: str) -> bytes:
            b = x.encode("utf-16-le")
            return struct.pack(">I", len(b)) + b
        body = s(r.path) + struct.pack(">I", r.kind) + s(r.name) + struct.pack(">I", 1) + s(r.catalog) + struct.pack(">I", 0)
        items.append(struct.pack(">I", 4 + len(body)) + body)
    return struct.pack(">II", 8, len(refs)) + b"".join(items)


def material_dir_parts(ref_path: str) -> list[str]:
    """'.:12:69:<uuid>:data:material_0.layer' -> ['12', '69', '<uuid>']."""
    parts = [p for p in ref_path.split(":") if p and p != "."]
    if "data" in parts:
        parts = parts[:parts.index("data")]
    return parts


# ---------------------------------------------------------------------------------------------
# MaterialFile.FileData: a POSIX tar of the material folder
# ---------------------------------------------------------------------------------------------
# Members: catalog.zip, info.zip (C2F, encrypted), data/material_0.layer (C2F, first chunk
# encrypted), thumbnail/thumbnail.png (plain PNG), icedata/layerData.xml (plain XML).
def material_members(file_data: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(file_data), mode="r:") as tf:
            for m in tf.getmembers():
                if m.isfile():
                    f = tf.extractfile(m)
                    if f is not None:
                        out[m.name.replace("\\", "/")] = f.read()
    except (tarfile.TarError, OSError):
        pass
    return out


MATERIAL_MEMBERS = ("catalog.zip", "info.zip", "data/material_0.layer", "thumbnail/thumbnail.png",
                    "icedata/layerData.xml")


def pack_material_folder(folder) -> Optional[bytes]:
    """Tar a material folder of the CSP material library the way CSP embeds it in a .sut.

    Returns None when the folder has no ``data`` payload (bundled materials whose data lives
    inside the CSP installation).
    """
    from pathlib import Path

    folder = Path(folder)
    layers = sorted((folder / "data").glob("*.layer")) if (folder / "data").is_dir() else []
    if not layers:
        return None
    names = [m for m in MATERIAL_MEMBERS if not m.startswith("data/")]
    names.insert(2, "data/" + layers[0].name)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name in names:
            p = folder / name
            if not p.is_file():
                continue
            data = p.read_bytes()
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.uname, ti.gname, ti.mtime = len(data), 0o777, "compress", "", int(p.stat().st_mtime)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def material_thumbnail(file_data: bytes) -> Optional[Image.Image]:
    for name, data in material_members(file_data).items():
        if name.endswith(".png") and "thumbnail" in name.lower():
            try:
                im = Image.open(io.BytesIO(data))
                im.load()
                return im
            except OSError:
                return None
    return None


def material_xml(file_data: bytes) -> str:
    data = material_members(file_data).get("icedata/layerData.xml", b"")
    return data.decode("utf-8-sig", "replace")


def thumbnail_to_tip(im: Image.Image) -> Image.Image:
    """CSP thumbnails are black ink on transparency -> L mask (255 = paint)."""
    rgba = im.convert("RGBA")
    alpha = rgba.getchannel("A")
    dark = rgba.convert("L").point(lambda v: 255 - v)
    from PIL import ImageChops

    return ImageChops.multiply(alpha, dark)


def thumbnail_to_texture(im: Image.Image) -> Image.Image:
    """Composite the thumbnail over white -> L height map (255 = white)."""
    rgba = im.convert("RGBA")
    bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    bg.alpha_composite(rgba)
    return bg.convert("L")


# ---------------------------------------------------------------------------------------------
# C2F container ("\x89C2F\r\n\x1a\n") used by catalog.zip / info.zip / *.layer
# ---------------------------------------------------------------------------------------------
C2F_MAGIC = b"\x89C2F\r\n\x1a\n"


def c2f_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Return [(tag, payload)]: u32 LE length, 4-byte tag, payload, 4-byte CRC."""
    if not data.startswith(C2F_MAGIC):
        return []
    out = []
    p = len(C2F_MAGIC)
    while p + 8 <= len(data):
        n = struct.unpack_from("<I", data, p)[0]
        tag = data[p + 4:p + 8]
        out.append((tag, data[p + 8:p + 8 + n]))
        p += 12 + n
        if tag == b"TAIL":
            break
    return out


def c2f_is_encrypted(data: bytes) -> bool:
    return any(tag == b"dATA" and payload[:2] != b"\x00\x00" for tag, payload in c2f_chunks(data))
