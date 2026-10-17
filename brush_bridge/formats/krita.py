"""Krita resource bundles (.bundle) and paintop presets (.kpp).

.kpp  A PNG (the preset thumbnail) with two text chunks: ``version`` ("2.2" = Krita 4 format,
      still loaded by Krita 5; "5.0" = Krita 5 only, may embed resources) and ``preset`` (XML):

        <Preset name=".." paintopid="paintbrush" [embedded_resources="N"]>
          [<resources><resource type="brushes|patterns" name filename md5sum>BASE64</resource></resources>]
          <param name="SizeSensor" type="string"><![CDATA[...]]></param> ...
        </Preset>

      Param types: string (CDATA), internal (plain text), bytearray (base64 of the stored bytes).
      The brush tip lives in ``brush_definition`` (nested XML, see ``_brush_definition_*``).
      Curve options ("Size", "Opacity", "Rotation", ...) use ``Pressure<X>`` (checked),
      ``<X>Sensor`` (sensor XML), ``<X>UseCurve`` ("Enable pen settings"), ``<X>UseSameCurve``,
      ``<X>commonCurve``, ``<X>Value`` (strength / base value) and ``<X>curveMode``.

.bundle  A zip: ``mimetype`` (application/x-krita-resourcebundle), ``META-INF/manifest.xml``,
      ``meta.xml``, ``preview.png`` and resource folders (``paintoppresets``, ``brushes``,
      ``patterns``, ...).

See docs/formats/krita.md for the full field mapping and the sources it was derived from.
"""
from __future__ import annotations

import base64
import hashlib
import io
import math
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from xml.sax.saxutils import escape as _xml_escape, quoteattr as _quoteattr

import numpy as np
from PIL import Image, ImageOps, PngImagePlugin

from ..core.imaging import fit, round_tip, stroke_preview
from ..core.model import Brush, BrushRef, BrushSource, ColorDynamics, DualTip, Dynamic, Texture
from ..core.plugin import FileInfo, FormatCodec, Fix, register_codec
from ..core.system import safe_filename, unique_path
from . import gimp as _gimp

BUNDLE_MIMETYPE = "application/x-krita-resourcebundle"
MANIFEST_NS = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
META_NS = "urn:oasis:names:tc:opendocument:xmlns:meta:1.0"
DC_NS = "http://purl.org/dc/elements/1.1"
KPP_VERSION_K4 = "2.2"
KPP_VERSION_K5 = "5.0"
DEFAULT_CURVE = "0,0;1,1;"
MAX_TIP_SIDE = 2500

# Resource folders that only exist in Krita 5 bundles.
_K5_ONLY_TYPES = {"seexpr_scripts", "fonts", "css_styles"}
# Paint engines that only exist in Krita 5.
_K5_ONLY_PAINTOPS = {"mypaintbrush"}

_PAINTOP_NAMES = {
    "paintbrush": "像素笔刷", "colorsmudge": "涂抹笔刷", "hairybrush": "鬃毛笔刷", "spraybrush": "喷雾笔刷",
    "roundmarker": "快速笔刷", "duplicate": "克隆笔刷", "filter": "滤镜笔刷", "deformbrush": "变形笔刷",
    "sketchbrush": "素描笔刷", "gridbrush": "网格笔刷", "hatchingbrush": "排线笔刷", "curvebrush": "曲线笔刷",
    "particlebrush": "粒子笔刷", "experimentbrush": "形状笔刷", "tangentnormal": "切线法线笔刷",
    "mypaintbrush": "MyPaint 笔刷", "dynabrush": "动态笔刷", "chalkbrush": "粉笔笔刷",
}

# Krita composite op id <-> neutral blend mode
_COMPOSITE_TO = {
    "normal": "normal", "multiply": "multiply", "screen": "screen", "overlay": "overlay", "darken": "darken",
    "lighten": "lighten", "dodge": "color_dodge", "burn": "color_burn", "linear_burn": "linear_burn",
    "add": "add", "linear_dodge": "add", "subtract": "subtract", "diff": "difference", "hard_mix": "hard_mix",
    "hard_mix_photoshop": "hard_mix", "dissolve": "dissolve", "behind": "behind", "clear": "clear",
    "soft_light": "soft_light", "soft_light_svg": "soft_light", "hard_light": "hard_light",
    "exclusion": "exclusion", "hue": "hue", "saturation": "saturation", "color": "color",
    "luminize": "luminosity", "divide": "divide", "height": "height", "linear_height": "linear_height",
}
_COMPOSITE_FROM = {
    "normal": "normal", "multiply": "multiply", "screen": "screen", "overlay": "overlay", "darken": "darken",
    "lighten": "lighten", "color_dodge": "dodge", "color_burn": "burn", "linear_burn": "linear_burn",
    "add": "add", "subtract": "subtract", "difference": "diff", "hard_mix": "hard_mix_photoshop",
    "dissolve": "dissolve", "behind": "behind", "clear": "clear", "soft_light": "soft_light",
    "hard_light": "hard_light", "exclusion": "exclusion", "hue": "hue", "saturation": "saturation",
    "color": "color", "luminosity": "luminize", "divide": "divide",
}

# Texture/Pattern/TexturingMode (KisTextureOptionData::TexturingMode)
_TEX_MODES = ["multiply", "subtract", "lightness", "gradient", "darken", "overlay", "color_dodge", "color_burn",
              "add", "linear_burn", "hard_mix", "hard_mix_softer", "height", "linear_height"]
_TEX_MODE_ALIASES = {"linear_dodge": "add", "difference": "subtract", "color": "multiply", "normal": "multiply"}

# MaskingBrush/MaskingCompositeOp values accepted by Krita's masking brush (dual brush)
_MASK_OPS_FROM = {"multiply": "multiply", "darken": "darken", "overlay": "overlay", "color_dodge": "dodge",
                  "color_burn": "burn", "linear_burn": "linear_burn", "add": "linear_dodge",
                  "hard_mix": "hard_mix_photoshop", "subtract": "subtract", "height": "height_photoshop",
                  "linear_height": "linear_height_photoshop", "lighten": "lighten", "screen": "screen"}
_MASK_OPS_TO = {v: k for k, v in _MASK_OPS_FROM.items()}
_MASK_OPS_TO.update({"height": "height", "linear_height": "linear_height", "hard_mix_softer_photoshop": "hard_mix"})

_TILT_SENSORS = {"xtilt", "ytilt", "declination", "ascension", "tiltdirection", "tiltelevation"}
_JITTER_SENSORS = {"fuzzy", "fuzzystroke"}

# Curve options written to every preset (ids as used in the param names).
_CURVE_OPTIONS = ["Darken", "Flow", "LightnessStrength", "Mirror", "Mix", "Opacity", "Rate", "Ratio", "Rotation",
                  "Scatter", "Sharpness", "Size", "Softness", "Spacing", "Texture/Strength/", "h", "s", "v"]
_MANAGED_OPTIONS = {"Flow", "Opacity", "Ratio", "Rotation", "Scatter", "Size", "Texture/Strength/", "h", "s", "v"}


class KritaFormatError(ValueError):
    pass


# =============================================================================================
# Low level: .kpp
# =============================================================================================
@dataclass
class EmbeddedResource:
    type: str
    name: str
    filename: str
    md5: str
    data: bytes


@dataclass
class KritaPreset:
    name: str
    paintop: str
    version: str
    params: dict[str, str] = field(default_factory=dict)  # decoded values (bytearray -> latin-1 text)
    types: dict[str, str] = field(default_factory=dict)
    resources: list[EmbeddedResource] = field(default_factory=list)
    preview: Optional[Image.Image] = None
    filename: str = ""

    def raw(self, key: str) -> bytes:
        """Raw bytes of a param (bytearray params are already base64-decoded once)."""
        return self.params.get(key, "").encode("latin-1", "replace")


_BAD_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _parse_preset_xml(text: str) -> tuple[dict, dict[str, str], dict[str, str], list[EmbeddedResource]]:
    text = _BAD_XML_CHARS.sub("", text)
    params: dict[str, str] = {}
    types: dict[str, str] = {}
    resources: list[EmbeddedResource] = []
    try:
        root = ET.fromstring(text)
        attrs = dict(root.attrib)
        for p in root.iter("param"):
            name = p.get("name", "")
            ptype = p.get("type", "string")
            types[name] = ptype
            params[name] = _decode_param(p.text or "", ptype)
        for r in root.iter("resource"):
            try:
                data = base64.b64decode("".join((r.text or "").split()))
            except ValueError:
                continue
            resources.append(EmbeddedResource(r.get("type", ""), r.get("name", ""), r.get("filename", ""),
                                               r.get("md5sum", ""), data))
        return attrs, params, types, resources
    except ET.ParseError:
        pass
    # tolerant fallback for broken XML (e.g. binary garbage in PatternMD5)
    m = re.search(r"<Preset\b([^>]*)>", text)
    if not m:
        raise KritaFormatError("no <Preset> element")
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
    for pm in re.finditer(r'<param\s+([^>]*?)(?:/>|>(.*?)</param>)', text, re.S):
        a = dict(re.findall(r'(\w+)="([^"]*)"', pm.group(1)))
        body = pm.group(2) or ""
        cd = re.fullmatch(r"\s*<!\[CDATA\[(.*)\]\]>\s*", body, re.S)
        val = cd.group(1) if cd else _xml_unescape(body)
        name = _xml_unescape(a.get("name", ""))
        types[name] = a.get("type", "string")
        params[name] = _decode_param(val, types[name])
    return attrs, params, types, resources


