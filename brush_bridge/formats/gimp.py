"""GIMP brushes: .gbr (sampled), .gih (image pipe / animated) and .vbr (generated, read + write).

Binary layout (all integers big-endian), from GIMP ``app/core/gimpbrush-header.h`` / ``-load.c``:

  GBR v2   header_size u32 | version u32 (=2) | width u32 | height u32 | bytes u32 |
           magic u32 ("GIMP") | spacing u32 (percent) | name (UTF-8, NUL terminated) | pixels
  GBR v1   same without magic and spacing (20-byte header, spacing defaults to 25)
  GBR v3   CinePaint: like v2 but ``bytes`` = 18 (float16 gray, stored as the upper 16 bits
           of an IEEE float32)
  pixels   bytes=1: 8-bit gray mask, 255 = full paint (same convention as our model)
           bytes=4: RGBA "pixmap" (colour brush), mask = alpha

  GIH      line 1: name, line 2: "<n> ncells:<n> cellwidth:.. sel0:random ..." then n GBRs.
  VBR      text: "GIMP-VBR", version ("1.0" or "1.5"), name, [shape], spacing, radius,
           [spikes], hardness, aspect ratio, angle.

The module also exposes the GIMP pattern (.pat) reader used by the Krita codec.
"""
from __future__ import annotations

import io
import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from ..core.imaging import round_tip
from ..core.model import Brush, BrushSource
from ..core.plugin import FileInfo, FormatCodec, Fix, register_codec
from ..core.system import safe_filename, unique_path

GBR_MAGIC = 0x47494D50  # "GIMP"
PAT_MAGIC = 0x47504154  # "GPAT"
GBR_MAX_SIZE = 10000
GBR_MAX_NAME = 256
_GBR_HEADER = struct.Struct(">7I")  # v2/v3 header without the name

GIH_SELECTION_MODES = ("incremental", "angular", "random", "velocity", "pressure", "xtilt", "ytilt")


class GimpFormatError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------
# GBR
# ---------------------------------------------------------------------------------------------
@dataclass
class GbrBrush:
    name: str
    mask: Image.Image  # "L", 255 = paint
    spacing: int = 25  # percent of the brush size
    version: int = 2
    bytes: int = 1
    color: Optional[Image.Image] = None  # RGB pixmap of colour brushes (bytes=4)

    @property
    def size(self) -> tuple[int, int]:
        return self.mask.size


def _float16_hi_to_u8(raw: bytes) -> np.ndarray:
    """CinePaint float16: the 16 stored bits are the high half of a float32."""
    hi = np.frombuffer(raw, ">u2").astype(np.uint32) << 16
    f = hi.view(np.float32)
    return np.clip(f * 255.0 + 0.5, 0, 255).astype(np.uint8)


def parse_gbr_header(data: bytes, pos: int = 0) -> dict:
    """Decode a GBR header at ``pos``; returns a dict with version/size/name/data offset."""
    if len(data) - pos < 20:
        raise GimpFormatError("GBR header truncated")
    header_size, version, width, height, nbytes = struct.unpack_from(">5I", data, pos)
    if version == 1:
        spacing = 25
        name_start = pos + 20
        if header_size < 20:
            raise GimpFormatError("bad GBR v1 header size")
    elif version in (2, 3):
        if len(data) - pos < 28:
            raise GimpFormatError("GBR header truncated")
        magic, spacing = struct.unpack_from(">2I", data, pos + 20)
        if magic != GBR_MAGIC:
            raise GimpFormatError("GBR magic missing")
        if header_size < 28:
            raise GimpFormatError("bad GBR header size")
        name_start = pos + 28
    else:
        raise GimpFormatError(f"unknown GBR version {version}")
    if not (0 < width <= GBR_MAX_SIZE and 0 < height <= GBR_MAX_SIZE):
        raise GimpFormatError(f"bad GBR size {width}x{height}")
    if version == 3:
        if nbytes != 18:
            raise GimpFormatError(f"unsupported CinePaint brush depth {nbytes}")
        bpp = 2
    else:
        if nbytes not in (1, 4):
            raise GimpFormatError(f"unsupported GBR depth {nbytes}")
        bpp = nbytes
    name_end = pos + header_size
    if name_end - name_start > GBR_MAX_NAME + 1 or name_end > len(data):
        raise GimpFormatError("GBR name too long")
    raw_name = data[name_start:name_end].split(b"\0", 1)[0]
    try:
        name = raw_name.decode("utf-8")
    except UnicodeDecodeError:
        name = raw_name.decode("latin-1")
    return {"version": version, "width": width, "height": height, "bytes": nbytes, "bpp": bpp,
            "spacing": spacing, "name": name, "data_start": name_end,
            "data_end": name_end + width * height * bpp}


