"""Clip Studio Paint sub tool (.sut) codec.

A .sut is a SQLite database holding exactly one exported sub tool (see ``docs/formats/csp.md``).
Brush tip / texture materials are embedded in ``MaterialFile`` as tar archives. Materials made
by CSP have their ``.layer`` partly encrypted; we read their (plain PNG) thumbnails and copy the
archives verbatim on round trips. Custom tips / textures / dual tips coming from other apps are
written as newly generated (unencrypted) materials by :mod:`.csp_material`, at full resolution.
"""
from __future__ import annotations

import hashlib
import sqlite3
import struct
import zlib
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from ..core.model import Brush, BrushSource, ColorDynamics, Dynamic, DualTip, Texture, WetMix
from ..core.plugin import FileInfo, FormatCodec, Fix, register_codec
from ..core.system import safe_filename, unique_path
from . import csp_schema as S
from .csp_binary import (EFF_PRESSURE, EFF_RANDOM, EFF_TILT, EFF_VELOCITY, ROT_BASE, ROT_DIRECTION, ROT_RANDOM,
                         MaterialRef, decode_effector, decode_material_refs, encode_effector, encode_material_refs,
                         material_members, material_thumbnail, thumbnail_to_texture, thumbnail_to_tip)
from .csp_material import KIND_TEXTURE, KIND_TIP, build_material, read_material_ink

# Variant.CompositeMode (same order as CSP's blending-mode list); 27 = transparent colour/eraser.
COMPOSITE_TO_BLEND = {
    0: "normal", 1: "darken", 2: "multiply", 3: "color_burn", 4: "linear_burn", 5: "subtract",
    6: "lighten", 7: "screen", 8: "color_dodge", 9: "color_dodge", 10: "add", 11: "add",
    12: "overlay", 13: "soft_light", 14: "hard_light", 15: "difference", 16: "vivid_light",
    17: "linear_light", 18: "pin_light", 19: "hard_mix", 20: "exclusion", 21: "darker_color",
    22: "lighter_color", 23: "divide", 24: "hue", 25: "saturation", 26: "color", 27: "clear",
}
BLEND_TO_COMPOSITE = {v: k for k, v in COMPOSITE_TO_BLEND.items() if k not in (9, 11)}
BLEND_TO_COMPOSITE.update({"luminosity": 0, "behind": 0, "linear_dodge": 10})
COMPOSITE_ERASER = 27
# TextureCompositeMode / DualTextureCompositeMode: 0 multiply, 1 subtract (inferred from the UI order).
TEXTURE_MODES = {0: "multiply", 1: "subtract"}
TEXTURE_MODE_IDS = {v: k for k, v in TEXTURE_MODES.items()}
# DualBrushCompositeMode uses the CompositeMode list.
MAX_GENERATED_TIPS = 16

# Length units used by BrushSizeUnit / BrushInLengthUnit / ...: 0 px, 1 cm, 2 mm, 3 inch, 4 pt.
# Physical units are converted at 350 dpi (CSP's default canvas resolution for illustrations).
_DPI = 350.0
_UNIT_TO_PX = {0: 1.0, 1: _DPI / 2.54, 2: _DPI / 25.4, 3: _DPI, 4: _DPI / 72.0}
ORDER_RANDOM = 3


def to_px(value, unit) -> float:
    return float(value or 0.0) * _UNIT_TO_PX.get(int(unit or 0), 1.0)


def _dyn(blob) -> Dynamic:
    e = decode_effector(blob)
    if not e:
        return Dynamic()
    return Dynamic(pressure=e.influence(EFF_PRESSURE), tilt=e.influence(EFF_TILT),
                   velocity=e.influence(EFF_VELOCITY), jitter=e.influence(EFF_RANDOM))


def _enc(d: Dynamic) -> bytes:
    return encode_effector(d.pressure, d.tilt, d.velocity, d.jitter)