def _xml_unescape(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
            .replace("&amp;", "&"))


def _decode_param(text: str, ptype: str) -> str:
    if ptype == "bytearray":
        try:
            return base64.b64decode("".join(text.split())).decode("latin-1")
        except ValueError:
            return ""
    return text


def preset_display_name(data: bytes, filename: str = "") -> str:
    """Preset name as Krita 5 shows it (``name`` attribute, '_' -> ' '), read cheaply when possible."""
    m = re.search(rb'<Preset\b[^>]*?\bname="([^"]*)"', data[:200000])
    if m:
        try:
            return _xml_unescape(m.group(1).decode("utf-8")).replace("_", " ")
        except UnicodeDecodeError:
            pass
    try:
        return parse_kpp(data, filename).name.replace("_", " ")
    except KritaFormatError:
        return Path(filename).stem.replace("_", " ")


def parse_kpp(data: bytes, filename: str = "") -> KritaPreset:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001 - PIL raises many types
        raise KritaFormatError(f"not a PNG: {e}") from e
    text = getattr(img, "text", {}) or {}
    xml = text.get("preset")
    if not xml:
        raise KritaFormatError("PNG has no 'preset' text chunk")
    attrs, params, types, resources = _parse_preset_xml(xml)
    version = (text.get("version") or "").strip() or KPP_VERSION_K4
    name = attrs.get("name") or Path(filename).stem.replace("_", " ") or "Krita preset"
    return KritaPreset(name=name, paintop=attrs.get("paintopid", params.get("paintop", "")), version=version,
                       params=params, types=types, resources=resources, preview=img.convert("RGBA"),
                       filename=filename)


def _cdata(s: str) -> str:
    return "<![CDATA[" + s.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def build_preset_xml(name: str, params: dict[str, str], types: Optional[dict[str, str]] = None,
                     resources: Optional[list[EmbeddedResource]] = None, paintop: str = "paintbrush") -> str:
    """Serialise a preset like Krita (params sorted by key, string values in CDATA)."""
    types = types or {}
    head = f"<Preset paintopid={_quoteattr(paintop)} name={_quoteattr(name)}"
    parts = []
    if resources:
        head += f' embedded_resources="{len(resources)}"'
        parts.append(" <resources>")
        for r in resources:
            parts.append(f" <resource name={_quoteattr(r.name)} filename={_quoteattr(r.filename)} "
                         f"type={_quoteattr(r.type)} md5sum={_quoteattr(r.md5)}>"
                         f"{_cdata(base64.b64encode(r.data).decode('ascii'))}</resource>")
        parts.append(" </resources>")
    for key in sorted(params):
        t = types.get(key, "string")
        val = params[key]
        if t == "bytearray":
            body = base64.b64encode(val.encode("latin-1")).decode("ascii")
        elif t == "internal":
            body = _xml_escape(val)
        else:
            body = _cdata(val)
        parts.append(f" <param name={_quoteattr(key)} type={_quoteattr(t)}>{body}</param>")
    return head + ">" + "".join(parts) + "\n</Preset>\n"


def encode_kpp(xml: str, preview: Image.Image, version: str = KPP_VERSION_K4) -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_text("version", version)
    info.add_text("preset", xml)  # PIL switches to iTXt automatically for non-Latin-1 text
    buf = io.BytesIO()
    preview.save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


# =============================================================================================
# Curves and sensors
# =============================================================================================
def parse_curve(s: str) -> list[tuple[float, float]]:
    pts = []
    for part in (s or "").split(";"):
        if "," in part:
            try:
                x, y = part.split(",", 1)
                pts.append((float(x), float(y)))
            except ValueError:
                continue
    pts.sort()
    return pts or [(0.0, 0.0), (1.0, 1.0)]


def curve_at(pts: list[tuple[float, float]], x: float) -> float:
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def format_curve(pts: list[tuple[float, float]]) -> str:
    return "".join(f"{_num(x)},{_num(y)};" for x, y in pts)


def _num(v: float) -> str:
    s = f"{v:.6g}"
    return "0" if s in ("-0", "-0.0") else s


def parse_sensors(xml: str) -> dict[str, dict]:
    """Return {sensor_id: {"curve": str|None, **attributes}} from a <X>Sensor param."""
    out: dict[str, dict] = {}
    if not xml:
        return out
    text = re.sub(r"<!DOCTYPE[^>]*>", "", xml).strip()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        for m in re.finditer(r'id="(\w+)"', xml):
            if m.group(1) != "sensorslist":
                out[m.group(1)] = {"curve": None}
        return out
    nodes = [c for c in root if c.tag == "ChildSensor"] if root.get("id") == "sensorslist" else [root]
    for n in nodes:
        sid = n.get("id")
        if not sid or sid == "sensorslist":
            continue
        c = n.find("curve")
        d = {k: v for k, v in n.attrib.items() if k != "id"}
        d["curve"] = c.text if c is not None and c.text else None
        out[sid] = d
    return out


def sensor_xml(sensors: list[tuple[str, str, dict]]) -> str:
    """Build sensor XML from [(id, curve, extra_attrs)] like KisKritaSensorPack::write."""
    def one(tag: str, sid: str, curve: str, attrs: dict) -> str:
        a = "".join(f" {k}={_quoteattr(str(v))}" for k, v in attrs.items())
        return f"<{tag} id={_quoteattr(sid)}{a}>\n  <curve>{curve}</curve>\n </{tag}>"

    if not sensors:
        sensors = [("pressure", DEFAULT_CURVE, {})]
    if len(sensors) == 1:
        sid, curve, attrs = sensors[0]
        return "<!DOCTYPE params>\n" + one("params", sid, curve, attrs).replace("\n ", "\n") + "\n"
    kids = "\n ".join(one("ChildSensor", s, c, a) for s, c, a in sensors)
    return f'<!DOCTYPE params>\n<params id="sensorslist">\n {kids}\n</params>\n'


@dataclass
class CurveOption:
    id: str
    checked: bool
    use_curve: bool
    value: float
    sensors: dict[str, list[tuple[float, float]]]
    attrs: dict[str, dict]

    def active(self) -> bool:
        return self.checked and self.use_curve

    def span(self, sid: str) -> float:
        pts = self.sensors.get(sid)
        if not pts:
            return 0.0
        ys = [y for _, y in pts]
        return max(0.0, min(1.0, max(ys) - min(ys)))


def read_option(params: dict[str, str], oid: str, checkable: bool = True, default_value: float = 1.0) -> CurveOption:
    pressure_key = "Pressure" + oid
    if checkable:
        checked = _bool(params.get(pressure_key), False)
    else:
        checked = True
    use_curve = _bool(params.get(oid + "UseCurve"), True)
    same = _bool(params.get(oid + "UseSameCurve"), True)
    common = params.get(oid + "commonCurve")
    raw = parse_sensors(params.get(oid + "Sensor", ""))
    sensors: dict[str, list[tuple[float, float]]] = {}
    last_curve = None
    for sid, d in raw.items():
        if d.get("curve"):
            last_curve = d["curve"]
    for sid, d in raw.items():
        if same:
            curve = common or last_curve or DEFAULT_CURVE
        else:
            curve = d.get("curve") or DEFAULT_CURVE
        sensors[sid] = parse_curve(curve)
    return CurveOption(oid, checked, use_curve, _float(params.get(oid + "Value"), default_value), sensors, raw)