def parse_gbr(data: bytes, pos: int = 0) -> tuple[GbrBrush, int]:
    """Decode one GBR brush starting at ``pos``; returns (brush, offset after it)."""
    h = parse_gbr_header(data, pos)
    w, hgt = h["width"], h["height"]
    start, end = h["data_start"], h["data_end"]
    if end > len(data):
        raise GimpFormatError("GBR pixel data truncated")
    raw = data[start:end]
    color = None
    if h["version"] == 3:
        mask = Image.fromarray(_float16_hi_to_u8(raw).reshape(hgt, w), "L")
    elif h["bpp"] == 1:
        mask = Image.frombytes("L", (w, hgt), raw)
        # obsolete .gpb: a GIMP pattern with the same size may follow a gray brush
        if len(data) - end >= 24:
            ph_size, ph_ver, ph_w, ph_h, ph_bytes, ph_magic = struct.unpack_from(">6I", data, end)
            if ph_magic == PAT_MAGIC and ph_ver == 1 and ph_bytes == 3 and (ph_w, ph_h) == (w, hgt) and ph_size > 24:
                pstart = end + ph_size
                pend = pstart + w * hgt * 3
                if pend <= len(data):
                    color = Image.frombytes("RGB", (w, hgt), data[pstart:pend])
                    end = pend
    else:
        rgba = Image.frombytes("RGBA", (w, hgt), raw)
        mask = rgba.getchannel("A")
        color = rgba.convert("RGB")
    return GbrBrush(h["name"], mask, int(h["spacing"]), h["version"], h["bytes"], color), end


def encode_gbr(mask: Image.Image, name: str, spacing: int = 25, color: Optional[Image.Image] = None) -> bytes:
    """Encode a GBR v2 brush. ``mask`` is "L" with 255 = paint; ``color`` makes an RGBA brush."""
    mask = mask.convert("L")
    w, h = mask.size
    if not (0 < w <= GBR_MAX_SIZE and 0 < h <= GBR_MAX_SIZE):
        raise GimpFormatError(f"GBR size {w}x{h} out of range")
    raw_name = name.encode("utf-8")[:GBR_MAX_NAME]
    while raw_name:
        try:
            raw_name.decode("utf-8")
            break
        except UnicodeDecodeError:
            raw_name = raw_name[:-1]
    raw_name += b"\0"
    if color is not None:
        rgba = color.convert("RGB").resize(mask.size).convert("RGBA")
        rgba.putalpha(mask)
        nbytes, pixels = 4, rgba.tobytes()
    else:
        nbytes, pixels = 1, mask.tobytes()
    spacing = max(1, min(1000, int(round(spacing))))
    header = _GBR_HEADER.pack(28 + len(raw_name), 2, w, h, nbytes, GBR_MAGIC, spacing)
    return header + raw_name + pixels


# ---------------------------------------------------------------------------------------------
# GIH (brush pipe)
# ---------------------------------------------------------------------------------------------
def parse_gih(data: bytes) -> tuple[str, dict[str, str], list[GbrBrush]]:
    nl1 = data.find(b"\n")
    nl2 = data.find(b"\n", nl1 + 1)
    if nl1 < 0 or nl2 < 0:
        raise GimpFormatError("GIH header missing")
    name = data[:nl1].decode("utf-8", "replace").strip("\r")
    line2 = data[nl1 + 1:nl2].decode("latin-1").strip()
    m = re.match(r"(\d+)\s*(.*)", line2)
    if not m:
        raise GimpFormatError("GIH cell count missing")
    count = int(m.group(1))
    params = dict(re.findall(r"(\w+):(\S+)", m.group(2)))
    brushes, pos = [], nl2 + 1
    for _ in range(count):
        if pos >= len(data):
            break
        b, pos = parse_gbr(data, pos)
        brushes.append(b)
    if not brushes:
        raise GimpFormatError("GIH contains no brushes")
    return name, params, brushes


