"""Adobe Photoshop brushes (.abr v1/v2/v6/v10) and the Photoshop preset library (Brushes.psp).

File layout (v6+)::

    u16 version, u16 subversion             6.1 / 6.2 for exported files, 10.x for Brushes.psp
    8BIM samp <u32 len>  sampled tip images (UUID + Virtual Memory Array List)
    8BIM patt <u32 len>  texture patterns  (same layout as .pat files)
    8BIM desc <u32 len>  u32 16 + action descriptor { Brsh: VlLs[ brushPreset ... ] }
    8BIM phry <u32 len>  u32 16 + descriptor { hierarchy: VlLs[ Grup | preset | groupEnd ] }

Clip Studio Paint (tested 5.1.2) rejects v10 files but happily imports the very same data
written as v6.2, which is the most common "cannot import" problem this module fixes.
"""
from __future__ import annotations

import io
import mmap
import struct
import threading
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

import numpy as np
from PIL import Image

from ..core.model import Brush, BrushRef, BrushSource, ColorDynamics, DualTip, Dynamic, Texture, WetMix
from ..core.plugin import FileInfo, Fix, FormatCodec, register_codec

# ---------------------------------------------------------------------------------------------
# Action descriptor (de)serialisation
# ---------------------------------------------------------------------------------------------


class Desc:
    """Ordered Photoshop action descriptor. ``items`` keeps (key, type, value) for exact rewrite."""

    __slots__ = ("cls", "name", "items")

    def __init__(self, cls: str = "null", name: str = "", items: Optional[list] = None):
        self.cls = cls
        self.name = name
        self.items: list[tuple[str, str, Any]] = items if items is not None else []

    def get(self, key: str, default: Any = None) -> Any:
        for k, _t, v in self.items:
            if k == key:
                return v
        return default

    def __contains__(self, key: str) -> bool:
        return any(k == key for k, _t, _v in self.items)

    def num(self, key: str, default: float = 0.0) -> float:
        v = self.get(key)
        if isinstance(v, tuple) and len(v) == 2 and isinstance(v[1], (int, float)):
            return float(v[1])
        if isinstance(v, bool):
            return float(v)
        if isinstance(v, (int, float)):
            return float(v)
        return default

    def sub(self, key: str) -> "Desc":
        v = self.get(key)
        return v if isinstance(v, Desc) else Desc()

    def enum(self, key: str, default: str = "") -> str:
        v = self.get(key)
        return v[1] if isinstance(v, tuple) and len(v) == 2 and isinstance(v[1], str) else default

    # builder helpers -----------------------------------------------------------------------
    def add(self, key: str, typ: str, value: Any) -> "Desc":
        self.items.append((key, typ, value))
        return self

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Desc({self.cls!r}, {[k for k, _, _ in self.items]})"


class _Reader:
    __slots__ = ("d", "p")

    def __init__(self, data, pos: int = 0):
        self.d = data
        self.p = pos

    def u8(self) -> int:
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self) -> int:
        v = struct.unpack_from(">H", self.d, self.p)[0]
        self.p += 2
        return v

    def u32(self) -> int:
        v = struct.unpack_from(">I", self.d, self.p)[0]
        self.p += 4
        return v

    def i32(self) -> int:
        v = struct.unpack_from(">i", self.d, self.p)[0]
        self.p += 4
        return v

    def f64(self) -> float:
        v = struct.unpack_from(">d", self.d, self.p)[0]
        self.p += 8
        return v

    def raw(self, n: int) -> bytes:
        v = bytes(self.d[self.p:self.p + n])
        if len(v) != n:
            raise ValueError("unexpected end of file")
        self.p += n
        return v

    def key(self) -> str:
        n = self.u32()
        return self.raw(4 if n == 0 else n).decode("latin1")

    def ustr(self) -> str:
        n = self.u32()
        return self.raw(n * 2).decode("utf-16-be", "replace").rstrip("\x00")

    def pstr(self) -> str:
        n = self.u8()
        return self.raw(n).decode("latin1")


def read_descriptor(r: _Reader) -> Desc:
    name = r.ustr()
    cls = r.key()
    d = Desc(cls, name)
    for _ in range(r.u32()):
        k = r.key()
        t = r.raw(4).decode("latin1")
        d.items.append((k, t, _read_value(r, t)))
    return d


def _read_value(r: _Reader, t: str) -> Any:
    if t in ("Objc", "GlbO"):
        return read_descriptor(r)
    if t == "VlLs":
        out = []
        for _ in range(r.u32()):
            tt = r.raw(4).decode("latin1")
            out.append((tt, _read_value(r, tt)))
        return out
    if t == "doub":
        return r.f64()
    if t == "UntF":
        return (r.raw(4).decode("latin1"), r.f64())
    if t == "UnFl":  # unit float list
        unit = r.raw(4).decode("latin1")
        return (unit, [r.f64() for _ in range(r.u32())])
    if t == "TEXT":
        return r.ustr()
    if t == "enum":
        return (r.key(), r.key())
    if t == "long":
        return r.i32()
    if t == "comp":
        return struct.unpack(">q", r.raw(8))[0]
    if t == "bool":
        return r.u8() != 0
    if t in ("type", "GlbC"):
        return (r.ustr(), r.key())
    if t in ("alis", "tdta", "Pth "):
        return r.raw(r.u32())
    if t == "obj ":
        raise ValueError("reference descriptors are not supported")
    raise ValueError(f"unknown descriptor type {t!r} at {r.p}")


class _Writer:
    def __init__(self):
        self.b = io.BytesIO()

    def u8(self, v): self.b.write(struct.pack(">B", v))
    def u16(self, v): self.b.write(struct.pack(">H", v))
    def u32(self, v): self.b.write(struct.pack(">I", v))
    def i32(self, v): self.b.write(struct.pack(">i", int(v)))
    def f64(self, v): self.b.write(struct.pack(">d", float(v)))
    def raw(self, v): self.b.write(v)

    def key(self, k: str):
        kb = k.encode("latin1")
        if len(kb) == 4:
            self.u32(0)
        else:
            self.u32(len(kb))
        self.raw(kb)

    def ustr(self, s: str):
        s = s + "\x00"
        self.u32(len(s))
        self.raw(s.encode("utf-16-be"))

    def pstr(self, s: str):
        b = s.encode("latin1")
        self.u8(len(b))
        self.raw(b)

    def getvalue(self) -> bytes:
        return self.b.getvalue()