def _bool(v: Optional[str], default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("true", "1", "yes")


def _float(v: Optional[str], default: float) -> float:
    try:
        f = float(v)  # type: ignore[arg-type]
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _dynamic_from_option(opt: CurveOption) -> Dynamic:
    d = Dynamic()
    if not opt.active():
        return d
    for sid, pts in opt.sensors.items():
        span = opt.span(sid)
        if sid in ("pressure", "pressurein"):
            d.pressure = max(d.pressure, span)
        elif sid in _TILT_SENSORS:
            d.tilt = max(d.tilt, span)
        elif sid == "speed":
            d.velocity = max(d.velocity, span)
        elif sid in _JITTER_SENSORS:
            d.jitter = max(d.jitter, span)
    return d


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _sensors_for_dynamic(d: Dynamic) -> list[tuple[str, str, dict]]:
    out = []
    if d.pressure > 0:
        out.append(("pressure", format_curve([(0, 1 - _clamp(d.pressure)), (1, 1)]), {}))
    if d.tilt > 0:
        out.append(("declination", format_curve([(0, 1 - _clamp(d.tilt)), (1, 1)]), {}))
    if d.velocity > 0:
        out.append(("speed", format_curve([(0, 1), (1, 1 - _clamp(d.velocity))]), {}))
    if d.jitter > 0:
        out.append(("fuzzy", format_curve([(0, 1 - _clamp(d.jitter)), (1, 1)]), {}))
    return out


def _write_option(params: dict[str, str], oid: str, *, checked: bool, use_curve: bool, value: float,
                  sensors: Optional[list[tuple[str, str, dict]]] = None) -> None:
    sensors = sensors or [("pressure", DEFAULT_CURVE, {})]
    params["Pressure" + oid] = _tf(checked)
    params[oid + "Sensor"] = sensor_xml(sensors)
    params[oid + "UseCurve"] = _tf(use_curve)
    params[oid + "UseSameCurve"] = _tf(len(sensors) == 1)
    params[oid + "commonCurve"] = sensors[-1][1]
    params[oid + "Value"] = _num(value)
    params[oid + "curveMode"] = "0"


def _tf(b: bool) -> str:
    return "true" if b else "false"


# =============================================================================================
# Resources: lookup for tips and patterns
# =============================================================================================
class ResourceResolver:
    """Finds resource files by (type, filename, md5) in a bundle, embedded data or folders."""

    def __init__(self) -> None:
        self._by_name: dict[tuple[str, str], Callable[[], bytes]] = {}
        self._by_md5: dict[tuple[str, str], Callable[[], bytes]] = {}
        self.dirs: list[tuple[str, Path]] = []

    def add(self, rtype: str, filename: str, loader: Callable[[], bytes], md5: str = "") -> None:
        key = (rtype, Path(filename.replace("\\", "/")).name.lower())
        self._by_name.setdefault(key, loader)
        if md5:
            self._by_md5.setdefault((rtype, md5.lower()), loader)

    def add_dir(self, rtype: str, d: Path) -> None:
        if d.is_dir():
            self.dirs.append((rtype, d))

    def find(self, rtype: str, filename: str = "", md5: str = "") -> Optional[tuple[str, bytes]]:
        if md5 and (rtype, md5.lower()) in self._by_md5:
            loader = self._by_md5[(rtype, md5.lower())]
            name = next((k[1] for k, v in self._by_name.items() if v is loader), filename)
            return name, loader()
        base = Path((filename or "").replace("\\", "/")).name
        if base and (rtype, base.lower()) in self._by_name:
            return base, self._by_name[(rtype, base.lower())]()
        for t, d in self.dirs:
            if t == rtype and base:
                p = d / base
                if p.is_file():
                    return base, p.read_bytes()
        return None


def _tip_from_png(data: bytes) -> Image.Image:
    """Krita PNG brush -> mask. Gray without alpha: white = background, black = paint."""
    img = Image.open(io.BytesIO(data))
    img.load()
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        rgba = img.convert("RGBA")
        a = np.asarray(rgba.getchannel("A"), np.float32)
        if a.min() < 255:
            rgb = np.asarray(rgba.convert("RGB"), np.int16)
            all_gray = bool(np.all(rgb[..., 0] == rgb[..., 1]) and np.all(rgb[..., 1] == rgb[..., 2]))
            if all_gray:
                g = rgb[..., 0].astype(np.float32)
                m = a * (255.0 - g) / 255.0
                if m.max() >= 32:
                    return Image.fromarray(np.clip(m + 0.5, 0, 255).astype(np.uint8), "L")
            return Image.fromarray(a.astype(np.uint8), "L")
    return ImageOps.invert(img.convert("L"))


def _decode_tip_file(fname: str, data: bytes) -> tuple[list[Image.Image], dict]:
    ext = Path(fname).suffix.lower()
    if ext == ".png":
        return [_tip_from_png(data)], {}
    if ext == ".gbr":
        b, _ = _gimp.parse_gbr(data)
        return [b.mask], {"color": b.color is not None, "spacing": b.spacing}
    if ext == ".gih":
        _name, params, cells = _gimp.parse_gih(data)
        return [c.mask for c in cells], {"sel": params.get("sel0", "random"),
                                          "color": any(c.color is not None for c in cells)}
    if ext in (".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"):
        return [_tip_from_png(data)], {}
    raise KritaFormatError(f"unsupported tip file {fname}")


def _pattern_image(fname: str, data: bytes) -> Image.Image:
    img = _gimp.read_any_image(data, fname)
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, rgba)
    return img.convert("L")


# =============================================================================================
# Preset -> Brush
# =============================================================================================
def _parse_brush_definition(xml: str) -> Optional[ET.Element]:
    if not xml:
        return None
    try:
        return ET.fromstring(_BAD_XML_CHARS.sub("", xml))
    except ET.ParseError:
        return None


def _apply_brush_definition(b: Brush, el: Optional[ET.Element], resolver: ResourceResolver, prefix: str = ""
                            ) -> tuple[list[Image.Image], float, dict]:
    """Fill tip/size/spacing/angle from a <Brush> element; returns (tips, size, extra)."""
    extra: dict = {}
    if el is None:
        b.warn("预设里没有笔尖定义，已使用圆形笔尖。")
        return [], b.size_px, extra
    btype = el.get("type", "auto_brush")
    spacing = _float(el.get("spacing"), 0.1)
    angle = math.degrees(_float(el.get("angle"), 0.0))
    if _bool(el.get("useAutoSpacing"), False):
        extra["auto_spacing"] = _float(el.get("autoSpacingCoeff"), 1.0)
    extra.update(type=btype, spacing=spacing, angle=angle)
    if btype == "auto_brush":
        mg = el.find("MaskGenerator")
        if mg is None:
            return [], b.size_px, extra
        diameter = _float(mg.get("diameter"), 30.0)
        ratio = _clamp(_float(mg.get("ratio"), 1.0), 0.01, 1.0)
        hfade = _clamp(_float(mg.get("hfade"), 1.0))
        vfade = _clamp(_float(mg.get("vfade"), 1.0))
        hardness = (hfade + vfade) / 2
        gid, shape = mg.get("id", "default"), mg.get("type", "circle")
        if gid in ("gauss", "soft"):
            hardness *= 0.5
        extra.update(hardness=hardness, roundness=ratio, shape=shape, generator=gid)
        spikes = int(_float(mg.get("spikes"), 2))
        if spikes > 2:
            b.warn(f"Krita 的 {spikes} 角星形笔尖已近似为圆形/方形。")
        if _float(el.get("density"), 1.0) < 0.999 or _float(el.get("randomness"), 0.0) > 0.001:
            b.warn("Krita 自动笔尖的“密度/随机度”无法转换，已忽略。")
        if shape == "rect":
            side = max(8, min(1024, int(round(diameter))))
            tip = _rect_tip(side, hardness, ratio, angle)
            return [tip], diameter, extra
        return [], diameter, extra
    filename = el.get("filename", "")
    md5 = el.get("md5sum", "")
    scale = _float(el.get("scale"), 1.0)
    if el.get("BrushVersion", "1") == "1":
        scale *= 2.0
    found = resolver.find("brushes", filename, md5)
    if not found:
        b.warn(f"找不到 Krita 笔尖文件“{filename}”，已用圆形笔尖代替。")
        return [], b.size_px, extra
    fname, data = found
    if btype == "svg_brush" or fname.lower().endswith(".svg"):
        b.warn(f"Krita 矢量笔尖“{fname}”无法转换，已用圆形笔尖代替。")
        return [], b.size_px, extra
    if btype == "abr_brush" or fname.lower().endswith(".abr"):
        b.warn(f"Krita 预设引用了 ABR 笔尖“{fname}”，请直接转换该 .abr 文件；这里已用圆形笔尖代替。")
        return [], b.size_px, extra
    try:
        tips, info = _decode_tip_file(fname, data)
    except Exception:  # noqa: BLE001 - damaged resource
        b.warn(f"Krita 笔尖文件“{fname}”无法读取，已用圆形笔尖代替。")
        return [], b.size_px, extra
    if info.get("color") or el.get("brushApplication") in ("1", "3"):
        b.warn("这是 Krita 彩色图像笔尖，颜色信息已丢弃，只保留形状。")
    extra.update(file=fname, sel=info.get("sel"))
    size = scale * max(max(t.size) for t in tips)
    return tips, size, extra


def _rect_tip(side: int, hardness: float, ratio: float, angle: float) -> Image.Image:
    r = side / 2.0
    yy, xx = np.mgrid[0:side, 0:side].astype(np.float32) + 0.5 - r
    a = math.radians(angle)
    xr = xx * math.cos(a) + yy * math.sin(a)
    yr = (-xx * math.sin(a) + yy * math.cos(a)) / max(0.01, ratio)
    d = np.maximum(np.abs(xr), np.abs(yr)) / r
    h = min(0.999, hardness)
    val = np.where(d <= h, 1.0, np.clip((1 - d) / max(1e-6, 1 - h), 0, 1))
    return Image.fromarray((val * 255).astype(np.uint8), "L")


