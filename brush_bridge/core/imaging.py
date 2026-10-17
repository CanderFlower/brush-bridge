"""Image helpers shared by codecs."""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageOps


def round_tip(size: int, hardness: float = 1.0, roundness: float = 1.0, angle: float = 0.0) -> Image.Image:
    """Render a computed round/elliptical tip (mode L, 255 = paint)."""
    size = max(2, int(size))
    r = size / 2.0
    y, x = np.mgrid[0:size, 0:size].astype(np.float32) + 0.5
    x -= r
    y -= r
    a = math.radians(angle)
    xr = x * math.cos(a) + y * math.sin(a)
    yr = -x * math.sin(a) + y * math.cos(a)
    rr = max(roundness, 0.01)
    d = np.sqrt(xr ** 2 + (yr / rr) ** 2) / r
    h = min(max(hardness, 0.0), 0.999)
    v = np.clip((1.0 - d) / (1.0 - h), 0.0, 1.0)
    if hardness < 0.999:
        v = v * v * (3 - 2 * v)  # smoothstep falloff
    return Image.fromarray((v * 255).astype(np.uint8), "L")


def to_mask(img: Image.Image, *, invert_if_light_background: bool = True) -> Image.Image:
    """Convert an arbitrary image to a paint mask (L, 255 = paint).

    Uses the alpha channel when it carries information; otherwise luminance where dark = paint
    if the background is light (Photoshop/CSP style) or light = paint if the background is dark
    (Procreate style).
    """
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        alpha = np.asarray(rgba.getchannel("A"))
        if alpha.min() < 250:
            lum = np.asarray(rgba.convert("L"), dtype=np.float32)
            # dark pixels on transparent background: paint = alpha; light pixels keep alpha too
            return Image.fromarray(alpha, "L") if lum.mean() < 250 else Image.fromarray(alpha, "L")
    g = img.convert("L")
    if invert_if_light_background and background_is_light(g):
        g = ImageOps.invert(g)
    return g


def background_is_light(g: Image.Image) -> bool:
    a = np.asarray(g.convert("L"))
    border = np.concatenate([a[0, :], a[-1, :], a[:, 0], a[:, -1]])
    return float(border.mean()) > 127


def fit(img: Image.Image, max_side: int) -> Image.Image:
    if max(img.size) <= max_side:
        return img
    s = max_side / max(img.size)
    return img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)


def pad_square(img: Image.Image, fill: int = 0) -> Image.Image:
    w, h = img.size
    if w == h:
        return img
    s = max(w, h)
    out = Image.new(img.mode, (s, s), fill)
    out.paste(img, ((s - w) // 2, (s - h) // 2))
    return out


def trim(img: Image.Image, threshold: int = 1, margin: int = 1) -> Image.Image:
    a = np.asarray(img.convert("L"))
    ys, xs = np.nonzero(a >= threshold)
    if len(xs) == 0:
        return img
    x0, x1 = max(0, xs.min() - margin), min(img.width, xs.max() + 1 + margin)
    y0, y1 = max(0, ys.min() - margin), min(img.height, ys.max() + 1 + margin)
    return img.crop((x0, y0, x1, y1))


def stroke_preview(brush, width: int = 360, height: int = 72) -> Image.Image:
    """Cheap S-curve stroke preview rendered by stamping the tip. Returns RGBA, dark paint."""
    tip = brush.main_tip().convert("L")
    diam = int(height * 0.55)
    tip = tip.resize((max(1, int(diam * tip.width / max(tip.size))), max(1, int(diam * tip.height / max(tip.size)))),
                     Image.LANCZOS)
    canvas = np.zeros((height, width), np.float32)
    t_arr = np.asarray(tip, np.float32) / 255.0
    th, tw = t_arr.shape
    step = max(1.0, brush.spacing * diam if brush.spacing > 0 else diam * 0.1)
    x = diam / 2
    alpha = max(0.05, min(1.0, brush.flow * brush.opacity))
    while x < width - diam / 2:
        u = x / width
        p = math.sin(u * math.pi)  # simulated pressure
        y = height / 2 + math.sin(u * math.pi * 2) * height * 0.18
        s = 1.0 - brush.size_dynamic.pressure * (1 - p)
        if s > 0.05:
            if abs(s - 1) > 0.02:
                tt = tip.resize((max(1, int(tw * s)), max(1, int(th * s))), Image.BILINEAR)
                arr = np.asarray(tt, np.float32) / 255.0
            else:
                arr = t_arr
            h2, w2 = arr.shape
            x0, y0 = int(x - w2 / 2), int(y - h2 / 2)
            xs0, ys0 = max(0, -x0), max(0, -y0)
            xs1, ys1 = min(w2, width - x0), min(h2, height - y0)
            if xs1 > xs0 and ys1 > ys0:
                o = 1.0 - brush.opacity_dynamic.pressure * (1 - p)
                region = canvas[y0 + ys0:y0 + ys1, x0 + xs0:x0 + xs1]
                region += (1 - region) * arr[ys0:ys1, xs0:xs1] * alpha * o
        x += step * max(s, 0.2)
    a = (np.clip(canvas, 0, 1) * 255).astype(np.uint8)
    out = Image.new("RGBA", (width, height), (30, 30, 30, 0))
    out.putalpha(Image.fromarray(a, "L"))
    return out
