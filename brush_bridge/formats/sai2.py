"""PaintTool SAI Ver.2: custom tool presets (.saitdat) and brush assets (8-bit BMP + INI).

SAI2 keeps user brushes in a *settings* folder (``Documents\\SYSTEMAX Software Development\\SAIv2\\settings``
since build 2020-01-07, next to ``sai2.exe`` before that)::

    brushfom\\blotmap\\*.bmp      brush form "blots" (256x256 grey noise)
    brushfom\\bristle\\*.bmp      brush form "bristle" (63x63 RGB, black dots = hairs)
    brushfom\\brshape\\*.bmp+ini  brush form "brush shape" (white = paint)
    scatter\\*.bmp+ini            scatter shapes (white = paint)
    brushtex\\*.bmp, papertex\\*.bmp   512x512 grey textures
    custool\\{nrm,ink,shp}\\N.saitdat   custom tools, grouped by _N.saitgrp, ordered by _0.saitset

See docs/formats/sai2.md for the full description.
"""
from __future__ import annotations

import copy
import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image

from ..core.model import Brush, BrushRef, BrushSource, Texture, WetMix, join_group
from ..core.plugin import FileInfo, Fix, FormatCodec, register_codec
from ..core.system import backup, documents_dir, safe_filename, unique_path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------
ASSET_DIRS: dict[str, tuple[str, ...]] = {
    "scatter": ("scatter",),
    "brshape": ("brushfom", "brshape"),
    "blotmap": ("brushfom", "blotmap"),
    "bristle": ("brushfom", "bristle"),
    "brushtex": ("brushtex",),
    "papertex": ("papertex",),
}
ASSET_LABELS = {
    "scatter": "散布形状", "brshape": "笔刷形状", "blotmap": "渗化形态", "bristle": "鬃毛形态",
    "brushtex": "笔刷纹理", "papertex": "纸张纹理",
}
TEXTURE_KINDS = ("brushtex", "papertex")
FOMCAT = {1: "blotmap", 2: "bristle", 3: "brshape", 4: "scatter"}
FOMCAT_ID = {v: k for k, v in FOMCAT.items()}
TOOL_CATEGORIES = {"nrm": "普通", "ink": "墨水", "shp": "形状"}
TOOL_LABELS = {
    "pencil": "铅笔", "airbrush": "喷枪", "brush": "画笔", "water": "水彩笔", "marker": "马克笔",
    "eraser": "橡皮擦", "selpen": "选区笔", "selers": "选区擦", "bucket": "油漆桶", "gradation": "渐变",
    "blur": "模糊", "effpen": "特效笔", "scatter": "散布", "smudge": "涂抹", "binpen": "二值笔",
    "dotpen": "像素画", "biners": "二值橡皮擦", "bselpen": "选区笔", "bselers": "选区擦", "inkpen": "钢笔",
    "inkers": "修正液", "inkchg": "修改线", "inkcurv": "曲线", "inkline": "折线", "inkprs": "笔压",
    "binkpen": "钢笔", "binkers": "修正液", "binkchg": "修改线", "binkcurv": "曲线", "binkline": "折线",
    "shapecol": "改颜色", "pathedit": "编辑路径", "pathplot": "创建路径",
}
ERASER_TOOLS = {"eraser", "biners", "inkers", "binkers"}
NON_PAINT_TOOLS = {"selpen", "selers", "bselpen", "bselers", "inkchg", "binkchg", "inkprs"}
WET_TOOLS = {"brush", "water", "marker"}
MODSTR = 1836216174  # constant found in every tool parameter section

# Build dates (from history.txt) that introduced the features this codec reads/writes.
FEATURES: list[tuple[str, str, str]] = [
    ("2016-01-19", "bristle", "鬃毛形态参数"),
    ("2018-09-17", "scatter_shape", "散布形状（scatter 文件夹）"),
    ("2019-05-18", "scatter_tool", "散布工具、特效笔、笔刷形状（brshape）"),
    ("2019-07-28", "brshape_angle", "笔刷形状的角度/角度抖动"),
    ("2020-01-07", "custool", "自定义工具文件（custool）和“文档\\SYSTEMAX Software Development\\SAIv2\\settings”设置目录"),
]
FEATURE_BUILD = {key: build for build, key, _ in FEATURES}
FEATURE_TEXT = {key: text for _, key, text in FEATURES}
SETTINGS_LAYOUT_BUILD = FEATURE_BUILD["custool"]
TEXTURE_SIZE = 512
MAX_SHAPE_SIDE = 1024