def write_descriptor(w: _Writer, d: Desc) -> None:
    w.ustr(d.name)
    w.key(d.cls)
    w.u32(len(d.items))
    for k, t, v in d.items:
        w.key(k)
        w.raw(t.encode("latin1"))
        _write_value(w, t, v)


def _write_value(w: _Writer, t: str, v: Any) -> None:
    if t in ("Objc", "GlbO"):
        write_descriptor(w, v)
    elif t == "VlLs":
        w.u32(len(v))
        for tt, vv in v:
            w.raw(tt.encode("latin1"))
            _write_value(w, tt, vv)
    elif t == "doub":
        w.f64(v)
    elif t == "UntF":
        w.raw(v[0].encode("latin1"))
        w.f64(v[1])
    elif t == "UnFl":
        w.raw(v[0].encode("latin1"))
        w.u32(len(v[1]))
        for x in v[1]:
            w.f64(x)
    elif t == "TEXT":
        w.ustr(v)
    elif t == "enum":
        w.key(v[0])
        w.key(v[1])
    elif t == "long":
        w.i32(v)
    elif t == "comp":
        w.raw(struct.pack(">q", v))
    elif t == "bool":
        w.u8(1 if v else 0)
    elif t in ("type", "GlbC"):
        w.ustr(v[0])
        w.key(v[1])
    elif t in ("alis", "tdta", "Pth "):
        w.u32(len(v))
        w.raw(v)
    else:
        raise ValueError(f"cannot write descriptor type {t!r}")


def descriptor_bytes(d: Desc) -> bytes:
    w = _Writer()
    write_descriptor(w, d)
    return w.getvalue()


# ---------------------------------------------------------------------------------------------
# Image data (Virtual Memory Array List, PackBits)
# ---------------------------------------------------------------------------------------------


def packbits_decode(data: bytes, expected: int) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n and len(out) < expected:
        c = data[i]
        i += 1
        if c < 128:
            out += data[i:i + c + 1]
            i += c + 1
        elif c > 128:
            if i < n:
                out += bytes([data[i]]) * (257 - c)
            i += 1
    return bytes(out[:expected]).ljust(expected, b"\0")