def _num(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _img_hash(images: list[Image.Image]) -> str:
    h = hashlib.sha1()
    for im in images:
        h.update(f"{im.mode}{im.size}".encode())
        h.update(im.tobytes())
    return h.hexdigest()


def _hash_opt(im: Optional[Image.Image]) -> str:
    return _img_hash([im]) if im is not None else ""


# ---------------------------------------------------------------------------------------------
# Variant <-> Brush mapping
# ---------------------------------------------------------------------------------------------
ImageResolver = Callable[[MaterialRef], Optional[Image.Image]]


def variant_to_brush(v: dict, node: dict, resolve: ImageResolver, name: str = "") -> Brush:
    b = Brush(name=name or node.get("NodeName") or "CSP brush")
    size = to_px(v.get("BrushSize"), v.get("BrushSizeUnit"))
    b.size_px = max(0.1, size or 1.0)
    b.size_dynamic = _dyn(v.get("BrushSizeEffector"))
    b.opacity = _clamp(_num(v.get("Opacity"), 100) / 100)
    b.opacity_dynamic = _dyn(v.get("BrushOpacityEffector"))
    b.flow = _clamp(_num(v.get("BrushFlow"), 100) / 100)
    b.flow_dynamic = _dyn(v.get("BrushFlowEffector"))
    b.hardness = _clamp(_num(v.get("BrushHardness"), 100) / 100)
    b.antialias = int(v.get("AntiAlias") or 0) > 0
    b.stabilization = _clamp(_num(v.get("Stickness")) / 100)

    auto = int(v.get("BrushAutoIntervalType") or 0)
    if auto == 0:
        b.spacing = max(0.01, _num(v.get("BrushInterval"), 10) / 100)
    else:  # automatic spacing presets (narrow / normal / wide)
        b.spacing = {1: 0.05, 2: 0.1, 3: 0.25}.get(auto, 0.1)
        b.warn("Clip Studio Paint 的“自动”间隔已按近似的固定间隔转换。")

    b.roundness = _clamp(_num(v.get("BrushThickness"), 100) / 100, 0.01)
    if int(v.get("BrushVerticalThicknes") or 0):
        b.angle = 90.0
    b.angle = (b.angle + _num(v.get("BrushRotation"))) % 360
    if b.angle > 180:
        b.angle -= 360
    rot = int(v.get("BrushRotationEffector") or ROT_BASE)
    b.angle_follow_stroke = bool(rot & ROT_DIRECTION)
    if rot & ROT_RANDOM:
        b.angle_jitter = _clamp(_num(v.get("BrushRotationRandomScale"), 100) / 100)
    b.roundness_jitter = _dyn(v.get("BrushThicknessEffector")).jitter

    comp = int(v.get("CompositeMode") or 0)
    if comp == COMPOSITE_ERASER or node.get("NodeInputOp") == S.OP_ERASER:
        b.is_eraser = True
        b.blend_mode = "normal"
    else:
        b.blend_mode = COMPOSITE_TO_BLEND.get(comp, "normal")
        if comp not in COMPOSITE_TO_BLEND:
            b.warn(f"Clip Studio Paint 合成模式 {comp} 没有对应项，已按“通常”处理。")

    # Brush tips
    refs = decode_material_refs(v.get("BrushPatternImageArray")) if int(v.get("BrushUsePatternImage") or 0) else []
    for r in refs:
        im = resolve(r)
        if im is not None:
            b.tips.append(thumbnail_to_tip(im))
        else:
            b.warn(f"笔尖素材“{r.name or r.path}”未找到，已使用圆形笔尖代替。")
    b.flip_x = bool(v.get("BrushPatternReverseHorizontal"))
    b.flip_y = bool(v.get("BrushPatternReverseVertical"))
    b.tip_order = "random" if int(v.get("BrushPatternOrderType") or 0) == ORDER_RANDOM else "sequential"

    # Spray
    if int(v.get("BrushUseSpray") or 0):
        sp = _num(v.get("BrushSpraySize"))
        spray_px = b.size_px * sp / 100 if int(v.get("BrushSpraySizeSyncBrushSize") or 0) else to_px(sp, v.get("BrushSpraySizeUnit"))
        b.scatter = max(0.0, spray_px / max(b.size_px, 0.1))
        b.scatter_both_axes = True
        b.count = max(1, int(_num(v.get("BrushSprayDensity"), 1)))
        b.count_jitter = _dyn(v.get("BrushSprayDensityEffector")).jitter
        srot = int(v.get("BrushRotationEffectorInSpray") or ROT_BASE)
        if srot & ROT_RANDOM:
            b.angle_jitter = _clamp(_num(v.get("BrushRotationRandomInSpray"), 100) / 100)
        b.angle_follow_stroke = b.angle_follow_stroke or bool(srot & ROT_DIRECTION)

    # Taper ("入り抜き"); neutral unit = fraction of a 100 px stroke
    in_pct = int(v.get("BrushInOutType") or 0) == 1
    for side in ("In", "Out"):
        if int(v.get(f"BrushUse{side}") or 0):
            ln = _num(v.get(f"Brush{side}Length"))
            val = ln / 100 if in_pct else to_px(ln, v.get(f"Brush{side}LengthUnit")) / 100
            setattr(b, "taper_start" if side == "In" else "taper_end", _clamp(val))

    # Texture
    tex_refs = decode_material_refs(v.get("TextureImage"))
    if tex_refs:
        im = resolve(tex_refs[0])
        if im is not None:
            b.texture = Texture(
                image=thumbnail_to_texture(im), name=tex_refs[0].name,
                scale=max(0.01, _num(v.get("TextureScale2"), 100) / 100),
                depth=_clamp(_num(v.get("TextureDensity"), 50) / 100),
                invert=bool(v.get("TextureReverseDensity")),
                mode=TEXTURE_MODES.get(int(v.get("TextureCompositeMode") or 0), "multiply"),
                brightness=_clamp(_num(v.get("TextureBrightness")) / 100, -1),
                contrast=_clamp(_num(v.get("TextureContrast")) / 100, -1),
                per_tip=bool(v.get("TextureForPlot")),
                depth_dynamic=_dyn(v.get("TextureDensityEffector")))
        else:
            b.warn(f"纹理素材“{tex_refs[0].name}”未找到，已忽略纹理。")

    # Dual brush
    if int(v.get("UseDualBrush") or 0):
        drefs = decode_material_refs(v.get("DualPatternImageArray")) if int(v.get("DualUsePatternImage") or 0) else []
        dim = resolve(drefs[0]) if drefs else None
        dsize = to_px(v.get("DualSize"), v.get("DualSizeUnit"))
        if int(v.get("SyncDualBrushSize") or 0):
            dsize = b.size_px * _num(v.get("DualSize"), 100) / 100
        from ..core.imaging import round_tip

        b.dual = DualTip(
            image=thumbnail_to_tip(dim) if dim is not None else round_tip(64, _num(v.get("DualHardness"), 100) / 100),
            size_px=max(1.0, dsize), spacing=max(0.01, _num(v.get("DualInterval"), 10) / 100),
            count=max(1, int(_num(v.get("DualSprayDensity"), 1))) if int(v.get("DualUseSpray") or 0) else 1,
            mode=COMPOSITE_TO_BLEND.get(int(v.get("DualBrushCompositeMode") or 0), "multiply"),
            name=drefs[0].name if drefs else "")

    # Colour jitter
    if int(v.get("BrushChangePatternColor") or 0):
        b.color = ColorDynamics(
            fg_bg_jitter=_clamp(_num(v.get("BrushSubColor")) / 100),
            hue_jitter=_clamp(_num(v.get("BrushHueChange")) / 180),
            saturation_jitter=_clamp(_num(v.get("BrushSaturationChange")) / 100),
            brightness_jitter=_clamp(_num(v.get("BrushValueChange")) / 100), per_tip=True)
    elif int(v.get("BrushChangeStrokeColor") or 0):
        b.color = ColorDynamics(
            fg_bg_jitter=_clamp(_num(v.get("BrushStrokeSubColor")) / 100),
            hue_jitter=_clamp(_num(v.get("BrushStrokeHueChange")) / 180),
            saturation_jitter=_clamp(_num(v.get("BrushStrokeSaturationChange")) / 100),
            brightness_jitter=_clamp(_num(v.get("BrushStrokeValueChange")) / 100), per_tip=False)

    # Colour mixing
    if int(v.get("BrushUseWaterColor") or 0):
        load = _clamp(_num(v.get("BrushMixColor"), 50) / 100)
        b.wet = WetMix(enabled=True, load=load, mix=1 - load,
                       wetness=_clamp(1 - _num(v.get("BrushMixAlpha"), 50) / 100),
                       color_stretch=_clamp(_num(v.get("BrushMixColorExtension")) / 100))
    if int(v.get("BrushUseWaterEdge") or 0):
        b.warn("Clip Studio Paint 的“水彩边界”效果在其他软件中没有对应项，已忽略。")
    if int(v.get("BrushUseRevision") or 0):
        b.stabilization = max(b.stabilization, _clamp(_num(v.get("BrushRevision")) / 100))
    return b


def brush_columns(b: Brush) -> dict[str, object]:
    """Neutral Brush -> Variant column values (only the columns we understand)."""
    comp = COMPOSITE_ERASER if b.is_eraser else BLEND_TO_COMPOSITE.get(b.blend_mode, 0)
    rot = ROT_BASE | (ROT_DIRECTION if b.angle_follow_stroke else 0) | (ROT_RANDOM if b.angle_jitter > 0 else 0)
    spray = b.scatter > 0 or b.count > 1
    c: dict[str, object] = {
        "Opacity": int(round(_clamp(b.opacity) * 100)),
        "AntiAlias": 1 if b.antialias else 0,
        "CompositeMode": comp,
        "Stickness": int(round(_clamp(b.stabilization) * 100)),
        "BrushSize": round(float(b.size_px), 2),
        "BrushSizeUnit": 0,
        "BrushSizeEffector": _enc(b.size_dynamic),
        "BrushOpacityEffector": _enc(b.opacity_dynamic),
        "BrushFlow": int(round(_clamp(b.flow) * 100)),
        "BrushFlowEffector": _enc(b.flow_dynamic),
        "BrushHardness": int(round(_clamp(b.hardness) * 100)),
        "BrushInterval": round(max(0.01, b.spacing) * 100, 2),
        "BrushAutoIntervalType": 0,
        "BrushThickness": max(1, int(round(_clamp(b.roundness) * 100))),
        "BrushThicknessEffector": encode_effector(jitter=b.roundness_jitter),
        "BrushVerticalThicknes": 0,
        "BrushRotation": round(float(b.angle) % 360, 2),
        "BrushRotationEffector": rot,
        "BrushRotationRandomScale": int(round(_clamp(b.angle_jitter) * 100)) if b.angle_jitter > 0 else 100,
        "BrushPatternReverseHorizontal": int(b.flip_x),
        "BrushPatternReverseVertical": int(b.flip_y),
        "BrushPatternOrderType": ORDER_RANDOM if b.tip_order == "random" else 0,
        "BrushUseSpray": int(spray),
        "BrushSpraySize": round(max(b.scatter, 0.01) * b.size_px, 2) if spray else 2.0,
        "BrushSpraySizeUnit": 0,
        "BrushSpraySizeSyncBrushSize": 0,
        "BrushSprayDensity": max(1, int(b.count)),
        "BrushSprayDensityEffector": encode_effector(jitter=b.count_jitter),
        # with spray on, CSP rotates particles using the *InSpray fields
        "BrushRotationInSpray": round(float(b.angle) % 360, 2),
        "BrushRotationEffectorInSpray": rot,
        "BrushRotationRandomInSpray": int(round(_clamp(b.angle_jitter) * 100)) if b.angle_jitter > 0 else 100,
        "BrushInOutType": 0,
        "BrushUseIn": int(b.taper_start > 0),
        "BrushInLength": round(b.taper_start * 100, 2) if b.taper_start > 0 else 20.0,
        "BrushInLengthUnit": 0,
        "BrushUseOut": int(b.taper_end > 0),
        "BrushOutLength": round(b.taper_end * 100, 2) if b.taper_end > 0 else 20.0,
        "BrushOutLengthUnit": 0,
        "BrushUseWaterColor": int(b.wet.enabled),
        "BrushMixColor": int(round(_clamp(b.wet.load) * 100)),
        "BrushMixAlpha": int(round((1 - _clamp(b.wet.wetness)) * 100)),
        "BrushMixColorExtension": int(round(_clamp(b.wet.color_stretch) * 100)),
    }
    col = b.color
    per_tip = col.is_active() and col.per_tip
    per_stroke = col.is_active() and not col.per_tip
    pre = "Brush" if per_tip else "BrushStroke"
    c.update({"BrushChangePatternColor": int(per_tip), "BrushChangeStrokeColor": int(per_stroke)})
    if col.is_active():
        c.update({f"{pre}HueChange": int(round(_clamp(col.hue_jitter) * 180)),
                  f"{pre}SaturationChange": int(round(_clamp(col.saturation_jitter) * 100)),
                  f"{pre}ValueChange": int(round(_clamp(col.brightness_jitter) * 100)),
                  f"{pre}SubColor": int(round(_clamp(col.fg_bg_jitter) * 100))})
    t = b.texture
    if t is not None:
        c.update({"TextureScale2": round(t.scale * 100, 2), "TextureDensity": int(round(_clamp(t.depth) * 100)),
                  "TextureReverseDensity": int(t.invert), "TextureBrightness": int(round(t.brightness * 100)),
                  "TextureContrast": int(round(t.contrast * 100)), "TextureForPlot": int(t.per_tip),
                  "TextureCompositeMode": TEXTURE_MODE_IDS.get(t.mode, 0),
                  "TextureDensityEffector": _enc(t.depth_dynamic)})
    d = b.dual
    if d is not None:
        dspray = d.scatter > 0 or d.count > 1
        c.update({"DualBrushCompositeMode": BLEND_TO_COMPOSITE.get(d.mode, 2), "SyncDualBrushSize": 0,
                  "DualSize": round(max(1.0, d.size_px), 2), "DualSizeUnit": 0,
                  "DualInterval": round(max(0.01, d.spacing) * 100, 2), "DualAutoIntervalType": 0,
                  "DualUseSpray": int(dspray), "DualSprayDensity": max(1, int(d.count)),
                  "DualSpraySize": round(max(d.scatter, 0.01) * d.size_px, 2) if dspray else 2.0,
                  "DualSpraySizeUnit": 0, "DualSpraySizeSyncBrushSize": 0})
    return c


def _fingerprint(b: Brush) -> dict[str, object]:
    fp = brush_columns(b)
    fp["__tips"] = _img_hash(b.tips)
    fp["__texture"] = _hash_opt(b.texture.image if b.texture else None)
    fp["__dual"] = _hash_opt(b.dual.image if b.dual else None)
    fp["__eraser"] = b.is_eraser
    return fp


def loses_images(b: Brush) -> bool:
    """True when writing ``b`` as .sut needs newly generated tip / texture / dual-tip materials."""
    nat = (b.source.native or {}).get("csp") if b.source else None
    old = (nat or {}).get("fingerprint") or {}
    new = _fingerprint(b)
    return ((bool(b.tips) and old.get("__tips") != new["__tips"])
            or (b.texture is not None and old.get("__texture") != new["__texture"])
            or (b.dual is not None and old.get("__dual") != new["__dual"]))


# ---------------------------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------------------------
def _embedded_image(file_data: bytes) -> Optional[Image.Image]:
    """Full-resolution pixels for unencrypted materials, else the material thumbnail.

    Returned as black ink on transparency, like CSP thumbnails.
    """
    layer = next((v for k, v in material_members(file_data).items() if k.endswith(".layer")), None)
    try:
        ink = read_material_ink(layer) if layer else None
    except (sqlite3.Error, struct.error, zlib.error, ValueError):
        ink = None
    if ink is not None:
        im = Image.new("RGBA", (ink.shape[1], ink.shape[0]), (0, 0, 0, 0))
        im.putalpha(Image.fromarray(ink))
        return im
    return material_thumbnail(file_data)


def _material_resolver(rows: list[dict]) -> ImageResolver:
    """Embedded MaterialFile rows first, then the local CSP material library (bundled materials)."""
    cache: dict[str, Optional[Image.Image]] = {}
    local = S.store_resolver(S.material_root())

    def resolve(ref: MaterialRef) -> Optional[Image.Image]:
        if ref.path in cache:
            return cache[ref.path]
        im = None
        uid = next((p for p in reversed(ref.path.split(":")) if len(p) >= 32), "")
        for r in rows:
            if r.get("OriginalPath") == ref.path or (uid and uid in (r.get("OriginalPath") or "")):
                im = _embedded_image(r.get("FileData") or b"")
                break
        if im is None:
            try:
                im = local(ref)
            except OSError:
                im = None
        cache[ref.path] = im
        return im

    return resolve


def _paint_nodes(con: sqlite3.Connection) -> list[tuple[S.TreeNode, dict]]:
    variants = {}
    for r in con.execute("SELECT * FROM Variant"):
        d = dict(r)
        variants[d.get("VariantID")] = d
    out = []
    for n in S.walk_tree(con):
        v = variants.get(n.row.get("NodeVariantID"))
        if S.is_brush_variant(v):
            out.append((n, v))
    return out


@register_codec
class CspCodec(FormatCodec):
    id = "csp"
    name = "Clip Studio Paint 子工具 (.sut)"
    extensions = (".sut",)
    can_read = True
    can_write = True

    # -- sniff / compatibility -----------------------------------------------------------------
    def sniff(self, path: Path) -> Optional[FileInfo]:
        path = Path(path)
        if not S.is_sqlite(path):
            return None
        try:
            con = S.open_readonly(path)
        except sqlite3.Error:
            return None
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"Manager", "Node", "Variant"} <= tables:
                return None
            mgr = dict(con.execute("SELECT * FROM Manager").fetchone() or {})
            vcols = S.table_columns(con, "Variant")
            ncols = S.table_columns(con, "Node")
            nodes = _paint_nodes(con)
            nmat = con.execute("SELECT COUNT(*) FROM MaterialFile").fetchone()[0] if "MaterialFile" in tables else 0
        except sqlite3.Error:
            return None
        finally:
            con.close()
        version = int(mgr.get("Version") or 0)
        info = FileInfo(path=path, format_id=self.id, format_name=self.name, version=str(version),
                        brush_count=len(nodes))
        installed = S.installed_schema()
        info.details = {
            "Manager.Version": str(version),
            "版本推测": S.version_label(version, installed),
            "Variant 参数列数": str(len(vcols)),
            "子工具": "、".join(n.name for n, _ in nodes[:5]) or "（无笔刷类子工具）",
            "嵌入素材数": str(nmat),
        }
        if installed:
            info.details["已安装的 CSP"] = f"{installed.app_version}（Version {installed.version}）"
            problem = self._problem(version, {c for c, _ in vcols}, {c for c, _ in ncols}, installed)
            if problem:
                info.problems.append(problem)
                info.fixes.append(self._downgrade_fix(installed))
        return info

    @staticmethod
    def _problem(version: int, vcols: set[str], ncols: set[str], installed: S.Schema) -> Optional[str]:
        unknown = sorted((vcols - installed.variant_columns) | (ncols - installed.node_columns))
        if version <= installed.version and not unknown:
            return None
        msg = f"此子工具由比已安装的 Clip Studio Paint {installed.app_version} 更新的版本导出"
        msg += f"（文件版本 {version}，已安装版本 {installed.version}）" if version > installed.version else ""
        if unknown:
            msg += f"，包含 {len(unknown)} 个旧版不认识的参数（如 {', '.join(unknown[:3])}）"
        return msg + "，直接导入可能失败或丢失设置。"

    def _downgrade_fix(self, installed: S.Schema) -> Fix:
        def apply(src: Path, out_dir: Path) -> list[Path]:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            dst = unique_path(out_dir / f"{Path(src).stem}_CSP{installed.app_version or installed.version}.sut")
            return [S.downgrade_sut(Path(src), dst, installed)]

        return Fix(title="转换为旧版 CSP 可读取的格式",
                   description=f"复制一份子工具文件，删除 Clip Studio Paint {installed.app_version} 不认识的参数并降低版本号；"
                               "新版本特有的设置会丢失，原文件不会被修改。",
                   apply=apply)

    def compatibility(self, info: FileInfo, app_id: str, app_version: str) -> Optional[str]:
        if app_id != "csp":
            return None
        installed = S.installed_schema(app_version or "")
        if not installed:
            return None
        try:
            con = S.open_readonly(info.path)
            try:
                vcols = {c for c, _ in S.table_columns(con, "Variant")}
                ncols = {c for c, _ in S.table_columns(con, "Node")}
            finally:
                con.close()
        except sqlite3.Error:
            return None
        try:
            version = int(info.version or 0)
        except ValueError:
            version = 0
        return self._problem(version, vcols, ncols, installed)

    # -- read ----------------------------------------------------------------------------------
    def read(self, path: Path) -> list[Brush]:
        path = Path(path)
        con = S.open_readonly(path)
        try:
            mgr = dict(con.execute("SELECT * FROM Manager").fetchone() or {})
            vcols = S.table_columns(con, "Variant")
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            mats = [dict(r) for r in con.execute("SELECT * FROM MaterialFile")] if "MaterialFile" in tables else []
            nodes = _paint_nodes(con)
            init = {r["VariantID"]: dict(r) for r in con.execute("SELECT * FROM Variant")}
        finally:
            con.close()
        resolve = _material_resolver(mats)
        out = []
        for i, (n, v) in enumerate(nodes):
            b = variant_to_brush(v, n.row, resolve)
            b.group = "/".join(p for p in n.path if p)
            b.source = BrushSource(app="csp", format=self.id, version=str(mgr.get("Version") or ""), path=str(path),
                                   index=i)
            b.source.native = {"csp": {
                "manager": mgr, "node": dict(n.row), "variant": v,
                "init_variant": init.get(n.row.get("NodeInitVariantID")),
                "variant_columns": vcols, "materials": mats, "fingerprint": _fingerprint(b)}}
            out.append(b)
        return out

    # -- write ---------------------------------------------------------------------------------
    def write(self, brushes: list[Brush], out_path: Path, **options) -> list[Path]:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        files = []
        for b in brushes:
            if len(brushes) == 1:
                dst = out_path
            else:
                dst = unique_path(out_path.parent / f"{safe_filename(b.name)}.sut")
            if dst.exists():
                dst.unlink()
            write_sut(b, dst, version=options.get("version"))
            files.append(dst)
        return files


