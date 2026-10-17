"""Software-neutral brush model shared by every format codec and app adapter.

Conventions (every codec must convert to/from these):
  * Tip / dual-tip images are PIL images in mode "L" where 255 = full paint, 0 = no paint.
  * Texture images are mode "L" grain height maps where 255 = white (paint passes through
    fully) and 0 = black (paint blocked when depth is 100%).
  * Fractions are 0..1 (0.25 == 25%). Angles are degrees. Sizes are pixels (diameter).
"""
from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PIL import Image

#: Separator between levels of ``Brush.group`` / ``BrushRef.group`` (e.g. "毛笔 / 水彩").
GROUP_SEP = " / "


def join_group(*parts: str) -> str:
    """Build a group path from its levels, dropping empty ones."""
    return GROUP_SEP.join(p.strip() for p in parts if p and p.strip())


def split_group(group: str) -> list[str]:
    return [p for p in (group or "").split(GROUP_SEP) if p]


@dataclass
class Dynamic:
    """How inputs modulate a value. Each field is an influence strength 0..1.

    ``pressure=0.6`` means the value ranges from 40% (no pressure) to 100% (full pressure),
    i.e. Photoshop "Pen Pressure" with a 40% minimum.
    """

    pressure: float = 0.0
    tilt: float = 0.0
    velocity: float = 0.0
    jitter: float = 0.0
    # response curve for pen pressure as (input, output) points in 0..1; empty == linear
    pressure_curve: list[tuple[float, float]] = field(default_factory=list)

    def is_active(self) -> bool:
        return any(v > 0 for v in (self.pressure, self.tilt, self.velocity, self.jitter))


@dataclass
class Texture:
    image: Image.Image
    name: str = ""
    scale: float = 1.0
    depth: float = 1.0
    invert: bool = False
    mode: str = "multiply"
    brightness: float = 0.0  # -1..1
    contrast: float = 0.0  # -1..1
    per_tip: bool = True
    depth_dynamic: Dynamic = field(default_factory=Dynamic)


@dataclass
class DualTip:
    image: Image.Image
    size_px: float = 50.0
    spacing: float = 0.25
    scatter: float = 0.0
    count: int = 1
    mode: str = "multiply"
    name: str = ""


@dataclass
class ColorDynamics:
    """Colour variation.

    ``*_jitter`` are random ranges (0..1, applied in both directions). ``*_pressure`` / ``*_tilt``
    are signed shifts (hue: 1.0 == 180 degrees; saturation and brightness: 1.0 == 100%) applied at
    the lightest pressure / flattest tilt and fading to the chosen colour at full pressure / upright,
    like Procreate "Color pressure" / "Color tilt". With a pressure curve ``c`` the shift at pressure
    ``p`` is ``shift * (1 - c(p))``. They are always per tip.
    """

    fg_bg_jitter: float = 0.0
    hue_jitter: float = 0.0
    saturation_jitter: float = 0.0
    brightness_jitter: float = 0.0
    per_tip: bool = True
    # asymmetric brightness jitter (Procreate "Lightness" / "Darkness"); None == brightness_jitter
    brightness_up: Optional[float] = None
    brightness_down: Optional[float] = None
    hue_pressure: float = 0.0
    saturation_pressure: float = 0.0
    brightness_pressure: float = 0.0
    hue_tilt: float = 0.0
    saturation_tilt: float = 0.0
    brightness_tilt: float = 0.0
    pressure_curves: dict[str, list[tuple[float, float]]] = field(default_factory=dict)  # "hue"/"saturation"/...

    def has_jitter(self) -> bool:
        return any(v > 0 for v in (self.fg_bg_jitter, self.hue_jitter, self.saturation_jitter, self.brightness_jitter,
                                   self.brightness_up or 0, self.brightness_down or 0))

    def has_shift(self) -> bool:
        return any(abs(v) > 1e-4 for v in (self.hue_pressure, self.saturation_pressure, self.brightness_pressure,
                                           self.hue_tilt, self.saturation_tilt, self.brightness_tilt))

    def is_active(self) -> bool:
        return self.has_jitter() or self.has_shift()


