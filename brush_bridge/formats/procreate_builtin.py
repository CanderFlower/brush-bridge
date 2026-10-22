"""Procedural stand-ins for Procreate's built-in shape and grain images.

Brushes that use Procreate's bundled library (``bundledShapePath`` / ``bundledGrainPath``) do not carry
the image itself, and those images cannot be redistributed. To keep the character of such brushes when
converting, we synthesise an image of the same kind from keywords in the file name: canvas weave,
mottled acrylic, rust blotches, splatters, charcoal grain, wood, flat bristle stamps, leaves, ink blots…
All images are white = paint (``L`` mode); grains tile seamlessly.

Users who have the original images (e.g. exported from their own Procreate) can drop them into a
"Procreate library" folder; :func:`library_image` finds them by file name and they are used instead.
Searched folders: ``$BRUSH_BRIDGE_PROCREATE_LIBRARY`` (``os.pathsep``-separated),
``%LOCALAPPDATA%\\BrushBridge\\procreate_library`` and ``procreate_library`` next to the program.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageFilter

SHAPE_SIZE = 512
GRAIN_SIZE = 512
LIBRARY_ENV = "BRUSH_BRIDGE_PROCREATE_LIBRARY"
_IMAGE_EXTS = {".png", ".jpg", ".jpeg"}


def library_dirs() -> list[Path]:
    """Folders searched for user-supplied Procreate built-in images (existing ones only)."""
    dirs = [Path(x) for x in os.environ.get(LIBRARY_ENV, "").split(os.pathsep) if x.strip()]
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    dirs.append(Path(local) / "BrushBridge" / "procreate_library")
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    dirs.append(base / "procreate_library")
    return [d for d in dirs if d.is_dir()]


def default_library_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "BrushBridge" / "procreate_library"


_INDEX_CACHE: dict[tuple, dict[str, Path]] = {}


def _library_index() -> dict[str, Path]:
    dirs = library_dirs()
    key = tuple((str(d), d.stat().st_mtime_ns) for d in dirs)
    if key in _INDEX_CACHE:
        return _INDEX_CACHE[key]
    index: dict[str, Path] = {}
    for d in dirs:
        try:
            files = sorted(d.rglob("*"))
        except OSError:
            continue
        for f in files:
            if f.suffix.lower() in _IMAGE_EXTS and f.is_file():
                index.setdefault(f.name.lower(), f)
                index.setdefault(f.stem.lower(), f)
    _INDEX_CACHE.clear()
    _INDEX_CACHE[key] = index
    return index


def library_has(name: str) -> bool:
    base = os.path.basename((name or "").replace("\\", "/")).strip().lower()
    index = _library_index() if base else {}
    return base in index or os.path.splitext(base)[0] in index


def library_image(name: str) -> Optional[Image.Image]:
    """The user's copy of a Procreate built-in image (``Brush-Preset-Ink.jpg``…), or None."""
    base = os.path.basename((name or "").replace("\\", "/")).strip().lower()
    if not base:
        return None
    index = _library_index()
    path = index.get(base) or index.get(os.path.splitext(base)[0])
    if path is None:
        return None
    try:
        with Image.open(path) as im:
            im.load()
            return im.copy()
    except OSError:
        return None


def _rng(name: str) -> np.random.Generator:
    return np.random.default_rng(int.from_bytes(hashlib.sha1(name.encode("utf-8")).digest()[:8], "little"))


def _norm(a: np.ndarray) -> np.ndarray:
    lo, hi = float(a.min()), float(a.max())
    return (a - lo) / (hi - lo) if hi > lo else np.zeros_like(a)


