"""MyPaint codec, cross-version writing and regressions found by the corpus stress run."""
from __future__ import annotations

import io
import json
import sqlite3
import struct
import zipfile

import numpy as np
import pytest
from PIL import Image

from brush_bridge.core.model import Brush, Dynamic, Texture, WetMix
from brush_bridge.core.plugin import FileInfo, codec, load_plugins
from brush_bridge.formats import csp_schema as S
from brush_bridge.formats import krita as kr
from brush_bridge.formats import mypaint as mp
from brush_bridge.formats import sai2

load_plugins()


def _tip(n: int = 64) -> Image.Image:
    yy, xx = np.mgrid[:n, :n]
    d = np.hypot(xx - n / 2, yy - n / 2) / (n / 2)
    return Image.fromarray((np.clip(1 - d, 0, 1) * 255).astype(np.uint8), "L")


# ---------------------------------------------------------------------------------------------
# MyPaint
# ---------------------------------------------------------------------------------------------
V3 = json.dumps({
    "version": 3, "comment": "", "group": "", "description": "Pencil", "parent_brush_name": "",
    "settings": {
        "radius_logarithmic": {"base_value": 1.5, "inputs": {"pressure": [[0, -0.5], [1, 0.5]]}},
        "opaque": {"base_value": 0.8, "inputs": {}},
        "opaque_multiply": {"base_value": 0.0, "inputs": {"pressure": [[0, 0], [1, 1]]}},
        "hardness": {"base_value": 0.9, "inputs": {}},
        "dabs_per_actual_radius": {"base_value": 4.0, "inputs": {}},
        "offset_by_random": {"base_value": 0.5, "inputs": {}},
        "smudge": {"base_value": 0.6, "inputs": {}},
        "eraser": {"base_value": 0.0, "inputs": {}},
    },
})

V2 = """version 2
# a comment
radius_logarithmic 2.0
opaque 0.5 | pressure (0.000000 0.000000), (1.000000 1.000000)
hardness 0.3
dabs_per_actual_radius 2.0
eraser 1.0
"""


def test_mypaint_parse_v3_and_v2():
    ver, s, meta = mp.parse_myb(V3)
    assert ver == 3 and meta["description"] == "Pencil"
    assert s["radius_logarithmic"]["base_value"] == pytest.approx(1.5)
    assert s["radius_logarithmic"]["inputs"]["pressure"] == [(0, -0.5), (1, 0.5)]
    ver, s, _ = mp.parse_myb(V2)
    assert ver == 2 and s["opaque"]["inputs"]["pressure"][-1] == (1.0, 1.0)
    with pytest.raises(mp.MyPaintFormatError):
        mp.parse_myb("not a brush")


def test_mypaint_read_mapping(tmp_path):
    (tmp_path / "pencil.myb").write_text(V3, encoding="utf-8")
    (tmp_path / "eraser.myb").write_text(V2, encoding="utf-8")
    c = codec("mypaint")
    info = c.sniff(tmp_path / "pencil.myb")
    assert info and info.format_id == "mypaint" and info.version == "v3"
    b = c.read(tmp_path / "pencil.myb")[0]
    assert b.size_px == pytest.approx(2 * np.exp(1.5) * np.exp(0.5), rel=0.05)
    assert b.hardness == pytest.approx(0.9) and b.spacing == pytest.approx(0.125, rel=0.05)
    assert b.size_dynamic.pressure > 0 and b.opacity_dynamic.pressure > 0
    assert b.scatter > 0 and b.wet.enabled and not b.is_eraser
    e = c.read(tmp_path / "eraser.myb")[0]
    assert e.is_eraser and e.hardness == pytest.approx(0.3)
    assert c.sniff(tmp_path / "eraser.myb").version == "v2"


def test_mypaint_native_passthrough_and_versions(tmp_path):
    (tmp_path / "pencil.myb").write_text(V3, encoding="utf-8")
    c = codec("mypaint")
    b = c.read(tmp_path / "pencil.myb")[0]
    out = c.write([b], tmp_path / "out" / "again.myb")
    assert out[0].suffix == ".myb" and mp.preview_path(out[0]) in out
    assert Image.open(mp.preview_path(out[0])).size == (128, 128)
    _, s, _ = mp.parse_myb(out[0].read_text(encoding="utf-8"))
    _, s0, _ = mp.parse_myb(V3)
    for k, v in s0.items():
        assert s[k]["base_value"] == pytest.approx(v["base_value"]) and s[k]["inputs"] == v["inputs"]

    v2 = c.write([b], tmp_path / "old.myb", mypaint_version=2)[0]
    assert "\nversion 2\n" in v2.read_text(encoding="utf-8")
    assert c.write([b], tmp_path / "tv.myb", target_version="1.2.1")[0].read_text(encoding="utf-8").startswith("{")
    assert "\nversion 2\n" in c.write([b], tmp_path / "tv0.myb", target_version="1.0")[0].read_text(encoding="utf-8")
    info = c.sniff(v2)
    assert c.compatibility(info, "gimp", "2.10.34") and c.compatibility(info, "krita", "5.2")
    assert c.compatibility(c.sniff(out[0]), "gimp", "2.8") and not c.compatibility(c.sniff(out[0]), "gimp", "2.10")


def test_mypaint_from_foreign_brush(tmp_path):
    b = Brush("Foreign", size_px=30, hardness=0.5, spacing=0.25, opacity=0.7, flow=0.5,
              size_dynamic=Dynamic(pressure=1.0), scatter=0.4, angle_follow_stroke=True,
              wet=WetMix(enabled=True, load=0.5, mix=0.2))
    out = codec("mypaint").write([b], tmp_path / "f.myb")
    r = codec("mypaint").read(out[0])[0]
    assert r.size_px == pytest.approx(30, rel=0.05) and r.spacing == pytest.approx(0.25, rel=0.05)
    assert r.size_dynamic.pressure > 0.5 and r.wet.enabled and r.scatter > 0