def preset_to_brush(p: KritaPreset, resolver: ResourceResolver, path: str = "", index: int = -1,
                    group: str = "", bundle_version: str = "") -> Brush:
    pr = p.params
    # KisPaintOpPreset::name() shows underscores as spaces.
    b = Brush(name=p.name.replace("_", " "), group=group)
    if p.paintop and p.paintop != "paintbrush":
        b.warn(f"Krita 的“{_PAINTOP_NAMES.get(p.paintop, p.paintop)}”({p.paintop}) 引擎没有对应功能，"
               "已近似为普通笔刷。")
        if p.paintop == "colorsmudge":
            b.wet.enabled = True
            b.wet.mix = _clamp(_float(pr.get("SmudgeRateValue"), 0.5))
            b.wet.load = 1.0 - _clamp(_float(pr.get("ColorRateValue"), 0.0))

    # -- tip ----------------------------------------------------------------------------------
    for res in p.resources:
        resolver.add(res.type, res.filename or res.name, (lambda d=res.data: d), res.md5)
    el = _parse_brush_definition(pr.get("brush_definition", ""))
    tips, size, extra = _apply_brush_definition(b, el, resolver)
    b.tips = tips
    b.size_px = max(1.0, size)
    b.angle = extra.get("angle", 0.0) % 360.0
    if "hardness" in extra and not tips:
        b.hardness = extra["hardness"]
        b.roundness = extra["roundness"]
    elif "roundness" in extra:
        b.roundness = 1.0  # baked into the rect tip
    if "auto_spacing" in extra:
        coeff = extra["auto_spacing"]
        b.spacing = _clamp(coeff * math.sqrt(max(1.0, b.size_px)) / max(1.0, b.size_px), 0.01, 10.0)
    else:
        b.spacing = _clamp(extra.get("spacing", 0.1), 0.01, 10.0)
    if extra.get("sel"):
        b.tip_order = "random" if extra["sel"] in ("random", "velocity", "pressure", "xtilt", "ytilt") else "sequential"

    # -- dynamics -----------------------------------------------------------------------------
    size_opt = read_option(pr, "Size")
    b.size_dynamic = _dynamic_from_option(size_opt)
    op_opt = read_option(pr, "Opacity", checkable=False)
    b.opacity = _clamp(op_opt.value)
    b.opacity_dynamic = _dynamic_from_option(op_opt)
    flow_opt = read_option(pr, "Flow", checkable=False)
    b.flow = _clamp(flow_opt.value)
    b.flow_dynamic = _dynamic_from_option(flow_opt)

    rot = read_option(pr, "Rotation")
    if rot.active():
        if "drawingangle" in rot.sensors:
            b.angle_follow_stroke = True
        jit = [rot.span(s) for s in _JITTER_SENSORS if s in rot.sensors]
        if jit:
            b.angle_jitter = _clamp(max(jit) * _clamp(rot.value))
        if any(s in rot.sensors for s in _TILT_SENSORS | {"rotation"}):
            b.warn("Krita 的“笔倾斜/笔杆旋转控制角度”已忽略。")
    ratio = read_option(pr, "Ratio")
    if ratio.active():
        jit = [ratio.span(s) for s in _JITTER_SENSORS if s in ratio.sensors]
        if jit:
            b.roundness_jitter = _clamp(max(jit) * _clamp(ratio.value))
    sc = read_option(pr, "Scatter")
    if sc.checked:
        amount = max(0.0, sc.value)
        if sc.use_curve and sc.sensors and not any(s in sc.sensors for s in _JITTER_SENSORS):
            spans = [sc.span(s) for s in sc.sensors]
            amount *= max(spans) if spans else 1.0
        b.scatter = amount
        b.scatter_both_axes = _bool(pr.get("Scattering/AxisX"), True) and _bool(pr.get("Scattering/AxisY"), True)

    # -- paint --------------------------------------------------------------------------------
    comp = pr.get("CompositeOp", "normal")
    b.is_eraser = comp == "erase" or _bool(pr.get("EraserMode"), False)
    if comp not in ("erase", "copy"):
        b.blend_mode = _COMPOSITE_TO.get(comp, "normal")
        if comp not in _COMPOSITE_TO:
            b.warn(f"Krita 混合模式“{comp}”没有对应项，已改为“正常”。")

    cs = pr.get("ColorSource/Type", "plain")
    if cs in ("uniform_random", "total_random"):
        b.color = ColorDynamics(hue_jitter=1.0, saturation_jitter=0.0, brightness_jitter=0.0, per_tip=True)
    elif cs not in ("plain", ""):
        b.warn(f"Krita 颜色来源“{cs}”无法转换，已改为前景色。")
    for key, attr in (("h", "hue_jitter"), ("s", "saturation_jitter"), ("v", "brightness_jitter")):
        o = read_option(pr, key)
        if o.active():
            jit = [o.span(s) for s in _JITTER_SENSORS if s in o.sensors]
            if jit:
                setattr(b.color, attr, max(getattr(b.color, attr), _clamp(max(jit) * abs(o.value))))

    # -- texture ------------------------------------------------------------------------------
    if _bool(pr.get("Texture/Pattern/Enabled"), False):
        tex = _read_texture(p, resolver, b)
        if tex is not None:
            b.texture = tex

    # -- masking brush (dual brush) -----------------------------------------------------------
    if _bool(pr.get("MaskingBrush/Enabled"), False):
        mel = _parse_brush_definition(pr.get("MaskingBrush/Preset/brush_definition", ""))
        tmp = Brush(name="mask")
        mtips, msize, mextra = _apply_brush_definition(tmp, mel, resolver)
        if not mtips:
            mtips = [tmp.main_tip()] if mel is None else [round_tip(max(8, min(512, int(msize))),
                                                                     mextra.get("hardness", 1.0),
                                                                     mextra.get("roundness", 1.0))]
        coeff = _float(pr.get("MaskingBrush/MasterSizeCoeff"), 1.0)
        if _bool(pr.get("MaskingBrush/UseMasterSize"), True):
            msize = b.size_px * coeff
        b.dual = DualTip(image=mtips[0], size_px=max(1.0, msize), spacing=_clamp(mextra.get("spacing", 0.1), 0.01, 10),
                         mode=_MASK_OPS_TO.get(pr.get("MaskingBrush/MaskingCompositeOp", "multiply"), "multiply"),
                         name=mextra.get("file", ""))

    if _bool(pr.get("PaintOpSettings/isAirbrushing"), False):
        b.warn("Krita 的“喷枪”选项已忽略。")
    if _bool(pr.get("HorizontalMirrorEnabled"), False) or _bool(pr.get("VerticalMirrorEnabled"), False):
        b.warn("Krita 的“镜像”选项已忽略。")

    b.preview = p.preview
    native_params = {k: v for k, v in pr.items() if k != "Texture/Pattern/Pattern"}
    b.source = BrushSource(app="krita", format="krita", version=bundle_version or f"kpp {p.version}", path=path,
                           index=index, native={"paintop": p.paintop, "kpp_version": p.version,
                                                "params": native_params, "types": dict(p.types),
                                                "filename": p.filename})
    return b


def _read_texture(p: KritaPreset, resolver: ResourceResolver, b: Brush) -> Optional[Texture]:
    pr = p.params
    md5 = pr.get("Texture/Pattern/PatternMD5Sum", "")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", md5 or ""):
        md5 = ""
        raw_b64 = pr.get("Texture/Pattern/PatternMD5", "")
        try:
            raw = base64.b64decode(raw_b64.encode("latin-1"), validate=True) if raw_b64 else b""
            if len(raw) == 16:
                md5 = raw.hex()
        except ValueError:
            pass
    fname = Path((pr.get("Texture/Pattern/PatternFileName") or "").replace("\\", "/")).name
    name = pr.get("Texture/Pattern/Name", "")
    found = resolver.find("patterns", fname, md5) or (resolver.find("patterns", Path(name.replace("\\", "/")).name)
                                                     if name else None)
    img = None
    if found:
        try:
            img = _pattern_image(found[0], found[1])
        except Exception:  # noqa: BLE001
            img = None
    if img is None and pr.get("Texture/Pattern/Pattern"):
        try:
            data = base64.b64decode(pr["Texture/Pattern/Pattern"].encode("latin-1"))
            img = _pattern_image(fname or "pattern.png", data)
        except Exception:  # noqa: BLE001
            img = None
    if img is None:
        b.warn(f"找不到 Krita 纹理“{fname or name}”，纹理已忽略。")
        return None
    mode_idx = int(_float(pr.get("Texture/Pattern/TexturingMode"), 0))
    mode = _TEX_MODES[mode_idx] if 0 <= mode_idx < len(_TEX_MODES) else "multiply"
    strength = read_option(pr, "Texture/Strength/")
    depth = _clamp(strength.value) if strength.checked or "Texture/Strength/Value" in pr else 1.0
    tex = Texture(image=img, name=Path((name or fname).replace("\\", "/")).stem or "pattern",
                  scale=max(0.01, _float(pr.get("Texture/Pattern/Scale"), 1.0)),
                  depth=depth, invert=_bool(pr.get("Texture/Pattern/Invert"), False), mode=mode,
                  brightness=_clamp(_float(pr.get("Texture/Pattern/Brightness"), 0.0), -1, 1),
                  contrast=_clamp(_float(pr.get("Texture/Pattern/Contrast"), 1.0) - 1.0, -1, 1),
                  per_tip=True, depth_dynamic=_dynamic_from_option(strength))
    return tex