def _fbm(rng: np.random.Generator, n: int, beta: float = 2.0, lo: float = 1.0, hi: Optional[float] = None) -> np.ndarray:
    """Tileable 1/f^beta noise in 0..1 (filtered white noise, so it wraps around seamlessly)."""
    f = np.fft.fftfreq(n) * n
    r = np.hypot(*np.meshgrid(f, f))
    amp = np.zeros_like(r)
    band = (r >= lo) & ((r <= hi) if hi else True)
    amp[band] = r[band] ** (-beta / 2)
    spec = np.fft.fft2(rng.standard_normal((n, n))) * amp
    return _norm(np.real(np.fft.ifft2(spec)))


def _img(a: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(a * 255.0, 0, 255).astype(np.uint8), "L")


def _grid(n: int) -> tuple[np.ndarray, np.ndarray]:
    """Centred coordinates in -1..1."""
    c = (np.arange(n) + 0.5) / n * 2 - 1
    return np.meshgrid(c, c)


# ---------------------------------------------------------------------------------------------
# grains (tileable textures)
# ---------------------------------------------------------------------------------------------
def _canvas(rng, n):
    x = np.arange(n)
    period = n // 64
    wx = 0.5 + 0.5 * np.sin(2 * np.pi * x / period)
    weave = np.add.outer(wx, wx) / 2
    distress = _fbm(rng, n, 2.2)
    fine = _fbm(rng, n, 0.6, lo=40)
    return _norm(weave * 0.55 + distress * 0.3 + fine * 0.15)


def _mottled(rng, n):
    return _norm(_fbm(rng, n, 2.6) * 0.7 + _fbm(rng, n, 1.2, lo=12) * 0.3)


def _rust(rng, n):
    base = _fbm(rng, n, 2.4)
    blotch = 1 / (1 + np.exp(-(base - 0.5) * 14))
    return _norm(blotch * 0.75 + _fbm(rng, n, 0.8, lo=30) * 0.25)


def _splatter_field(rng, n, count, rmin, rmax, soft=0.15):
    out = np.zeros((n, n))
    for _ in range(count):
        cx, cy = rng.uniform(0, n, 2)
        r = max(rng.uniform(rmin, rmax) * (rng.random() ** 2 * 0.8 + 0.2), 0.5)
        h = int(np.ceil(r)) + 1
        ix = np.arange(int(cx) - h, int(cx) + h + 1)
        iy = np.arange(int(cy) - h, int(cy) + h + 1)
        d = np.hypot(*np.meshgrid(ix + 0.5 - cx, iy + 0.5 - cy)) / r
        patch = np.clip((1 - d) / soft, 0, 1)
        rows, cols = np.ix_(iy % n, ix % n)  # wrap -> tileable
        out[rows, cols] = np.maximum(out[rows, cols], patch)
    return out


def _splatters(rng, n):
    dots = _splatter_field(rng, n, 260, 1.5, n * 0.05)
    return _norm(1 - dots * 0.9 + _fbm(rng, n, 1.0, lo=20) * 0.1)


def _spray(rng, n):
    return _norm(1 - _splatter_field(rng, n, 900, 0.8, 3.5, soft=0.3) + _fbm(rng, n, 2.0) * 0.4)


def _charcoal(rng, n):
    paper = _fbm(rng, n, 0.9, lo=25)
    tooth = 1 / (1 + np.exp(-(paper - 0.55) * 12))
    return _norm(tooth * 0.75 + _fbm(rng, n, 2.0) * 0.25)


def _paper(rng, n):
    fibres = _fbm(rng, n, 1.4, lo=6)
    return _norm(fibres * 0.6 + _fbm(rng, n, 2.4) * 0.4)


def _clouds(rng, n):
    return _fbm(rng, n, 3.0)


def _stripes(rng, n, freq, wobble, sharp):
    yy, xx = np.mgrid[0:n, 0:n] / n
    warp = (_fbm(rng, n, 3.0) - 0.5) * wobble
    s = 0.5 + 0.5 * np.sin(2 * np.pi * (freq * yy + warp))
    return _norm(s ** sharp * 0.8 + _fbm(rng, n, 1.0, lo=20) * 0.2)


def _wood(rng, n):
    return _stripes(rng, n, 18, 3.0, 3.0)