def build_date(text: str) -> Optional[str]:
    """Extract a SAI2 build date ``YYYY-MM-DD`` from a version string / history line."""
    m = re.search(r"(20\d\d)[-./](\d\d)[-./](\d\d)", text or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def required_build(features: set[str] | list[str]) -> str:
    return max((FEATURE_BUILD[f] for f in features if f in FEATURE_BUILD), default="")


def default_settings_dir() -> Path:
    return documents_dir() / "SYSTEMAX Software Development" / "SAIv2" / "settings"


# ---------------------------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------------------------
def decode_text(data: bytes) -> str:
    """SAI2 text files are UTF-8; INI files from Japanese packs may be Shift-JIS, Chinese ones GBK."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    candidates = []
    for enc in ("cp932", "gbk"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        kana = sum(1 for ch in text if "\u3040" <= ch <= "\u30ff")
        candidates.append((kana if enc == "cp932" else 0.5, text))
    if candidates:
        return max(candidates, key=lambda c: c[0])[1]
    return data.decode("utf-8", "replace")


def parse_ini(text: str) -> dict[str, str]:
    """Parse a SAI2 shape INI (``[Parameters]`` with tab-indented ``Key = value`` lines)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in ";#[":
            continue
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.split(";", 1)[0].strip()
    return out


def ini_number(values: dict[str, str], key: str, default: float) -> float:
    v = values.get(key)
    if v is None:
        return default
    try:
        return float(v.replace("+", "").replace("%", "").strip())
    except ValueError:
        return default


def ratio_to_roundness(v: str) -> float:
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", v or "")
    if not m:
        return 1.0
    a, b = float(m.group(1)), float(m.group(2))
    if a <= 0 or b <= 0:
        return 1.0
    return min(a, b) / max(a, b)


# (key, label, range text, default) -- the comment style mirrors the files shipped with SAI2.
_INI_DOC = {
    "Opacity": ("浓度", "0 ～ 100", "100"),
    "AngleControl": ("角度控制", "0 ... 无 / 1 ... 自动 / 2 ... 笔杆方向", "1"),
    "Angle": ("角度", "-180 ～ +180 (角度)", "0"),
    "AngleJitter": ("角度抖动", "0 ～ 100 (%)", "0"),
    "Scaling": ("倍率", "0 ～ 200 (%)", "100"),
    "ShapeSize": ("图案大小", "1 ～ 5000 (px)", "100"),
    "SizeJitter": ("大小抖动", "0 ～ 100 (%)", "0"),
    "Spacing": ("间距", "0 ～ 1000 (%)；笔刷形状为 0 ～ 50", "25"),
    "Scattering": ("散布", "0 ～ 1000 (%)；笔刷形状为 0 ～ 50", "0"),
    "WxHRatio": ("W:H (纵横比)", "100:1 ～ 100:100 ～ 1:100", "100:100"),
    "WxHJitter": ("WH抖动 (纵横比抖动)", "0 ～ 100 (%)", "0"),
    "Count": ("计数", "1 ～ 20", "1"),
    "CountJitter": ("计数抖动", "0 ～ 100 (%)", "0"),
    "AllDirScattering": ("往全方向散布", "0 ... 仅与笔画方向直交 / 1 ... 全方向", "0"),
    "GaussianDistribution": ("高斯分布", "0 ... 均匀 / 1 ... 离光标越远密度越低", "0"),
    "AbsoluteSize": ("绝对大小", "0 ... 画笔大小×倍率 / 1 ... 使用“图案大小”", "0"),
    "IntegerPosition": ("整数坐标", "0 / 1", "0"),
    "HighQualitySampling": ("高品质缩小", "0 ... 速度优先 / 1 ... 品质优先", "0"),
    "ColJitter": ("前景色到背景色的抖动", "0 ～ 100 (%)", "0"),
    "HueJitter": ("色相抖动", "0 ～ 100 (%)", "0"),
    "SaturationJitter": ("饱和度抖动", "0 ～ 100 (%)", "0"),
    "BrightnessJitter": ("亮度抖动", "0 ～ 100 (%)", "0"),
    "ApplyToEachShape": ("应用到每一个形状", "0 ... 每个笔画 / 1 ... 每一个形状", "0"),
}


def format_ini(values: dict[str, str]) -> str:
    """Write a shape INI in SAI2's own layout (UTF-8, LF, tab indented, commented)."""
    lines = [";", "; 初始参数（由 brush-bridge 生成）", ";", "[Parameters]", ""]
    for key, value in values.items():
        label, rng, default = _INI_DOC.get(key, (key, "", ""))
        lines += ["\t;", f"\t; [{label}]", "\t;"]
        if rng:
            lines += [f"\t;    值的设置范围：{rng}", "\t;", f"\t;    ※ 选项不存在时默认值为 {default}。", "\t;"]
        lines += [f"\t{key} = {value}", ""]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------
# .saitdat / .saitgrp / .saitset / .saitlnk  ("key=T:value" lines, "." ends a section, "--EOF--")
# ---------------------------------------------------------------------------------------------
Entry = tuple[str, str, Any]  # (key, type letter or "" for plain "a=b" index lines, value)
Section = list[Entry]


def parse_sait(text: str) -> list[Section]:
    sections: list[Section] = [[]]
    for line in text.replace("\r\n", "\n").split("\n"):
        if line == "--EOF--":
            break
        if line == ".":
            sections.append([])
            continue
        if not line:
            continue
        key, _, rest = line.partition("=")
        m = re.match(r"^([A-Z]):(.*)$", rest, re.S)
        if m:
            t, v = m.group(1), m.group(2)
            sections[-1].append((key, t, _parse_int(v) if t == "I" else v))
        else:
            sections[-1].append((key, "", rest))
    if not sections[-1]:
        sections.pop()
    return sections


def _parse_int(v: str) -> Any:
    try:
        return int(v)
    except ValueError:
        return v


def dump_sait(sections: list[Section]) -> str:
    lines: list[str] = []
    for sec in sections:
        for key, t, v in sec:
            v = str(v).replace("\r", " ").replace("\n", " ")
            lines.append(f"{key}={t}:{v}" if t else f"{key}={v}")
        lines.append(".")
    lines.append("--EOF--")
    return "\n".join(lines) + "\n"


def sait_values(sec: Section) -> dict[str, Any]:
    return {k: v for k, _t, v in sec}


def sait_set(sec: Section, key: str, value: Any, t: str = "I") -> None:
    for i, (k, tt, _v) in enumerate(sec):
        if k == key:
            sec[i] = (k, tt, value)
            return
    sec.append((key, t, value))


def read_sait(path: Path) -> list[Section]:
    return parse_sait(decode_text(Path(path).read_bytes()))


def tool_groups(cat_dir: Path) -> list[tuple[int, str, list[tuple[int, int]]]]:
    """Groups of a custool category folder: [(group id, name, [(slot, tool id), ...])] in tab order."""
    cat_dir = Path(cat_dir)
    order: list[int] = []
    sset = cat_dir / "_0.saitset"
    if sset.is_file():
        secs = read_sait(sset)
        if len(secs) > 1:
            for k, _t, v in sorted(secs[1], key=lambda e: _parse_int(e[0]) if str(e[0]).isdigit() else 0):
                if str(v).strip().isdigit():
                    order.append(int(v))
    groups: dict[int, tuple[str, list[tuple[int, int]]]] = {}
    for f in cat_dir.glob("_*.saitgrp"):
        m = re.fullmatch(r"_(\d+)", f.stem)
        if not m:
            continue
        try:
            secs = read_sait(f)
        except OSError:
            continue
        name = str(sait_values(secs[0]).get("name", "")) if secs else ""
        slots = []
        for k, _t, v in (secs[1] if len(secs) > 1 else []):
            if str(k).isdigit() and str(v).strip().isdigit():
                slots.append((int(k), int(v)))
        groups[int(m.group(1))] = (name, sorted(slots))
    ordered = [g for g in order if g in groups] + sorted(g for g in groups if g not in order)
    return [(g, groups[g][0], groups[g][1]) for g in ordered]


def tool_group_names(cat_dir: Path) -> dict[int, str]:
    out: dict[int, str] = {}
    for _gid, name, slots in tool_groups(cat_dir):
        for _slot, tid in slots:
            out.setdefault(tid, name)
    return out


# Parameter section templates (key order as written by SAI2 2021).
_BASE = [("modstr", "I", MODSTR), ("wavno", "I", 4), ("curszscl", "I", 1), ("cursize", "I", 500),
         ("minsize", "I", 50), ("curdens", "I", 100), ("mindens", "I", 0), ("fomgid", "I", 0),
         ("fomcat", "I", 0), ("fomnam", "U", ""), ("blotval", "I", 50), ("blotscl", "I", 100),
         ("blotinvf", "I", 0), ("blotivtf", "I", 1), ("blotsharpf", "I", 0), ("brslmin", "I", 0),
         ("brslsiz", "I", 50), ("brslrnd", "I", 0), ("brsldir", "I", 1), ("brslturnf", "I", 1),
         ("scatden", "I", 100), ("scatanc", "I", 1), ("scatang", "I", 0), ("scatanj", "I", 0),
         ("scatscl", "I", 100), ("scatszj", "I", 0), ("scatspc", "I", 25), ("scatsct", "I", 0),
         ("scathqsf", "I", 0)]
_TEX = [("texgid", "I", 0), ("texcat", "I", 0), ("texnam", "U", ""), ("texval", "I", 95), ("texscl", "I", 100),
        ("texscr", "I", 0), ("texinvf", "I", 0), ("texivtf", "I", 1)]
_WET = [("blend", "I", 50), ("dilution", "I", 0), ("persist", "I", 50), ("keepaf", "I", 0), ("blurthres", "I", 50)]
_MID = [("hardness", "I", 50), ("densamp", "I", 0), ("v1dnprsf", "I", 0), ("flattenf", "I", 1), ("szprsf", "I", 1),
        ("dnprsf", "I", 1)]
_WETPRS = [("blprsf", "I", 1), ("dlprsf", "I", 0)]
_SENS = [("szsens", "I", 100), ("dnsens", "I", 100)]
_TAIL = [("sbpos_patlist", "I", 0), ("previewbg", "I", 0), ("showf_fom", "I", 0), ("showf_tex", "I", 0)]
_SCATTER_TOOL = [
    ("modstr", "I", MODSTR), ("wavno", "I", 0), ("curszscl", "I", 1), ("cursize", "I", 500), ("minsize", "I", 50),
    ("curdens", "I", 100), ("mindens", "I", 0), ("fomgid", "I", 0), ("fomcat", "I", 4), ("fomnam", "U", ""),
    ("scathard", "I", 100), ("scatden", "I", 100), ("scatanc", "I", 1), ("scatang", "I", 0), ("scatanj", "I", 0),
    ("scatscl", "I", 100), ("scatsiz", "I", 50), ("scatszscl", "I", 1), ("scatszj", "I", 0), ("scatspc", "I", 25),
    ("scatsct", "I", 0), ("scatwth", "I", 0), ("scatwhj", "I", 0), ("scatcnt", "I", 1), ("scatcnj", "I", 0),
    ("scataldf", "I", 0), ("scatgauf", "I", 0), ("scatabsf", "I", 0), ("scatintf", "I", 0), ("scathqsf", "I", 0),
    ("scatcolj", "I", 0), ("scathuej", "I", 0), ("scatsatj", "I", 0), ("scatbrij", "I", 0), ("scataesf", "I", 0),
    *_TEX, ("szprsf", "I", 1), ("dnprsf", "I", 1), ("szsens", "I", 100), ("dnsens", "I", 100),
    ("sbpos_patlist", "I", 0), ("previewbg", "I", 0), ("showf_scat", "I", 0), ("showf_scatcol", "I", 0),
    ("showf_tex", "I", 0)]


def tool_template(tid: str) -> Section:
    if tid == "scatter":
        return list(_SCATTER_TOOL)
    if tid in WET_TOOLS:
        return _BASE + _TEX + _WET + _MID + _WETPRS + _SENS + [("blsens", "I", 100)] + _TAIL
    return _BASE + _TEX + _MID + _SENS + _TAIL


def tool_header(tid: str, name: str, memo: str = "") -> Section:
    return [("tidstr", "S", tid), ("xidstr", "S", ""), ("vkc", "I", 0), ("tabflt", "I", 0), ("tfmode", "I", 0),
            ("bzintf", "I", 1), ("name", "U", name), ("addt", "U", memo)]


# Tool parameter key <-> shape INI key (same meaning, same units).
TOOL_TO_INI = {
    "scatanc": "AngleControl", "scatang": "Angle", "scatanj": "AngleJitter", "scatscl": "Scaling",
    "scatszj": "SizeJitter", "scatspc": "Spacing", "scatsct": "Scattering", "scathqsf": "HighQualitySampling",
    "scatwhj": "WxHJitter", "scatcnt": "Count", "scatcnj": "CountJitter", "scataldf": "AllDirScattering",
    "scatgauf": "GaussianDistribution", "scatabsf": "AbsoluteSize", "scatintf": "IntegerPosition",
    "scatcolj": "ColJitter", "scathuej": "HueJitter", "scatsatj": "SaturationJitter", "scatbrij": "BrightnessJitter",
    "scataesf": "ApplyToEachShape",
}
INI_TO_TOOL = {v: k for k, v in TOOL_TO_INI.items()}


# ---------------------------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------------------------
def load_gray(data: bytes | Path) -> Image.Image:
    im = Image.open(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data)
    im.load()
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
        im = Image.alpha_composite(bg, im)
    return im.convert("L")


def encode_bmp(img: Image.Image) -> bytes:
    """8 bpp BMP with an identity grey palette, the only kind SAI2 accepts."""
    buf = io.BytesIO()
    img.convert("L").save(buf, "BMP")
    return buf.getvalue()


def bristle_to_tip(img: Image.Image) -> Image.Image:
    """Bristle BMPs mark each hair with a black dot on white; turn the dots into a paint mask."""
    a = np.asarray(img.convert("L"), dtype=np.uint8)
    return Image.fromarray(np.where(a < 48, 255, 0).astype(np.uint8), "L")


def prepare_tip(img: Image.Image) -> Image.Image:
    g = img.convert("L")
    if max(g.size) > MAX_SHAPE_SIDE:
        g = g.copy()
        g.thumbnail((MAX_SHAPE_SIDE, MAX_SHAPE_SIDE), Image.LANCZOS)
    return g


def _seam_ratio(a: np.ndarray) -> float:
    inner = (np.abs(np.diff(a, axis=0)).mean() + np.abs(np.diff(a, axis=1)).mean()) / 2 + 1e-3
    seam = (np.abs(a[0] - a[-1]).mean() + np.abs(a[:, 0] - a[:, -1]).mean()) / 2
    return float(seam / inner)


def make_tileable(img: Image.Image) -> Image.Image:
    """Hide the wrap-around seam by cross-fading with a half-period shifted copy."""
    a = np.asarray(img.convert("L"), dtype=np.float32)
    if _seam_ratio(a) < 2.0:
        return img.convert("L")
    h, w = a.shape
    r = np.roll(a, (h // 2, w // 2), axis=(0, 1))
    y = np.minimum(np.arange(h), h - 1 - np.arange(h))[:, None]
    x = np.minimum(np.arange(w), w - 1 - np.arange(w))[None, :]
    wgt = np.clip(np.minimum(y, x) / (min(h, w) / 4), 0, 1)
    out = a * wgt + r * (1 - wgt)
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8), "L")


def prepare_texture(img: Image.Image) -> tuple[Image.Image, float]:
    """Return a 512x512 tileable grey texture and the factor its pattern was shrunk by."""
    g = img.convert("L")
    w, h = g.size
    s = min(w, h)
    if (w, h) != (s, s):
        g = g.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    if s >= TEXTURE_SIZE:
        period = TEXTURE_SIZE
        g = g.resize((TEXTURE_SIZE, TEXTURE_SIZE), Image.LANCZOS) if s != TEXTURE_SIZE else g
        g = make_tileable(g)
    else:
        reps = max(1, round(TEXTURE_SIZE / s))
        period = TEXTURE_SIZE // reps
        tile = make_tileable(g.resize((period, period), Image.LANCZOS))
        g = Image.new("L", (period * reps, period * reps))
        for iy in range(reps):
            for ix in range(reps):
                g.paste(tile, (ix * period, iy * period))
        if g.size != (TEXTURE_SIZE, TEXTURE_SIZE):
            g = g.resize((TEXTURE_SIZE, TEXTURE_SIZE), Image.LANCZOS)
    return g, s / period


# ---------------------------------------------------------------------------------------------
# Assets and settings folders
# ---------------------------------------------------------------------------------------------
@dataclass
class Asset:
    kind: str  # key of ASSET_DIRS
    name: str  # file stem, also the name shown in SAI2's drop-down lists
    image: Image.Image  # model polarity ("L", 255 = paint / white)
    ini: Optional[dict[str, str]] = None
    bmp_bytes: Optional[bytes] = None  # original file (lossless reinstall)
    ini_bytes: Optional[bytes] = None

    def encoded(self) -> tuple[bytes, Optional[bytes]]:
        bmp = self.bmp_bytes if self.bmp_bytes is not None else encode_bmp(self.image)
        if self.ini_bytes is not None:
            ini = self.ini_bytes
        else:
            ini = format_ini(self.ini).encode("utf-8") if self.ini is not None else None
        return bmp, ini


def asset_dirs(root: Path, kind: str) -> list[Path]:
    rel = ASSET_DIRS[kind]
    dirs = [Path(root).joinpath(*rel)]
    if len(rel) == 2:
        dirs.append(Path(root) / rel[1])  # pre-2020 layout: brshape/blotmap/bristle next to sai2.exe
    return dirs


def find_asset(roots: list[Path], kind: str, name: str) -> Optional[Path]:
    if not name:
        return None
    for root in roots:
        for d in asset_dirs(root, kind):
            p = d / f"{name}.bmp"
            if p.is_file():
                return p
    return None


def load_asset(path: Path, kind: str) -> Asset:
    path = Path(path)
    data = path.read_bytes()
    ini_path = path.with_suffix(".ini")
    ini_bytes = ini_path.read_bytes() if ini_path.is_file() else None
    img = Image.open(io.BytesIO(data))
    img.load()
    img = bristle_to_tip(img) if kind == "bristle" else load_gray(data)
    return Asset(kind, path.stem, img, parse_ini(decode_text(ini_bytes)) if ini_bytes else None, data, ini_bytes)


_ROOT_MARKERS = ("custool", "brushfom", "scatter", "brushtex", "papertex", "brshape", "blotmap", "bristle")


def is_settings_root(d: Path, minimum: int = 1) -> bool:
    try:
        return Path(d).is_dir() and sum((Path(d) / m).is_dir() for m in _ROOT_MARKERS) >= minimum
    except OSError:
        return False


def settings_root_of(path: Path) -> Optional[Path]:
    p = Path(path)
    p = p if p.is_dir() else p.parent
    for cand in [p, *list(p.parents)[:3]]:
        if is_settings_root(cand):
            return cand
    return None


def asset_kind_of(path: Path) -> Optional[str]:
    """Classify a BMP by its folder (scatter/brshape/...) or, for loose files, by its INI keys."""
    path = Path(path)
    parent = path.parent.name.lower()
    if parent in ASSET_DIRS:
        return parent
    ini = path.with_suffix(".ini")
    if ini.is_file():
        try:
            values = parse_ini(decode_text(ini.read_bytes()[:65536]))
        except OSError:
            return None
        if not any(k in values for k in _INI_DOC):
            return None
        scatter_only = ("ShapeSize", "Count", "WxHRatio", "AbsoluteSize", "HueJitter", "ColJitter")
        if "Opacity" in values and not any(k in values for k in scatter_only):
            return "brshape"
        return "scatter"
    return None


def _resolve_roots(path: Path) -> list[Path]:
    roots = []
    r = settings_root_of(path)
    if r:
        roots.append(r)
    try:
        d = default_settings_dir()
        if d.is_dir() and d not in roots:
            roots.append(d)
    except Exception:
        pass
    return roots


# ---------------------------------------------------------------------------------------------
# SAI2 -> model
# ---------------------------------------------------------------------------------------------
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _apply_shape_params(b: Brush, p: dict[str, float], kind: str) -> None:
    """Apply shape parameters (INI keys) to ``b``; the same keys are used by tool presets."""
    anc = int(p.get("AngleControl", 1))
    b.angle_follow_stroke = anc == 1
    if anc == 2:
        b.warn("SAI2 的角度控制“笔杆方向”在其他软件中可能不受支持。")
    b.angle = float(p.get("Angle", 0))
    b.angle_jitter = _clamp(p.get("AngleJitter", 0) / 100, 0, 1)
    b.size_dynamic.jitter = _clamp(p.get("SizeJitter", 0) / 100, 0, 1)
    b.spacing = max(0.01, p.get("Spacing", 25) / 100)
    b.scatter = max(0.0, p.get("Scattering", 0) / 100)
    if "Opacity" in p:
        b.flow = _clamp(p["Opacity"] / 100, 0, 1)
    if kind == "scatter":
        b.count = int(_clamp(p.get("Count", 1), 1, 20))
        b.count_jitter = _clamp(p.get("CountJitter", 0) / 100, 0, 1)
        b.roundness_jitter = _clamp(p.get("WxHJitter", 0) / 100, 0, 1)
        b.scatter_both_axes = bool(p.get("AllDirScattering", 0))
        b.color.fg_bg_jitter = _clamp(p.get("ColJitter", 0) / 100, 0, 1)
        b.color.hue_jitter = _clamp(p.get("HueJitter", 0) / 100, 0, 1)
        b.color.saturation_jitter = _clamp(p.get("SaturationJitter", 0) / 100, 0, 1)
        b.color.brightness_jitter = _clamp(p.get("BrightnessJitter", 0) / 100, 0, 1)
        b.color.per_tip = bool(p.get("ApplyToEachShape", 0))


def brush_from_asset(path: Path, kind: Optional[str] = None, asset: Optional[Asset] = None) -> Brush:
    """A scatter / brush-shape BMP (+INI) or a texture BMP as a model brush."""
    kind = kind or asset_kind_of(path) or "scatter"
    a = asset or load_asset(path, kind)
    b = Brush(name=a.name, group=ASSET_LABELS.get(kind, ""))
    asset_only = kind not in ("scatter", "brshape")
    if kind in TEXTURE_KINDS or kind == "blotmap":
        b.texture = Texture(image=a.image, name=a.name)
        b.notes.append(f"这是 SAI2 的{ASSET_LABELS[kind]}，不是完整的笔刷。")
    else:
        b.tips = [a.image]
        p = {k: ini_number(a.ini or {}, k, 0) for k in _INI_DOC if a.ini and k in a.ini}
        _apply_shape_params(b, p, kind)
        if kind == "scatter":
            b.roundness = ratio_to_roundness((a.ini or {}).get("WxHRatio", "100:100"))
            b.size_px = _clamp(p.get("ShapeSize", 100), 1, 5000)
        elif kind == "brshape":
            b.size_px = _clamp(max(a.image.size) * p.get("Scaling", 100) / 100, 1, 5000)
        else:  # bristle
            b.size_px = float(max(a.image.size))
            b.warn("SAI2 的鬃毛形态已近似为笔尖图案（毛的散开、方向等效果无法转换）。")
    b.source = BrushSource(app="sai2", format="sai2", version=kind, path=str(path),
                           native={"sai2_assets": {"tex" if asset_only and kind != "bristle" else "fom": a},
                                   "sai2_asset_only": asset_only})
    return b


def brush_from_tool(path: Path, sections: Optional[list[Section]] = None, roots: Optional[list[Path]] = None,
                    group: str = "", cache: Optional[dict] = None) -> Optional[Brush]:
    """Convert a custom tool preset to a model brush (None for non-painting tools)."""
    path = Path(path)
    sections = sections if sections is not None else read_sait(path)
    roots = roots if roots is not None else _resolve_roots(path)
    cache = cache if cache is not None else {}
    if not sections:
        return None
    hdr = sait_values(sections[0])
    tid = str(hdr.get("tidstr", ""))
    params = next((sait_values(s) for s in sections[1:] if any(k == "cursize" for k, _t, _v in s)), None)
    if params is None or tid in NON_PAINT_TOOLS:
        return None

    def asset(kind: str, name: str) -> Optional[Asset]:
        key = (kind, name)
        if key not in cache:
            fp = find_asset(roots, kind, name)
            cache[key] = load_asset(fp, kind) if fp else None
        return cache[key]

    def num(key: str, default: float = 0) -> float:
        v = params.get(key, default)
        return float(v) if isinstance(v, int) else default

    name = str(hdr.get("name") or "").strip() or TOOL_LABELS.get(tid, tid) or path.stem
    b = Brush(name=name, group=group)
    assets: dict[str, Asset] = {}
    size = max(0.1, num("cursize", 500) / 10)
    b.size_px = size
    kind = FOMCAT.get(int(num("fomcat")))
    fomnam = str(params.get("fomnam", ""))
    if kind in ("scatter", "brshape"):
        a = asset(kind, fomnam)
        if a:
            assets["fom"] = a
            b.tips = [a.image]
        else:
            if fomnam:
                b.warn(f"找不到{ASSET_LABELS[kind]}“{fomnam}”，已改用圆形笔尖。")
        shape = {ini: num(k) for k, ini in TOOL_TO_INI.items() if k in params}
        if "scatden" in params:
            shape["Opacity"] = num("scatden", 100)
        _apply_shape_params(b, shape, "scatter" if tid == "scatter" else "brshape")
        if tid == "scatter" and num("scatabsf"):
            b.size_px = max(0.1, num("scatsiz", 500) / 10)
        else:
            b.size_px = max(0.1, size * shape.get("Scaling", 100) / 100)
        if tid == "scatter" and num("scatwth"):
            b.roundness = _clamp(1 - abs(num("scatwth")) / 100, 0.01, 1)
    else:
        b.hardness = _clamp(num("wavno", 4) / 4, 0, 1)
        if kind == "bristle":
            a = asset("bristle", fomnam)
            if a:
                assets["fom"] = a
                b.tips = [a.image]
            b.warn("SAI2 的鬃毛形态已近似为笔尖图案（毛的散开、方向等效果无法转换）。")
    b.min_size_px = b.size_px * _clamp(num("minsize", 0), 0, 100) / 100
    if num("szprsf"):
        b.size_dynamic.pressure = _clamp(1 - num("minsize", 0) / 100, 0, 1)
    b.opacity = _clamp(num("curdens", 100) / 100, 0, 1)
    if num("dnprsf"):
        b.opacity_dynamic.pressure = _clamp(1 - num("mindens", 0) / 100, 0, 1)

    texnam = str(params.get("texnam", ""))
    if int(num("texcat")) and texnam:
        a = asset("brushtex", texnam) or asset("papertex", texnam)
        if a:
            assets["tex"] = a
            b.texture = Texture(image=a.image, name=texnam, scale=max(0.01, num("texscl", 100) / 100),
                                depth=_clamp(num("texval", 95) / 100, 0, 1), invert=bool(num("texinvf")))
        else:
            b.warn(f"找不到笔刷纹理“{texnam}”，已忽略纹理。")
    if kind == "blotmap":
        a = asset("blotmap", fomnam)
        if a:
            assets["fom"] = a
            if b.texture is None:
                b.texture = Texture(image=a.image, name=fomnam, scale=max(0.01, num("blotscl", 100) / 100),
                                    depth=_clamp(num("blotval", 50) / 100, 0, 1), invert=bool(num("blotinvf")))
                b.warn("SAI2 的“渗化”笔刷形态已近似为纹理。")
            else:
                b.warn("SAI2 的“渗化”笔刷形态无法与纹理同时转换，已忽略渗化。")

    if tid in WET_TOOLS:
        blend, dil, persist = num("blend", 0), num("dilution", 0), num("persist", 0)
        b.wet = WetMix(enabled=blend > 0 or dil > 0, mix=_clamp(blend / 100, 0, 1), load=_clamp(1 - dil / 100, 0, 1),
                       wetness=_clamp(dil / 100, 0, 1), color_stretch=_clamp(persist / 100, 0, 1))
    b.is_eraser = tid in ERASER_TOOLS
    if tid.startswith("bin") or tid in ("dotpen", "bselpen", "bselers"):
        b.antialias = False
    if tid in ("blur", "smudge"):
        b.warn(f"SAI2 的“{TOOL_LABELS[tid]}”工具在其他软件中没有对应的笔刷，已按普通笔刷转换。")
    if tid.startswith(("ink", "bink")):
        b.warn("这是 SAI2 钢笔图层（矢量）工具，已按普通笔刷转换。")
    b.source = BrushSource(app="sai2", format="sai2", version="custool", path=str(path),
                           native={"sai2_sections": copy.deepcopy(sections), "sai2_tid": tid,
                                   "sai2_category": path.parent.name, "sai2_assets": assets})
    return b


def _tool_files(cat_dir: Path) -> list[Path]:
    return sorted(Path(cat_dir).glob("*.saitdat"), key=lambda f: (0, int(f.stem)) if f.stem.isdigit() else (1, 0))


def read_settings(root: Path, categories: tuple[str, ...] = ("nrm", "ink")) -> list[Brush]:
    """All painting tools of a settings folder / package plus the shapes no tool refers to."""
    root = Path(root)
    roots = [root, *[r for r in _resolve_roots(root) if r != root]]
    cache: dict = {}
    out: list[Brush] = []
    for cat in categories:
        d = root / "custool" / cat
        if not d.is_dir():
            continue
        groups = tool_group_names(d)
        for f in _tool_files(d):
            try:
                gid = int(f.stem) if f.stem.isdigit() else -1
                b = brush_from_tool(f, roots=roots, group=groups.get(gid, ""), cache=cache)
            except Exception:
                log.exception("SAI2 tool %s", f)
                continue
            if b:
                out.append(b)
    used = {(a.kind, a.name) for b in out for a in b.source.native.get("sai2_assets", {}).values()}
    for kind in ("scatter", "brshape"):
        for d in asset_dirs(root, kind):
            for f in sorted(d.glob("*.bmp")) if d.is_dir() else []:
                if (kind, f.stem) not in used:
                    try:
                        out.append(brush_from_asset(f, kind))
                    except Exception:
                        log.exception("SAI2 shape %s", f)
    return out


# ---------------------------------------------------------------------------------------------
# model -> SAI2
# ---------------------------------------------------------------------------------------------
def choose_mode(b: Brush) -> str:
    """'scatter' (scatter tool + scatter shape), 'brshape' (brush + brush shape) or 'round'."""
    needs_scatter = (b.count > 1 or b.count_jitter > 0 or b.scatter > 0.5 or b.spacing > 0.5
                     or b.color.has_jitter())
    if b.is_eraser:
        return "brshape" if b.tips else "round"
    if needs_scatter:
        return "scatter"
    return "brshape" if b.tips else "round"


def shape_params_from_brush(b: Brush, kind: str) -> dict[str, Any]:
    """Model brush -> shape parameters (INI keys, SAI2 units)."""
    lim = 50 if kind == "brshape" else 1000
    angle = int(round(((b.angle + 180) % 360) - 180))
    d: dict[str, Any] = {}
    if kind == "brshape":
        d["Opacity"] = int(round(_clamp(b.flow, 0, 1) * 100))
    d["AngleControl"] = 1 if b.angle_follow_stroke else 0
    d["Angle"] = angle
    d["AngleJitter"] = int(round(_clamp(b.angle_jitter, 0, 1) * 100))
    d["Scaling"] = 100
    if kind == "scatter":
        d["ShapeSize"] = int(round(_clamp(b.size_px, 1, 5000)))
    d["SizeJitter"] = int(round(_clamp(b.size_dynamic.jitter, 0, 1) * 100))
    d["Spacing"] = int(round(_clamp(b.spacing * 100, 1, lim)))
    d["Scattering"] = int(round(_clamp(b.scatter * 100, 0, lim)))
    if kind == "scatter":
        d["WxHRatio"] = f"100:{int(round(_clamp(b.roundness, 0.01, 1) * 100))}"
        d["WxHJitter"] = int(round(_clamp(b.roundness_jitter, 0, 1) * 100))
        d["Count"] = int(_clamp(b.count, 1, 20))
        d["CountJitter"] = int(round(_clamp(b.count_jitter, 0, 1) * 100))
        d["AllDirScattering"] = 1 if b.scatter_both_axes else 0
        d["GaussianDistribution"] = 0
        d["AbsoluteSize"] = 0
        d["IntegerPosition"] = 0
    d["HighQualitySampling"] = 1
    if kind == "scatter":
        d["ColJitter"] = int(round(_clamp(b.color.fg_bg_jitter, 0, 1) * 100))
        d["HueJitter"] = int(round(_clamp(b.color.hue_jitter, 0, 1) * 100))
        d["SaturationJitter"] = int(round(_clamp(b.color.saturation_jitter, 0, 1) * 100))
        d["BrightnessJitter"] = int(round(_clamp(b.color.brightness_jitter, 0, 1) * 100))
        d["ApplyToEachShape"] = 1 if b.color.per_tip else 0
    return d


def ini_strings(d: dict[str, Any]) -> dict[str, str]:
    return {k: (f"{int(v):+d}" if k == "Angle" else str(v)) for k, v in d.items()}


def compose_tool(b: Brush, mode: str, fom: Optional[str], tex: Optional[str], tex_factor: float = 1.0,
                 memo: str = "") -> list[Section]:
    """Build the sections of a new .saitdat for ``b``."""
    if mode == "scatter":
        tid = "scatter"
    elif b.is_eraser:
        tid = "eraser"
    elif b.wet.enabled:
        tid = "water" if b.wet.wetness > 0 else "brush"
    elif mode == "brshape" or tex:
        tid = "brush"
    elif b.hardness >= 0.9 and b.size_px <= 30:
        tid = "pencil"
    elif b.hardness <= 0.3:
        tid = "airbrush"
    else:
        tid = "brush"
    sec = tool_template(tid)
    size = _clamp(b.size_px, 0.1, 5000)
    sp, op = _clamp(b.size_dynamic.pressure, 0, 1), _clamp(b.opacity_dynamic.pressure, 0, 1)
    dens = b.opacity if mode != "round" else b.opacity * b.flow
    vals: dict[str, Any] = {
        "cursize": int(round(size * 10)), "curszscl": 0 if size < 10 else (1 if size <= 1000 else 10),
        "minsize": int(round((1 - sp) * 100)), "szprsf": 1 if sp > 0 else 0,
        "curdens": int(round(_clamp(dens, 0, 1) * 100)),
        "mindens": int(round((1 - op) * 100)), "dnprsf": 1 if op > 0 else 0,
    }
    if mode == "round":
        vals["wavno"] = int(round(_clamp(b.hardness, 0, 1) * 4))
    else:
        vals["fomcat"], vals["fomnam"] = FOMCAT_ID[mode], fom or ""
        shape = shape_params_from_brush(b, mode)
        for k, ini in TOOL_TO_INI.items():
            if ini in shape:
                vals[k] = shape[ini]
        vals["scatden"] = int(round(_clamp(b.flow, 0, 1) * 100))
        if mode == "scatter":
            vals["scatsiz"] = int(round(size * 10))
            vals["scatwth"] = int(round((1 - _clamp(b.roundness, 0.01, 1)) * 100))
    if tex and b.texture is not None:
        t = b.texture
        vals.update(texcat=2, texnam=tex, texval=int(round(_clamp(t.depth, 0, 1) * 100)),
                    texscl=int(round(_clamp(t.scale * tex_factor * 100, 10, 500))), texinvf=1 if t.invert else 0)
    if tid in WET_TOOLS:
        w = b.wet if b.wet.enabled else WetMix()
        vals.update(blend=int(round(_clamp(w.mix, 0, 1) * 100)), dilution=int(round(_clamp(w.wetness, 0, 1) * 100)),
                    persist=int(round(_clamp(w.color_stretch, 0, 1) * 100)))
    keys = {k for k, _t, _v in sec}
    for k, v in vals.items():
        if k in keys:
            sait_set(sec, k, v)
    return [tool_header(tid, b.name.replace("\n", " ").strip() or "brush", memo), sec, [], []]


def _write_warnings(b: Brush, mode: str) -> None:
    if len(b.tips) > 1:
        b.warn("SAI2 每个笔刷只能有一个形状，已只使用第一个笔尖。")
    if b.dual is not None:
        b.warn("SAI2 不支持双重画笔，已忽略。")
    if b.blend_mode not in ("normal", ""):
        b.warn(f"SAI2 笔刷不支持混合模式“{b.blend_mode}”，已改为正常。")
    if any(d.tilt > 0 or d.velocity > 0 for d in (b.size_dynamic, b.opacity_dynamic, b.flow_dynamic)):
        b.warn("SAI2 只支持笔压，倾斜和速度动态已忽略。")
    if mode == "scatter" and b.wet.enabled:
        b.warn("SAI2 的散布工具不支持混色，已忽略湿画效果。")
    if mode != "scatter" and b.tips and (b.scatter > 0.5 or b.spacing > 0.5):
        b.warn("SAI2 笔刷形状的间距/散布最大 50%，已截断。")
    b.warn_extras("SAI2")


@dataclass
class ExportResult:
    files: list[Path] = field(default_factory=list)  # every file created
    tools: list[tuple[int, Path, str]] = field(default_factory=list)  # (tool id, .saitdat, brush name)
    assets: list[tuple[str, str]] = field(default_factory=list)  # (kind, name) referenced, new or reused
    existing: list[tuple[int, str]] = field(default_factory=list)  # identical tools already registered
    skipped: list[str] = field(default_factory=list)


class AssetWriter:
    """Writes BMP(+INI) assets into a settings folder; never overwrites, reuses identical files."""

    def __init__(self, root: Path, legacy: bool = False):
        self.root = Path(root)
        self.legacy = legacy
        self.files: list[Path] = []
        self._cache: dict[tuple[str, str], str] = {}

    def folder(self, kind: str) -> Path:
        rel = ASSET_DIRS[kind]
        return self.root / rel[-1] if self.legacy else self.root.joinpath(*rel)

    def put(self, a: Asset) -> str:
        bmp, ini = a.encoded()
        key = (a.kind, hashlib.sha1(bmp + b"\0" + (ini or b"")).hexdigest())
        if key in self._cache:
            return self._cache[key]
        d = self.folder(a.kind)
        d.mkdir(parents=True, exist_ok=True)
        base = safe_filename(a.name, a.kind)
        i = 1
        while True:
            name = base if i == 1 else f"{base} ({i})"
            p_bmp, p_ini = d / f"{name}.bmp", d / f"{name}.ini"
            if p_bmp.exists() or p_ini.exists():
                same = (p_bmp.is_file() and p_bmp.read_bytes() == bmp
                        and (p_ini.read_bytes() == ini if (ini is not None and p_ini.is_file()) else
                             (ini is None and not p_ini.exists())))
                if same:
                    break
                i += 1
                continue
            with open(p_bmp, "xb") as fh:
                fh.write(bmp)
            self.files.append(p_bmp)
            if ini is not None:
                with open(p_ini, "xb") as fh:
                    fh.write(ini)
                self.files.append(p_ini)
            break
        self._cache[key] = name
        return name


def next_tool_id(cat_dir: Path) -> int:
    ids = [int(f.stem) for d in (Path(cat_dir), Path(cat_dir) / "bak") if d.is_dir()
           for f in d.glob("*.sait*") if f.stem.isdigit()]
    return max(ids, default=0) + 1


def export_brushes(brushes: list[Brush], root: Path, *, tools_dir: Optional[Path] = None, first_id: int = 1,
                   prefer_native: bool = True, legacy: bool = False) -> ExportResult:
    """Write the assets of ``brushes`` into ``root`` and (if ``tools_dir``) one .saitdat per brush."""
    res = ExportResult()
    aw = AssetWriter(root, legacy)
    next_id = first_id
    known: Optional[dict[bytes, int]] = None
    for b in brushes:
        nat = b.source.native if b.source.format == "sai2" else {}
        nat_assets: dict[str, Asset] = nat.get("sai2_assets") or {}
        try:
            if nat.get("sai2_asset_only"):
                for a in nat_assets.values():
                    res.assets.append((a.kind, aw.put(a)))
                continue
            sections: Optional[list[Section]] = None
            if prefer_native and nat.get("sai2_sections"):
                sections = copy.deepcopy(nat["sai2_sections"])
                names = {role: aw.put(a) for role, a in nat_assets.items()}
                res.assets += [(nat_assets[r].kind, n) for r, n in names.items()]
                sait_set(sections[0], "name", b.name, "U")
                for sec in sections[1:]:
                    keys = {k for k, _t, _v in sec}
                    if "fom" in names and "fomnam" in keys:
                        sait_set(sec, "fomnam", names["fom"], "U")
                    if "tex" in names and "texnam" in keys:
                        sait_set(sec, "texnam", names["tex"], "U")
            else:
                mode = choose_mode(b)
                fom_asset = nat_assets.get("fom")
                if fom_asset is not None and fom_asset.kind in ("scatter", "brshape") and not b.is_eraser:
                    mode = fom_asset.kind
                _write_warnings(b, mode)
                fom = tex = None
                tex_factor = 1.0
                if mode != "round":
                    if fom_asset is None or fom_asset.kind != mode:
                        ini = ini_strings(shape_params_from_brush(b, mode))
                        fom_asset = Asset(mode, b.name, prepare_tip(b.main_tip()), ini)
                    fom = aw.put(fom_asset)
                    res.assets.append((mode, fom))
                if b.texture is not None:
                    t_asset = nat_assets.get("tex")
                    if t_asset is not None and t_asset.kind in TEXTURE_KINDS:
                        t_asset = Asset("brushtex", t_asset.name, t_asset.image, None, t_asset.bmp_bytes)
                    else:
                        img, tex_factor = prepare_texture(b.texture.image)
                        t_asset = Asset("brushtex", b.texture.name or f"{b.name} 纹理", img)
                    tex = aw.put(t_asset)
                    res.assets.append(("brushtex", tex))
                memo = "brush-bridge"
                if b.source.app and b.source.app != "sai2":
                    memo += f" 转换自 {b.source.app}"
                sections = compose_tool(b, mode, fom, tex, tex_factor, memo)
        except Exception as e:
            log.exception("SAI2 export %s", b.name)
            res.skipped.append(f"{b.name}：{e}")
            continue
        if tools_dir is None:
            continue
        data = dump_sait(sections).encode("utf-8")
        if known is None:
            known = {}
            if tools_dir.is_dir():
                registered = {t for _g, _n, slots in tool_groups(tools_dir) for _s, t in slots}
                for t in registered:
                    try:
                        known.setdefault((tools_dir / f"{t}.saitdat").read_bytes(), t)
                    except OSError:
                        pass
        if data in known:
            res.existing.append((known[data], b.name))
            continue
        tools_dir.mkdir(parents=True, exist_ok=True)
        while (tools_dir / f"{next_id}.saitdat").exists():
            next_id += 1
        p = tools_dir / f"{next_id}.saitdat"
        with open(p, "xb") as fh:
            fh.write(data)
        known[data] = next_id
        res.tools.append((next_id, p, b.name))
        res.files.append(p)
        next_id += 1
    res.files = aw.files + res.files
    return res


def register_tools(cat_dir: Path, tool_ids: list[int], group_name: str) -> tuple[list[Path], list[Path]]:
    """Put tools into a custom tool group (created if needed). Returns (changed files, backups).

    Only call while SAI2 is closed: SAI2 rewrites custool on exit.
    """
    cat_dir = Path(cat_dir)
    changed: list[Path] = []
    backups: list[Path] = []
    groups = tool_groups(cat_dir)
    target = next((g for g in groups if g[1] == group_name), None)
    sset = cat_dir / "_0.saitset"
    if target is not None:
        gid = target[0]
        gpath = cat_dir / f"_{gid}.saitgrp"
        secs = read_sait(gpath)
        while len(secs) < 2:
            secs.append([])
        start = max((s for s, _t in target[2]), default=-1) + 1
        backups.append(backup(gpath))
        secs[1] += [(str(start + i), "", str(t)) for i, t in enumerate(tool_ids)]
        gpath.write_bytes(dump_sait(secs).encode("utf-8"))
        changed.append(gpath)
        return changed, backups
    existing = [int(m.group(1)) for f in cat_dir.glob("_*.saitgrp") if (m := re.fullmatch(r"_(\d+)", f.stem))]
    gid = max(existing, default=0) + 1
    gpath = cat_dir / f"_{gid}.saitgrp"
    head: Section = [("vsbpos", "I", 0), ("name", "U", group_name), ("csrbid0", "I", 0), ("csrbid1", "I", 0),
                     ("csrbid2", "I", 0)]
    with open(gpath, "xb") as fh:
        fh.write(dump_sait([head, [(str(i), "", str(t)) for i, t in enumerate(tool_ids)]]).encode("utf-8"))
    changed.append(gpath)
    if sset.is_file():
        secs = read_sait(sset)
        backups.append(backup(sset))
    else:
        secs = [[("curgid", "I", gid), ("tabno", "I", 0), ("newgid", "I", gid)]]
    while len(secs) < 2:
        secs.append([])
    idx = max((int(k) for k, _t, _v in secs[1] if str(k).isdigit()), default=-1) + 1
    secs[1].append((str(idx), "", str(gid)))
    sset.write_bytes(dump_sait(secs).encode("utf-8"))
    changed.append(sset)
    return changed, backups


README_TEXT = """SAI2 笔刷包（由 brush-bridge 生成）

手动安装方法：
1. 关闭 SAI2。
2. 打开 SAI2 的设置文件夹：
   文档\\SYSTEMAX Software Development\\SAIv2\\settings
   （2020-01-07 之前的旧版 SAI2：sai2.exe 所在的文件夹）
3. 把本包里的 scatter、brushfom、brushtex 文件夹复制进去（合并文件夹，不要覆盖同名文件）。
4. 启动 SAI2：
   - 散布形状：在“散布”工具或画笔的“笔刷形态”下拉列表中选择；
   - 笔刷形状：在画笔/水彩笔的“笔刷形态”下拉列表中选择；
   - 笔刷纹理：在“笔刷纹理”下拉列表中选择。
custool 文件夹里是对应的自定义工具参数。它们需要登记到工具组里才会显示，
建议使用 brush-bridge 的“安装到 SAI2”功能自动完成（会先备份原文件）。
"""


# ---------------------------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------------------------
def _is_bmp(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"BM"
    except OSError:
        return False


def _gray_problem(path: Path) -> Optional[str]:
    try:
        with Image.open(path) as im:
            if im.mode == "L":
                return None
            if im.mode == "P":
                pal = im.getpalette()[:768]
                if all(pal[i] == pal[i + 1] == pal[i + 2] for i in range(0, len(pal), 3)):
                    return None
            return f"图片是 {im.mode} 模式，SAI2 只能读取 8 位灰度 BMP。"
    except Exception:
        return "无法读取该 BMP 图片。"


def _fix_to_gray(src: Path, out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = unique_path(out_dir / Path(src).name)
    dst.write_bytes(encode_bmp(load_gray(Path(src))))
    files = [dst]
    ini = Path(src).with_suffix(".ini")
    if ini.is_file():
        d_ini = dst.with_suffix(".ini")
        if not d_ini.exists():
            d_ini.write_bytes(ini.read_bytes())
            files.append(d_ini)
    return files


@register_codec
class Sai2Codec(FormatCodec):
    id = "sai2"
    name = "SAI2 笔刷 (.saitdat / 笔刷形状 BMP+INI)"
    extensions = (".saitdat", ".bmp", ".ini")
    can_read = True
    can_write = True

    def sniff(self, path: Path) -> Optional[FileInfo]:
        path = Path(path)
        if path.is_dir():
            return self._sniff_dir(path)
        ext = path.suffix.lower()
        if ext == ".saitdat":
            return self._sniff_tool(path)
        if ext == ".ini":
            bmp = path.with_suffix(".bmp")
            return self._sniff_bmp(bmp) if bmp.is_file() else None
        if ext == ".bmp":
            return self._sniff_bmp(path)
        return None

    def _sniff_dir(self, path: Path) -> Optional[FileInfo]:
        if not is_settings_root(path):
            return None
        tools = sum(len(list((path / "custool" / c).glob("*.saitdat"))) for c in ("nrm", "ink"))
        shapes = sum(len(list(d.glob("*.bmp"))) for k in ("scatter", "brshape") for d in asset_dirs(path, k)
                     if d.is_dir())
        if not tools and not shapes:
            return None
        feats = {"custool"} if tools else {"scatter_shape"}
        return FileInfo(path=path, format_id=self.id, format_name=self.name, version="settings",
                        brush_count=-1, details={"自定义工具": str(tools), "形状": str(shapes),
                                                 "min_build": required_build(feats)})

    def _sniff_tool(self, path: Path) -> Optional[FileInfo]:
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if not data.lstrip(b"\xef\xbb\xbf").startswith(b"tidstr="):
            return None
        secs = parse_sait(decode_text(data))
        hdr = sait_values(secs[0]) if secs else {}
        tid = str(hdr.get("tidstr", ""))
        is_brush = tid not in NON_PAINT_TOOLS and any(k == "cursize" for s in secs[1:] for k, _t, _v in s)
        p = next((sait_values(s) for s in secs[1:] if any(k == "cursize" for k, _t, _v in s)), {})
        feats = {"custool"}
        if tid in ("scatter", "effpen") or p.get("fomcat") == 3:
            feats.add("scatter_tool")
        info = FileInfo(path=path, format_id=self.id, format_name=self.name, version="custool",
                        brush_count=1 if is_brush else 0,
                        details={"工具": TOOL_LABELS.get(tid, tid), "名称": str(hdr.get("name", "")),
                                 "分类": TOOL_CATEGORIES.get(path.parent.name, path.parent.name),
                                 "min_build": required_build(feats)})
        if not is_brush:
            info.problems.append(f"这是 SAI2 的“{TOOL_LABELS.get(tid, tid)}”工具，不是笔刷。")
        if p.get("fomcat") in (1, 2, 3, 4) or p.get("texcat"):
            roots = _resolve_roots(path)
            kind = FOMCAT.get(p.get("fomcat", 0))
            if kind and p.get("fomnam") and not find_asset(roots, kind, str(p["fomnam"])):
                info.problems.append(f"缺少{ASSET_LABELS[kind]}“{p['fomnam']}”（应位于设置文件夹中）。")
        return info

    def _sniff_bmp(self, path: Path) -> Optional[FileInfo]:
        if not path.is_file() or not _is_bmp(path):
            return None
        kind = asset_kind_of(path)
        if kind is None:
            return None
        has_ini = path.with_suffix(".ini").is_file()
        if not has_ini:
            root = settings_root_of(path)
            if root is None or not is_settings_root(root, 2) or kind in ("scatter", "brshape") and \
                    path.parent.name.lower() != kind:
                return None
        feats = {"scatter_shape"} if kind == "scatter" else ({"scatter_tool"} if kind == "brshape" else set())
        if kind == "bristle":
            feats = {"bristle"}
        info = FileInfo(path=path, format_id=self.id, format_name=self.name, version=kind,
                        brush_count=1 if kind in ("scatter", "brshape", "bristle") else 0,
                        details={"类型": ASSET_LABELS[kind], "参数文件": "有" if has_ini else "无",
                                 "min_build": required_build(feats)})
        if kind != "bristle":
            prob = _gray_problem(path)
            if prob:
                info.problems.append(prob)
                info.fixes.append(_gray_fix())
        if kind in TEXTURE_KINDS:
            with Image.open(path) as im:
                if im.size != (TEXTURE_SIZE, TEXTURE_SIZE):
                    info.problems.append(f"纹理尺寸为 {im.size[0]}x{im.size[1]}，SAI2 的纹理应为 512x512。")
        return info

    def read(self, path: Path) -> list[Brush]:
        path = Path(path)
        if path.is_dir():
            return read_settings(path)
        ext = path.suffix.lower()
        if ext == ".saitdat":
            b = brush_from_tool(path, group=tool_group_names(path.parent).get(
                int(path.stem) if path.stem.isdigit() else -1, ""))
            return [b] if b else []
        if ext == ".ini":
            path = path.with_suffix(".bmp")
        return [brush_from_asset(path)]

    def list_refs(self, path: Path) -> list[BrushRef]:
        path = Path(path)
        if path.is_dir():
            return list_settings(path)
        return super().list_refs(path)

    def write(self, brushes: list[Brush], out_path: Path, **options) -> list[Path]:
        """Write a SAI2 package folder (scatter/, brushfom/brshape/, brushtex/, custool/nrm/)."""
        out_path = Path(out_path)
        folder = out_path.with_suffix("") if out_path.suffix.lower() in self.extensions else out_path
        if folder.exists() and any(folder.iterdir()):
            folder = unique_path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        res = export_brushes(brushes, folder, tools_dir=folder / "custool" / "nrm",
                             prefer_native=options.get("prefer_native", True))
        readme = folder / "安装说明.txt"
        readme.write_text(README_TEXT, encoding="utf-8")
        return res.files + [readme]

    def compatibility(self, info: FileInfo, app_id: str, app_version: str) -> Optional[str]:
        if app_id != "sai2":
            return None
        need, have = info.details.get("min_build", ""), build_date(app_version)
        if need and have and have < need:
            feats = [FEATURE_TEXT[k] for b, k, _t in FEATURES if have < b <= need]
            return (f"需要 SAI2 {need} 或更新的版本（当前为 {have}），旧版本不支持："
                    f"{'、'.join(feats)}。请到官网 https://www.systemax.jp/sai/ 下载最新版。")
        return None


def _gray_fix() -> Fix:
    return Fix(title="转换为 8 位灰度 BMP", description="生成 SAI2 能读取的 8 位灰度 BMP（保留同名 INI 参数文件）。",
               apply=_fix_to_gray)


# ---------------------------------------------------------------------------------------------
# Listing (used by the codec for folders and by the SAI2 app adapter)
# ---------------------------------------------------------------------------------------------
def _thumb(path: Path, kind: str):
    def load() -> Optional[Image.Image]:
        try:
            with Image.open(path) as im:
                im.load()
                g = bristle_to_tip(im) if kind == "bristle" else load_gray(path)
            g.thumbnail((128, 128))
            return g
        except Exception:
            return None
    return load


def list_settings(root: Path, include_textures: bool = True) -> list[BrushRef]:
    root = Path(root)
    refs: list[BrushRef] = []
    roots = [root]
    cache: dict = {}
    for cat, cat_label in TOOL_CATEGORIES.items():
        d = root / "custool" / cat
        if not d.is_dir():
            continue
        groups = tool_groups(d)
        order = {tid: (gi, slot) for gi, (_g, _n, slots) in enumerate(groups) for slot, tid in slots}
        names = tool_group_names(d)
        for f in sorted(_tool_files(d), key=lambda f: order.get(int(f.stem) if f.stem.isdigit() else -1, (999, 0))):
            try:
                secs = read_sait(f)
            except OSError:
                continue
            hdr = sait_values(secs[0]) if secs else {}
            tid = str(hdr.get("tidstr", ""))
            if tid in NON_PAINT_TOOLS or not any(k == "cursize" for s in secs[1:] for k, _t, _v in s):
                continue
            gid = int(f.stem) if f.stem.isdigit() else -1
            group = names.get(gid, "")
            label = str(hdr.get("name") or "").strip() or TOOL_LABELS.get(tid, tid)

            def loader(f=f, secs=secs, group=group) -> Brush:
                b = brush_from_tool(f, secs, roots + _resolve_roots(f), group, cache)
                if b is None:
                    raise RuntimeError(f"{f} is not a brush tool")
                return b

            def thumb(loader=loader) -> Optional[Image.Image]:
                try:
                    tip = loader().main_tip().copy()
                    tip.thumbnail((128, 128))
                    return tip
                except Exception:
                    return None

            refs.append(BrushRef(name=label, key=f"custool/{cat}/{f.name}",
                                 group=join_group("自定义工具", cat_label, group),
                                 loader=loader, thumbnail=thumb,
                                 detail=f"{TOOL_LABELS.get(tid, tid)}工具" + ("" if group else "（未分组）")))
    kinds = ["scatter", "brshape", "bristle", "blotmap"] + (list(TEXTURE_KINDS) if include_textures else [])
    for kind in kinds:
        for d in asset_dirs(root, kind):
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.bmp")):
                rel = f.relative_to(root).as_posix()
                refs.append(BrushRef(name=f.stem, key=rel, group=join_group("素材", ASSET_LABELS[kind]),
                                     loader=(lambda f=f, k=kind: brush_from_asset(f, k)), thumbnail=_thumb(f, kind),
                                     detail=ASSET_LABELS[kind] + ("（含参数 INI）" if f.with_suffix(".ini").is_file() else "")))
    return refs