# =============================================================================================
# Bundles
# =============================================================================================
@dataclass
class BundleInfo:
    title: str = ""
    generator: str = ""
    author: str = ""
    description: str = ""
    meta: dict[str, str] = field(default_factory=dict)
    entries: list[tuple[str, str, str]] = field(default_factory=list)  # (media type, full path, md5)
    tags: dict[str, list[str]] = field(default_factory=dict)

    def krita_major(self) -> int:
        m = re.search(r"Krita\s*\((\d+)", self.generator)
        return int(m.group(1)) if m else 0


def read_bundle_info(z: zipfile.ZipFile) -> BundleInfo:
    info = BundleInfo()
    names = set(z.namelist())
    if "meta.xml" in names:
        text = z.read("meta.xml").decode("utf-8", "replace")
        for tag in ("generator", "bundle-version", "creation-date", "dc-date", "initial-creator"):
            m = re.search(rf"<meta:{tag}>(.*?)</meta:{tag}>", text, re.S)
            if m:
                info.meta[tag] = _xml_unescape(m.group(1).strip())
        for tag in ("title", "author", "description", "creator"):
            m = re.search(rf"<(?:dc|cd):{tag}>(.*?)</(?:dc|cd):{tag}>", text, re.S)
            if m:
                info.meta[tag] = _xml_unescape(m.group(1).strip())
        for m in re.finditer(r'<meta:meta-userdefined\s+meta:name="([^"]*)"\s+meta:value="([^"]*)"', text):
            info.meta.setdefault(m.group(1), _xml_unescape(m.group(2)))
    info.generator = info.meta.get("generator", "")
    info.title = info.meta.get("title", "")
    info.author = info.meta.get("author", info.meta.get("creator", ""))
    info.description = info.meta.get("description", "")
    if "META-INF/manifest.xml" in names:
        text = z.read("META-INF/manifest.xml").decode("utf-8", "replace")
        for m in re.finditer(r"<manifest:file-entry\b([^>]*?)(/>|>(.*?)</manifest:file-entry>)", text, re.S):
            a = dict(re.findall(r'manifest:([\w-]+)="([^"]*)"', m.group(1)))
            path = _xml_unescape(a.get("full-path", ""))
            if path == "/":
                continue
            info.entries.append((a.get("media-type", ""), path, a.get("md5sum", "")))
            tags = re.findall(r"<manifest:tag>(.*?)</manifest:tag>", m.group(3) or "")
            if tags:
                info.tags[path] = [_xml_unescape(t) for t in tags]
    if not info.entries:  # manifest missing: fall back to folder names
        for n in sorted(names):
            if "/" in n and not n.endswith("/") and not n.startswith("META-INF/"):
                info.entries.append((n.split("/", 1)[0], n, ""))
    return info


def _resolver_for_zip(z: zipfile.ZipFile, info: BundleInfo) -> ResourceResolver:
    r = ResourceResolver()
    names = set(z.namelist())
    for mtype, path, md5 in info.entries:
        if path in names:
            r.add(mtype, path, (lambda p=path: z.read(p)), md5)
    for n in names:
        if "/" in n and not n.endswith("/"):
            r.add(n.split("/", 1)[0], n, (lambda p=n: z.read(p)))
    return r


def _preset_entries(z: zipfile.ZipFile, info: BundleInfo) -> list[str]:
    names = set(z.namelist())
    out = [p for t, p, _ in info.entries if (t == "paintoppresets" or p.startswith("paintoppresets/")) and p in names]
    return [p for p in out if p.lower().endswith(".kpp")] + [p for p in out if not p.lower().endswith(".kpp")]


def _brush_entries(z: zipfile.ZipFile, info: BundleInfo) -> list[str]:
    names = set(z.namelist())
    return [p for t, p, _ in info.entries if (t == "brushes" or p.startswith("brushes/")) and p in names]


def _bundle_version_label(info: BundleInfo, kpp_versions: set[str], types: set[str], paintops: set[str]) -> tuple[str, int]:
    major = info.krita_major()
    needs5 = KPP_VERSION_K5 in kpp_versions or bool(types & _K5_ONLY_TYPES) or bool(paintops & _K5_ONLY_PAINTOPS)
    min_major = 5 if needs5 else 4 if major >= 4 or not major else major
    label = f"Krita {major} 资源包" if major else "Krita 资源包"
    return label, min_major


def _tips_to_brushes(z: zipfile.ZipFile, paths: list[str], group: str, src: str) -> list[Brush]:
    out = []
    for i, p in enumerate(paths):
        try:
            tips, extra = _decode_tip_file(p, z.read(p))
        except Exception:  # noqa: BLE001
            continue
        b = Brush(name=Path(p).stem.replace("_", " "), tips=tips, size_px=float(max(tips[0].size)), group=group)
        if extra.get("spacing"):
            b.spacing = max(0.01, extra["spacing"] / 100.0)
        if extra.get("sel"):
            b.tip_order = "random" if extra["sel"] == "random" else "sequential"
        b.source = BrushSource(app="krita", format="krita", version="bundle", path=src, index=i,
                               native={"tip_file": p})
        out.append(b)
    return out


# =============================================================================================
# Brush -> preset (writer)
# =============================================================================================
def _base_params() -> dict[str, str]:
    p = {
        "ColorSource/Type": "plain", "CompositeOp": "normal", "EraserMode": "false",
        "HorizontalMirrorEnabled": "false", "VerticalMirrorEnabled": "false",
        "KisPrecisionOption/AutoPrecisionEnabled": "false", "KisPrecisionOption/DeltaValue": "15",
        "KisPrecisionOption/SizeToStartFrom": "10", "KisPrecisionOption/precisionLevel": "5",
        "MaskingBrush/Enabled": "false", "MaskingBrush/MaskingCompositeOp": "multiply",
        "MaskingBrush/MasterSizeCoeff": "1", "MaskingBrush/UseMasterSize": "true",
        "PaintOpAction": "2", "PaintOpSettings/ignoreSpacing": "false", "PaintOpSettings/isAirbrushing": "false",
        "PaintOpSettings/rate": "20", "PaintOpSettings/updateSpacingBetweenDabs": "false",
        "Scattering/AxisX": "true", "Scattering/AxisY": "true",
        "Sharpness/threshold": "4", "Sharpness/alignoutline": "false", "Sharpness/softness": "0",
        "Spacing/Isotropic": "false", "Texture/Pattern/Enabled": "false",
        "lodSizeThreshold": "100", "lodUserAllowed": "false",
        "paintop": "paintbrush", "requiredBrushFile": "", "requiredBrushFilesList": "",
    }
    for oid in _CURVE_OPTIONS:
        _write_option(p, oid, checked=oid in ("Opacity", "Flow", "Texture/Strength/"),
                      use_curve=oid not in ("Opacity", "Flow"), value=1.0)
    return p


@dataclass
class _Res:
    """A resource file that goes into the bundle (or is embedded in a 5.0 preset)."""
    type: str  # brushes | patterns
    filename: str
    data: bytes
    name: str

    @property
    def md5(self) -> str:
        return hashlib.md5(self.data).hexdigest()


def _ascii_slug(name: str, fallback: str = "brush") -> str:
    s = re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_")
    return s[:40] or fallback


def _png_bytes(img: Image.Image, text: Optional[dict[str, str]] = None) -> bytes:
    buf = io.BytesIO()
    info = None
    if text:
        info = PngImagePlugin.PngInfo()
        for k, v in text.items():
            info.add_text(k, v)
    img.save(buf, "PNG", pnginfo=info, optimize=False)
    return buf.getvalue()


def _limit(img: Image.Image, side: int = MAX_TIP_SIDE) -> Image.Image:
    return fit(img, side) if max(img.size) > side else img


def _tip_resource(b: Brush, tips: list[Image.Image], stem: str, prefix: str) -> tuple[_Res, str, float]:
    """Encode tips as Krita brush file; returns (resource, brush type, native side)."""
    tips = [_limit(t.convert("L")) for t in tips]
    if len(tips) == 1:
        img = ImageOps.invert(tips[0])  # Krita gray PNG: black = paint, white = background
        data = _png_bytes(img, {"brush_spacing": _num(b.spacing), "brush_name": b.name})
        return _Res("brushes", f"{prefix}{stem}.png", data, b.name), "png_brush", float(max(tips[0].size))
    sel = "random" if b.tip_order == "random" else "incremental"
    data = _gimp.encode_gih(tips, b.name, int(round(_clamp(b.spacing, 0.01, 10) * 100)), sel)
    return _Res("brushes", f"{prefix}{stem}.gih", data, b.name), "gbr_brush", float(max(max(t.size) for t in tips))