def build_variant(b: Brush) -> tuple[dict, list[tuple[str, str]], list[dict], dict]:
    """Return (variant row, variant column DDL, material rows, native info) for a brush."""
    nat = (b.source.native or {}).get("csp") if b.source else None
    if nat and nat.get("variant"):
        row = dict(nat["variant"])
        cols = [tuple(c) for c in nat.get("variant_columns") or []] or _default_cols()
        fp_old = nat.get("fingerprint") or {}
        mats = list(nat.get("materials") or [])
    else:
        row = S.variant_defaults()
        row["BrushUsePatternImage"] = 0
        row["BrushUseIn"] = 0
        row["BrushUseOut"] = 0
        cols = _default_cols()
        fp_old, mats = {}, []
    fp_new = _fingerprint(b)
    for k, val in brush_columns(b).items():
        if not fp_old or fp_old.get(k) != val:
            row[k] = val
    if fp_old and fp_old.get("__eraser") and not b.is_eraser and row.get("CompositeMode") == COMPOSITE_ERASER:
        row["CompositeMode"] = BLEND_TO_COMPOSITE.get(b.blend_mode, 0)

    # Images without a native CSP material: generate new (unencrypted) materials
    generated: list[dict] = []

    def gen(images: list[Image.Image], kind: str, label: str) -> bytes:
        refs = []
        for i, im in enumerate(images):
            name = f"{b.name} {label}" + (f" {i + 1}" if len(images) > 1 else "")
            m = build_material(im, name, kind)
            refs.append(m.ref())
            generated.append(m.material_row())
        return encode_material_refs(refs)

    if fp_old.get("__tips", "") != fp_new["__tips"]:
        if b.tips:
            if len(b.tips) > MAX_GENERATED_TIPS:
                b.warn(f"笔尖图像超过 {MAX_GENERATED_TIPS} 个，只保留前 {MAX_GENERATED_TIPS} 个。")
            row["BrushUsePatternImage"] = 1
            row["BrushPatternImageArray"] = gen(b.tips[:MAX_GENERATED_TIPS], KIND_TIP, "笔尖")
        else:
            row["BrushUsePatternImage"] = 0
            row["BrushPatternImageArray"] = None
    if fp_old.get("__texture", "") != fp_new["__texture"]:
        row["TextureImage"] = gen([b.texture.image], KIND_TEXTURE, "纹理") if b.texture is not None else None
    if fp_old.get("__dual", "") != fp_new["__dual"]:
        if b.dual is not None:
            row["UseDualBrush"] = 1
            row["DualUsePatternImage"] = 1
            row["DualPatternImageArray"] = gen([b.dual.image], KIND_TIP, "双重笔尖")
        else:
            row["UseDualBrush"] = 0
            row["DualPatternImageArray"] = None
    used = set()
    for col in ("BrushPatternImageArray", "TextureImage", "DualPatternImageArray", "DualTextureImage"):
        for r in decode_material_refs(row.get(col)):
            used.add(r.path)
    mats = [m for m in mats if m.get("OriginalPath") in used] + generated
    return row, cols, mats, nat or {}