@dataclass
class EdgeEffect:
    """Stroke edge darkening / pooling (CSP "Watercolor edge", Procreate "Wet edges" / "Burnt edges")."""

    opacity: float = 0.0  # 0..1 how much more opaque the rim is than the inside
    darken: float = 0.0  # 0..1 how much darker the rim is
    width_px: float = 0.0  # 0 == automatic (from brush size)
    blur_px: float = 0.0
    after_stroke: bool = False  # computed once the stroke is finished

    def is_active(self) -> bool:
        return self.opacity > 1e-3 or self.darken > 1e-3


@dataclass
class WetMix:
    """Painterly mixing (PS mixer brush, CSP colour mixing, Procreate wet mix, SAI blending)."""

    enabled: bool = False
    mix: float = 0.0  # how much canvas colour is picked up
    load: float = 1.0  # amount of paint
    wetness: float = 0.0
    color_stretch: float = 0.0


@dataclass
class BrushSource:
    app: str = ""  # adapter id, e.g. "photoshop"
    format: str = ""  # codec id, e.g. "abr"
    version: str = ""
    path: str = ""
    index: int = -1
    native: dict[str, Any] = field(default_factory=dict)  # codec-specific raw data for lossless round-trips


@dataclass
class Brush:
    name: str
    uid: str = field(default_factory=lambda: str(_uuid.uuid4()))

    # --- tip shape ---------------------------------------------------------------------------
    tips: list[Image.Image] = field(default_factory=list)  # empty => computed round tip
    hardness: float = 1.0  # used when tips is empty
    size_px: float = 30.0
    min_size_px: float = 1.0
    max_size_px: float = 5000.0
    spacing: float = 0.1
    angle: float = 0.0
    angle_follow_stroke: bool = False
    roundness: float = 1.0
    flip_x: bool = False
    flip_y: bool = False
    tip_order: str = "sequential"  # multi-tip brushes: sequential | random

    # --- dynamics ---------------------------------------------------------------------------
    size_dynamic: Dynamic = field(default_factory=Dynamic)
    opacity_dynamic: Dynamic = field(default_factory=Dynamic)
    flow_dynamic: Dynamic = field(default_factory=Dynamic)
    angle_jitter: float = 0.0  # fraction of 360 degrees
    roundness_jitter: float = 0.0
    scatter: float = 0.0  # fraction of diameter (1.0 == 100%)
    scatter_both_axes: bool = False
    count: int = 1
    count_jitter: float = 0.0

    # --- paint ------------------------------------------------------------------------------
    opacity: float = 1.0
    flow: float = 1.0
    blend_mode: str = "normal"
    antialias: bool = True
    stabilization: float = 0.0
    taper_start: float = 0.0
    taper_end: float = 0.0
    is_eraser: bool = False

    texture: Optional[Texture] = None
    dual: Optional[DualTip] = None
    color: ColorDynamics = field(default_factory=ColorDynamics)
    wet: WetMix = field(default_factory=WetMix)
    edge: EdgeEffect = field(default_factory=EdgeEffect)

    preview: Optional[Image.Image] = None
    group: str = ""
    source: BrushSource = field(default_factory=BrushSource)
    notes: list[str] = field(default_factory=list)  # conversion warnings shown to the user

    def warn(self, msg: str) -> None:
        if msg not in self.notes:
            self.notes.append(msg)

    def warn_extras(self, app: str, *, edge: bool = False, color_shift: bool = False) -> None:
        """Note effects that the target ``app`` cannot store."""
        if self.edge.is_active() and not edge:
            self.warn(f"{app} 没有“边缘加深/湿边/灼边”效果，已忽略。")
        if self.color.has_shift() and not color_shift:
            self.warn(f"{app} 不支持颜色随压力/倾斜变化（色相/饱和度/亮度），已忽略。")

    def main_tip(self) -> Image.Image:
        """First tip, or a rendered computed round tip."""
        if self.tips:
            return self.tips[0]
        from .imaging import round_tip

        return round_tip(max(8, min(int(round(self.size_px)), 512)), self.hardness, self.roundness, self.angle)


@dataclass
class BrushRef:
    """Lightweight list entry; the full brush is decoded on demand."""

    name: str
    key: str
    group: str = ""
    loader: Optional[Callable[[], Brush]] = None
    thumbnail: Optional[Callable[[], Optional[Image.Image]]] = None
    detail: str = ""

    def load(self) -> Brush:
        if self.loader is None:
            raise RuntimeError(f"brush {self.name!r} has no loader")
        return self.loader()