def _predefined_definition(res: _Res, btype: str, side: float, size_px: float, spacing: float, angle: float) -> str:
    scale = max(0.001, size_px / max(1.0, side))
    return (f'<Brush type="{btype}" filename={_quoteattr(res.filename)} md5sum="{res.md5}" BrushVersion="2" '
            f'scale="{scale:.10g}" angle="{_num(math.radians(angle % 360.0))}" spacing="{_num(spacing)}" '
            f'useAutoSpacing="0" autoSpacingCoeff="1" brushApplication="0" ColorAsMask="1" '
            f'AdjustmentMidPoint="127" BrightnessAdjustment="0" ContrastAdjustment="0" AutoAdjustMidPoint="0" '
            f'AdjustmentVersion="2"/>')


def _auto_definition(size_px: float, hardness: float, roundness: float, angle: float, spacing: float) -> str:
    fade = _clamp(hardness, 0.02, 1.0)
    return (f'<Brush type="auto_brush" BrushVersion="2" angle="{_num(math.radians(angle % 360.0))}" '
            f'spacing="{_num(spacing)}" useAutoSpacing="0" autoSpacingCoeff="1" density="1" randomness="0">'
            f'<MaskGenerator id="default" type="circle" diameter="{_num(max(1.0, size_px))}" '
            f'ratio="{_num(_clamp(roundness, 0.01, 1.0))}" hfade="{_num(fade)}" vfade="{_num(fade)}" spikes="2" '
            f'antialiasEdges="1"/></Brush>')


_UNMANAGED_SKIP_PREFIXES = ("MaskingBrush/", "Texture/", "brush_definition", "requiredBrushFile", "Scattering/",
                            "CompositeOp", "EraserMode", "ColorSource/", "paintop")


def brush_to_preset(b: Brush, prefix: str, used: set[str]) -> tuple[str, dict[str, str], dict[str, str], list[_Res]]:
    """Map a neutral brush to (preset file stem, params, param types, resources)."""
    stem = _ascii_slug(b.name)
    base_stem, i = stem, 2
    while stem.lower() in used:
        stem = f"{base_stem}_{i}"
        i += 1
    used.add(stem.lower())
    params = _base_params()
    types: dict[str, str] = {}
    native = b.source.native if b.source.format == "krita" else {}
    if native.get("paintop") == "paintbrush" and isinstance(native.get("params"), dict):
        for k, v in native["params"].items():
            if k.startswith(_UNMANAGED_SKIP_PREFIXES):
                continue
            opt = next((o for o in _CURVE_OPTIONS if k == "Pressure" + o or (k.startswith(o) and k[len(o):] in
                        ("Sensor", "UseCurve", "UseSameCurve", "Value", "curveMode", "commonCurve"))), None)
            if opt in _MANAGED_OPTIONS:
                continue
            if native.get("types", {}).get(k) == "bytearray":
                continue
            params[k] = v
    resources: list[_Res] = []
    spacing = _clamp(b.spacing, 0.01, 10.0)

    # -- tip ------------------------------------------------------------------------------------
    if b.tips:
        res, btype, side = _tip_resource(b, b.tips, stem, prefix)
        resources.append(res)
        params["brush_definition"] = _predefined_definition(res, btype, side, b.size_px, spacing, b.angle)
        params["requiredBrushFile"] = res.filename
        params["requiredBrushFilesList"] = res.filename
        if b.roundness < 0.99:
            b.warn("Krita 位图笔尖不支持“圆度”，已忽略（可在 Krita 中用“比例”选项调整）。")
    else:
        params["brush_definition"] = _auto_definition(b.size_px, b.hardness, b.roundness, b.angle, spacing)
    if b.flip_x or b.flip_y:
        b.warn("Krita 预设不保存笔尖翻转，已忽略。")

    # -- dynamics -------------------------------------------------------------------------------
    sd = b.size_dynamic
    _write_option(params, "Size", checked=sd.is_active(), use_curve=sd.is_active(), value=1.0,
                  sensors=_sensors_for_dynamic(sd) or None)
    od = b.opacity_dynamic
    _write_option(params, "Opacity", checked=True, use_curve=od.is_active(), value=_clamp(b.opacity),
                  sensors=_sensors_for_dynamic(od) or None)
    fd = b.flow_dynamic
    _write_option(params, "Flow", checked=True, use_curve=fd.is_active(), value=_clamp(b.flow),
                  sensors=_sensors_for_dynamic(fd) or None)

    rot_sensors: list[tuple[str, str, dict]] = []
    rot_value = 1.0
    if b.angle_follow_stroke:
        rot_sensors.append(("drawingangle", DEFAULT_CURVE, {"fanCornersEnabled": "0", "fanCornersStep": "30",
                                                            "angleOffset": "0", "lockedAngleMode": "0"}))
    if b.angle_jitter > 0:
        j = _clamp(b.angle_jitter)
        if rot_sensors:
            rot_sensors.append(("fuzzy", format_curve([(0, 0.5 - j / 2), (1, 0.5 + j / 2)]), {}))
        else:
            rot_sensors.append(("fuzzy", DEFAULT_CURVE, {}))
            rot_value = j
    _write_option(params, "Rotation", checked=bool(rot_sensors), use_curve=bool(rot_sensors), value=rot_value,
                  sensors=rot_sensors or None)
    if b.roundness_jitter > 0:
        _write_option(params, "Ratio", checked=True, use_curve=True, value=1.0,
                      sensors=[("fuzzy", format_curve([(0, 1 - _clamp(b.roundness_jitter)), (1, 1)]), {})])
    if b.scatter > 0:
        amount = min(5.0, b.scatter)
        if b.scatter > 5.0:
            b.warn("Krita 的散布最大为 500%，已截断。")
        _write_option(params, "Scatter", checked=True, use_curve=False, value=amount)
        params["Scattering/AxisX"] = "true"
        params["Scattering/AxisY"] = "true"
    if b.count > 1:
        b.warn("Krita 普通笔刷没有“数量”选项，已忽略。")

    # -- paint ----------------------------------------------------------------------------------
    if b.is_eraser:
        params["CompositeOp"] = "erase"
    else:
        params["CompositeOp"] = _COMPOSITE_FROM.get(b.blend_mode, "normal")
        if b.blend_mode not in _COMPOSITE_FROM:
            b.warn(f"混合模式“{b.blend_mode}”在 Krita 中没有对应项，已改为“正常”。")
    c = b.color
    for key, val in (("h", c.hue_jitter), ("s", c.saturation_jitter), ("v", c.brightness_jitter)):
        if val > 0:
            _write_option(params, key, checked=True, use_curve=True, value=_clamp(val), sensors=[("fuzzy", DEFAULT_CURVE, {})])
    if c.fg_bg_jitter > 0:
        b.warn("Krita 普通笔刷没有“前景/背景色抖动”，已忽略。")
    if b.wet.enabled:
        b.warn("Krita 像素笔刷不支持混色，可在 Krita 中改用“涂抹笔刷”引擎。")
    if b.stabilization > 0 or b.taper_start > 0 or b.taper_end > 0:
        b.warn("防抖/起笔收笔不属于 Krita 笔刷预设，请在 Krita 工具选项中设置。")

    # -- texture --------------------------------------------------------------------------------
    if b.texture is not None:
        t = b.texture
        tex_img = _limit(t.image.convert("L"), 2048)
        pname = t.name or f"{b.name} texture"
        pres = _Res("patterns", f"{prefix}{stem}_texture.png", _png_bytes(tex_img), pname)
        resources.append(pres)
        mode = _TEX_MODE_ALIASES.get(t.mode, t.mode)
        mode_idx = _TEX_MODES.index(mode) if mode in _TEX_MODES else 0
        if mode not in _TEX_MODES:
            b.warn(f"纹理模式“{t.mode}”在 Krita 中没有对应项，已改为“相乘”。")
        md5_raw = hashlib.md5(pres.data).digest()
        params.update({
            "Texture/Pattern/Enabled": "true", "Texture/Pattern/Scale": _num(max(0.01, t.scale)),
            "Texture/Pattern/Brightness": _num(_clamp(t.brightness, -1, 1)),
            "Texture/Pattern/Contrast": _num(1.0 + _clamp(t.contrast, -1, 1)),
            "Texture/Pattern/NeutralPoint": "0.5", "Texture/Pattern/OffsetX": "0", "Texture/Pattern/OffsetY": "0",
            "Texture/Pattern/isRandomOffsetX": "false", "Texture/Pattern/isRandomOffsetY": "false",
            "Texture/Pattern/MaximumOffsetX": str(tex_img.width), "Texture/Pattern/MaximumOffsetY": str(tex_img.height),
            "Texture/Pattern/CutoffLeft": "0", "Texture/Pattern/CutoffRight": "255", "Texture/Pattern/CutoffPolicy": "0",
            "Texture/Pattern/Invert": _tf(t.invert), "Texture/Pattern/AutoInvertOnErase": "false",
            "Texture/Pattern/UseSoftTexturing": "false", "Texture/Pattern/TexturingMode": str(mode_idx),
            "Texture/Pattern/Name": pres.name, "Texture/Pattern/PatternFileName": pres.filename,
            "Texture/Pattern/PatternMD5Sum": pres.md5,
            # Krita 4 keys: base64 strings stored as bytearray params (i.e. base64 twice in the XML)
            "Texture/Pattern/PatternMD5": base64.b64encode(md5_raw).decode("ascii"),
            "Texture/Pattern/Pattern": base64.b64encode(pres.data).decode("ascii"),
        })
        types["Texture/Pattern/PatternMD5"] = "bytearray"
        types["Texture/Pattern/Pattern"] = "bytearray"
        _write_option(params, "Texture/Strength/", checked=True, use_curve=t.depth_dynamic.is_active(),
                      value=_clamp(t.depth), sensors=_sensors_for_dynamic(t.depth_dynamic) or None)

    # -- dual brush -> masking brush -----------------------------------------------------------
    if b.dual is not None:
        d = b.dual
        mres, mtype, mside = _tip_resource(Brush(name=f"{b.name} mask", spacing=d.spacing), [d.image],
                                           stem + "_mask", prefix)
        resources.append(mres)
        params.update({
            "MaskingBrush/Enabled": "true",
            "MaskingBrush/MaskingCompositeOp": _MASK_OPS_FROM.get(d.mode, "multiply"),
            "MaskingBrush/UseMasterSize": "true",
            "MaskingBrush/MasterSizeCoeff": f"{max(0.01, d.size_px / max(1.0, b.size_px)):.10g}",
            "MaskingBrush/Preset/brush_definition": _predefined_definition(mres, mtype, mside, d.size_px,
                                                                           _clamp(d.spacing, 0.01, 10), 0.0),
            "MaskingBrush/Preset/requiredBrushFile": mres.filename,
            "MaskingBrush/Preset/requiredBrushFilesList": mres.filename,
            "MaskingBrush/Preset/paintop": "paintbrush",
        })
        if d.scatter > 0:
            params["MaskingBrush/Preset/PressureScatter"] = "true"
            params["MaskingBrush/Preset/ScatterValue"] = _num(min(5.0, d.scatter))
            params["MaskingBrush/Preset/ScatterUseCurve"] = "false"
        if d.mode not in _MASK_OPS_FROM:
            b.warn(f"双重笔刷模式“{d.mode}”在 Krita 中没有对应项，已改为“相乘”。")
    return stem, params, types, resources