def encode_gih(masks: list[Image.Image], name: str, spacing: int = 25, selection: str = "random") -> bytes:
    if selection not in GIH_SELECTION_MODES:
        selection = "random"
    masks = [m.convert("L") for m in masks]
    cw = max(m.width for m in masks)
    ch = max(m.height for m in masks)
    n = len(masks)
    params = (f"ncells:{n} cellwidth:{cw} cellheight:{ch} step:100 dim:1 cols:1 rows:1 "
              f"placement:constant rank0:{n} sel0:{selection}")
    clean = name.replace("\n", " ").replace("\r", " ")
    out = [f"{clean}\n{n} {params}\n".encode("utf-8")]
    for i, m in enumerate(masks):
        out.append(encode_gbr(m, f"{clean} {i + 1}", spacing))
    return b"".join(out)


# ---------------------------------------------------------------------------------------------
# VBR (generated brush)
# ---------------------------------------------------------------------------------------------
@dataclass
class VbrBrush:
    name: str
    version: str = "1.5"
    shape: str = "circle"  # circle | square | diamond
    spacing: float = 20.0  # percent
    radius: float = 5.0
    spikes: int = 2
    hardness: float = 1.0
    aspect: float = 1.0
    angle: float = 0.0  # degrees


def parse_vbr(data: bytes) -> VbrBrush:
    text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    if not lines or lines[0] != "GIMP-VBR":
        raise GimpFormatError("not a GIMP-VBR file")
    try:
        version = lines[1]
        name = lines[2]
        if version == "1.0":
            spacing, radius, hardness, aspect, angle = (float(x) for x in lines[3:8])
            return VbrBrush(name, version, "circle", spacing, radius, 2, hardness, aspect, angle)
        if version == "1.5":
            shape = lines[3].lower()
            spacing, radius = float(lines[4]), float(lines[5])
            spikes = int(float(lines[6]))
            hardness, aspect, angle = (float(x) for x in lines[7:10])
            return VbrBrush(name, version, shape, spacing, radius, spikes, hardness, aspect, angle)
    except (IndexError, ValueError) as e:
        raise GimpFormatError(f"bad VBR: {e}") from e
    raise GimpFormatError(f"unknown VBR version {lines[1] if len(lines) > 1 else '?'}")


def encode_vbr(v: VbrBrush) -> bytes:
    def f(x: float) -> str:
        return f"{x:.6f}"

    lines = ["GIMP-VBR", "1.5", v.name.replace("\n", " "), v.shape,
             f(max(1.0, min(5000.0, v.spacing))), f(max(0.1, min(32767.0, v.radius))),
             str(max(2, min(20, int(v.spikes)))), f(max(0.0, min(1.0, v.hardness))),
             f(max(1.0, min(1000.0, v.aspect))), f(v.angle % 180.0)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_vbr(v: VbrBrush, max_size: int = 1024) -> Image.Image:
    """Rasterise a generated brush roughly like GIMP (circle/square/diamond, aspect, angle)."""
    size = max(3, min(max_size, int(math.ceil(v.radius * 2))))
    if v.shape == "circle" and v.spikes <= 2:
        return round_tip(size, v.hardness, 1.0 / max(1.0, v.aspect), v.angle)
    r = size / 2.0
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32) + 0.5 - r
    a = math.radians(v.angle)
    xr = xx * math.cos(a) + yy * math.sin(a)
    yr = (-xx * math.sin(a) + yy * math.cos(a)) * max(1.0, v.aspect)
    if v.shape == "square":
        d = np.maximum(np.abs(xr), np.abs(yr)) / r
    elif v.shape == "diamond":
        d = (np.abs(xr) + np.abs(yr)) / r
    else:
        d = np.hypot(xr, yr) / r
    if v.spikes > 2:
        theta = np.arctan2(yr, xr)
        d = d * (1 + 0.6 * (0.5 - 0.5 * np.cos(theta * v.spikes)))
    h = max(0.0, min(0.999, v.hardness))
    val = np.where(d <= h, 1.0, np.clip((1 - d) / max(1e-6, 1 - h), 0, 1))
    return Image.fromarray((val * 255).astype(np.uint8), "L")