def _default_cols() -> list[tuple[str, str]]:
    return [("VariantID", "INTEGER")] + [(n, t) for n, t, _ in S.VARIANT_COLUMNS]


def write_sut(b: Brush, dst: Path, version: Optional[int] = None) -> Path:
    row, cols, mats, nat = build_variant(b)
    node_uuid = S.new_uuid()
    mgr_old = nat.get("manager") or {}
    ver = int(version or mgr_old.get("Version") or S.DEFAULT_WRITE_VERSION)
    node_old = dict(nat.get("node") or {})
    op, icon = (S.OP_ERASER, S.ICON_ERASER) if b.is_eraser else (S.OP_BRUSH, S.ICON_BRUSH)
    if node_old.get("NodeInputOp") in S.PAINT_OPS and bool(node_old.get("NodeInputOp") == S.OP_ERASER) == b.is_eraser:
        op, icon = node_old["NodeInputOp"], node_old.get("NodeIcon") or icon
    node = {c: None for c, _ in S.NODE_COLUMNS}
    node.update({
        "NodeUuid": node_uuid, "NodeName": b.name, "NodeShortCutKey": 0, "NodeLock": 0,
        "NodeInputOp": op, "NodeOutputOp": node_old.get("NodeOutputOp", op) if node_old else op,
        "NodeRangeOp": node_old.get("NodeRangeOp", 0) or 0, "NodeIcon": icon,
        "NodeIconColor": node_old.get("NodeIconColor", 0) or 0, "NodeHidden": 0,
        "NodeInstalledState": 0, "NodeInstalledVersion": min(int(node_old.get("NodeInstalledVersion") or 0), ver),
        "NodeHideByGrade": 0, "NodeDefaultIdentifier": 0, "NodeMaterialIDExists": 0, "NodeMaterialContentID": 0,
        "NodeMaterialUuid": "", "NodeNextUuid": S.EMPTY_UUID, "NodeFirstChildUuid": S.EMPTY_UUID,
        "NodeSelectedUuid": S.EMPTY_UUID,
        "NodeVariantID": 1, "NodeInitVariantID": 2, "NodeCustomIcon": node_old.get("NodeCustomIcon"),
    })
    con = sqlite3.connect(dst)
    try:
        S.create_sut_schema(con, cols)
        con.execute("INSERT INTO Manager(ToolType, Version, RootUuid, CurrentNodeUuid, MaxVariantID, CommonVariantID,"
                    " ObjectNodeUuid, PressureGraph, SavedCount, PressureGraphInitialized, UsePressureFile)"
                    " VALUES (0,?,?,?,2,?,?,NULL,1,0,0)",
                    (ver, node_uuid, S.EMPTY_UUID, S.COMMON_VARIANT_ID, S.OBJECT_NODE_UUID))
        names = [c for c, _ in S.NODE_COLUMNS]
        con.execute(f"INSERT INTO Node({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                    [node[c] for c in names])
        vnames = [c for c, _ in cols]
        for vid in (1, 2):
            r = dict(row)
            r["VariantID"] = vid
            con.execute(f"INSERT INTO Variant({','.join(vnames)}) VALUES ({','.join('?' * len(vnames))})",
                        [r.get(c) for c in vnames])
        mnames = [c for c, _ in S.MATERIAL_COLUMNS]
        for m in mats:
            con.execute(f"INSERT INTO MaterialFile({','.join(mnames)}) VALUES ({','.join('?' * len(mnames))})",
                        [m.get(c) for c in mnames])
        con.commit()
    finally:
        con.close()
    return dst
