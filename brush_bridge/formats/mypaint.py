"""MyPaint brushes (.myb), also used by GIMP's MyPaint Brush tool and Krita 5's MyPaint engine.

A brush is a list of *settings*; each has a ``base_value`` plus optional input curves (pressure,
speed, tilt, random, direction, ...) whose outputs are added to the base value. Formats:

* v3 (MyPaint >= 1.1, libmypaint, GIMP 2.10+, Krita 5): JSON ``{"version": 3, "settings": {...}}``
* v2 / v1 (MyPaint 0.x - 1.0): text lines ``name base | input (x y), (x y) | ...``

Tips are always computed (round/elliptical dabs): there are no tip images, textures or dual tips.
A brush is usually shipped with a 128x128 ``<name>_prev.png`` preview next to the ``.myb``.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Optional

from PIL import Image

from ..core.model import Brush, BrushRef, BrushSource
from ..core.plugin import FileInfo, Fix, FormatCodec, register_codec
from ..core.system import safe_filename, unique_path

Settings = dict[str, dict[str, Any]]  # name -> {"base_value": float, "inputs": {input: [[x, y], ...]}}

_POINT = re.compile(r"\(\s*([-+\d.eE]+)\s+([-+\d.eE]+)\s*\)")
MAX_DABS_PER_RADIUS = 6.0  # libmypaint's range for dabs_per_basic/actual_radius


class MyPaintFormatError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------
# parsing / encoding
# ---------------------------------------------------------------------------------------------
def parse_myb(text: str) -> tuple[int, Settings, dict[str, str]]:
    """Return (version, settings, metadata) for a v1/v2 INI or v3 JSON brush."""
    s = text.lstrip("\ufeff").strip()
    if s.startswith("{"):
        try:
            d = json.loads(s)
        except json.JSONDecodeError as e:
            raise MyPaintFormatError(f"bad JSON: {e}") from e
        if not isinstance(d, dict) or not isinstance(d.get("settings"), dict):
            raise MyPaintFormatError("no settings")
        settings: Settings = {}
        for k, v in d["settings"].items():
            if isinstance(v, dict):
                inputs = {i: [(float(p[0]), float(p[1])) for p in pts] for i, pts in (v.get("inputs") or {}).items()}
                settings[k] = {"base_value": float(v.get("base_value", 0.0)), "inputs": inputs}
        meta = {k: str(d.get(k, "")) for k in ("comment", "description", "group", "notes", "parent_brush_name")}
        return int(d.get("version", 3)), settings, meta
    version, settings, seen = 1, {}, False
    for line in s.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        head = parts[0].split()
        if len(head) < 2:
            continue
        if head[0] == "version":
            version = int(float(head[1]))
            continue
        try:
            base = float(head[1])
        except ValueError:  # e.g. "parent_brush_name foo"
            continue
        seen = True
        inputs = {}
        for p in parts[1:]:
            name, _, rest = p.partition(" ")
            pts = [(float(a), float(b)) for a, b in _POINT.findall(rest)]
            if name and pts:
                inputs[name] = pts
        settings[head[0]] = {"base_value": base, "inputs": inputs}
    if not seen:
        raise MyPaintFormatError("not a MyPaint brush")
    return version, settings, {}


def encode_myb(settings: Settings, version: int = 3, meta: Optional[dict[str, str]] = None) -> str:
    if version >= 3:
        d: dict[str, Any] = {"comment": "MyPaint brush file", "version": 3}
        d.update({k: v for k, v in (meta or {}).items() if v})
        d["settings"] = {k: {"base_value": round(v["base_value"], 6),
                             "inputs": {i: [[round(x, 6), round(y, 6)] for x, y in pts] for i, pts in v["inputs"].items()}}
                         for k, v in sorted(settings.items())}
        return json.dumps(d, indent=4, ensure_ascii=False) + "\n"
    lines = ["# mypaint brush file", "# written by brush-bridge", "version 2"]
    for k, v in sorted(settings.items()):
        segs = [f"{k} {v['base_value']:g}"]
        for i, pts in v["inputs"].items():
            segs.append(f"{i} " + ", ".join(f"({x:f} {y:f})" for x, y in pts))
        lines.append(" | ".join(segs))
    return "\n".join(lines) + "\n"


def _curve_at(pts: list[tuple[float, float]], x: float) -> float:
    if not pts:
        return 0.0
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


# ---------------------------------------------------------------------------------------------
# settings -> model
# ---------------------------------------------------------------------------------------------
def settings_to_brush(settings: Settings, name: str, path: Optional[Path] = None, version: int = 3) -> Brush:
    def base(k: str, default: float = 0.0) -> float:
        return float(settings.get(k, {}).get("base_value", default))

    def inp(k: str, i: str) -> list[tuple[float, float]]:
        return list(settings.get(k, {}).get("inputs", {}).get(i, []))

    def at_pressure(k: str, p: float, default: float) -> float:
        return base(k, default) + _curve_at(inp(k, "pressure"), p)

    def span(k: str, i: str) -> float:
        ys = [y for _x, y in inp(k, i)]
        return (max(ys) - min(ys)) if ys else 0.0

    b = Brush(name=name)
    r_lo, r_hi = at_pressure("radius_logarithmic", 0.0, 2.0), at_pressure("radius_logarithmic", 1.0, 2.0)
    b.size_px = max(0.5, 2 * math.exp(max(r_lo, r_hi)))
    if r_hi > r_lo:
        b.size_dynamic.pressure = min(1.0, 1 - math.exp(r_lo - r_hi))
    if span("radius_logarithmic", "tilt_declination") > 0.05:
        b.size_dynamic.tilt = min(1.0, 1 - math.exp(-span("radius_logarithmic", "tilt_declination")))
    if span("radius_logarithmic", "speed1") + span("radius_logarithmic", "speed2") > 0.05:
        b.size_dynamic.velocity = min(1.0, 1 - math.exp(-(span("radius_logarithmic", "speed1")
                                                          + span("radius_logarithmic", "speed2"))))
    rbr = base("radius_by_random", 0.0)
    b.size_dynamic.jitter = min(1.0, max(0.0, 1 - math.exp(-rbr)))

    def opac(p: float) -> float:
        o = min(1.0, max(0.0, at_pressure("opaque", p, 1.0)))
        return o * min(1.0, max(0.0, at_pressure("opaque_multiply", p, 0.0)))

    o_lo, o_hi = opac(0.0), opac(1.0)
    b.opacity = max(0.01, max(o_lo, o_hi))
    if o_hi > o_lo + 1e-3:
        b.opacity_dynamic.pressure = min(1.0, 1 - o_lo / o_hi)
    b.flow = 1.0
    b.hardness = min(1.0, max(0.0, base("hardness", 0.8)))
    b.antialias = base("anti_aliasing", 1.0) > 0.1

    dabs = max(0.0, base("dabs_per_actual_radius", 2.0)) + max(0.0, base("dabs_per_basic_radius", 0.0))
    b.spacing = min(10.0, max(0.01, 1 / (2 * dabs))) if dabs > 0 else 0.1
    b.scatter = max(0.0, base("offset_by_random", 0.0) + max([y for _x, y in inp("offset_by_random", "pressure")] or [0]))
    b.scatter_both_axes = True

    ratio = max(1.0, base("elliptical_dab_ratio", 1.0))
    b.roundness = 1 / ratio
    angle_inputs = settings.get("elliptical_dab_angle", {}).get("inputs", {})
    b.angle_follow_stroke = any(i in angle_inputs for i in ("direction", "direction_360", "attack_angle"))
    b.angle = 0.0 if b.angle_follow_stroke else -(base("elliptical_dab_angle", 90.0) % 180)
    if "random" in angle_inputs and ratio > 1.01:
        b.angle_jitter = min(1.0, span("elliptical_dab_angle", "random") / 360)

    smudge = base("smudge", 0.0)
    if smudge > 0.02:
        b.wet.enabled = True
        b.wet.mix = min(1.0, smudge)
        b.wet.load = max(0.0, 1 - smudge)
        b.wet.color_stretch = min(1.0, max(0.0, base("smudge_length", 0.5)))
    b.is_eraser = base("eraser", 0.0) > 0.5
    if base("colorize", 0.0) > 0.5:
        b.blend_mode = "color"
    b.stabilization = min(1.0, max(0.0, base("slow_tracking", 0.0) / 10))

    c = b.color
    c.hue_jitter = min(1.0, 2 * span("change_color_h", "random"))
    c.saturation_jitter = min(1.0, max(span("change_color_hsv_s", "random"), span("change_color_hsl_s", "random")))
    c.brightness_jitter = min(1.0, max(span("change_color_v", "random"), span("change_color_l", "random")))

    b.source = BrushSource(app="mypaint", format="mypaint", version=f"v{version}", path=str(path or ""),
                           native={"mypaint_settings": settings})
    return b


# ---------------------------------------------------------------------------------------------
# model -> settings
# ---------------------------------------------------------------------------------------------
def brush_to_settings(b: Brush) -> Settings:
    native = b.source.native.get("mypaint_settings") if b.source.format == "mypaint" else None
    if native:
        return json.loads(json.dumps(native))

    s: Settings = {}

    def put(k: str, v: float, **inputs: list[tuple[float, float]]) -> None:
        s[k] = {"base_value": float(v), "inputs": {i: p for i, p in inputs.items() if p}}

    radius = max(0.2, b.size_px / 2)
    sz_in: dict[str, list[tuple[float, float]]] = {}
    if b.size_dynamic.pressure > 0:
        lo = math.log(max(0.02, 1 - min(1.0, b.size_dynamic.pressure)))
        sz_in["pressure"] = [(0.0, lo), (1.0, 0.0)]
    put("radius_logarithmic", math.log(radius), **sz_in)
    if b.size_dynamic.jitter > 0:
        put("radius_by_random", min(1.5, -math.log(max(0.05, 1 - b.size_dynamic.jitter))))
    put("hardness", min(1.0, max(0.0, b.hardness)))
    put("anti_aliasing", 1.0 if b.antialias else 0.0)

    put("opaque", min(1.0, max(0.0, b.opacity * b.flow)))
    op = max(b.opacity_dynamic.pressure, b.flow_dynamic.pressure)
    if op > 0:
        put("opaque_multiply", 0.0, pressure=[(0.0, 1 - min(1.0, op)), (1.0, 1.0)])
    else:
        put("opaque_multiply", 1.0)
    put("opaque_linearize", 0.9)

    dabs = 1 / (2 * max(0.005, b.spacing))
    put("dabs_per_actual_radius", min(MAX_DABS_PER_RADIUS, dabs))
    put("dabs_per_basic_radius", min(MAX_DABS_PER_RADIUS, max(0.0, dabs - MAX_DABS_PER_RADIUS)))
    put("dabs_per_second", 0.0)
    put("offset_by_random", min(25.0, max(0.0, b.scatter)))

    put("elliptical_dab_ratio", min(10.0, 1 / max(0.1, min(1.0, b.roundness))))
    ang_in: dict[str, list[tuple[float, float]]] = {}
    if b.angle_follow_stroke:
        ang_in["direction"] = [(0.0, 0.0), (180.0, 180.0)]
    if b.angle_jitter > 0 and b.roundness < 0.99:
        half = min(180.0, b.angle_jitter * 180)
        ang_in["random"] = [(0.0, -half), (1.0, half)]
    put("elliptical_dab_angle", 0.0 if b.angle_follow_stroke else (-b.angle) % 180, **ang_in)

    if b.wet.enabled:
        put("smudge", min(1.0, max(0.0, b.wet.mix, 1 - b.wet.load, b.wet.color_stretch)))
        put("smudge_length", min(1.0, max(0.0, b.wet.color_stretch or 0.5)))
    put("eraser", 1.0 if b.is_eraser else 0.0)
    if b.blend_mode == "color":
        put("colorize", 1.0)
    put("slow_tracking", min(10.0, max(0.0, b.stabilization * 10)))

    c = b.color
    if c.hue_jitter > 0:
        put("change_color_h", 0.0, random=[(0.0, -c.hue_jitter / 4), (1.0, c.hue_jitter / 4)])
    if c.saturation_jitter > 0:
        put("change_color_hsv_s", 0.0, random=[(0.0, -c.saturation_jitter / 2), (1.0, c.saturation_jitter / 2)])
    if c.brightness_jitter > 0:
        put("change_color_v", 0.0, random=[(0.0, -c.brightness_jitter / 2), (1.0, c.brightness_jitter / 2)])
    return s


def _warn_lossy(b: Brush) -> None:
    if b.source.format == "mypaint":
        return
    if b.tips:
        b.warn("MyPaint 笔刷只有圆形/椭圆笔尖，图像笔尖已改为同尺寸的圆形笔尖。")
    lost = []
    if b.texture is not None:
        lost.append("纹理")
    if b.dual is not None:
        lost.append("双重画笔")
    if b.count > 1:
        lost.append("每次落笔多个笔尖")
    if b.blend_mode not in ("normal", "", "color"):
        lost.append(f"混合模式“{b.blend_mode}”")
    if lost:
        b.warn("MyPaint 笔刷不支持" + "、".join(lost) + "，已忽略。")
    if b.flow < 0.99:
        b.warn("MyPaint 没有单独的“流量”，已合并到不透明度。")
    b.warn_extras("MyPaint")


def _preview(b: Brush) -> Image.Image:
    if b.preview is not None:
        img = b.preview.convert("RGBA")
    else:
        from ..core.imaging import stroke_preview

        img = stroke_preview(b, 256, 96)
    img.thumbnail((120, 120))
    out = Image.new("RGB", (128, 128), (255, 255, 255))
    out.paste(img, ((128 - img.width) // 2, (128 - img.height) // 2), img)
    return out


def preview_path(myb: Path) -> Path:
    return myb.with_name(myb.stem + "_prev.png")


# ---------------------------------------------------------------------------------------------
# codec
# ---------------------------------------------------------------------------------------------
def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _fix_to_v3(src: Path, out_dir: Path) -> list[Path]:
    _v, settings, meta = parse_myb(_read_text(src))
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = unique_path(out_dir / src.name)
    dst.write_text(encode_myb(settings, 3, meta), encoding="utf-8")
    files = [dst]
    prev = preview_path(src)
    if prev.is_file():
        files.append(preview_path(dst))
        files[-1].write_bytes(prev.read_bytes())
    return files


@register_codec
class MyPaintCodec(FormatCodec):
    id = "mypaint"
    name = "MyPaint 笔刷 (.myb)"
    extensions = (".myb",)
    can_read = True
    can_write = True

    def sniff(self, path: Path) -> Optional[FileInfo]:
        path = Path(path)
        if path.suffix.lower() != ".myb":
            return None
        try:
            version, settings, meta = parse_myb(_read_text(path))
        except (OSError, MyPaintFormatError, ValueError):
            return None
        fmt = "JSON" if version >= 3 else "INI"
        info = FileInfo(path, self.id, self.name, f"v{version}", 1,
                        details={"类型": "MyPaint 笔刷", "格式": f"v{version} ({fmt})", "参数数": str(len(settings))})
        if meta.get("notes"):
            info.details["说明"] = meta["notes"][:120]
        if version < 3:
            info.problems.append("这是旧版 MyPaint 笔刷（v1/v2 文本格式），GIMP 和 Krita 只能读取 v3（JSON）格式。")
            info.fixes.append(Fix("升级为 v3 (JSON)", "转换为 MyPaint 1.1+ / GIMP 2.10+ / Krita 5 都能读取的 v3 格式。",
                                  _fix_to_v3))
        return info

    def _load(self, path: Path) -> Brush:
        version, settings, meta = parse_myb(_read_text(path))
        b = settings_to_brush(settings, path.stem, path, version)
        if meta.get("group"):
            b.group = meta["group"]
        prev = preview_path(path)
        if prev.is_file():
            try:
                with Image.open(prev) as im:
                    b.preview = im.convert("RGBA")
            except OSError:
                pass
        return b

    def list_refs(self, path: Path) -> list[BrushRef]:
        path = Path(path)

        def thumb(p=path) -> Optional[Image.Image]:
            prev = preview_path(p)
            if prev.is_file():
                with Image.open(prev) as im:
                    return im.convert("RGBA")
            return None

        return [BrushRef(name=path.stem, key=str(path), group="", loader=lambda p=path: self._load(p), thumbnail=thumb,
                         detail="MyPaint .myb")]

    def read(self, path: Path) -> list[Brush]:
        return [self._load(Path(path))]

    def write(self, brushes: list[Brush], out_path: Path, **options) -> list[Path]:
        """Write one ``.myb`` (+ ``_prev.png``) per brush.

        options: ``mypaint_version`` 3 (default, MyPaint 1.1+ / GIMP 2.10+ / Krita 5) or 2 (MyPaint 1.0 and older).
        """
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        mv = options.get("mypaint_version")
        if mv is not None:
            version = 2 if str(mv).strip() in ("1", "2") else 3
        else:
            tv = str(options.get("target_version") or "")
            version = 2 if tv and _major_minor(tv) < (1, 1) else 3
        single = len(brushes) == 1
        written: list[Path] = []
        for b in brushes:
            _warn_lossy(b)
            settings = brush_to_settings(b)
            meta = {"description": b.name, "group": b.group}
            # MyPaint derives the brush name from the file name; "_prev" would clash with previews
            stem = safe_filename(b.name).replace("_prev", "-prev")
            target = out_path.with_suffix(".myb") if single else unique_path(out_path.parent / f"{stem}.myb")
            target.write_text(encode_myb(settings, version, meta), encoding="utf-8")
            written.append(target)
            try:
                _preview(b).save(preview_path(target))
                written.append(preview_path(target))
            except Exception:
                pass
        return written

    def compatibility(self, info: FileInfo, app_id: str, app_version: str) -> Optional[str]:
        old = info.version in ("v1", "v2")
        major = _major_minor(app_version)
        if app_id == "gimp":
            if app_version and major < (2, 10):
                return "GIMP 2.10 之前没有 MyPaint 笔刷工具，无法使用 .myb 笔刷。"
            if old:
                return "GIMP 只能读取 v3（JSON）格式的 MyPaint 笔刷，请先升级。"
        if app_id == "krita":
            if app_version and major < (5, 0):
                return "Krita 5.0 之前不能导入 MyPaint 笔刷（.myb）。"
            if old:
                return "Krita 只能读取 v3（JSON）格式的 MyPaint 笔刷，请先升级。"
        if app_id == "mypaint" and not old and app_version and major < (1, 1):
            return "MyPaint 1.1 之前的版本不能读取 v3（JSON）笔刷，请用 v2 格式导出。"
        return None


def _major_minor(v: str) -> tuple[int, int]:
    nums = [int(x) for x in re.findall(r"\d+", v or "")[:2]]
    while len(nums) < 2:
        nums.append(0)
    return nums[0], nums[1]