# ---------------------------------------------------------------------------------------------
# GIMP pattern (.pat) - used for Krita textures
# ---------------------------------------------------------------------------------------------
def parse_pat(data: bytes) -> tuple[str, Image.Image]:
    """Decode a GIMP pattern; returns (name, RGB/RGBA/L image)."""
    if len(data) < 24:
        raise GimpFormatError("PAT header truncated")
    header_size, version, w, h, nbytes, magic = struct.unpack_from(">6I", data, 0)
    if magic != PAT_MAGIC or version != 1 or nbytes not in (1, 2, 3, 4):
        raise GimpFormatError("not a GIMP pattern")
    name = data[24:header_size].split(b"\0", 1)[0].decode("utf-8", "replace")
    raw = data[header_size:header_size + w * h * nbytes]
    if len(raw) < w * h * nbytes:
        raise GimpFormatError("PAT data truncated")
    mode = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}[nbytes]
    return name, Image.frombytes(mode, (w, h), raw)


def encode_pat(img: Image.Image, name: str) -> bytes:
    img = img.convert("L") if img.mode in ("1", "P", "I", "F") else img
    if img.mode not in ("L", "LA", "RGB", "RGBA"):
        img = img.convert("RGB")
    nbytes = {"L": 1, "LA": 2, "RGB": 3, "RGBA": 4}[img.mode]
    raw_name = name.encode("utf-8")[:255] + b"\0"
    return struct.pack(">6I", 24 + len(raw_name), 1, img.width, img.height, nbytes, PAT_MAGIC) + raw_name + img.tobytes()


# ---------------------------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------------------------
def _sel_to_order(sel: str) -> str:
    return "random" if sel in ("random", "velocity", "pressure", "xtilt", "ytilt") else "sequential"


def _gbr_to_brush(b: GbrBrush, name: str, path: Path, fmt_version: str, index: int = 0) -> Brush:
    br = Brush(name=name or path.stem, tips=[b.mask], size_px=float(max(b.size)), spacing=max(0.01, b.spacing / 100.0))
    br.source = BrushSource(app="gimp", format="gimp", version=fmt_version, path=str(path), index=index,
                            native={"spacing_percent": b.spacing, "bytes": b.bytes})
    if b.color is not None:
        br.warn("这是 GIMP 彩色笔刷，颜色信息已丢弃，只保留了笔刷形状（透明度）。")
    return br


def _vbr_to_brush(v: VbrBrush, path: Path) -> Brush:
    br = Brush(name=v.name or path.stem, size_px=max(1.0, v.radius * 2), hardness=max(0.0, min(1.0, v.hardness)),
               roundness=1.0 / max(1.0, v.aspect), angle=v.angle, spacing=max(0.01, v.spacing / 100.0))
    if v.shape != "circle" or v.spikes > 2:
        br.tips = [render_vbr(v)]
        br.warn(f"GIMP 生成笔刷的形状“{v.shape}”已转换为位图笔尖。")
    br.source = BrushSource(app="gimp", format="gimp", version=f"VBR {v.version}", path=str(path),
                            native={"vbr": v.__dict__.copy()})
    return br


def _gih_to_brush(name: str, params: dict[str, str], cells: list[GbrBrush], path: Path) -> Brush:
    sel = params.get("sel0", "random")
    br = Brush(name=name or path.stem, tips=[c.mask for c in cells],
               size_px=float(max(max(c.size) for c in cells)),
               spacing=max(0.01, cells[0].spacing / 100.0), tip_order=_sel_to_order(sel))
    if sel == "angular":
        br.warn("GIMP 动态笔刷按笔画方向选择笔尖，已改为依次使用各笔尖。")
    elif sel in ("velocity", "pressure", "xtilt", "ytilt"):
        br.warn(f"GIMP 动态笔刷按“{sel}”选择笔尖，已改为随机使用各笔尖。")
    try:
        dim = int(params.get("dim", "1"))
    except ValueError:
        dim = 1
    if dim > 1:
        br.warn("该 GIMP 动态笔刷有多个选择维度，已合并为一组笔尖。")
    if any(c.color is not None for c in cells):
        br.warn("这是 GIMP 彩色笔刷，颜色信息已丢弃，只保留了笔刷形状（透明度）。")
    br.source = BrushSource(app="gimp", format="gimp", version="GIH", path=str(path),
                            native={"gih_params": dict(params)})
    return br


def brush_from_bytes(data: bytes, filename: str) -> list[Brush]:
    """Decode GIMP brush bytes (used by the Krita codec for tips inside bundles)."""
    path = Path(filename)
    ext = path.suffix.lower()
    if ext == ".gih":
        name, params, cells = parse_gih(data)
        return [_gih_to_brush(name, params, cells, path)]
    if ext == ".vbr":
        return [_vbr_to_brush(parse_vbr(data), path)]
    b, _ = parse_gbr(data)
    return [_gbr_to_brush(b, b.name, path, f"GBR v{b.version}")]


