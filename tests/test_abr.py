import os
import struct
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from brush_bridge.core.model import Brush, DualTip, Dynamic, Texture
from brush_bridge.core.plugin import codec, sniff
from brush_bridge.formats import abr


def _tip(w=64, h=48):
    im = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(im)
    d.ellipse((4, 4, w - 5, h - 5), fill=200)
    d.rectangle((10, 10, 20, 30), fill=255)
    return im


def _brush(name="测试笔刷"):
    tex = Image.fromarray((np.random.default_rng(1).random((40, 50)) * 255).astype(np.uint8), "L")
    return Brush(
        name=name, tips=[_tip()], size_px=80, spacing=0.12, angle=30, roundness=0.8,
        size_dynamic=Dynamic(pressure=0.75, jitter=0.1), opacity_dynamic=Dynamic(pressure=0.5),
        scatter=1.5, count=3, opacity=0.9, flow=0.7, blend_mode="multiply",
        texture=Texture(image=tex, name="纸纹", scale=0.6, depth=0.4, invert=True, mode="subtract"),
        dual=DualTip(image=_tip(30, 30), size_px=40, spacing=0.3, scatter=0.2, count=2, mode="darken"),
    )


def test_packbits_roundtrip():
    rng = np.random.default_rng(0)
    for row in (b"", b"\0" * 300, bytes(rng.integers(0, 4, 513, dtype=np.uint8)), bytes(range(256)) * 3):
        enc = abr.packbits_encode_row(row)
        assert abr.packbits_decode(enc, len(row)) == row


def test_write_read_roundtrip(tmp_path):
    out = tmp_path / "x.abr"
    c = codec("abr")
    b = _brush()
    c.write([b, Brush(name="computed", size_px=25, hardness=0.5)], out, prefer_native=False)
    info = sniff(out)
    assert info.format_id == "abr" and info.version == "6.2" and not info.problems
    r = c.read(out)
    assert [x.name for x in r] == ["测试笔刷", "computed"]
    x = r[0]
    assert np.array_equal(np.asarray(x.tips[0]), np.asarray(b.tips[0]))
    assert x.size_px == 80 and abs(x.spacing - 0.12) < 1e-9 and x.angle == 30 and abs(x.roundness - 0.8) < 1e-9
    assert abs(x.size_dynamic.pressure - 0.75) < 1e-6 and abs(x.size_dynamic.jitter - 0.1) < 1e-6
    assert abs(x.opacity_dynamic.pressure - 0.5) < 1e-6
    assert x.count == 3 and abs(x.scatter - 1.5) < 1e-9
    assert x.blend_mode == "multiply" and abs(x.opacity - 0.9) < 1e-9 and abs(x.flow - 0.7) < 1e-9
    assert np.array_equal(np.asarray(x.texture.image), np.asarray(b.texture.image))
    assert x.texture.invert and x.texture.mode == "subtract" and abs(x.texture.scale - 0.6) < 1e-9
    assert x.dual is not None and x.dual.mode == "darken" and x.dual.count == 2
    assert np.array_equal(np.asarray(x.dual.image), np.asarray(b.dual.image))
    assert r[1].tips == [] and abs(r[1].hardness - 0.5) < 1e-9


def test_native_passthrough_is_lossless(tmp_path):
    c = codec("abr")
    a = tmp_path / "a.abr"
    c.write([_brush("一"), _brush("二")], a, prefer_native=False)
    second = tmp_path / "b.abr"
    c.write(c.read(a)[::-1], second)  # re-export uses the raw bytes
    pa, pb = abr.parse(a), abr.parse(second)
    assert [pa.raw(s, e) for _, s, e in pa.presets][::-1] == [pb.raw(s, e) for _, s, e in pb.presets]
    assert set(pa.samples) == set(pb.samples) and set(pa.patterns) == set(pb.patterns)
    pa.close()
    pb.close()


def test_v10_detection_and_fix(tmp_path):
    src = tmp_path / "new.abr"
    codec("abr").write([_brush()], src, prefer_native=False)
    data = bytearray(src.read_bytes())
    data[0:4] = struct.pack(">HH", 10, 2)
    src.write_bytes(bytes(data))
    info = sniff(src)
    assert info.version == "10.2" and info.problems and info.fixes
    assert codec("abr").compatibility(info, "csp", "5.1.2")
    files = info.fixes[0].apply(src, tmp_path / "out")
    assert sniff(files[0]).version == "6.2"
    assert [b.name for b in codec("abr").read(files[0])] == ["测试笔刷"]


def test_legacy_v2_sampled(tmp_path):
    tip = np.asarray(_tip(20, 10))
    body = bytearray()
    body += struct.pack(">IH", 0, 25)  # misc, spacing
    name = "Old\0"
    body += struct.pack(">I", len(name)) + name.encode("utf-16-be")
    body += b"\x01" + struct.pack(">4h", 0, 0, 10, 20) + struct.pack(">4i", 0, 0, 10, 20)
    body += struct.pack(">HB", 8, 0) + tip.tobytes()
    data = struct.pack(">HH", 2, 1) + struct.pack(">HI", 2, len(body)) + bytes(body)
    p = tmp_path / "old.abr"
    p.write_bytes(data)
    assert sniff(p).version == "2"
    (b,) = codec("abr").read(p)
    assert b.name == "Old" and np.array_equal(np.asarray(b.tips[0]), tip) and b.spacing == 0.25


PSP = Path(os.path.expandvars(r"%APPDATA%\Adobe\Adobe Photoshop 2021\Adobe Photoshop 2021 Settings\Brushes.psp"))


@pytest.mark.skipif(not PSP.exists(), reason="no local Photoshop 2021 library")
def test_local_photoshop_library(tmp_path):
    refs = codec("abr").list_refs(PSP)
    assert refs
    b = refs[min(10, len(refs) - 1)].load()
    assert b.name
    out = abr.convert_to_v62(PSP, tmp_path / "subset.abr", [0, 1])
    assert sniff(out).version == "6.2" and len(codec("abr").read(out)) == 2