def _corrugated(rng, n):
    return _stripes(rng, n, 24, 0.2, 1.0)


def _ocean(rng, n):
    return _stripes(rng, n, 10, 2.0, 2.0)


def _streaks(rng, n):
    a = rng.standard_normal((1, n)).repeat(n, axis=0)
    img = Image.fromarray(np.uint8(_norm(a) * 255)).filter(ImageFilter.GaussianBlur(1.2))
    return _norm(np.asarray(img, dtype=np.float64) / 255 * 0.7 + _fbm(rng, n, 2.0) * 0.3)


def _grunge(rng, n):
    return _norm(_rust(rng, n) * 0.5 + _splatters(rng, n) * 0.3 + _charcoal(rng, n) * 0.2)


_GRAINS: tuple[tuple[tuple[str, ...], Callable], ...] = (
    (("canvas", "linen", "burlap", "weave"), _canvas),
    (("ocean", "wave", "water"), _ocean),
    (("wood", "bonobo", "fur", "hair"), _wood),
    (("corrugat", "roll", "ribbed"), _corrugated),
    (("cloud", "nebula", "neblua", "smoke"), _clouds),
    (("rust", "oxid"), _rust),
    (("splatter", "splat"), _splatters),
    (("spray", "aero"), _spray),
    (("charcoal", "chalk", "pastel", "crayon", "pencil", "graphite"), _charcoal),
    (("acrylic", "oil", "gouache", "mix"), _mottled),
    (("paper", "mush", "pulp"), _paper),
    (("streak", "dry", "bristle"), _streaks),
    (("grunge", "noise", "ink", "concrete", "rough"), _grunge),
)


def builtin_grain(path: str) -> Image.Image:
    low = path.lower()
    rng = _rng("grain:" + low)
    fn = next((f for keys, f in _GRAINS if any(k in low for k in keys)), _grunge)
    return _img(fn(rng, GRAIN_SIZE))


# ---------------------------------------------------------------------------------------------
# shapes (single stamps, white on black)
# ---------------------------------------------------------------------------------------------
def _round(rng, n, hardness=1.0, roundness=1.0, rough=0.0):
    x, y = _grid(n)
    d = np.hypot(x, y / max(roundness, 0.05))
    if rough:
        ang = np.arctan2(y, x)
        k = np.arange(1, 13)
        ph = rng.uniform(0, 2 * np.pi, k.size)
        am = rng.normal(0, 1, k.size) / k
        edge = 1 + rough * np.tensordot(am, np.cos(np.multiply.outer(k, ang) + ph[:, None, None]), axes=1)
        d = d / np.clip(edge, 0.3, None)
    r0 = 0.9
    inner = r0 * hardness
    return np.clip((r0 - d) / max(r0 - inner, 0.02), 0, 1)


def _ink(rng, n):
    blob = _round(rng, n, 0.92, 1.0, rough=0.25)
    return np.clip(blob * (0.85 + 0.15 * _fbm(rng, n, 2.0)), 0, 1)


def _ink_sponge(rng, n):
    holes = _fbm(rng, n, 1.6, lo=6) > 0.42
    return _round(rng, n, 0.8, 1.0, rough=0.3) * holes


def _flat(rng, n, roundness=0.35):
    x, y = _grid(n)
    body = np.clip((1 - np.abs(x) / 0.88) / 0.08, 0, 1) * np.clip((1 - np.abs(y) / roundness) / 0.1, 0, 1)
    bristle = rng.random(n)
    bristle = np.asarray(Image.fromarray(np.uint8(bristle[:, None].repeat(n, 1).T * 255))
                         .filter(ImageFilter.GaussianBlur(0.8)), dtype=np.float64) / 255
    return np.clip(body * (0.55 + 0.6 * _norm(bristle)), 0, 1)