def _prepare_tip(img: Image.Image, size_px: float, resample: bool = True) -> Image.Image:
    img = img.convert("L")
    target = int(round(max(1.0, min(float(GBR_MAX_SIZE), size_px))))
    side = max(img.size)
    if side > GBR_MAX_SIZE or (resample and side != target and target > 0):
        s = target / side if resample else GBR_MAX_SIZE / side
        img = img.resize((max(1, int(round(img.width * s))), max(1, int(round(img.height * s)))), Image.LANCZOS)
    return img


def brush_to_vbr(b: Brush) -> VbrBrush:
    rnd = max(0.001, min(1.0, b.roundness))
    return VbrBrush(name=b.name, version="1.5", shape="circle", spacing=max(1.0, b.spacing * 100.0),
                    radius=max(0.5, b.size_px / 2.0), spikes=2, hardness=b.hardness, aspect=1.0 / rnd,
                    angle=b.angle)


def _warn_lossy(b: Brush) -> None:
    lost = []
    if b.size_dynamic.is_active() or b.opacity_dynamic.is_active() or b.flow_dynamic.is_active():
        lost.append("压感/动态")
    if b.texture is not None:
        lost.append("纹理")
    if b.scatter > 0 or b.angle_jitter > 0:
        lost.append("散布/角度抖动")
    if b.dual is not None:
        lost.append("双重笔刷")
    if b.color.is_active():
        lost.append("颜色动态")
    if lost:
        b.warn("GIMP 笔刷文件只保存笔尖形状和间距，" + "、".join(lost) + "需要在 GIMP 的“绘画动态”中自行设置。")


