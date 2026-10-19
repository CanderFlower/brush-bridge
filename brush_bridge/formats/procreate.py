"""Procreate brushes: ``.brush`` (one brush) and ``.brushset`` (a named set of brushes).

File layout (both are plain zip archives):

* ``.brush``    -> ``Brush.archive`` (binary plist, NSKeyedArchiver of a ``SilicaBrush`` object),
  optional ``Shape.png`` / ``Grain.png`` (8-bit grey, white = paint), ``QuickLook/Thumbnail.png``
  (1060x324 RGBA, white stroke on transparent), ``Signature/``, ``AuthorPicture/`` and an optional
  ``Reset/`` folder holding the factory state of the same files.
* ``.brushset`` -> ``brushset.plist`` (``{"name": str, "brushes": [UUID, ...]}``) plus one folder per
  UUID containing the files of a ``.brush``.

When ``bundledShapePath`` / ``bundledGrainPath`` name a file (e.g. ``Brush-Preset-Hard.png``) the
brush uses an image that ships inside Procreate itself; those images are not in the file and
cannot be redistributed, so we approximate the shape with a computed round tip.

Format knowledge comes from studying public brushes and these open-source projects (no code
copied): aumlette-lab/procreate-brush-decoder (MIT, parameter ranges and GUI formulas),
Leon-Schoenbrunn/CSP2PC (MIT), barakbl/procreate_brush_claude_tools (MIT) and
javadhamidi/brush-converters (MIT). See docs/THIRD_PARTY.md.
"""
from __future__ import annotations

import io
import logging
import math
import plistlib
import re
import time
import uuid as _uuid
import zipfile
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image, ImageOps

from ..core.imaging import pad_square, round_tip, stroke_preview
from ..core.model import Brush, BrushRef, BrushSource, ColorDynamics, Dynamic, EdgeEffect, Texture, WetMix
from ..core.plugin import FileInfo, FormatCodec, register_codec

log = logging.getLogger(__name__)

ARCHIVE = "Brush.archive"
SHAPE = "Shape.png"
GRAIN = "Grain.png"
THUMB = "QuickLook/Thumbnail.png"
BRUSHSET_PLIST = "brushset.plist"

# Procreate stores sizes as multiples of an app-internal unit (``maxSize`` 1.0 == "100%" in Brush
# Studio). There is no absolute pixel size in the file; this factor is an approximation chosen so
# that typical brushes land in a sensible pixel range on desktop canvases.
SIZE_UNIT_PX = 500.0
MAX_SHAPE_SIDE = 2048
MAX_GRAIN_SIDE = 2048
THUMB_SIZE = (1060, 324)
_APPLE_EPOCH = 978307200.0  # 2001-01-01T00:00:00Z as a Unix timestamp

# ---------------------------------------------------------------------------------------------
# NSKeyedArchiver decoding / encoding
# ---------------------------------------------------------------------------------------------
CLASS_CHAINS: dict[str, list[str]] = {
    "SilicaBrush": ["SilicaBrush", "ValkyrieBrush", "NSObject"],
    "ValkyrieMagnitudinalCurve": ["ValkyrieMagnitudinalCurve", "NSObject"],
    "ValkyrieBrushRollParameters": ["ValkyrieBrushRollParameters", "NSObject"],
    "NSDate": ["NSDate", "NSObject"],
    "NSArray": ["NSArray", "NSObject"],
    "NSMutableArray": ["NSMutableArray", "NSArray", "NSObject"],
    "NSDictionary": ["NSDictionary", "NSObject"],
    "NSMutableDictionary": ["NSMutableDictionary", "NSDictionary", "NSObject"],
}
_ARRAY_CLASSES = {"NSArray", "NSMutableArray", "NSSet", "NSMutableSet", "NSOrderedSet", "NSMutableOrderedSet"}
_DICT_CLASSES = {"NSDictionary", "NSMutableDictionary"}
_STRING_CLASSES = {"NSString", "NSMutableString"}
_DATA_CLASSES = {"NSData", "NSMutableData"}
_POINT_RE = re.compile(r"\{\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\}")


class ArchiveError(ValueError):
    pass