def _preset_preview(b: Brush, size: int = 200) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    try:
        stroke = stroke_preview(b, size, size)
        canvas.alpha_composite(stroke)
    except Exception:  # noqa: BLE001 - preview is cosmetic
        tip = fit(b.main_tip().convert("L"), int(size * 0.8))
        dark = Image.new("RGBA", tip.size, (20, 20, 20, 255))
        dark.putalpha(tip)
        canvas.alpha_composite(dark, ((size - tip.width) // 2, (size - tip.height) // 2))
    return canvas


def _bundle_preview(brushes: list[Brush], size: int = 256) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    shown = brushes[:4]
    if not shown:
        return canvas
    n = 1 if len(shown) == 1 else 2
    cell = size // n
    for i, b in enumerate(shown):
        tip = fit(b.main_tip().convert("L"), int(cell * 0.8))
        dark = Image.new("RGBA", tip.size, (20, 20, 20, 255))
        dark.putalpha(tip)
        x = (i % n) * cell + (cell - tip.width) // 2
        y = (i // n) * cell + (cell - tip.height) // 2
        canvas.alpha_composite(dark, (x, y))
    return canvas


def _manifest_xml(files: list[tuple[str, str, str]], tags: list[str]) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<manifest:manifest xmlns:manifest="{MANIFEST_NS}" manifest:version="1.2">',
           ' <manifest:file-entry manifest:media-type="application/x-krita-resourcebundle" manifest:full-path="/"/>']
    for mtype, path, md5 in files:
        entry = (f" <manifest:file-entry manifest:media-type={_quoteattr(mtype)} manifest:full-path={_quoteattr(path)} "
                 f"manifest:md5sum={_quoteattr(md5)}")
        if tags and mtype == "paintoppresets":
            tag_xml = "".join(f"<manifest:tag>{_xml_escape(t)}</manifest:tag>" for t in tags)
            out.append(entry + f">\n  <manifest:tags>{tag_xml}</manifest:tags>\n </manifest:file-entry>")
        else:
            out.append(entry + "/>")
    out.append("</manifest:manifest>")
    return "\n".join(out) + "\n"


def _meta_xml(title: str, author: str, description: str, tags: list[str], website: str = "",
              license_: str = "") -> str:
    date = time.strftime("%d/%m/%Y")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    e = _xml_escape
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<meta:meta xmlns:meta="{META_NS}" xmlns:dc="{DC_NS}">',
             " <meta:generator>Krita (5.2.0) via brush-bridge</meta:generator>",
             " <meta:bundle-version>1</meta:bundle-version>",
             f" <dc:author>{e(author)}</dc:author>",
             f" <dc:title>{e(title)}</dc:title>",
             f" <dc:description>{e(description)}</dc:description>",
             f" <meta:initial-creator>{e(author)}</meta:initial-creator>",
             f" <dc:creator>{e(author)}</dc:creator>",
             f" <meta:creation-date>{date}</meta:creation-date>",
             f" <meta:dc-date>{stamp}</meta:dc-date>",
             ' <meta:meta-userdefined meta:name="email" meta:value=""/>',
             f' <meta:meta-userdefined meta:name="license" meta:value={_quoteattr(license_)}/>',
             f' <meta:meta-userdefined meta:name="website" meta:value={_quoteattr(website)}/>']
    for t in tags:
        lines.append(f' <meta:meta-userdefined meta:name="tag" meta:value={_quoteattr(t)}/>')
    lines.append("</meta:meta>")
    return "\n".join(lines) + "\n"


def write_bundle(brushes: list[Brush], out_path: Path, *, title: str = "", author: str = "brush-bridge",
                 description: str = "", tag: Optional[str] = None) -> Path:
    out_path = Path(out_path)
    title = title or out_path.stem
    tag = title if tag is None else tag
    tags = [tag] if tag else []
    prefix = _ascii_slug(title, "bb") + "_"
    used: set[str] = set()
    files: dict[str, bytes] = {}
    manifest: list[tuple[str, str, str]] = []
    for b in brushes:
        stem, params, types, resources = brush_to_preset(b, prefix, used)
        for r in resources:
            path = f"{r.type}/{r.filename}"
            if path not in files:
                files[path] = r.data
                manifest.append((r.type, path, r.md5))
        xml = build_preset_xml(b.name, params, types)
        kpp = encode_kpp(xml, _preset_preview(b), KPP_VERSION_K4)
        path = f"paintoppresets/{prefix}{stem}.kpp"
        files[path] = kpp
        manifest.append(("paintoppresets", path, hashlib.md5(kpp).hexdigest()))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype", date_time=time.localtime()[:6]), BUNDLE_MIMETYPE,
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/manifest.xml", _manifest_xml(manifest, tags))
        z.writestr("meta.xml", _meta_xml(title, author, description or f"由 brush-bridge 转换的 {len(brushes)} 个笔刷",
                                          tags))
        z.writestr("preview.png", _png_bytes(_bundle_preview(brushes)))
        for path, data in files.items():
            z.writestr(path, data)
    tmp.replace(out_path)
    return out_path


def write_kpp(b: Brush, out_path: Path, version: Optional[str] = None) -> Path:
    """Standalone preset. Resources are embedded (Krita 5 format) unless the brush needs none."""
    stem, params, types, resources = brush_to_preset(b, "", set())
    if version is None:
        version = KPP_VERSION_K5 if any(r.type == "brushes" for r in resources) else KPP_VERSION_K4
    embedded = [EmbeddedResource(r.type, r.name if r.type == "patterns" else Path(r.filename).stem, r.filename,
                                 r.md5, r.data) for r in resources] if version == KPP_VERSION_K5 else []
    xml = build_preset_xml(b.name, params, types, embedded)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(encode_kpp(xml, _preset_preview(b), version))
    return out_path


# =============================================================================================
# Codec
# =============================================================================================
def _fix_to_k4_bundle(src: Path, out_dir: Path) -> list[Path]:
    codec = KritaCodec()
    brushes = codec.read(src)
    out = unique_path(Path(out_dir) / (Path(src).stem + ".bundle"))
    return codec.write(brushes, out, title=Path(src).stem)


@register_codec
class KritaCodec(FormatCodec):
    id = "krita"
    name = "Krita 资源包 (.bundle) / 预设 (.kpp)"
    extensions = (".bundle", ".kpp")
    can_read = True
    can_write = True

    # -- sniff ----------------------------------------------------------------------------------
    def sniff(self, path: Path) -> Optional[FileInfo]:
        path = Path(path)
        try:
            with open(path, "rb") as f:
                head = f.read(64)
        except OSError:
            return None
        if head.startswith(b"\x89PNG"):
            try:
                p = parse_kpp(path.read_bytes(), path.name)
            except KritaFormatError:
                return None
            info = FileInfo(path, self.id, self.name, f"kpp {p.version}", 1,
                            details={"类型": "Krita 笔刷预设", "预设格式": p.version,
                                     "引擎": _PAINTOP_NAMES.get(p.paintop, p.paintop),
                                     "最低版本": "Krita 5" if p.version == KPP_VERSION_K5 or p.paintop in _K5_ONLY_PAINTOPS else "Krita 4"})
            if p.version == KPP_VERSION_K5:
                info.problems.append("这是 Krita 5 格式的预设（内嵌资源），Krita 4 无法导入。")
                info.fixes.append(Fix("转换为 Krita 4 资源包", "把预设和它内嵌的笔尖/纹理重新打包成 Krita 4 和 5 都能导入的 .bundle。",
                                      _fix_to_k4_bundle))
            if p.paintop != "paintbrush":
                info.details["说明"] = "非像素笔刷引擎，转换到其他软件时只能近似"
            return info
        if not head.startswith(b"PK"):
            return None
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                mt = z.read("mimetype").decode("ascii", "replace").strip() if "mimetype" in names else ""
                if mt != BUNDLE_MIMETYPE and "META-INF/manifest.xml" not in names:
                    return None
                if mt != BUNDLE_MIMETYPE and not any(n.startswith(("paintoppresets/", "brushes/")) for n in names):
                    return None
                binfo = read_bundle_info(z)
                presets = _preset_entries(z, binfo)
                types = {t for t, _, _ in binfo.entries}
                kpp_versions: set[str] = set()
                paintops: set[str] = set()
                for pth in presets[:400]:
                    try:
                        data = z.read(pth)
                        img = Image.open(io.BytesIO(data))
                        img.load()
                        kpp_versions.add((img.text.get("version") or KPP_VERSION_K4).strip())
                        m = re.search(r'paintopid="([^"]+)"', img.text.get("preset", "")[:400])
                        if m:
                            paintops.add(m.group(1))
                    except Exception:  # noqa: BLE001
                        if pth.lower().endswith(".myb"):
                            paintops.add("mypaintbrush")
                label, min_major = _bundle_version_label(binfo, kpp_versions, types, paintops)
                count = len(presets) or len(_brush_entries(z, binfo))
                info = FileInfo(path, self.id, self.name, label, count,
                                details={"类型": "Krita 资源包", "标题": binfo.title or path.stem,
                                         "作者": binfo.author, "生成程序": binfo.generator,
                                         "预设数": str(len(presets)), "笔尖数": str(len(_brush_entries(z, binfo))),
                                         "资源类型": ", ".join(sorted(types)),
                                         "最低版本": f"Krita {min_major}"})
                if min_major >= 5:
                    info.problems.append("这个资源包含 Krita 5 专有内容，Krita 4 无法完整导入。")
                    info.fixes.append(Fix("转换为 Krita 4 资源包", "重新打包为 Krita 4 和 5 都能读取的格式（Krita 5 专有引擎会被近似）。",
                                          _fix_to_k4_bundle))
                missing = [p for _, p, _ in binfo.entries if p not in names]
                if missing:
                    info.problems.append(f"资源包已损坏：缺少 {len(missing)} 个文件（例如 {missing[0]}）。")
                if mt != BUNDLE_MIMETYPE:
                    info.problems.append("资源包缺少 mimetype 标记，Krita 可能拒绝导入。")
                    info.fixes.append(Fix("修复资源包", "重新打包，补上 mimetype 和清单。", _fix_to_k4_bundle))
                return info
        except (zipfile.BadZipFile, KeyError, OSError):
            return None

    # -- read -----------------------------------------------------------------------------------
    def _open_kpp(self, path: Path) -> tuple[KritaPreset, ResourceResolver]:
        p = parse_kpp(path.read_bytes(), path.name)
        r = ResourceResolver()
        for base in (path.parent, path.parent.parent):
            r.add_dir("brushes", base / "brushes")
            r.add_dir("patterns", base / "patterns")
        r.add_dir("brushes", path.parent)
        r.add_dir("patterns", path.parent)
        return p, r

    def read(self, path: Path) -> list[Brush]:
        path = Path(path)
        with open(path, "rb") as f:
            head = f.read(8)
        if head.startswith(b"\x89PNG"):
            p, r = self._open_kpp(path)
            return [preset_to_brush(p, r, str(path), 0, group="", bundle_version=f"kpp {p.version}")]
        out: list[Brush] = []
        with zipfile.ZipFile(path) as z:
            info = read_bundle_info(z)
            group = info.title or path.stem
            resolver = _resolver_for_zip(z, info)
            presets = _preset_entries(z, info)
            label = f"bundle ({info.generator})" if info.generator else "bundle"
            for i, pth in enumerate(presets):
                if not pth.lower().endswith(".kpp"):
                    continue
                try:
                    p = parse_kpp(z.read(pth), Path(pth).name)
                except KritaFormatError:
                    continue
                out.append(preset_to_brush(p, resolver, str(path), i, group=group, bundle_version=label))
            if not out:
                out = _tips_to_brushes(z, _brush_entries(z, info), group, str(path))
        return out

    def list_refs(self, path: Path) -> list[BrushRef]:
        path = Path(path)
        with open(path, "rb") as f:
            head = f.read(8)
        if head.startswith(b"\x89PNG"):
            return super().list_refs(path)
        refs: list[BrushRef] = []
        with zipfile.ZipFile(path) as z:
            info = read_bundle_info(z)
            presets = [p for p in _preset_entries(z, info) if p.lower().endswith(".kpp")]
            names = {}
            for p in presets:
                try:
                    names[p] = preset_display_name(z.read(p), Path(p).name)
                except (KeyError, zipfile.BadZipFile, OSError):
                    names[p] = Path(p).stem.replace("_", " ")
        group = info.title or path.stem
        if not presets:
            return super().list_refs(path)

        def loader(i: int, pth: str) -> Brush:
            with zipfile.ZipFile(path) as z2:
                inf = read_bundle_info(z2)
                p = parse_kpp(z2.read(pth), Path(pth).name)
                return preset_to_brush(p, _resolver_for_zip(z2, inf), str(path), i, group=group,
                                       bundle_version=f"bundle ({inf.generator})")

        def thumb(pth: str) -> Optional[Image.Image]:
            with zipfile.ZipFile(path) as z2:
                img = Image.open(io.BytesIO(z2.read(pth)))
                img.load()
                return img.convert("RGBA")

        for i, pth in enumerate(presets):
            name = names.get(pth) or Path(pth).stem.replace("_", " ")
            refs.append(BrushRef(name=name, key=pth, group=group, loader=(lambda i=i, p=pth: loader(i, p)),
                                 thumbnail=(lambda p=pth: thumb(p)), detail="Krita 预设"))
        return refs

    # -- write ----------------------------------------------------------------------------------
    def write(self, brushes: list[Brush], out_path: Path, **options) -> list[Path]:
        """Write a .bundle (default) or standalone .kpp files (``out_path`` ending in .kpp).

        options: ``title``, ``author``, ``description``, ``tag`` (bundle tag, defaults to title),
        ``kpp_version`` ("2.2" or "5.0") for standalone presets.
        """
        out_path = Path(out_path)
        if out_path.suffix.lower() == ".kpp":
            if len(brushes) == 1:
                return [write_kpp(brushes[0], out_path, options.get("kpp_version"))]
            out = []
            for b in brushes:
                target = unique_path(out_path.parent / (safe_filename(b.name) + ".kpp"))
                out.append(write_kpp(b, target, options.get("kpp_version")))
            return out
        if out_path.suffix.lower() != ".bundle":
            out_path = out_path.with_suffix(".bundle")
        return [write_bundle(brushes, out_path, title=options.get("title", ""),
                             author=options.get("author", "brush-bridge"),
                             description=options.get("description", ""), tag=options.get("tag"))]

    def compatibility(self, info: FileInfo, app_id: str, app_version: str) -> Optional[str]:
        if app_id != "krita":
            if app_id == "gimp":
                return "GIMP 不能直接导入 Krita 资源包/预设，需要先转换为 GIMP 笔刷（.gbr/.gih）。"
            return None
        need = info.details.get("最低版本", "Krita 4")
        m = re.match(r"(\d+)", app_version or "")
        major = int(m.group(1)) if m else 0
        if need == "Krita 5" and 0 < major < 5:
            return f"该文件需要 Krita 5 或更高版本，当前是 Krita {app_version}。可使用“转换为 Krita 4 资源包”修复。"
        if major and major < 4 and info.path.suffix.lower() == ".bundle":
            return "Krita 3 及更早版本对资源包支持不完整，建议升级到 Krita 5。"
        return None