@register_codec
class GimpCodec(FormatCodec):
    id = "gimp"
    name = "GIMP 笔刷 (.gbr/.gih/.vbr)"
    extensions = (".gbr", ".gih", ".vbr")
    can_read = True
    can_write = True

    # -- sniff ----------------------------------------------------------------------------------
    def sniff(self, path: Path) -> Optional[FileInfo]:
        path = Path(path)
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
        except OSError:
            return None
        ext = path.suffix.lower()
        if head.startswith(b"GIMP-VBR"):
            try:
                v = parse_vbr(path.read_bytes())
            except GimpFormatError:
                return None
            info = FileInfo(path, self.id, self.name, f"VBR {v.version}", 1,
                            details={"类型": "GIMP 生成笔刷", "形状": v.shape, "半径": f"{v.radius:g}px"})
            info.fixes.append(Fix("转换为 GBR", "把参数化的生成笔刷渲染成普通位图笔刷（.gbr），方便 Krita 等软件导入。",
                                  _fix_vbr_to_gbr))
            return info
        try:
            h = parse_gbr_header(head)
            if h["version"] == 1 and ext != ".gbr":  # v1 has no magic number: trust the extension only
                raise GimpFormatError("unverified GBR v1")
            info = FileInfo(path, self.id, self.name, f"GBR v{h['version']}", 1,
                            details={"类型": "GIMP 位图笔刷", "名称": h.get("name", ""), "尺寸": f"{h['width']}x{h['height']}",
                                     "颜色": "彩色" if h["bytes"] == 4 else "灰度", "间距": f"{h['spacing']}%"})
            if h["version"] == 3:
                info.details["来源"] = "CinePaint (16 位浮点)"
                info.problems.append("这是 CinePaint 的 16 位浮点笔刷（GBR v3），只有 GIMP 能读取，Krita 不支持。")
                info.fixes.append(Fix("转换为标准 GBR v2", "转换为 8 位灰度 GBR v2，所有软件都能导入。", _fix_to_gbr_v2))
            elif h["version"] == 1:
                info.fixes.append(Fix("升级为 GBR v2", "旧版 GBR v1 没有间距信息，升级为 v2 格式。", _fix_to_gbr_v2))
            return info
        except GimpFormatError:
            pass
        if ext == ".gih" or b"ncells:" in head[:1024]:
            try:
                name, params, cells = parse_gih(path.read_bytes())
            except (GimpFormatError, struct.error):
                return None
            return FileInfo(path, self.id, self.name, "GIH", 1,
                            details={"类型": "GIMP 动态笔刷（图像管道）", "笔尖数": str(len(cells)),
                                     "选择方式": params.get("sel0", "?"), "名称": name})
        return None

    # -- read -----------------------------------------------------------------------------------
    def read(self, path: Path) -> list[Brush]:
        path = Path(path)
        data = path.read_bytes()
        if data.startswith(b"GIMP-VBR"):
            return [_vbr_to_brush(parse_vbr(data), path)]
        try:
            b, end = parse_gbr(data)
        except GimpFormatError:
            name, params, cells = parse_gih(data)
            return [_gih_to_brush(name, params, cells, path)]
        return [_gbr_to_brush(b, b.name, path, f"GBR v{b.version}")]

    # -- write ----------------------------------------------------------------------------------
    def write(self, brushes: list[Brush], out_path: Path, **options) -> list[Path]:
        """Write each brush as .gbr (one tip), .gih (several tips) or .vbr (computed, on request).

        options: ``resample`` (default True) scales tips to the brush size so GIMP's default
        size matches; ``prefer_vbr`` writes computed round brushes as .vbr.
        """
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        resample = options.get("resample", True)
        prefer_vbr = options.get("prefer_vbr", out_path.suffix.lower() == ".vbr")
        written: list[Path] = []
        single = len(brushes) == 1
        for b in brushes:
            if not b.tips and prefer_vbr:
                ext, data = ".vbr", encode_vbr(brush_to_vbr(b))
            elif len(b.tips) > 1:
                masks = [_prepare_tip(t, b.size_px, resample) for t in b.tips]
                sel = "random" if b.tip_order == "random" else "incremental"
                ext, data = ".gih", encode_gih(masks, b.name, int(round(b.spacing * 100)), sel)
            else:
                ext, data = ".gbr", encode_gbr(_prepare_tip(b.main_tip(), b.size_px, resample), b.name,
                                               int(round(b.spacing * 100)))
            _warn_lossy(b)
            if b.roundness < 0.99 and b.tips:
                b.warn("GIMP 位图笔刷不保存“圆度”，请在 GIMP 工具选项的“纵横比”中调整。")
            if abs(b.angle) > 0.5 and b.tips:
                b.warn("GIMP 位图笔刷不保存角度，请在 GIMP 工具选项的“角度”中调整。")
            if single:
                target = out_path.with_suffix(ext)
            else:
                target = unique_path(out_path.parent / (safe_filename(b.name) + ext))
            target.write_bytes(data)
            written.append(target)
        return written

    def compatibility(self, info: FileInfo, app_id: str, app_version: str) -> Optional[str]:
        v = info.version
        if app_id == "krita":
            if v.startswith("VBR"):
                return "Krita 不能导入 GIMP 生成笔刷（.vbr），请先转换为 .gbr。"
            if v == "GBR v3":
                return "Krita 不支持 CinePaint 16 位浮点笔刷（GBR v3），请先转换为标准 GBR v2。"
        if app_id == "gimp":
            if v == "VBR 1.5" and app_version and _major_minor(app_version) < (2, 4):
                return "GIMP 2.4 之前的版本不支持带形状/尖角的生成笔刷（VBR 1.5）。"
        return None


def _major_minor(v: str) -> tuple[int, int]:
    nums = [int(x) for x in re.findall(r"\d+", v)[:2]]
    while len(nums) < 2:
        nums.append(0)
    return nums[0], nums[1]


def _fix_to_gbr_v2(src: Path, out_dir: Path) -> list[Path]:
    b, _ = parse_gbr(Path(src).read_bytes())
    out = unique_path(Path(out_dir) / (Path(src).stem + ".gbr"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(encode_gbr(b.mask, b.name or Path(src).stem, b.spacing, b.color if b.bytes == 4 else None))
    return [out]


def _fix_vbr_to_gbr(src: Path, out_dir: Path) -> list[Path]:
    v = parse_vbr(Path(src).read_bytes())
    out = unique_path(Path(out_dir) / (Path(src).stem + ".gbr"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(encode_gbr(render_vbr(v), v.name or Path(src).stem, int(round(v.spacing))))
    return [out]


def read_any_image(data: bytes, filename: str = "") -> Image.Image:
    """Open PNG/JPG/... or GIMP .pat bytes as a PIL image (sniffed by content, not by name)."""
    if len(data) >= 24 and data[20:24] == b"GPAT":
        return parse_pat(data)[1]
    img = Image.open(io.BytesIO(data))
    img.load()
    return img