def _dab(rng, n):
    blob = _round(rng, n, 0.6, 0.85, rough=0.35)
    x, _y = _grid(n)
    streak = _norm(np.sin(x * 60 + _fbm(rng, n, 2.5) * 6))
    return np.clip(blob * (0.6 + 0.4 * streak), 0, 1)


def _leaf(rng, n):
    x, y = _grid(n)
    w = 0.42 * np.clip(1 - (y / 0.92) ** 2, 0, 1) ** 0.75 * (1 + 0.15 * y)
    return np.clip((w - np.abs(x)) / 0.02, 0, 1) * (np.abs(y) < 0.92)


def _scatter(rng, n, count, rmin, rmax):
    x, y = _grid(n)
    out = np.zeros((n, n))
    for _ in range(count):
        cx, cy = rng.normal(0, 0.35, 2).clip(-0.85, 0.85)
        r = rng.uniform(rmin, rmax) * (rng.random() ** 1.5 + 0.15)
        out = np.maximum(out, np.clip((r - np.hypot(x - cx, y - cy)) / max(r * 0.25, 0.004), 0, 1))
    return out


def _burst(rng, n):
    x, y = _grid(n)
    ang, d = np.arctan2(y, x), np.hypot(x, y)
    rays = np.zeros((n, n))
    for a in rng.uniform(-np.pi, np.pi, 28):
        diff = np.abs((ang - a + np.pi) % (2 * np.pi) - np.pi)
        length = rng.uniform(0.4, 0.95)
        rays = np.maximum(rays, np.clip(1 - diff / 0.03, 0, 1) * (d < length))
    return rays


def _rain(rng, n):
    x, y = _grid(n)
    out = np.zeros((n, n))
    for _ in range(40):
        cx, cy = rng.uniform(-0.85, 0.85, 2)
        ln = rng.uniform(0.1, 0.4)
        out = np.maximum(out, np.clip(1 - np.abs(x - cx) / 0.008, 0, 1) * (np.abs(y - cy) < ln / 2))
    return out


def _charcoal_block(rng, n):
    x, y = _grid(n)
    body = np.clip((0.85 - np.abs(x)) / 0.1, 0, 1) * np.clip((0.55 - np.abs(y)) / 0.1, 0, 1)
    return body * (_fbm(rng, n, 0.9, lo=20) > 0.4)


def _streaky(rng, n):
    return _round(rng, n, 0.7) * _streaks(rng, n)


def _crayon(rng, n):
    return _round(rng, n, 0.85, rough=0.1) * (_charcoal(rng, n) > 0.35)


def _nebula(rng, n):
    return _round(rng, n, 0.0) * _fbm(rng, n, 2.8)


_SHAPES: tuple[tuple[tuple[str, ...], Callable], ...] = (
    (("leaf",), _leaf),
    (("flat",), _flat),
    (("dab", "acrylic", "gouache-dab"), _dab),
    (("charcoal-block", "block"), _charcoal_block),
    (("sponge",), _ink_sponge),
    (("splatter", "dots", "spark"), lambda r, n: _scatter(r, n, 60, 0.02, 0.12)),
    (("burst", "star"), _burst),
    (("rain",), _rain),
    (("nebula", "neblua", "cloud"), _nebula),
    (("crayon", "chalk", "pastel"), _crayon),
    (("streak", "bristle"), _streaky),
    (("ink", "bleed", "blot"), _ink),
    (("ultra-soft", "soft", "airbrush"), lambda r, n: _round(r, n, 0.0)),
    (("oval",), lambda r, n: _round(r, n, 1.0, 0.55)),
    (("medium",), lambda r, n: _round(r, n, 0.5)),
)


def builtin_shape(path: str) -> Optional[Image.Image]:
    """Approximate stamp image, or ``None`` when a plain computed round tip is as good."""
    low = path.lower()
    rng = _rng("shape:" + low)
    fn = next((f for keys, f in _SHAPES if any(k in low for k in keys)), None)
    if fn is None:
        return None
    return _img(fn(rng, SHAPE_SIZE))
