"""Generate the app icon (brush stroke arching like a bridge between two colour dots).

Writes brush_bridge/gui/icon.png and packaging/icon.ico. Run: python scripts/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
S = 512


def make() -> Image.Image:
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((16, 16, S - 16, S - 16), radius=110, fill=(36, 44, 72, 255))
    stroke = Image.new("L", (S, S), 0)
    sd = ImageDraw.Draw(stroke)
    # arch drawn as many overlapping dabs whose size tapers at the ends
    import math

    for i in range(400):
        t = i / 399
        x = 96 + t * (S - 192)
        y = 360 - math.sin(t * math.pi) * 210
        r = 10 + 26 * math.sin(t * math.pi) ** 0.6
        sd.ellipse((x - r, y - r, x + r, y + r), fill=255)
    stroke = stroke.filter(ImageFilter.GaussianBlur(1.2))
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for x in range(S):
        t = x / (S - 1)
        c = (int(255 * (1 - t) + 80 * t), int(120 * (1 - t) + 200 * t), int(90 * (1 - t) + 255 * t), 255)
        gd.line((x, 0, x, S), fill=c)
    im.paste(grad, (0, 0), stroke)
    for cx, col in ((96, (255, 120, 90, 255)), (S - 96, (80, 200, 255, 255))):
        d.ellipse((cx - 44, 360 - 44 + 40, cx + 44, 360 + 44 + 40), fill=col)
    return im


if __name__ == "__main__":
    img = make()
    (ROOT / "brush_bridge" / "gui").mkdir(parents=True, exist_ok=True)
    img.resize((256, 256), Image.LANCZOS).save(ROOT / "brush_bridge" / "gui" / "icon.png")
    (ROOT / "packaging").mkdir(exist_ok=True)
    img.save(ROOT / "packaging" / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icon written")