def unarchive(data: bytes) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Decode an NSKeyedArchiver plist into plain Python values.

    Returns ``(root, class_chains)``. Objects become dicts with a ``"$class"`` entry, arrays become
    lists, curves become ``{"$class": "ValkyrieMagnitudinalCurve", "points": [(x, y), ...]}``,
    ``$null`` becomes None; numbers, booleans, strings and bytes stay as they are.
    """
    try:
        pl = plistlib.loads(data)
    except Exception as e:  # noqa: BLE001 - any parse failure means "not an archive"
        raise ArchiveError(f"not a plist: {e}") from e
    if not isinstance(pl, dict) or pl.get("$archiver") != "NSKeyedArchiver" or "$objects" not in pl:
        raise ArchiveError("not an NSKeyedArchiver plist")
    objs = pl["$objects"]
    chains: dict[str, list[str]] = {}
    busy: set[int] = set()

    def cls_of(o: dict) -> str:
        c = o.get("$class")
        if isinstance(c, plistlib.UID) and c.data < len(objs) and isinstance(objs[c.data], dict):
            cd = objs[c.data]
            name = str(cd.get("$classname", ""))
            if name and isinstance(cd.get("$classes"), list):
                chains.setdefault(name, [str(x) for x in cd["$classes"]])
            return name
        return ""

    def dec(v: Any) -> Any:
        if isinstance(v, plistlib.UID):
            i = v.data
            if i == 0 or i >= len(objs):
                return None
            if i in busy:  # cyclic graphs do not occur in brushes; cut them defensively
                return None
            busy.add(i)
            try:
                return dec_obj(objs[i])
            finally:
                busy.discard(i)
        if isinstance(v, list):
            return [dec(x) for x in v]
        return v

    def dec_obj(o: Any) -> Any:
        if isinstance(o, str) and o == "$null":
            return None
        if not isinstance(o, dict) or "$class" not in o:
            return dec(o) if isinstance(o, list) else o
        name = cls_of(o)
        if name in _ARRAY_CLASSES:
            return [dec(x) for x in o.get("NS.objects", [])]
        if name in _DICT_CLASSES:
            return {str(dec(k)): dec(x) for k, x in zip(o.get("NS.keys", []), o.get("NS.objects", []))}
        if name in _STRING_CLASSES:
            return str(o.get("NS.string", ""))
        if name in _DATA_CLASSES:
            return bytes(o.get("NS.data", b""))
        if name == "ValkyrieMagnitudinalCurve":
            pts = []
            for s in dec(o.get("points")) or []:
                m = _POINT_RE.match(str(s))
                if m:
                    pts.append((float(m.group(1)), float(m.group(2))))
            out = {"$class": name, "points": pts}
            for k, x in o.items():
                if k not in ("$class", "points"):
                    out[k] = dec(x)
            return out
        out = {"$class": name}
        for k, x in o.items():
            if k != "$class":
                out[k] = dec(x)
        return out

    top = pl.get("$top", {})
    root_uid = top.get("root") if isinstance(top, dict) else None
    if not isinstance(root_uid, plistlib.UID):
        raise ArchiveError("archive has no root object")
    root = dec(root_uid)
    if not isinstance(root, dict):
        raise ArchiveError("archive root is not an object")
    return root, chains


class _Encoder:
    def __init__(self, chains: dict[str, list[str]]):
        self.objects: list[Any] = ["$null"]
        self.chains = chains
        self.class_uids: dict[str, plistlib.UID] = {}
        self.empty_array: Optional[plistlib.UID] = None

    def add(self, obj: Any) -> plistlib.UID:
        self.objects.append(obj)
        return plistlib.UID(len(self.objects) - 1)

    def cls(self, name: str) -> plistlib.UID:
        if name not in self.class_uids:
            chain = self.chains.get(name) or CLASS_CHAINS.get(name) or [name, "NSObject"]
            self.class_uids[name] = self.add({"$classname": name, "$classes": list(chain)})
        return self.class_uids[name]

    def boxed(self, v: Any) -> plistlib.UID:
        """Always return a UID (array members / dictionary entries must be references)."""
        r = self.value(v)
        return r if isinstance(r, plistlib.UID) else self.add(r)

    def value(self, v: Any) -> Any:
        if v is None:
            return plistlib.UID(0)
        if isinstance(v, (bool, int, float)):
            return v
        if isinstance(v, (str, bytes, bytearray)):
            return self.add(bytes(v) if isinstance(v, bytearray) else v)
        if isinstance(v, (list, tuple)):
            if not v:
                if self.empty_array is None:  # NSKeyedArchiver shares the immutable empty array
                    arr = {"NS.objects": []}
                    self.empty_array = self.add(arr)
                    arr["$class"] = self.cls("NSArray")
                return self.empty_array
            arr = {}
            uid = self.add(arr)
            arr["NS.objects"] = [self.boxed(x) for x in v]
            arr["$class"] = self.cls("NSArray")
            return uid
        if isinstance(v, dict):
            name = v.get("$class")
            if not name:
                d: dict[str, Any] = {}
                uid = self.add(d)
                d["NS.keys"] = [self.boxed(str(k)) for k in v]
                d["NS.objects"] = [self.boxed(x) for x in v.values()]
                d["$class"] = self.cls("NSDictionary")
                return uid
            if name == "ValkyrieMagnitudinalCurve":
                return self.curve(v)
            obj: dict[str, Any] = {}
            uid = self.add(obj)
            for k, x in v.items():
                if k != "$class":
                    obj[k] = self.value(x)
            obj["$class"] = self.cls(name)
            return uid
        raise TypeError(f"cannot archive {type(v).__name__}")

    def curve(self, v: dict) -> plistlib.UID:
        obj: dict[str, Any] = {}
        uid = self.add(obj)
        arr: dict[str, Any] = {}
        obj["points"] = self.add(arr)
        arr["NS.objects"] = [self.add("{%.6f, %.6f}" % (float(x), float(y))) for x, y in v.get("points", [])]
        arr["$class"] = self.cls("NSMutableArray")
        for k, x in v.items():
            if k not in ("$class", "points"):
                obj[k] = self.value(x)
        obj["$class"] = self.cls("ValkyrieMagnitudinalCurve")
        return uid


def archive(root: dict[str, Any], chains: Optional[dict[str, list[str]]] = None) -> bytes:
    """Encode a decoded object graph (see :func:`unarchive`) back into a binary NSKeyedArchiver plist."""
    enc = _Encoder(dict(chains or {}))
    obj: dict[str, Any] = {}
    enc.add(obj)  # the root object is always $objects[1]
    for k in sorted(root):
        if k != "$class":
            obj[k] = enc.value(root[k])
    obj["$class"] = enc.cls(root.get("$class") or "SilicaBrush")
    pl = {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": plistlib.UID(1)},
          "$objects": enc.objects}
    return plistlib.dumps(pl, fmt=plistlib.FMT_BINARY, sort_keys=False)


# ---------------------------------------------------------------------------------------------
# SilicaBrush template (key set and defaults of a brush saved by Procreate 5.x, 2024 builds)
# ---------------------------------------------------------------------------------------------
_TEMPLATE_SCALARS: dict[str, Any] = {
    'attackRoll': False, 'attackTilt': False, 'attackTiltAngle': 0.1, 'bleedTiltAngle': 0.1,
    'blendGammaCorrect': True, 'blendMode': 0, 'brightnessTiltAngle': 0.1, 'burntEdgesAmount': 0.0,
    'burntEdgesBlendMode': 1, 'darknessJitterTilt': False, 'darknessJitterTiltAngle': 0.1, 'dualBlendMode': 0,
    'dynamicsBlur': 0.0, 'dynamicsBlurJitter': 0.0, 'dynamicsFalloff': 0.0, 'dynamicsGlazedFlow': 1.0,
    'dynamicsJitterDarkness': 0.0, 'dynamicsJitterHue': 0.0, 'dynamicsJitterLightness': 0.0,
    'dynamicsJitterOpacity': 0.0, 'dynamicsJitterSaturation': 0.0, 'dynamicsJitterSize': 0.0,
    'dynamicsJitterStrokeDarkness': 0.0, 'dynamicsJitterStrokeHue': 0.0, 'dynamicsJitterStrokeLightness': 0.0,
    'dynamicsJitterStrokeSaturation': 0.0, 'dynamicsLoad': 0.0, 'dynamicsMix': 0.0,
    'dynamicsMixSoftening': 0.0, 'dynamicsPressureBleed': 0.0, 'dynamicsPressureBleedSpeed': 0.399705,
    'dynamicsPressureBrightness': 0.0, 'dynamicsPressureHue': 0.0, 'dynamicsPressureMix': 0.0,
    'dynamicsPressureOpacity': 0.0, 'dynamicsPressureOpacitySpeed': 0.399705,
    'dynamicsPressureOpacityTransfer': 0.0, 'dynamicsPressureResponse': 0.0, 'dynamicsPressureSaturation': 0.0,
    'dynamicsPressureSecondaryColor': 0.0, 'dynamicsPressureShapeRoundness': 1.0,
    'dynamicsPressureShapeRoundnessMinimum': 1.0, 'dynamicsPressureSize': 0.0,
    'dynamicsPressureSizeSpeed': 0.399705, 'dynamicsPressureSmoothing': 0.0, 'dynamicsRollBleed': 0.0,
    'dynamicsRollBrightness': 0.0, 'dynamicsRollHue': 0.0, 'dynamicsRollOpacity': 0.0,
    'dynamicsRollSaturation': 0.0, 'dynamicsRollSecondaryColor': 0.0, 'dynamicsRollSize': 0.0,
    'dynamicsSmudgeAccumulation': 0.700213, 'dynamicsSpeedOpacity': 0.0, 'dynamicsSpeedSize': 0.0,
    'dynamicsTiltAngle': 0.1, 'dynamicsTiltBleed': 0.0, 'dynamicsTiltBrightness': 0.0,
    'dynamicsTiltCompression': 1.0, 'dynamicsTiltGradation': 0.0, 'dynamicsTiltHue': 0.0,
    'dynamicsTiltOpacity': 0.0, 'dynamicsTiltSaturation': 0.0, 'dynamicsTiltSecondaryColor': 0.0,
    'dynamicsTiltShapeRoundness': 1.0, 'dynamicsTiltShapeRoundnessMinimum': 1.0, 'dynamicsTiltSize': 0.0,
    'dynamicsWetAccumulation': 0.750139, 'dynamicsWetnessJitter': 0.0, 'eraseOpacity': 1.0,
    'eraseSize': 0.0286123, 'extendedBlend': 0, 'gradationTiltAngle': 0.1, 'grainBlendMode': 1,
    'grainDepth': 1.0, 'grainDepthJitter': 0.0, 'grainDepthMinimum': 0.0, 'grainOrientation': 1,
    'heightAmount': 0.5, 'heightScale': 0.16, 'hoverFill': 1, 'hoverOutline': 0, 'hoverPressure': 0.759171,
    'hueJitterTilt': False, 'hueJitterTiltAngle': 0.1, 'hueTiltAngle': 0.1, 'importedFromABR': False,
    'jitterSecondary': 0.0, 'jitterStrokeSecondary': 0.0, 'lightnessJitterTilt': False,
    'lightnessJitterTiltAngle': 0.1, 'maxOpacity': 1.0, 'maxPressureSizeClamped': False, 'maxSize': 1.0,
    'metallicAmount': 0.0, 'metallicGrainInverted': False, 'metallicScale': 0.16, 'minOpacity': 0.0,
    'minSize': 0.0, 'opacityTiltAngle': 0.1, 'oriented': False, 'paintOpacity': 1.0,
    'paintPressure': 1.0, 'paintSize': 0.5, 'pencilTaperEndLength': 0.0, 'pencilTaperOpacity': 0.0,
    'pencilTaperShape': 0.0, 'pencilTaperSize': 0.0, 'pencilTaperSizeLinked': False,
    'pencilTaperStartLength': 0.0, 'pencilTipAnimation': True, 'plotFFTSmoothingAmount': 0.0,
    'plotFFTSmoothingBias': 0.0, 'plotJitter': 0.0, 'plotJitterRoll': False, 'plotJitterTilt': False,
    'plotJitterTiltAngle': 0.1, 'plotMovingAverageStabilization': 0.0, 'plotSmoothing': 0.0,
    'plotSpacing': 0.1, 'plotSpacingVersion': 1, 'previewSize': 0.3, 'renderingMaxTransfer': True,
    'renderingModulatedTransfer': False, 'renderingRecursiveMixing': False, 'roughnessAmount': 0.5,
    'roughnessGrainInverted': False, 'roughnessScale': 0.16, 'saturationJitterTilt': False,
    'saturationJitterTiltAngle': 0.1, 'saturationTiltAngle': 0.1, 'secondaryColorJitterTilt': False,
    'secondaryColorJitterTiltAngle': 0.1, 'secondaryColorTiltAngle': 0.1, 'shapeAngle': 0.0,
    'shapeAzimuth': False, 'shapeCount': 0.0, 'shapeCountJitter': 0.0, 'shapeCountRoll': False,
    'shapeCountTilt': False, 'shapeCountTiltAngle': 0.1, 'shapeFilter': True, 'shapeFilterMode': 0,
    'shapeFlipXJitter': False, 'shapeFlipYJitter': False, 'shapeInverted': False, 'shapeOrientation': 1,
    'shapeRandomise': False, 'shapeRoll': False, 'shapeRollMode': 0, 'shapeRotation': 0.0,
    'shapeRoundness': 1.0, 'shapeRoundnessTiltAngle': 0.1, 'shapeScatter': 0.0, 'shapeScatterRoll': False,
    'sizeTiltAngle': 0.1, 'smudgeOpacity': 1.0, 'smudgeSize': 0.358654, 'stamp': False, 'taperEndLength': 0.0,
    'taperOpacity': 0.0, 'taperPressure': 1.0, 'taperShape': 0.0, 'taperSize': 0.0, 'taperSizeLinked': False,
    'taperStartLength': 0.0, 'taperVersion': 0, 'textureApplication': 0, 'textureBrightness': 0.0,
    'textureContrast': 0.0, 'textureDepthTilt': False, 'textureDepthTiltAngle': 0.1, 'textureFilter': False,
    'textureFilterMode': 0, 'textureInverted': False, 'textureMovement': 0.0, 'textureOffsetJitter': True,
    'textureOrientation': 1, 'textureRotation': 0.0, 'textureScale': 1.0, 'textureZoom': 0.0,
    'texturizedGrainFollowsCamera': True, 'version': 2, 'wetEdgesAmount': 0.0,
}
_CURVE_KEYS = (
    "dynamicsPressureBleedCurve", "dynamicsPressureBrightnessCurve", "dynamicsPressureHueCurve",
    "dynamicsPressureOpacityCurve", "dynamicsPressureSaturationCurve", "dynamicsPressureSecondaryColorCurve",
    "dynamicsPressureShapeRoundnessCurve", "dynamicsPressureSizeCurve", "dynamicsPressureTransferModulationCurve",
)
_ROLL_KEYS = (
    "attackRollParameters", "dynamicsRollBleedParameters", "dynamicsRollBrightnessParameters",
    "dynamicsRollHueParameters", "dynamicsRollOpacityParameters", "dynamicsRollSaturationParameters",
    "dynamicsRollSecondaryColorParameters", "dynamicsRollSizeParameters", "plotJitterRollParameters",
    "shapeCountRollParameters", "shapeScatterRollParameters",
)
_SAVED_KEYS = ("savedEraseOpacities", "savedEraseSizes", "savedPaintOpacities", "savedPaintSizes",
               "savedSmudgeOpacities", "savedSmudgeSizes")
_REF_KEYS = ("authorName", "bundledGrainPath", "bundledHeightPath", "bundledMetallicPath",
             "bundledRoughnessPath", "bundledShapePath", "color", "creationDate", "name")
BLANK_GRAIN = "Brush-Preset-Blank.png"


def template_params(name: str = "Brush") -> dict[str, Any]:
    """A complete SilicaBrush parameter dict (all keys Procreate 5.x writes) with neutral defaults."""
    p: dict[str, Any] = {"$class": "SilicaBrush"}
    p.update(_TEMPLATE_SCALARS)
    for k in _CURVE_KEYS:
        p[k] = {"$class": "ValkyrieMagnitudinalCurve", "points": [(0.0, 0.0), (1.0, 1.0)]}
    for k in _ROLL_KEYS:
        p[k] = {"$class": "ValkyrieBrushRollParameters", "activationFalloff": math.pi / 2,
                "activationOrigin": math.pi, "mode": 1, "activationRange": math.pi}
    for k in _SAVED_KEYS:
        p[k] = []
    p.update({
        "authorName": None, "bundledGrainPath": BLANK_GRAIN, "bundledHeightPath": None,
        "bundledMetallicPath": None, "bundledRoughnessPath": None, "bundledShapePath": None,
        "color": bytes(12) + np.float32(1.0).tobytes(),
        "creationDate": {"$class": "NSDate", "NS.time": time.time() - _APPLE_EPOCH},
        "name": name,
    })
    return p


TEMPLATE_KEYS = frozenset(k for k in template_params() if k != "$class")

# ---------------------------------------------------------------------------------------------
# Value tables
# ---------------------------------------------------------------------------------------------
# neutral blend name -> (blendMode, extendedBlend)
_BLEND_CODES: dict[str, tuple[int, int]] = {
    "normal": (0, 0), "multiply": (1, 1), "darken": (19, 34), "color_burn": (10, 10),
    "linear_burn": (8, 8), "darker_color": (25, 0), "lighten": (4, 4), "screen": (2, 2),
    "color_dodge": (9, 9), "add": (3, 3), "lighter_color": (0, 24), "overlay": (11, 11),
    "soft_light": (17, 17), "hard_light": (12, 12), "vivid_light": (0, 21), "linear_light": (0, 22),
    "pin_light": (0, 23), "hard_mix": (20, 20), "difference": (6, 6), "exclusion": (5, 5),
    "subtract": (7, 7), "divide": (26, 26), "hue": (15, 15), "saturation": (16, 16), "color": (13, 13),
    "luminosity": (14, 14), "behind": (18, 18),
}
_BLEND_BY_EXT = {ext: n for n, (_b, ext) in _BLEND_CODES.items() if ext}
_BLEND_BY_EXT[19] = "darken"  # older files store Darken with extendedBlend 19
_BLEND_BY_MODE = {b: n for n, (b, _e) in _BLEND_CODES.items() if b}
_GRAIN_HEIGHT_MODE = 27
_BLEND_ALIASES = {"linear_dodge": "add", "darker_colour": "darker_color", "lighter_colour": "lighter_color",
                  "colour": "color", "colour_burn": "color_burn", "colour_dodge": "color_dodge"}


def _blend_from_codes(mode: Any, ext: Any) -> str:
    mode, ext = int(mode or 0), int(ext or 0)
    if ext:
        return _BLEND_BY_EXT.get(ext, _BLEND_BY_MODE.get(mode, "normal"))
    return _BLEND_BY_MODE.get(mode, "normal")


def _blend_codes(name: str) -> Optional[tuple[int, int]]:
    name = (name or "normal").lower()
    return _BLEND_CODES.get(_BLEND_ALIASES.get(name, name))


# Built-in Procreate shapes we can approximate with a computed tip: (hardness, roundness)
_BUNDLED_SHAPE_HINTS = (
    ("oval", (1.0, 0.55)), ("medium-hard", (0.6, 1.0)), ("medium", (0.5, 1.0)), ("soft", (0.0, 1.0)),
    ("hard", (1.0, 1.0)), ("point", (1.0, 1.0)), ("round", (0.9, 1.0)), ("blank", (1.0, 1.0)),
)


def _bundled_shape_hint(path: str) -> Optional[tuple[float, float]]:
    low = path.lower()
    if not low.startswith("brush-") and "preset" not in low:
        return None
    for key, val in _BUNDLED_SHAPE_HINTS:
        if key in low:
            return val
    return None


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return lo
    if math.isnan(v):
        return lo
    return max(lo, min(hi, v))


def _f(p: dict, key: str, default: float = 0.0) -> float:
    v = p.get(key, default)
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _curve(p: dict, key: str) -> list[tuple[float, float]]:
    """Points of a ValkyrieMagnitudinalCurve; [] when missing or the identity line."""
    v = p.get(key)
    pts = v.get("points") if isinstance(v, dict) else None
    try:
        out = [(_clamp(x), _clamp(y)) for x, y in pts or []]
    except (TypeError, ValueError):
        return []
    out.sort()
    if len(out) < 2 or all(abs(x - y) < 1e-3 for x, y in out):
        return []
    return out


def _set_curve(p: dict, key: str, pts: list[tuple[float, float]]) -> None:
    if pts and len(pts) >= 2:
        p[key] = {"$class": "ValkyrieMagnitudinalCurve", "points": [(float(x), float(y)) for x, y in pts]}


# GUI conversions documented by aumlette-lab/procreate-brush-decoder
def jitter_raw_to_fraction(raw: float) -> float:
    return 0.606919 * max(raw, 0.0) ** 0.4545 if raw > 0 else 0.0


def jitter_fraction_to_raw(frac: float) -> float:
    return min(13.7844, (max(frac, 0.0) / 0.606919) ** (1 / 0.4545)) if frac > 0 else 0.0


def grain_scale_raw_to_fraction(raw: float) -> float:
    return 0.25 * math.sqrt(max(raw, 0.0))


def grain_scale_fraction_to_raw(frac: float) -> float:
    return min(16.0, (max(frac, 0.0) / 0.25) ** 2)


# ---------------------------------------------------------------------------------------------
# Container handling
# ---------------------------------------------------------------------------------------------
class _Entry:
    """One brush inside a .brush/.brushset zip."""

    def __init__(self, prefix: str, set_index: int):
        self.prefix = prefix  # "" for .brush, "UUID/" for brushset folders
        self.set_index = set_index


def _is_junk(name: str) -> bool:
    return name.startswith("__MACOSX/") or "/._" in name or name.startswith("._")


def _list_entries(zf: zipfile.ZipFile) -> tuple[list[_Entry], str]:
    """Return the brushes (in Procreate's order) and the brushset name ('' for single brushes)."""
    names = [n for n in zf.namelist() if not _is_junk(n)]
    prefixes = []
    for n in names:
        if n == ARCHIVE or n.endswith("/" + ARCHIVE):
            pre = n[: -len(ARCHIVE)]
            if pre.rstrip("/").split("/")[-1] == "Reset":
                continue
            prefixes.append(pre)
    set_name = ""
    order: list[str] = []
    plist_name = next((n for n in names if n == BRUSHSET_PLIST or n.endswith("/" + BRUSHSET_PLIST)), None)
    if plist_name:
        base = plist_name[: -len(BRUSHSET_PLIST)]
        try:
            meta = plistlib.loads(zf.read(plist_name))
        except Exception:  # noqa: BLE001
            meta = {}
        if isinstance(meta, dict):
            set_name = str(meta.get("name") or "")
            for u in meta.get("brushes") or []:
                pre = f"{base}{u}/"
                if pre in prefixes and pre not in order:
                    order.append(pre)
    order += sorted(p for p in prefixes if p not in order)
    return [_Entry(p, i) for i, p in enumerate(order)], set_name


def _read_png(zf: zipfile.ZipFile, name: str) -> Optional[Image.Image]:
    try:
        data = zf.read(name)
    except KeyError:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception:  # noqa: BLE001
        log.warning("cannot decode %s", name)
        return None


def _gray_white_paint(img: Image.Image) -> Image.Image:
    """Procreate shape/grain image -> L, 255 = paint (alpha is composited over black)."""
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
        bg.alpha_composite(rgba)
        return bg.convert("L")
    if img.mode == "I;16" or img.mode.startswith("I"):
        arr = np.asarray(img, dtype=np.float64)
        top = 65535.0 if arr.max() > 255 else 255.0
        return Image.fromarray(np.clip(arr / top * 255.0, 0, 255).astype(np.uint8), "L")
    return img.convert("L")


def _thumb_to_preview(img: Image.Image) -> Image.Image:
    """Procreate thumbnails are a white stroke on transparency; the app shows dark paint."""
    rgba = img.convert("RGBA")
    lum = np.asarray(rgba.convert("L"), dtype=np.float32) / 255.0
    alpha = np.asarray(rgba.getchannel("A"), dtype=np.float32) * lum
    out = Image.new("RGBA", rgba.size, (30, 30, 30, 0))
    out.putalpha(Image.fromarray(np.clip(alpha, 0, 255).astype(np.uint8), "L"))
    return out


def _preview_to_thumb(img: Image.Image) -> Image.Image:
    """Any preview (dark paint or RGBA) -> Procreate's white-on-transparent 1060x324 thumbnail."""
    rgba = img.convert("RGBA")
    a = np.asarray(rgba.getchannel("A"), dtype=np.float32)
    if a.min() >= 250:  # opaque preview: use darkness on a light background as coverage
        g = rgba.convert("L")
        from ..core.imaging import background_is_light

        a = np.asarray(ImageOps.invert(g) if background_is_light(g) else g, dtype=np.float32)
    mask = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "L")
    w, h = THUMB_SIZE
    s = min(w / mask.width, h / mask.height)
    mask = mask.resize((max(1, int(mask.width * s)), max(1, int(mask.height * s))), Image.LANCZOS)
    out = Image.new("RGBA", THUMB_SIZE, (255, 255, 255, 0))
    layer = Image.new("RGBA", mask.size, (255, 255, 255, 255))
    layer.putalpha(mask)
    out.paste(layer, ((w - mask.width) // 2, (h - mask.height) // 2))
    return out


def _read_archive(zf: zipfile.ZipFile, e: _Entry) -> tuple[bytes, dict[str, Any], dict[str, list[str]]]:
    data = zf.read(e.prefix + ARCHIVE)
    root, chains = unarchive(data)
    return data, root, chains


def _version_label(params: dict[str, Any]) -> tuple[str, str]:
    """(short version, human description) estimated from the archive's key set."""
    if "attackRollParameters" in params or "shapeRollMode" in params:
        return "5.4+", "Procreate 5X 新版（含 Apple Pencil Pro 笔杆旋转参数，约 2024 年起）"
    if "metallicAmount" in params or "hoverFill" in params:
        return "5.2+", "Procreate 5.2 及以上（含 3D 绘画参数，约 2021 年起）"
    if int(_f(params, "version", 0)) >= 2:
        return "5", "Procreate 5 / 5X（约 2019–2021 年）"
    if "version" not in params:
        return "?", "未知（缺少版本字段，可能由第三方工具生成）"
    return "4", "Procreate 4 或更早"


# ---------------------------------------------------------------------------------------------
# SilicaBrush params  ->  neutral Brush
# ---------------------------------------------------------------------------------------------
def params_to_brush(p: dict[str, Any], *, shape: Optional[Image.Image], grain: Optional[Image.Image],
                    thumb: Optional[Image.Image]) -> Brush:
    name = p.get("name") if isinstance(p.get("name"), str) else ""
    br = Brush(name=(name or "Procreate Brush").strip() or "Procreate Brush")

    # --- tip ----------------------------------------------------------------------------------
    bundled_shape = p.get("bundledShapePath") if isinstance(p.get("bundledShapePath"), str) else ""
    if shape is not None:
        tip = _gray_white_paint(shape)
        if p.get("shapeInverted"):
            tip = ImageOps.invert(tip)
        br.tips = [tip]
    else:
        hint = _bundled_shape_hint(bundled_shape) if bundled_shape else (1.0, 1.0)
        if hint is None:
            br.warn(f"该笔刷使用了 Procreate 内置形状「{bundled_shape}」，文件中不包含该图像，已用圆形笔尖代替。")
            hint = (0.8, 1.0)
        elif bundled_shape:
            br.warn(f"该笔刷使用了 Procreate 内置形状「{bundled_shape}」，已用近似的圆形笔尖代替。")
        br.hardness, br.roundness = hint
    if p.get("shapeInverted") and shape is None:
        br.warn("Procreate 的“反转形状”无法应用到内置形状，已忽略。")

    # --- size / spacing / shape behaviour -----------------------------------------------------
    max_s = max(_f(p, "maxSize", 1.0), 1e-6)
    min_s = _clamp(_f(p, "minSize", 0.0), 0.0, max_s)
    cur = min_s + _clamp(_f(p, "paintSize", 0.5)) * (max_s - min_s)
    br.size_px = max(1.0, cur * SIZE_UNIT_PX)
    br.min_size_px = max(0.0, min_s * SIZE_UNIT_PX)
    br.max_size_px = max(br.size_px, max_s * SIZE_UNIT_PX)
    br.spacing = _clamp(_f(p, "plotSpacing", 0.1), 0.01, 2.0)
    br.scatter = _clamp(jitter_raw_to_fraction(_f(p, "plotJitter")), 0.0, 2.0)
    br.scatter_both_axes = False  # Procreate jitter is perpendicular to the stroke only
    br.angle = math.degrees(_f(p, "shapeAngle")) % 360.0
    br.roundness = _clamp(_f(p, "shapeRoundness", 1.0), 0.01, 1.0) if br.tips else br.roundness * _clamp(
        _f(p, "shapeRoundness", 1.0), 0.01, 1.0)
    rot = _f(p, "shapeRotation")
    br.angle_follow_stroke = rot >= 0.5
    if 0.01 < abs(rot) < 0.99:
        br.warn(f"Procreate 的形状“旋转”为 {round(rot * 100)}%（部分跟随笔画方向），已近似为"
                f"{'跟随' if br.angle_follow_stroke else '不跟随'}笔画方向。")
    elif rot <= -0.99:
        br.warn("Procreate 的形状“旋转”为 -100%（反向跟随笔画），已近似为不跟随。")
    br.angle_jitter = _clamp(_f(p, "shapeScatter"), 0.0, 1.0)
    br.count = max(1, int(round(_f(p, "shapeCount") * 16)))
    br.count_jitter = _clamp(_f(p, "shapeCountJitter"))
    br.tip_order = "random" if p.get("shapeRandomise") else "sequential"
    br.flip_x = bool(p.get("shapeFlipXJitter"))
    br.flip_y = bool(p.get("shapeFlipYJitter"))

    # --- dynamics -----------------------------------------------------------------------------
    def signed(key: str, label: str) -> float:
        v = _f(p, key)
        if v < -0.01:
            br.warn(f"Procreate 的“{label}”为负值（反向作用），已按正向近似。")
        return _clamp(abs(v))

    br.size_dynamic = Dynamic(pressure=signed("dynamicsPressureSize", "压力-大小"),
                              tilt=_clamp(_f(p, "dynamicsTiltSize")),
                              velocity=signed("dynamicsSpeedSize", "速度-大小"),
                              jitter=_clamp(_f(p, "dynamicsJitterSize")),
                              pressure_curve=_curve(p, "dynamicsPressureSizeCurve"))
    br.opacity_dynamic = Dynamic(pressure=_clamp(_f(p, "dynamicsPressureOpacityTransfer")),
                                 tilt=_clamp(_f(p, "dynamicsTiltOpacity")),
                                 velocity=signed("dynamicsSpeedOpacity", "速度-不透明度"),
                                 jitter=_clamp(_f(p, "dynamicsJitterOpacity")))
    br.flow_dynamic = Dynamic(pressure=signed("dynamicsPressureOpacity", "压力-流量"),
                              pressure_curve=_curve(p, "dynamicsPressureOpacityCurve"))

    # Bleed thins the paint (lighter, broken edges) at low pressure / when tilted; gradation fades the
    # stamp when tilted. Both become paint density (flow) responding to pressure / tilt.
    bleed_p = _clamp(abs(_f(p, "dynamicsPressureBleed")))
    bleed_t = _clamp(abs(_f(p, "dynamicsTiltBleed")) + abs(_f(p, "dynamicsTiltGradation")))
    if bleed_p > 0.001 and bleed_p > br.flow_dynamic.pressure:
        br.flow_dynamic.pressure = bleed_p
        br.flow_dynamic.pressure_curve = _curve(p, "dynamicsPressureBleedCurve")
    if bleed_t > 0.001:
        br.flow_dynamic.tilt = _clamp(max(br.flow_dynamic.tilt, bleed_t))
    if bleed_p > 0.001 or bleed_t > 0.001:
        br.warn("Procreate 的“渗流/渐变”（压力、倾斜）已近似为“流量（浓度）随压力/倾斜变化”。")

    # Wet edges / burnt edges -> edge effect (CSP "watercolor edge")
    wet_e = _clamp(_f(p, "wetEdgesAmount"))
    burnt = _clamp(_f(p, "burntEdgesAmount"))
    if wet_e > 0.001 or burnt > 0.001:
        br.edge = EdgeEffect(opacity=_clamp(wet_e + burnt * 0.5), darken=burnt,
                             blur_px=round(wet_e * 4.0, 2))

    # --- paint --------------------------------------------------------------------------------
    max_o = _clamp(_f(p, "maxOpacity", 1.0))
    min_o = _clamp(_f(p, "minOpacity", 0.0), 0.0, max_o)
    br.opacity = _clamp(min_o + _clamp(_f(p, "paintOpacity", 1.0)) * (max_o - min_o), 0.01, 1.0)
    br.flow = _clamp(_f(p, "dynamicsGlazedFlow", 1.0), 0.01, 1.0)
    br.blend_mode = _blend_from_codes(p.get("blendMode"), p.get("extendedBlend"))
    br.stabilization = _clamp(max(_f(p, "plotSmoothing"), _f(p, "plotMovingAverageStabilization")))

    # Pressure taper (Apple Pencil) is what most artists see; touch taper is used when linked.
    pre = "taper" if p.get("pencilTaperSizeLinked") else "pencilTaper"
    if _f(p, pre + "Size") > 0 or _f(p, pre + "Opacity") > 0:
        br.taper_start = _clamp(_f(p, pre + "StartLength"))
        br.taper_end = _clamp(_f(p, pre + "EndLength"))

    # --- grain --------------------------------------------------------------------------------
    bundled_grain = p.get("bundledGrainPath") if isinstance(p.get("bundledGrainPath"), str) else ""
    if grain is not None:
        gimg = _gray_white_paint(grain)
        gmode = int(_f(p, "grainBlendMode", 1))
        br.texture = Texture(
            image=gimg, name=f"{br.name} Grain",
            scale=max(0.01, grain_scale_raw_to_fraction(_f(p, "textureScale", 1.0))),
            depth=_clamp(_f(p, "grainDepth", 1.0)), invert=bool(p.get("textureInverted")),
            mode="height" if gmode == _GRAIN_HEIGHT_MODE else _BLEND_BY_MODE.get(gmode, "multiply"),
            brightness=_clamp(_f(p, "textureBrightness"), -1.0, 1.0),
            contrast=_clamp(_f(p, "textureContrast"), -1.0, 1.0),
            per_tip=int(_f(p, "textureApplication")) == 0 and _f(p, "textureMovement") < 0.5,
            depth_dynamic=Dynamic(jitter=_clamp(_f(p, "grainDepthJitter"))))
    elif bundled_grain and bundled_grain != BLANK_GRAIN:
        br.warn(f"该笔刷使用了 Procreate 内置纹理「{bundled_grain}」，文件中不包含该图像，纹理已丢失。")

    # --- colour / wet mix ---------------------------------------------------------------------
    light, dark = _clamp(_f(p, "dynamicsJitterLightness")), _clamp(_f(p, "dynamicsJitterDarkness"))
    stamp = ColorDynamics(fg_bg_jitter=_clamp(_f(p, "jitterSecondary")),
                          hue_jitter=_clamp(_f(p, "dynamicsJitterHue")),
                          saturation_jitter=_clamp(_f(p, "dynamicsJitterSaturation")),
                          brightness_jitter=max(light, dark),
                          brightness_up=light if abs(light - dark) > 1e-4 else None,
                          brightness_down=dark if abs(light - dark) > 1e-4 else None,
                          per_tip=True)
    stroke = ColorDynamics(fg_bg_jitter=_clamp(_f(p, "jitterStrokeSecondary")),
                           hue_jitter=_clamp(_f(p, "dynamicsJitterStrokeHue")),
                           saturation_jitter=_clamp(_f(p, "dynamicsJitterStrokeSaturation")),
                           brightness_jitter=_clamp(max(_f(p, "dynamicsJitterStrokeLightness"),
                                                        _f(p, "dynamicsJitterStrokeDarkness"))),
                           per_tip=False)
    br.color = stamp if stamp.is_active() or not stroke.is_active() else stroke
    if stamp.is_active() and stroke.is_active():
        br.warn("Procreate 同时设置了“图章颜色抖动”和“笔画颜色抖动”，只保留了图章颜色抖动。")
    # Colour driven by pressure / tilt. Procreate shifts the colour at light pressure and reaches the
    # chosen colour at full pressure: saturation 100% = white -> saturated, brightness 100% = lighter
    # at light pressure, hue 100% = the whole spectrum (360 degrees).
    for ch, label, sign, scale in (("hue", "Hue", 1.0, 2.0), ("saturation", "Saturation", -1.0, 1.0),
                                   ("brightness", "Brightness", 1.0, 1.0)):
        setattr(br.color, f"{ch}_pressure", _clamp(sign * scale * _f(p, f"dynamicsPressure{label}"), -1.0, 1.0))
        setattr(br.color, f"{ch}_tilt", _clamp(sign * scale * _f(p, f"dynamicsTilt{label}"), -1.0, 1.0))
        cv = _curve(p, f"dynamicsPressure{label}Curve")
        if cv:
            br.color.pressure_curves[ch] = cv

    dilution = _clamp(_f(p, "dynamicsMix"))
    recursive = bool(p.get("renderingRecursiveMixing"))
    if dilution > 0.001 or recursive:
        br.wet = WetMix(enabled=True, mix=_clamp(_f(p, "dynamicsWetAccumulation")),
                        load=_clamp(_f(p, "dynamicsLoad", 1.0)), wetness=dilution,
                        color_stretch=_clamp(_f(p, "dynamicsMixSoftening")))

    # --- things the neutral model cannot express ----------------------------------------------
    unsupported = [
        ("dynamicsBlur", "模糊"), ("dynamicsFalloff", "渐隐（Fall off）"),
        ("metallicAmount", "3D 金属度"), ("dynamicsPressureMix", "湿混-侵染（Attack）"),
    ]
    for key, label in unsupported:
        if abs(_f(p, key)) > 0.001:
            br.warn(f"Procreate 的“{label}”效果无法在通用模型中表示，转换到其他软件时会丢失。")
    if isinstance(p.get("bundledHeightPath"), str) or isinstance(p.get("bundledMetallicPath"), str):
        br.warn("该笔刷带有 Procreate 3D 材质（高度/金属/粗糙度）设置，其他软件不支持。")

    if thumb is not None:
        br.preview = _thumb_to_preview(thumb)
    return br


# ---------------------------------------------------------------------------------------------
# neutral Brush  ->  SilicaBrush params
# ---------------------------------------------------------------------------------------------
def _set(p: dict[str, Any], key: str, value: Any) -> None:
    """Assign keeping the template's value type (bool / int / float)."""
    old = p.get(key)
    if isinstance(old, bool):
        p[key] = bool(value)
    elif isinstance(old, int):
        p[key] = int(round(float(value)))
    elif isinstance(old, float):
        p[key] = float(value)
    else:
        p[key] = value


def brush_to_params(br: Brush, *, has_grain: bool) -> dict[str, Any]:
    p = template_params(br.name or "Brush")

    # size: keep the useful slider range around the current size
    size = max(1.0, float(br.size_px))
    hi = max(size, min(float(br.max_size_px or size), max(size * 3.0, 50.0)))
    lo = min(max(float(br.min_size_px or 0.0), 0.0), size)
    max_s = _clamp(hi / SIZE_UNIT_PX, 0.002, 16.0)
    min_s = _clamp(lo / SIZE_UNIT_PX, 0.0, max_s)
    paint = (size / SIZE_UNIT_PX - min_s) / (max_s - min_s) if max_s > min_s else 1.0
    _set(p, "maxSize", max_s)
    _set(p, "minSize", min_s)
    _set(p, "paintSize", _clamp(paint))
    _set(p, "maxOpacity", 1.0)
    _set(p, "minOpacity", 0.0)
    _set(p, "paintOpacity", _clamp(br.opacity))

    _set(p, "plotSpacing", _clamp(br.spacing, 0.0, 2.0))
    _set(p, "plotJitter", jitter_fraction_to_raw(_clamp(br.scatter, 0.0, 2.0)))
    _set(p, "shapeAngle", math.radians(br.angle % 360.0))
    _set(p, "shapeRoundness", _clamp(br.roundness, 0.01, 1.0))
    _set(p, "shapeRotation", 1.0 if br.angle_follow_stroke else 0.0)
    _set(p, "shapeScatter", _clamp(br.angle_jitter))
    _set(p, "shapeCount", _clamp(max(1, int(br.count)) / 16.0) if br.count > 1 else 0.0)
    _set(p, "shapeCountJitter", _clamp(br.count_jitter))
    _set(p, "shapeRandomise", br.tip_order == "random")
    _set(p, "shapeFlipXJitter", br.flip_x)
    _set(p, "shapeFlipYJitter", br.flip_y)

    _set(p, "dynamicsPressureSize", _clamp(br.size_dynamic.pressure))
    _set(p, "dynamicsTiltSize", _clamp(br.size_dynamic.tilt))
    _set(p, "dynamicsSpeedSize", _clamp(br.size_dynamic.velocity))
    _set(p, "dynamicsJitterSize", _clamp(br.size_dynamic.jitter))
    _set(p, "dynamicsPressureOpacityTransfer", _clamp(br.opacity_dynamic.pressure))
    _set(p, "dynamicsTiltOpacity", _clamp(br.opacity_dynamic.tilt))
    _set(p, "dynamicsSpeedOpacity", _clamp(br.opacity_dynamic.velocity))
    _set(p, "dynamicsJitterOpacity", _clamp(br.opacity_dynamic.jitter))
    _set(p, "dynamicsPressureOpacity", _clamp(br.flow_dynamic.pressure))
    _set_curve(p, "dynamicsPressureSizeCurve", br.size_dynamic.pressure_curve)
    _set_curve(p, "dynamicsPressureOpacityCurve", br.flow_dynamic.pressure_curve)
    if br.size_dynamic.tilt > 0 or br.opacity_dynamic.tilt > 0:
        _set(p, "dynamicsTiltAngle", 0.5)
    if br.flow_dynamic.tilt > 0:
        _set(p, "dynamicsTiltBleed", _clamp(br.flow_dynamic.tilt))
        _set(p, "dynamicsTiltAngle", 0.5)

    e = br.edge
    if e.is_active():
        _set(p, "burntEdgesAmount", _clamp(e.darken))
        _set(p, "wetEdgesAmount", _clamp(e.opacity - e.darken * 0.5))

    _set(p, "dynamicsGlazedFlow", _clamp(br.flow, 0.01, 1.0))
    codes = _blend_codes(br.blend_mode)
    if codes is None:
        br.warn(f"Procreate 不支持混合模式“{br.blend_mode}”，已改为正常。")
        codes = (0, 0)
    _set(p, "blendMode", codes[0])
    _set(p, "extendedBlend", codes[1])
    _set(p, "plotSmoothing", _clamp(br.stabilization))

    if br.taper_start > 0 or br.taper_end > 0:
        for pre in ("pencilTaper", "taper"):
            _set(p, pre + "StartLength", _clamp(br.taper_start))
            _set(p, pre + "EndLength", _clamp(br.taper_end))
            _set(p, pre + "Size", 1.0)
        _set(p, "taperVersion", 1)

    tx = br.texture
    if tx is not None and has_grain:
        p["bundledGrainPath"] = None
        _set(p, "textureScale", grain_scale_fraction_to_raw(tx.scale))
        _set(p, "grainDepth", _clamp(tx.depth))
        _set(p, "grainDepthJitter", _clamp(tx.depth_dynamic.jitter))
        _set(p, "textureInverted", tx.invert)
        _set(p, "textureBrightness", _clamp(tx.brightness, -0.75, 0.75))
        _set(p, "textureContrast", _clamp(tx.contrast, -1.0, 1.0))
        _set(p, "textureApplication", 0 if tx.per_tip else 1)
        _set(p, "textureMovement", 0.0)
        _set(p, "textureZoom", 0.0 if tx.per_tip else 1.0)
        mode = (tx.mode or "multiply").lower()
        if mode in ("height", "linear_height"):
            gcode = _GRAIN_HEIGHT_MODE
        else:
            gcodes = _blend_codes(mode)
            gcode = gcodes[0] if gcodes and gcodes[0] else 1
        _set(p, "grainBlendMode", gcode)

    c = br.color
    if c.has_jitter():
        pre_h, pre_s, pre_l, pre_d, sec = (
            ("dynamicsJitterHue", "dynamicsJitterSaturation", "dynamicsJitterLightness",
             "dynamicsJitterDarkness", "jitterSecondary") if c.per_tip else
            ("dynamicsJitterStrokeHue", "dynamicsJitterStrokeSaturation", "dynamicsJitterStrokeLightness",
             "dynamicsJitterStrokeDarkness", "jitterStrokeSecondary"))
        _set(p, pre_h, _clamp(c.hue_jitter))
        _set(p, pre_s, _clamp(c.saturation_jitter))
        _set(p, pre_l, _clamp(c.brightness_jitter if c.brightness_up is None else c.brightness_up))
        _set(p, pre_d, _clamp(c.brightness_jitter if c.brightness_down is None else c.brightness_down))
        _set(p, sec, _clamp(c.fg_bg_jitter))
    for ch, label, sign, scale in (("hue", "Hue", 1.0, 2.0), ("saturation", "Saturation", -1.0, 1.0),
                                   ("brightness", "Brightness", 1.0, 1.0)):
        _set(p, f"dynamicsPressure{label}", _clamp(sign * getattr(c, f"{ch}_pressure") / scale, -1.0, 1.0))
        _set(p, f"dynamicsTilt{label}", _clamp(sign * getattr(c, f"{ch}_tilt") / scale, -1.0, 1.0))
        _set_curve(p, f"dynamicsPressure{label}Curve", c.pressure_curves.get(ch, []))
    if any(abs(getattr(c, f"{ch}_tilt")) > 1e-4 for ch in ("hue", "saturation", "brightness")):
        _set(p, "dynamicsTiltAngle", 0.5)

    if br.wet.enabled:
        _set(p, "renderingRecursiveMixing", True)
        _set(p, "renderingModulatedTransfer", True)
        _set(p, "dynamicsMix", _clamp(br.wet.wetness))
        _set(p, "dynamicsLoad", _clamp(br.wet.load))
        _set(p, "dynamicsWetAccumulation", _clamp(br.wet.mix))
        _set(p, "dynamicsMixSoftening", _clamp(br.wet.color_stretch))
    else:
        _set(p, "dynamicsWetAccumulation", 0.0)

    # things Procreate cannot represent
    if len(br.tips) > 1:
        br.warn(f"Procreate 每个笔刷只能有一个形状，已使用 {len(br.tips)} 个笔尖中的第一个。")
    if br.dual is not None:
        br.warn("Procreate 的笔刷文件不支持双重画笔，已忽略双重画笔部分。")
    if br.roundness_jitter > 0:
        br.warn("Procreate 没有“圆度抖动”，已忽略。")
    if br.is_eraser:
        br.warn("Procreate 的笔刷可同时用于绘画和擦除，不再区分橡皮擦。")
    if br.flow_dynamic.velocity > 0 or br.flow_dynamic.jitter > 0:
        br.warn("Procreate 的流量只能受压力/倾斜控制，速度/抖动对流量的影响已忽略。")
    return p


def _pow2(n: int, lo: int, hi: int) -> int:
    s = lo
    while s < n and s < hi:
        s *= 2
    return min(s, hi)


def make_shape_png(tip: Image.Image) -> Image.Image:
    """Square, power-of-two, white-on-black L image as Procreate expects for Shape.png."""
    g = tip.convert("L")
    g = pad_square(g, 0)
    side = _pow2(g.width, 64, MAX_SHAPE_SIDE)
    if g.width != side:
        g = g.resize((side, side), Image.LANCZOS if g.width > side else Image.BICUBIC)
    return g


def make_grain_png(img: Image.Image) -> Image.Image:
    """Square, power-of-two, seamless L grain (255 = paint passes) from a tileable pattern."""
    g = img.convert("L")
    w, h = g.size
    m = max(w, h)
    nx, ny = max(1, round(m / w)), max(1, round(m / h))
    if nx > 1 or ny > 1:
        t = Image.new("L", (w * nx, h * ny))
        for i in range(nx):
            for j in range(ny):
                t.paste(g, (i * w, j * h))
        g = t
    side = _pow2(max(g.size), 256, MAX_GRAIN_SIDE)
    if g.size != (side, side):
        # resample a 3x3 tiling and keep the centre so the result still wraps without seams
        big = Image.new("L", (g.width * 3, g.height * 3))
        for i in range(3):
            for j in range(3):
                big.paste(g, (i * g.width, j * g.height))
        big = big.resize((side * 3, side * 3), Image.LANCZOS if g.width > side else Image.BICUBIC)
        g = big.crop((side, side, side * 2, side * 2))
    return g


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False)
    return buf.getvalue()


def _rename_archive(data: bytes, new_name: str) -> bytes:
    """Change only the brush name inside an existing archive (everything else stays untouched)."""
    pl = plistlib.loads(data)
    objs = pl["$objects"]
    root = objs[pl["$top"]["root"].data]
    ref = root.get("name")
    if isinstance(ref, plistlib.UID) and ref.data > 0:
        objs[ref.data] = new_name
    else:
        objs.append(new_name)
        root["name"] = plistlib.UID(len(objs) - 1)
    return plistlib.dumps(pl, fmt=plistlib.FMT_BINARY, sort_keys=False)


def brush_files(br: Brush, *, prefer_native: bool = True) -> dict[str, bytes]:
    """All zip members (relative to the brush folder) needed to store ``br`` as a Procreate brush."""
    nat = br.source.native if br.source.format == "procreate" else {}
    if prefer_native and nat.get("archive") and "files" in nat:
        files = dict(nat["files"])
        data = nat["archive"]
        if br.name and br.name != nat.get("name", (nat.get("params") or {}).get("name")):
            data = _rename_archive(data, br.name)
        files[ARCHIVE] = data
        return files

    files: dict[str, bytes] = {}
    grain_img = None
    if br.texture is not None and br.texture.image is not None:
        grain_img = make_grain_png(br.texture.image)
    if prefer_native and nat.get("params"):  # decoded params without original bytes
        params = template_params(br.name)
        params.update(nat["params"])
        params["name"] = br.name
        chains = nat.get("classes") or {}
    else:
        params = brush_to_params(br, has_grain=grain_img is not None)
        chains = {}
    files[ARCHIVE] = archive(params, chains)
    # computed tips are rendered circular: Procreate applies roundness/angle from the archive itself
    tip = br.tips[0] if br.tips else round_tip(256, br.hardness)
    files[SHAPE] = _png_bytes(make_shape_png(tip))
    if grain_img is not None:
        files[GRAIN] = _png_bytes(grain_img)
    preview = br.preview
    try:
        if preview is None:
            preview = stroke_preview(br, 720, 220)
        files[THUMB] = _png_bytes(_preview_to_thumb(preview))
    except Exception:  # noqa: BLE001 - a thumbnail must never block the export
        log.exception("thumbnail for %s", br.name)
    return files


def write_procreate(brushes: list[Brush], out_path: Path, *, set_name: str = "",
                    prefer_native: bool = True) -> Path:
    out_path = Path(out_path)
    if not brushes:
        raise ValueError("no brushes to write")
    single = out_path.suffix.lower() == ".brush"
    if single and len(brushes) != 1:
        out_path = out_path.with_suffix(".brushset")
        single = False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".part")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            if single:
                for name, data in brush_files(brushes[0], prefer_native=prefer_native).items():
                    zf.writestr(name, data)
            else:
                ids = []
                for br in brushes:
                    uid = str(_uuid.uuid4()).upper()
                    ids.append(uid)
                    for name, data in brush_files(br, prefer_native=prefer_native).items():
                        zf.writestr(f"{uid}/{name}", data)
                title = set_name or next((b.group for b in brushes if b.group), "") or out_path.stem
                zf.writestr(BRUSHSET_PLIST, plistlib.dumps({"name": title, "brushes": ids},
                                                           fmt=plistlib.FMT_XML))
        tmp.replace(out_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return out_path


# ---------------------------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------------------------
def _load_entry(zf: zipfile.ZipFile, e: _Entry, path: Path, set_name: str) -> Brush:
    data, params, chains = _read_archive(zf, e)
    names = set(zf.namelist())
    shape = _read_png(zf, e.prefix + SHAPE) if e.prefix + SHAPE in names else None
    grain = _read_png(zf, e.prefix + GRAIN) if e.prefix + GRAIN in names else None
    thumb = _read_png(zf, e.prefix + THUMB) if e.prefix + THUMB in names else None
    br = params_to_brush(params, shape=shape, grain=grain, thumb=thumb)
    br.group = set_name
    files = {}
    for n in zf.namelist():
        if n.startswith(e.prefix) and not n.endswith("/") and not _is_junk(n) and n != e.prefix + ARCHIVE:
            rel = n[len(e.prefix):]
            if rel == BRUSHSET_PLIST:
                continue
            files[rel] = zf.read(n)
    short, _desc = _version_label(params)
    br.source = BrushSource(app="procreate", format="procreate", version=short, path=str(path), index=e.set_index,
                            native={"params": params, "archive": data, "files": files, "classes": chains,
                                    "name": br.name})
    return br


@register_codec
class ProcreateCodec(FormatCodec):
    id = "procreate"
    name = "Procreate 笔刷 (.brushset/.brush)"
    extensions = (".brushset", ".brush")
    can_read = True
    can_write = True

    def sniff(self, path: Path) -> Optional[FileInfo]:
        path = Path(path)
        try:
            with open(path, "rb") as f:
                if f.read(4) != b"PK\x03\x04":
                    return None
            zf = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile):
            return None
        with zf:
            try:
                entries, set_name = _list_entries(zf)
            except Exception:  # noqa: BLE001
                return None
            if not entries:
                return None
            is_set = any(e.prefix for e in entries) or set_name != ""
            info = FileInfo(path=path, format_id=self.id, format_name=self.name, brush_count=len(entries))
            info.details["类型"] = "笔刷集 (.brushset)" if is_set else "单个笔刷 (.brush)"
            if set_name:
                info.details["笔刷集名称"] = set_name
            info.details["笔刷数量"] = str(len(entries))
            versions: dict[str, str] = {}
            archive_versions: set[str] = set()
            missing_shapes: list[str] = []
            missing_grains: list[str] = []
            broken = 0
            names = set(zf.namelist())
            for e in entries:
                try:
                    _data, params, _c = _read_archive(zf, e)
                except Exception:  # noqa: BLE001
                    broken += 1
                    continue
                short, desc = _version_label(params)
                versions[short] = desc
                archive_versions.add(str(params.get("version", "?")))
                bname = params.get("name") if isinstance(params.get("name"), str) else e.prefix.strip("/")
                bs, bg = params.get("bundledShapePath"), params.get("bundledGrainPath")
                if isinstance(bs, str) and e.prefix + SHAPE not in names:
                    missing_shapes.append(f"{bname}（{bs}）")
                if isinstance(bg, str) and bg != BLANK_GRAIN and e.prefix + GRAIN not in names:
                    missing_grains.append(f"{bname}（{bg}）")
            if versions:
                rank = ["?", "4", "5", "5.2+", "5.4+"]
                order = sorted(versions, key=lambda v: rank.index(v) if v in rank else 0)
                info.version = order[-1]
                info.details["Procreate 版本（估计）"] = "；".join(versions[k] for k in order)
                info.details["存档格式"] = "SilicaBrush v" + "/".join(sorted(archive_versions))

            def listing(items: list[str]) -> str:
                return "、".join(items[:3]) + (f" 等 {len(items)} 个" if len(items) > 3 else "")

            if missing_shapes:
                info.problems.append(
                    f"笔刷 {listing(missing_shapes)} 使用 Procreate 内置形状，文件里没有该图像（属于 Procreate 的"
                    f"版权素材，无法附带）。转换到其他软件时将用圆形笔尖代替。")
            if missing_grains:
                info.problems.append(
                    f"笔刷 {listing(missing_grains)} 使用 Procreate 内置纹理，文件里没有该图像。"
                    f"转换到其他软件时纹理会丢失。")
            if broken:
                info.problems.append(f"有 {broken} 个笔刷的参数文件已损坏，无法读取。")
            return info

    def compatibility(self, info: FileInfo, app_id: str, app_version: str) -> Optional[str]:
        if app_id != "procreate" or not app_version:
            return None
        nums = [int(x) for x in re.findall(r"\d+", app_version)[:1]] + [0]
        # every Procreate 5.x reads SilicaBrush v2 archives and ignores keys it doesn't know
        if info.version in ("5", "5.2+", "5.4+") and nums[0] < 5:
            return "该笔刷是 Procreate 5 格式，Procreate 4 及更早版本无法导入。"
        return None

    def list_refs(self, path: Path) -> list[BrushRef]:
        path = Path(path)
        refs = []
        with zipfile.ZipFile(path) as zf:
            entries, set_name = _list_entries(zf)
            names = set(zf.namelist())
            for i, e in enumerate(entries):
                try:
                    _d, params, _c = _read_archive(zf, e)
                    nm = params.get("name") if isinstance(params.get("name"), str) else ""
                except Exception:  # noqa: BLE001
                    params, nm = {}, ""
                nm = nm or f"Brush {i + 1}"
                detail = []
                if e.prefix + GRAIN in names or (isinstance(params.get("bundledGrainPath"), str)
                                                  and params.get("bundledGrainPath") != BLANK_GRAIN):
                    detail.append("含纹理")
                if isinstance(params.get("bundledShapePath"), str) and e.prefix + SHAPE not in names:
                    detail.append("内置形状")
                refs.append(BrushRef(name=nm, key=str(i), group=set_name,
                                     loader=(lambda i=i: self.read_one(path, i)),
                                     thumbnail=(lambda i=i: self._thumb(path, i)),
                                     detail="，".join(detail)))
        return refs

    def _thumb(self, path: Path, idx: int) -> Optional[Image.Image]:
        with zipfile.ZipFile(path) as zf:
            entries, _n = _list_entries(zf)
            e = entries[idx]
            img = _read_png(zf, e.prefix + THUMB) if e.prefix + THUMB in zf.namelist() else None
            if img is not None:
                return _thumb_to_preview(img)
            shape = _read_png(zf, e.prefix + SHAPE) if e.prefix + SHAPE in zf.namelist() else None
            return _gray_white_paint(shape) if shape is not None else None

    def read_one(self, path: Path, idx: int) -> Brush:
        path = Path(path)
        with zipfile.ZipFile(path) as zf:
            entries, set_name = _list_entries(zf)
            return _load_entry(zf, entries[idx], path, set_name)

    def read(self, path: Path) -> list[Brush]:
        path = Path(path)
        out = []
        with zipfile.ZipFile(path) as zf:
            entries, set_name = _list_entries(zf)
            for e in entries:
                try:
                    out.append(_load_entry(zf, e, path, set_name))
                except ArchiveError as exc:
                    log.warning("skip broken brush %s%s: %s", path, e.prefix, exc)
        return out

    def write(self, brushes: list[Brush], out_path: Path, **options) -> list[Path]:
        return [write_procreate(brushes, Path(out_path), set_name=options.get("set_name", "") or options.get("name", ""),
                                prefer_native=options.get("prefer_native", True))]