def packbits_encode_row(row: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(row)
    while i < n:
        j = i + 1
        while j < n and j - i < 128 and row[j] == row[i]:
            j += 1
        if j - i >= 3:
            out.append(257 - (j - i))
            out.append(row[i])
            i = j
            continue
        # literal run
        j = i
        while j < n and j - i < 128:
            if j + 2 < n and row[j] == row[j + 1] == row[j + 2]:
                break
            j += 1
        out.append(j - i - 1)
        out += row[i:j]
        i = j
    return bytes(out)


def decode_channel(r: _Reader, end: int) -> Optional[tuple[np.ndarray, int]]:
    """Read one VMAL channel at ``r.p`` → (uint8 array HxW, depth) or None if not written."""
    if r.p + 4 > end or r.u32() == 0:
        return None
    length = r.u32()
    ch_end = r.p + length
    depth = r.u32()
    top, left, bottom, right = (r.i32() for _ in range(4))
    r.u16()  # pixel depth again
    comp = r.u8()
    h, w = bottom - top, right - left
    if h <= 0 or w <= 0:
        r.p = ch_end
        return None
    bpp = 2 if depth == 16 else 1
    row_bytes = w * bpp
    if comp == 1:
        counts = struct.unpack_from(f">{h}H", r.d, r.p)
        r.p += 2 * h
        rows = []
        for c in counts:
            rows.append(packbits_decode(bytes(r.d[r.p:r.p + c]), row_bytes))
            r.p += c
        buf = b"".join(rows)
    else:
        buf = bytes(r.d[r.p:r.p + h * row_bytes])
    r.p = ch_end
    if bpp == 2:
        arr = (np.frombuffer(buf, ">u2").reshape(h, w) >> 8).astype(np.uint8)
    else:
        arr = np.frombuffer(buf, np.uint8).reshape(h, w)
    return arr, depth


def encode_channel(img: Image.Image, top: int = 0, left: int = 0) -> bytes:
    """8-bit RLE VMAL channel (written flag included)."""
    a = np.asarray(img.convert("L"), dtype=np.uint8)
    h, w = a.shape
    rows = [packbits_encode_row(a[y].tobytes()) for y in range(h)]
    body = _Writer()
    body.u32(8)
    for v in (top, left, top + h, left + w):
        body.i32(v)
    body.u16(8)
    body.u8(1)
    body.raw(struct.pack(f">{h}H", *[len(x) for x in rows]))
    body.raw(b"".join(rows))
    data = body.getvalue()
    return struct.pack(">II", 1, len(data)) + data


# ---------------------------------------------------------------------------------------------
# Container parsing
# ---------------------------------------------------------------------------------------------


@dataclass
class _Section:
    key: str
    start: int  # first byte of payload
    length: int

    @property
    def end(self) -> int:
        return self.start + self.length


@dataclass
class AbrFile:
    path: Path
    data: Any  # bytes or mmap
    version: int
    subversion: int
    sections: dict[str, _Section] = field(default_factory=dict)
    samples: dict[str, tuple[int, int]] = field(default_factory=dict)  # uuid -> (entry start, len)
    patterns: dict[str, tuple[int, int]] = field(default_factory=dict)
    presets: list[tuple[Desc, int, int]] = field(default_factory=list)  # desc, raw start, raw end
    groups: list[str] = field(default_factory=list)  # group path per preset
    legacy: list[Brush] = field(default_factory=list)  # v1/v2 brushes

    @property
    def version_str(self) -> str:
        return f"{self.version}.{self.subversion}" if self.version >= 6 else str(self.version)

    def raw(self, a: int, b: int) -> bytes:
        return bytes(self.data[a:b])

    def close(self) -> None:
        if isinstance(self.data, mmap.mmap):
            try:
                self.data.close()
            except Exception:
                pass


def _open(path: Path):
    f = open(path, "rb")
    try:
        if path.stat().st_size > 32 * 1024 * 1024:
            return mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        return f.read()
    finally:
        f.close()


def _scan_sections(d) -> dict[str, _Section]:
    out: dict[str, _Section] = {}
    i, n = 4, len(d)
    while i + 12 <= n:
        if d[i:i + 4] != b"8BIM":
            i += 1  # sections may be padded
            continue
        key = bytes(d[i + 4:i + 8]).decode("latin1")
        ln = struct.unpack_from(">I", d, i + 8)[0]
        out[key] = _Section(key, i + 12, ln)
        i += 12 + ln
    return out


def _iter_entries(d, sec: _Section) -> Iterator[tuple[int, int]]:
    """(start, length) of length-prefixed entries, each padded to 4 bytes within the section."""
    i = sec.start
    while i + 4 <= sec.end:
        ln = struct.unpack_from(">I", d, i)[0]
        if ln == 0:
            break
        yield i, 4 + ln
        i += 4 + ln
        i = sec.start + ((i - sec.start + 3) // 4) * 4


def parse(path: Path) -> AbrFile:
    path = Path(path)
    d = _open(path)
    version, sub = struct.unpack_from(">HH", d, 0)
    af = AbrFile(path, d, version, sub)
    if version in (1, 2):
        af.legacy = _parse_legacy(d, version, path)
        return af
    if version not in (6, 7, 8, 9, 10):
        raise ValueError(f"unsupported ABR version {version}")
    af.sections = _scan_sections(d)
    if "samp" in af.sections:
        for s, ln in _iter_entries(d, af.sections["samp"]):
            uid = _Reader(d, s + 4).pstr()
            af.samples[uid] = (s, ln)
    if "patt" in af.sections:
        for s, ln in _iter_entries(d, af.sections["patt"]):
            r = _Reader(d, s + 4 + 12)
            r.ustr()
            af.patterns[r.pstr()] = (s, ln)
    if "desc" in af.sections:
        sec = af.sections["desc"]
        r = _Reader(d, sec.start + 4)
        r.ustr()
        r.key()
        for _ in range(r.u32()):
            k = r.key()
            t = r.raw(4).decode("latin1")
            if k == "Brsh" and t == "VlLs":
                for _ in range(r.u32()):
                    tt = r.raw(4).decode("latin1")
                    start = r.p
                    v = _read_value(r, tt)
                    if isinstance(v, Desc):
                        af.presets.append((v, start, r.p))
            else:
                _read_value(r, t)
    af.groups = [""] * len(af.presets)
    if "phry" in af.sections:
        try:
            af.groups = _parse_groups(d, af.sections["phry"], len(af.presets))
        except Exception:
            pass
    return af


def _parse_groups(d, sec: _Section, n: int) -> list[str]:
    r = _Reader(d, sec.start + 4)
    h = read_descriptor(r).get("hierarchy") or []
    stack: list[str] = []
    out: list[str] = []
    for _t, item in h:
        if not isinstance(item, Desc):
            continue
        if item.cls == "Grup":
            stack.append(str(item.get("Nm  ", "")))
        elif item.cls == "groupEnd":
            if stack:
                stack.pop()
        elif item.cls == "preset":
            out.append(" / ".join(stack))
    return (out + [""] * n)[:n]


def sample_image(af: AbrFile, uid: str) -> Optional[Image.Image]:
    if uid not in af.samples:
        return None
    s, ln = af.samples[uid]
    r = _Reader(af.data, s + 4)
    r.pstr()
    end = s + ln
    r.u32()  # 0x00010000
    r.u32()  # VMAL version 3
    r.u32()  # VMAL length
    for _ in range(4):
        r.i32()
    nch = r.u32()
    for _ in range(nch + 2):
        if r.p + 4 > end:
            break
        ch = decode_channel(r, end)
        if ch is not None:
            return Image.fromarray(ch[0], "L")
    return None


def pattern_image(af: AbrFile, uid: str) -> tuple[Optional[Image.Image], str]:
    if uid not in af.patterns:
        return None, ""
    s, ln = af.patterns[uid]
    end = s + ln
    r = _Reader(af.data, s + 4)
    r.u32()  # version
    mode = r.u32()
    r.u16()
    r.u16()
    name = r.ustr()
    r.pstr()
    palette = None
    if mode == 2:  # indexed
        palette = np.frombuffer(r.raw(768), np.uint8).reshape(256, 3)
        r.raw(4)
    r.u32()  # VMAL version
    r.u32()
    for _ in range(4):
        r.i32()
    nch = r.u32()
    chans = []
    for _ in range(nch + 2):
        if r.p + 4 > end:
            break
        ch = decode_channel(r, end)
        if ch is not None:
            chans.append(ch[0])
        if (mode in (1, 2, 8) and chans) or len(chans) >= 3:
            break
    if not chans:
        return None, name
    if mode == 2 and palette is not None:
        rgb = palette[chans[0]]
        g = (rgb @ np.array([0.299, 0.587, 0.114])).astype(np.uint8)
    elif len(chans) >= 3 and chans[0].shape == chans[1].shape == chans[2].shape:
        g = (chans[0] * 0.299 + chans[1] * 0.587 + chans[2] * 0.114).astype(np.uint8)
    else:
        g = chans[0]
    return Image.fromarray(g, "L"), name


# ---------------------------------------------------------------------------------------------
# Legacy v1 / v2
# ---------------------------------------------------------------------------------------------


def _parse_legacy(d, version: int, path: Path) -> list[Brush]:
    r = _Reader(d, 2)
    count = r.u16()
    out: list[Brush] = []
    for i in range(count):
        if r.p + 6 > len(d):
            break
        btype = r.u16()
        size = r.u32()
        nxt = r.p + size
        try:
            if btype == 1:  # computed
                r.u32()
                spacing = r.u16()
                diameter = r.u16()
                roundness = r.u16()
                angle = r.u16()
                hardness = r.u16()
                b = Brush(name=f"Brush {i + 1}", size_px=diameter, spacing=spacing / 100 or 0.25,
                          roundness=roundness / 100, angle=angle, hardness=hardness / 100)
            elif btype == 2:  # sampled
                r.u32()
                spacing = r.u16()
                name = r.ustr() if version == 2 else f"Brush {i + 1}"
                r.u8()  # antialias
                for _ in range(4):
                    r.u16()
                top, left, bottom, right = (r.i32() for _ in range(4))
                depth = r.u16()
                comp = r.u8()
                h, w = bottom - top, right - left
                bpp = 2 if depth == 16 else 1
                if comp == 1:
                    counts = struct.unpack_from(f">{h}H", d, r.p)
                    r.p += 2 * h
                    rows = []
                    for c in counts:
                        rows.append(packbits_decode(bytes(d[r.p:r.p + c]), w * bpp))
                        r.p += c
                    buf = b"".join(rows)
                else:
                    buf = bytes(d[r.p:r.p + h * w * bpp])
                arr = np.frombuffer(buf, ">u2" if bpp == 2 else np.uint8).reshape(h, w)
                if bpp == 2:
                    arr = (arr >> 8).astype(np.uint8)
                tip = Image.fromarray(np.ascontiguousarray(arr), "L")
                b = Brush(name=name or f"Brush {i + 1}", tips=[tip], size_px=max(w, h),
                          spacing=spacing / 100 or 0.25)
            else:
                r.p = nxt
                continue
            b.source = BrushSource(app="photoshop", format="abr", version=str(version), path=str(path), index=i)
            out.append(b)
        except Exception:
            pass
        r.p = nxt
    return out


# ---------------------------------------------------------------------------------------------
# Descriptor  <->  neutral Brush
# ---------------------------------------------------------------------------------------------

_BLEND_TO = {
    "Nrml": "normal", "Mltp": "multiply", "Scrn": "screen", "Ovrl": "overlay", "Drkn": "darken",
    "Lghn": "lighten", "CDdg": "color_dodge", "CBrn": "color_burn", "linearBurn": "linear_burn",
    "linearDodge": "add", "Sbtr": "subtract", "Dfrn": "difference", "hardMix": "hard_mix",
    "linearHeight": "linear_height", "Hght": "height", "Dslv": "dissolve", "Bhnd": "behind",
    "Clar": "clear", "SftL": "soft_light", "HrdL": "hard_light", "Xclu": "exclusion",
    "H   ": "hue", "Strt": "saturation", "Clr ": "color", "Lmns": "luminosity", "blendDivide": "divide",
}
_BLEND_FROM = {v: k for k, v in _BLEND_TO.items()}

# brVr.bVTy control values
_OFF, _FADE, _PRESSURE, _TILT, _WHEEL, _ROTATION, _INIT_DIR, _DIRECTION = range(8)


def _dyn(v: Desc, minimum: float) -> Dynamic:
    """Convert a brVr (variation) descriptor. ``minimum`` 0..1 is the floor with control active."""
    d = Dynamic()
    ctrl = int(v.num("bVTy"))
    strength = max(0.0, min(1.0, 1.0 - minimum))
    if ctrl == _PRESSURE:
        d.pressure = strength
    elif ctrl in (_TILT, _WHEEL):
        d.tilt = strength
    d.jitter = v.num("jitter") / 100
    return d


def _pct(d: Desc, key: str, default: float) -> float:
    return d.num(key, default * 100) / 100 if key in d else default


def preset_to_brush(af: AbrFile, idx: int) -> Brush:
    p, a, b = af.presets[idx]
    tip_d = p.sub("Brsh")
    name = str(p.get("Nm  ", "") or tip_d.get("Nm  ", "") or f"Brush {idx + 1}")
    br = Brush(name=name, uid=str(p.get("zuid") or _uuid.uuid4()))
    br.group = af.groups[idx] if idx < len(af.groups) else ""
    br.source = BrushSource(app="photoshop", format="abr", version=af.version_str, path=str(af.path), index=idx)

    br.size_px = tip_d.num("Dmtr", 30.0)
    br.angle = tip_d.num("Angl", 0.0)
    br.roundness = tip_d.num("Rndn", 100.0) / 100
    br.spacing = tip_d.num("Spcn", 25.0) / 100 if tip_d.get("Intr", True) else 0.25
    br.flip_x = bool(tip_d.get("flipX", False))
    br.flip_y = bool(tip_d.get("flipY", False))
    br.hardness = tip_d.num("Hrdn", 100.0) / 100
    sid = tip_d.get("sampledData")
    if tip_d.cls == "sampledBrush" and sid:
        img = sample_image(af, sid)
        if img is not None:
            br.tips = [img]
        else:
            br.warn("笔尖图像缺失（引用的样本不在此文件中），已用圆形笔尖代替。")
    elif tip_d.cls not in ("computedBrush", "sampledBrush", ""):
        br.warn(f"特殊笔尖类型「{tip_d.cls}」（如侵蚀/喷枪笔尖）只能近似转换。")

    if p.get("useTipDynamics", False):
        br.size_dynamic = _dyn(p.sub("szVr"), p.num("minimumDiameter", 0) / 100)
        ang = p.sub("angleDynamics")
        br.angle_jitter = ang.num("jitter") / 100
        if int(ang.num("bVTy")) in (_INIT_DIR, _DIRECTION):
            br.angle_follow_stroke = True
        br.roundness_jitter = p.sub("roundnessDynamics").num("jitter") / 100
    if p.get("useScatter", False):
        br.scatter = p.num("Sctr") / 100
        br.scatter_both_axes = bool(p.get("bothAxes", False))
        br.count = max(1, int(p.num("Cnt ", p.num("Cnt", 1))))
        br.count_jitter = p.sub("countDynamics").num("jitter") / 100

    tool = p.sub("toolOptions")
    br.opacity = tool.num("Opct", 100) / 100
    br.flow = tool.num("flow", 100) / 100
    br.blend_mode = _BLEND_TO.get(tool.enum("Md  "), "normal")
    if tool.cls == "MixB":
        br.wet = WetMix(enabled=True, mix=tool.num("mix", 0) / 100, load=tool.num("dryness", 100) / 100,
                        wetness=tool.num("wetness", 0) / 100)
    if tool.cls in ("ErTl", "Ersr"):
        br.is_eraser = True
    if p.get("usePaintDynamics", False):
        br.opacity_dynamic = _dyn(p.sub("opVr"), p.sub("opVr").num("Mnm ") / 100)
        br.flow_dynamic = _dyn(p.sub("prVr"), p.sub("prVr").num("Mnm ") / 100)
    elif tool.get("usePressureOverridesOpacity", False):
        br.opacity_dynamic.pressure = 1.0
    if tool.get("usePressureOverridesSize", False) and not br.size_dynamic.is_active():
        br.size_dynamic.pressure = 1.0

    if p.get("useColorDynamics", False):
        br.color = ColorDynamics(
            fg_bg_jitter=p.sub("clVr").num("jitter") / 100,
            hue_jitter=p.num("H   ") / 100,
            saturation_jitter=p.num("Strt") / 100,
            brightness_jitter=p.num("Brgh") / 100,
            per_tip=bool(p.get("colorDynamicsPerTip", True)),
        )

    if p.get("useTexture", False):
        tx = p.sub("Txtr")
        img, pname = pattern_image(af, str(tx.get("Idnt", "")))
        if img is not None:
            br.texture = Texture(
                image=img, name=str(tx.get("Nm  ", pname)), scale=p.num("textureScale", 100) / 100,
                depth=p.num("textureDepth", 100) / 100, invert=bool(p.get("InvT", False)),
                mode=_BLEND_TO.get(p.enum("textureBlendMode"), "multiply"),
                brightness=p.num("textureBrightness") / 150, contrast=p.num("textureContrast") / 100,
                per_tip=bool(p.get("TxtC", True)),
                depth_dynamic=_dyn(p.sub("textureDepthDynamics"), p.num("minimumDepth") / 100),
            )
        else:
            br.warn("纹理图案缺失（引用的图案不在此文件中），已忽略纹理。")

    dual = p.sub("dualBrush")
    if dual.get("useDualBrush", False):
        dtip = dual.sub("Brsh")
        img = sample_image(af, dtip.get("sampledData", "")) if dtip.get("sampledData") else None
        if img is None:
            from ..core.imaging import round_tip

            img = round_tip(max(8, int(dtip.num("Dmtr", 30))), dtip.num("Hrdn", 100) / 100)
        br.dual = DualTip(image=img, size_px=dtip.num("Dmtr", 30), spacing=dtip.num("Spcn", 25) / 100,
                          scatter=dual.num("Sctr") / 100, count=max(1, int(dual.num("Cnt ", dual.num("Cnt", 1)))),
                          mode=_BLEND_TO.get(dual.enum("BlnM"), "multiply"), name=str(dtip.get("Nm  ", "")))

    if p.get("Wtdg", False):
        br.warn("「湿边」效果在其他软件中可能无法还原。")
    if p.get("Nose", False):
        br.warn("「杂色」效果在其他软件中可能无法还原。")
    if p.get("brushGroup", Desc()).get("useBrushGroup", False) if isinstance(p.get("brushGroup"), Desc) else False:
        br.warn("「画笔笔势/笔刷组」设置无法转换。")

    sample_ids = [x for x in (sid, dual.sub("Brsh").get("sampledData") if dual.get("useDualBrush") else None) if x]
    pattern_ids = [str(p.sub("Txtr").get("Idnt"))] if p.get("useTexture") and p.sub("Txtr").get("Idnt") else []
    br.source.native = {
        "abr_desc": af.raw(a, b),
        "abr_samples": {u: af.raw(*_span(af.samples[u])) for u in sample_ids if u in af.samples},
        "abr_patterns": {u: af.raw(*_span(af.patterns[u])) for u in pattern_ids if u in af.patterns},
    }
    return br


def _span(t: tuple[int, int]) -> tuple[int, int]:
    return t[0], t[0] + t[1]


def _brvr(dyn: Dynamic, minimum_out: list) -> Desc:
    ctrl = _PRESSURE if dyn.pressure > 0 else (_TILT if dyn.tilt > 0 else _OFF)
    strength = dyn.pressure or dyn.tilt
    minimum_out.append(round((1 - strength) * 100, 2) if ctrl else 0.0)
    return (Desc("brVr").add("bVTy", "long", ctrl).add("fStp", "long", 25)
            .add("jitter", "UntF", ("#Prc", round(dyn.jitter * 100, 2))).add("Mnm ", "UntF", ("#Prc", minimum_out[-1])))


def brush_to_preset(br: Brush, sample_id: Optional[str], dual_id: Optional[str],
                    pattern_id: Optional[str]) -> Desc:
    tip = Desc("sampledBrush" if sample_id else "computedBrush")
    tip.add("Dmtr", "UntF", ("#Pxl", float(br.size_px)))
    if not sample_id:
        tip.add("Hrdn", "UntF", ("#Prc", br.hardness * 100))
    tip.add("Angl", "UntF", ("#Ang", float(br.angle)))
    tip.add("Rndn", "UntF", ("#Prc", br.roundness * 100))
    tip.add("Nm  ", "TEXT", br.name)
    tip.add("Spcn", "UntF", ("#Prc", max(1.0, br.spacing * 100)))
    tip.add("Intr", "bool", True)
    tip.add("flipX", "bool", br.flip_x)
    tip.add("flipY", "bool", br.flip_y)
    if sample_id:
        tip.add("sampledData", "TEXT", sample_id)

    p = Desc("brushPreset")
    p.add("Nm  ", "TEXT", br.name)
    p.add("Brsh", "Objc", tip)
    use_tip_dyn = br.size_dynamic.is_active() or br.angle_jitter > 0 or br.roundness_jitter > 0 or br.angle_follow_stroke
    p.add("useTipDynamics", "bool", use_tip_dyn)
    p.add("flipX", "bool", False).add("flipY", "bool", False).add("brushProjection", "bool", False)
    mins: list = []
    szvr = _brvr(br.size_dynamic, mins)
    p.add("minimumDiameter", "UntF", ("#Prc", mins[-1]))
    p.add("minimumRoundness", "UntF", ("#Prc", 25.0))
    p.add("tiltScale", "UntF", ("#Prc", 200.0))
    p.add("szVr", "Objc", szvr)
    ang = Desc("brVr").add("bVTy", "long", _DIRECTION if br.angle_follow_stroke else _OFF).add("fStp", "long", 25)
    ang.add("jitter", "UntF", ("#Prc", br.angle_jitter * 100)).add("Mnm ", "UntF", ("#Prc", 0.0))
    p.add("angleDynamics", "Objc", ang)
    rnd = Desc("brVr").add("bVTy", "long", 0).add("fStp", "long", 25)
    rnd.add("jitter", "UntF", ("#Prc", br.roundness_jitter * 100)).add("Mnm ", "UntF", ("#Prc", 0.0))
    p.add("roundnessDynamics", "Objc", rnd)

    p.add("useScatter", "bool", br.scatter > 0 or br.count > 1)
    p.add("Sctr", "UntF", ("#Prc", br.scatter * 100))
    p.add("bothAxes", "bool", br.scatter_both_axes)
    p.add("Cnt ", "doub", float(max(1, min(16, br.count))))
    cnt = Desc("brVr").add("bVTy", "long", 0).add("fStp", "long", 25)
    cnt.add("jitter", "UntF", ("#Prc", br.count_jitter * 100)).add("Mnm ", "UntF", ("#Prc", 0.0))
    p.add("countDynamics", "Objc", cnt)

    dual = Desc("dualBrush").add("useDualBrush", "bool", bool(br.dual and dual_id))
    if br.dual and dual_id:
        dt = Desc("sampledBrush").add("Dmtr", "UntF", ("#Pxl", float(br.dual.size_px)))
        dt.add("Angl", "UntF", ("#Ang", 0.0)).add("Rndn", "UntF", ("#Prc", 100.0))
        dt.add("Nm  ", "TEXT", br.dual.name or "Dual").add("Spcn", "UntF", ("#Prc", max(1.0, br.dual.spacing * 100)))
        dt.add("Intr", "bool", True).add("flipX", "bool", False).add("flipY", "bool", False)
        dt.add("sampledData", "TEXT", dual_id)
        dual.add("Flip", "bool", False).add("Brsh", "Objc", dt)
        dual.add("BlnM", "enum", ("BlnM", _BLEND_FROM.get(br.dual.mode, "Mltp")))
        dual.add("useScatter", "bool", br.dual.scatter > 0).add("Sctr", "UntF", ("#Prc", br.dual.scatter * 100))
        dual.add("Cnt ", "doub", float(max(1, br.dual.count))).add("bothAxes", "bool", False)
        dual.add("countDynamics", "Objc", Desc("brVr").add("bVTy", "long", 0).add("fStp", "long", 25)
                 .add("jitter", "UntF", ("#Prc", 0.0)).add("Mnm ", "UntF", ("#Prc", 0.0)))
        dual.add("scatterDynamics", "Objc", Desc("brVr").add("bVTy", "long", 0).add("fStp", "long", 25)
                 .add("jitter", "UntF", ("#Prc", 0.0)).add("Mnm ", "UntF", ("#Prc", 0.0)))
    p.add("dualBrush", "Objc", dual)
    p.add("brushGroup", "Objc", Desc("brushGroup").add("useBrushGroup", "bool", False))

    tx = br.texture if pattern_id else None
    p.add("useTexture", "bool", tx is not None)
    if tx is not None:
        p.add("TxtC", "bool", tx.per_tip).add("interpretation", "bool", True)
        p.add("textureBlendMode", "enum", ("BlnM", _BLEND_FROM.get(tx.mode, "Mltp")))
        p.add("textureDepth", "UntF", ("#Prc", tx.depth * 100))
        tmins: list = []
        tdd = _brvr(tx.depth_dynamic, tmins)
        p.add("minimumDepth", "UntF", ("#Prc", tmins[-1]))
        p.add("textureDepthDynamics", "Objc", tdd)
        p.add("Txtr", "Objc", Desc("Ptrn").add("Nm  ", "TEXT", tx.name or "Texture").add("Idnt", "TEXT", pattern_id))
        p.add("textureScale", "UntF", ("#Prc", tx.scale * 100))
        p.add("InvT", "bool", tx.invert).add("protectTexture", "bool", False)
        p.add("textureBrightness", "long", int(round(tx.brightness * 150)))
        p.add("textureContrast", "long", int(round(tx.contrast * 100)))

    use_paint = br.opacity_dynamic.is_active() or br.flow_dynamic.is_active()
    p.add("usePaintDynamics", "bool", use_paint)
    omins: list = []
    p.add("opVr", "Objc", _brvr(br.opacity_dynamic, omins))
    p.add("prVr", "Objc", _brvr(br.flow_dynamic, omins))
    c = br.color
    p.add("useColorDynamics", "bool", c.has_jitter())
    p.add("clVr", "Objc", Desc("brVr").add("bVTy", "long", 0).add("fStp", "long", 25)
          .add("jitter", "UntF", ("#Prc", c.fg_bg_jitter * 100)).add("Mnm ", "UntF", ("#Prc", 0.0)))
    p.add("H   ", "UntF", ("#Prc", c.hue_jitter * 100))
    p.add("Strt", "UntF", ("#Prc", c.saturation_jitter * 100))
    p.add("Brgh", "UntF", ("#Prc", c.brightness_jitter * 100))
    p.add("purity", "UntF", ("#Prc", 0.0))
    p.add("colorDynamicsPerTip", "bool", c.per_tip)
    p.add("Wtdg", "bool", False).add("Nose", "bool", False).add("Rpt ", "bool", False)
    p.add("useBrushSize", "bool", True).add("useBrushPose", "bool", False)

    if br.wet.enabled:
        tool = Desc("MixB").add("flow", "long", int(round(br.flow * 100)))
        tool.add("wetness", "doub", br.wet.wetness * 100).add("dryness", "doub", br.wet.load * 100)
        tool.add("mix", "doub", br.wet.mix * 100)
    else:
        tool = Desc("PbTl").add("flow", "long", int(round(br.flow * 100)))
    tool.add("Md  ", "enum", ("BlnM", _BLEND_FROM.get(br.blend_mode, "Nrml")))
    tool.add("Opct", "long", int(round(br.opacity * 100)))
    tool.add("usePressureOverridesSize", "bool", False)
    tool.add("usePressureOverridesOpacity", "bool", False)
    tool.add("brushPreset", "bool", True)
    p.add("toolOptions", "Objc", tool)
    br.warn_extras("Photoshop")
    return p


# ---------------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------------

# Empty brush hierarchy as written by Photoshop's "Export Selected Brushes".
_PHRY_EMPTY = bytes.fromhex(
    "3842494d706872790000002b00000010000000010000000000006e756c6c0000000100000009686965726172636879566c4c7300000000")


def _sample_entry(img: Image.Image, uid: str) -> bytes:
    img = img.convert("L")
    w = _Writer()
    w.pstr(uid)
    vm = _Writer()
    for v in (0, 0, img.height, img.width):
        vm.i32(v)
    vm.u32(56)
    vm.raw(b"\0" * 4 * 55)
    vm.raw(encode_channel(img))
    vm.raw(b"\0" * 8)
    vmb = vm.getvalue()
    w.u32(0x00010000)
    w.u32(3)
    w.u32(len(vmb))
    w.raw(vmb)
    body = w.getvalue()
    return struct.pack(">I", len(body)) + body


def _pattern_entry(img: Image.Image, uid: str, name: str) -> bytes:
    img = img.convert("L")
    w = _Writer()
    w.u32(1)
    w.u32(1)  # grayscale
    w.u16(img.height)
    w.u16(img.width)
    w.ustr(name or "Texture")
    w.pstr(uid)
    vm = _Writer()
    for v in (0, 0, img.height, img.width):
        vm.i32(v)
    vm.u32(24)
    vm.raw(encode_channel(img))
    vm.raw(b"\0" * 8)
    vmb = vm.getvalue()
    w.u32(3)
    w.u32(len(vmb))
    w.raw(vmb)
    body = w.getvalue()
    return struct.pack(">I", len(body)) + body


def _section(key: str, payload: bytes) -> bytes:
    if len(payload) % 2:
        payload += b"\0"
    return b"8BIM" + key.encode("latin1") + struct.pack(">I", len(payload)) + payload


def _join_entries(entries: list[bytes]) -> bytes:
    out = bytearray()
    for e in entries:
        out += e
        out += b"\0" * ((-len(out)) % 4)
    return bytes(out)


def write_abr(brushes: list[Brush], out_path: Path, *, prefer_native: bool = True) -> Path:
    """Write a v6.2 ABR (the version every app understands)."""
    samples: dict[str, bytes] = {}
    patterns: dict[str, bytes] = {}
    presets: list[bytes] = []
    for br in brushes:
        nat = br.source.native if br.source.format == "abr" else {}
        if prefer_native and nat.get("abr_desc"):
            presets.append(nat["abr_desc"])
            samples.update(nat.get("abr_samples", {}))
            patterns.update(nat.get("abr_patterns", {}))
            continue
        sid = did = pid = None
        if br.tips:
            sid = str(_uuid.uuid4())
            samples[sid] = _sample_entry(br.tips[0], sid)
            if len(br.tips) > 1:
                br.warn("Photoshop 笔刷只支持单个笔尖图像，已使用第一张。")
        if br.dual is not None:
            did = str(_uuid.uuid4())
            samples[did] = _sample_entry(br.dual.image, did)
        if br.texture is not None:
            pid = str(_uuid.uuid4())
            patterns[pid] = _pattern_entry(br.texture.image, pid, br.texture.name)
        presets.append(descriptor_bytes(brush_to_preset(br, sid, did, pid)))

    desc = _Writer()
    desc.u32(16)
    desc.ustr("")
    desc.key("null")
    desc.u32(1)
    desc.key("Brsh")
    desc.raw(b"VlLs")
    desc.u32(len(presets))
    for raw in presets:
        desc.raw(b"Objc")
        desc.raw(raw)

    out = bytearray(struct.pack(">HH", 6, 2))
    out += _section("samp", _join_entries(list(samples.values())))
    out += _section("patt", _join_entries(list(patterns.values())))
    out += _section("desc", desc.getvalue())
    out += _PHRY_EMPTY
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(out))
    return out_path


def convert_to_v62(src: Path, out_path: Path, indices: Optional[list[int]] = None) -> Path:
    """Losslessly rewrite (a subset of) any v6+ ABR / Brushes.psp as v6.2."""
    af = parse(src)
    try:
        if af.legacy:
            brushes = [b for i, b in enumerate(af.legacy) if indices is None or i in indices]
        else:
            idx = range(len(af.presets)) if indices is None else indices
            brushes = [_native_only(af, i) for i in idx]
        return write_abr(brushes, out_path)
    finally:
        af.close()


def _native_only(af: AbrFile, idx: int) -> Brush:
    """Cheap Brush carrying only the raw bytes (no image decoding)."""
    p, a, b = af.presets[idx]
    tip = p.sub("Brsh")
    sids = [tip.get("sampledData")]
    dual = p.sub("dualBrush")
    if dual.get("useDualBrush"):
        sids.append(dual.sub("Brsh").get("sampledData"))
    pid = p.sub("Txtr").get("Idnt") if p.get("useTexture") else None
    br = Brush(name=str(p.get("Nm  ", "")))
    br.source = BrushSource(app="photoshop", format="abr", version=af.version_str, path=str(af.path), index=idx)
    br.source.native = {
        "abr_desc": af.raw(a, b),
        "abr_samples": {u: af.raw(*_span(af.samples[u])) for u in sids if u and u in af.samples},
        "abr_patterns": {pid: af.raw(*_span(af.patterns[pid]))} if pid and pid in af.patterns else {},
    }
    return br


# ---------------------------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------------------------


_CACHE: dict[tuple, AbrFile] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TIMER: Optional[threading.Timer] = None


def _close_cache() -> None:
    with _CACHE_LOCK:
        for af in _CACHE.values():
            af.close()
        _CACHE.clear()


def _thumb(af_path: Path, idx: int):
    """Thumbnail loader. Parsed files are shared between thumbnails of the same library (parsing a
    large Brushes.psp dominates), and closed again after a short idle period so the file isn't
    kept locked while Photoshop wants to write it."""
    def f():
        global _CACHE_TIMER
        with _CACHE_LOCK:
            st = Path(af_path).stat()
            key = (str(af_path), st.st_mtime_ns, st.st_size)
            af = _CACHE.get(key)
            if af is None:
                for k in [k for k in _CACHE if k[0] == key[0] or len(_CACHE) >= 4]:
                    _CACHE.pop(k).close()
                af = _CACHE[key] = parse(af_path)
            if _CACHE_TIMER is not None:
                _CACHE_TIMER.cancel()
            _CACHE_TIMER = threading.Timer(3.0, _close_cache)
            _CACHE_TIMER.daemon = True
            _CACHE_TIMER.start()
            if af.legacy:
                return af.legacy[idx].main_tip()
            p = af.presets[idx][0]
            sid = p.sub("Brsh").get("sampledData")
            img = sample_image(af, sid) if sid else None
            if img is None:
                from ..core.imaging import round_tip

                t = p.sub("Brsh")
                img = round_tip(64, t.num("Hrdn", 100) / 100, t.num("Rndn", 100) / 100, t.num("Angl", 0))
            return img

    return f


@register_codec
class AbrCodec(FormatCodec):
    id = "abr"
    name = "Photoshop 笔刷 (.abr)"
    extensions = (".abr", ".psp")
    can_read = True
    can_write = True

    def sniff(self, path: Path) -> Optional[FileInfo]:
        path = Path(path)
        try:
            with open(path, "rb") as f:
                head = f.read(16)
        except OSError:
            return None
        if len(head) < 4:
            return None
        ver, sub = struct.unpack(">HH", head[:4])
        if ver in (1, 2):
            if path.suffix.lower() != ".abr":
                return None
            vs = str(ver)
            count = sub
        elif ver in (6, 7, 8, 9, 10) and head[4:8] == b"8BIM":
            vs = f"{ver}.{sub}"
            count = -1
        else:
            return None
        info = FileInfo(path=path, format_id=self.id, format_name=self.name, version=vs, brush_count=count)
        info.details["文件版本"] = f"ABR v{vs}"
        if path.suffix.lower() == ".psp":
            info.details["说明"] = "Photoshop 预设库（Brushes.psp）"
        if ver >= 7:
            info.problems.append(
                f"这是新版 Photoshop 的 ABR v{vs} 格式，Clip Studio Paint、Krita、GIMP 等软件以及旧版 Photoshop 可能无法导入。")
            info.fixes.append(Fix(
                title="转换为通用的 v6.2 格式",
                description="无损重写为 Photoshop CS6 及以后、Clip Studio Paint 等都能识别的 ABR v6.2。",
                apply=lambda src, out_dir: [convert_to_v62(src, Path(out_dir) / (Path(src).stem + "_v6.abr"))],
            ))
        return info

    def list_refs(self, path: Path) -> list[BrushRef]:
        path = Path(path)
        af = parse(path)
        try:
            if af.legacy:
                return [BrushRef(name=b.name, key=str(i), loader=(lambda b=b: b), thumbnail=(lambda b=b: b.main_tip()),
                                 detail=f"ABR v{af.version}") for i, b in enumerate(af.legacy)]
            refs = []
            for i, (p, _a, _b) in enumerate(af.presets):
                tip = p.sub("Brsh")
                name = str(p.get("Nm  ", "") or tip.get("Nm  ", "") or f"Brush {i + 1}")
                size = tip.num("Dmtr", 0)
                refs.append(BrushRef(
                    name=name, key=str(i), group=af.groups[i] if i < len(af.groups) else "",
                    loader=(lambda i=i: self.read_one(path, i)), thumbnail=_thumb(path, i),
                    detail=f"{int(size)} px" + ("，含纹理" if p.get("useTexture") else "") +
                           ("，双重画笔" if p.sub("dualBrush").get("useDualBrush") else "")))
            return refs
        finally:
            af.close()

    def read_one(self, path: Path, idx: int) -> Brush:
        af = parse(Path(path))
        try:
            if af.legacy:
                return af.legacy[idx]
            return preset_to_brush(af, idx)
        finally:
            af.close()

    def read(self, path: Path) -> list[Brush]:
        af = parse(Path(path))
        try:
            if af.legacy:
                return list(af.legacy)
            return [preset_to_brush(af, i) for i in range(len(af.presets))]
        finally:
            af.close()

    def write(self, brushes: list[Brush], out_path: Path, **options) -> list[Path]:
        return [write_abr(brushes, Path(out_path), prefer_native=options.get("prefer_native", True))]

    def compatibility(self, info: FileInfo, app_id: str, app_version: str) -> Optional[str]:
        try:
            major = int(info.version.split(".")[0])
        except ValueError:
            return None
        if major >= 7 and app_id in ("csp", "krita", "gimp", "sai2"):
            return f"ABR v{info.version} 是 Photoshop 2020 之后的预设库格式，该软件只认 v6 及更早的格式。"
        if major >= 6 and app_id == "photoshop":
            try:
                ps_major = int(app_version.split(".")[0])
            except ValueError:
                return None
            if ps_major and ps_major < 13:
                return "该笔刷需要 Photoshop CS6 或更高版本。"
        return None