# ---------------------------------------------------------------------------------------------
# CSP: Manager.Version / column set follows the target release
# ---------------------------------------------------------------------------------------------
def _sut_version(path):
    con = sqlite3.connect(path)
    try:
        ver = con.execute("SELECT Version FROM Manager").fetchone()[0]
        cols = {r[1] for r in con.execute("PRAGMA table_info(Variant)")}
    finally:
        con.close()
    return ver, cols


def test_csp_target_versions(tmp_path):
    assert S.DEFAULT_WRITE_VERSION == 139
    assert S.manager_version_for("1.13.2") == 138 and S.manager_version_for("2.0") == 139
    assert S.manager_version_for("5.1.2.0") >= 145 and S.manager_version_for("") is None
    c = codec("csp")
    b = Brush("v", size_px=20)
    ver, cols = _sut_version(c.write([b], tmp_path / "d.sut")[0])
    assert ver == 139 and "BrushColorMixingMode" in cols
    ver, cols = _sut_version(c.write([b], tmp_path / "old.sut", target_version="1.13")[0])
    assert ver == 138 and "BrushColorMixingMode" not in cols and "BrushLMSLinearity" not in cols
    assert codec("csp").read(tmp_path / "old.sut")[0].size_px == pytest.approx(20)


# ---------------------------------------------------------------------------------------------
# Krita 4 target
# ---------------------------------------------------------------------------------------------
def test_krita4_kpp_target(tmp_path):
    b = Brush("Tip", tips=[_tip()], size_px=40)
    k = codec("krita")
    assert kr.parse_kpp(k.write([b], tmp_path / "k5.kpp")[0].read_bytes()).version == "5.0"
    b2 = Brush("Tip", tips=[_tip()], size_px=40)
    p = kr.parse_kpp(k.write([b2], tmp_path / "k4.kpp", krita_version="4.4")[0].read_bytes())
    assert p.version == "2.2" and not p.resources
    assert any("Krita 4" in n for n in b2.notes)


def test_krita_colorsmudge_round_trip(tmp_path):
    b = Brush("Wet", size_px=30, wet=WetMix(enabled=True, load=0.4, mix=0.7))
    out = codec("krita").write([b], tmp_path / "w.bundle")
    with zipfile.ZipFile(out[0]) as z:
        kpp = next(n for n in z.namelist() if n.endswith(".kpp"))
        assert kr.parse_kpp(z.read(kpp)).paintop == "colorsmudge"
    r = codec("krita").read(out[0])[0]
    assert r.wet.enabled and r.wet.mix == pytest.approx(0.7, abs=0.05)


def test_krita_large_text_chunk(tmp_path):
    # presets with huge embedded patterns exceed Pillow's default 1 MB text limit
    noise = Image.fromarray(np.random.default_rng(1).integers(0, 256, (1200, 1200), dtype=np.uint8), "L")
    b = Brush("Big", size_px=30, texture=Texture(image=noise))
    out = codec("krita").write([b], tmp_path / "big.kpp", kpp_version="2.2")
    assert codec("krita").read(out[0])[0].texture is not None


# ---------------------------------------------------------------------------------------------
# SAI2: scattered tipless brushes use a synthetic round tip
# ---------------------------------------------------------------------------------------------
def test_sai2_scattered_round_brush_uses_brshape(tmp_path):
    b = Brush("Spray", size_px=20, hardness=0.8, scatter=0.3, spacing=0.3)
    assert sai2.choose_mode(b) == "brshape"
    res = sai2.export_brushes([b], tmp_path)
    assert ("brshape", "Spray") in res.assets and any(tmp_path.rglob("*.bmp"))


# ---------------------------------------------------------------------------------------------
# ABR 6.1 (Photoshop CS) sample layout
# ---------------------------------------------------------------------------------------------
def test_abr_61_bare_sample(tmp_path):
    from brush_bridge.formats import abr

    img = np.zeros((6, 5), np.uint8)
    img[2:4, 1:4] = 200
    uid = b"$abc"
    body = bytes([len(uid)]) + uid + b"\0" * 10 + struct.pack(">4iHB", 0, 0, 6, 5, 8, 0) + img.tobytes()
    entry = struct.pack(">I", len(body)) + body
    entry += b"\0" * (-len(entry) % 4)
    data = struct.pack(">HH", 6, 1) + b"8BIMsamp" + struct.pack(">I", len(entry)) + entry
    p = tmp_path / "cs.abr"
    p.write_bytes(data)
    af = abr.parse(p)
    got = abr.sample_image(af, uid.decode())
    assert got is not None and np.array_equal(np.asarray(got), img)


# ---------------------------------------------------------------------------------------------
# Procreate / ABR version notes
# ---------------------------------------------------------------------------------------------
def test_procreate_and_abr_compatibility_notes(tmp_path):
    pc = codec("procreate")
    out = pc.write([Brush("P", size_px=20)], tmp_path / "p.brush")[0]
    info = pc.sniff(out)
    assert pc.compatibility(info, "procreate", "4.3") and not pc.compatibility(info, "procreate", "5.2")
    abr = codec("abr")
    v10 = FileInfo(path=out, format_id="abr", format_name="ABR", version="10.0")
    assert abr.compatibility(v10, "krita", "") and not abr.compatibility(v10, "gimp", "2.10")
    assert abr.compatibility(v10, "photoshop", "18.0") and not abr.compatibility(v10, "photoshop", "25.0")
