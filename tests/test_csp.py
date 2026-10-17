"""Tests for Clip Studio Paint support (.sut codec + CSP adapter).

Real-world samples are never committed. Point BRUSH_BRIDGE_SAMPLES at a folder containing
``sample.sut`` (any sub tool exported from CSP) and/or ``tool.todb`` (a *copy* of
EditImageTool.todb) to run the sample-based tests; they are skipped otherwise.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from brush_bridge.core.model import Brush, ColorDynamics, DualTip, Dynamic, Texture, WetMix
from brush_bridge.core.plugin import AppInstall, load_plugins
from brush_bridge.formats import csp_schema as S
from brush_bridge.formats.csp import CspCodec, loses_images
from brush_bridge.formats.csp_binary import (MaterialRef, decode_effector, decode_material_refs, encode_effector,
                                             encode_material_refs, material_members)
from brush_bridge.formats.csp_material import c2f_unwrap

load_plugins()

_SESSION = Path(r"C:\Users\Persia\.copilot\session-state\dc5f260e-3678-4ee3-9b13-ee28bd47ae3a\files\work")


def _sample(*names: str) -> Path | None:
    dirs = [Path(os.environ["BRUSH_BRIDGE_SAMPLES"])] if os.environ.get("BRUSH_BRIDGE_SAMPLES") else []
    dirs += [_SESSION, Path.home() / "Downloads"]
    for d in dirs:
        for n in names:
            if (d / n).is_file():
                return d / n
    return None


SAMPLE_SUT = _sample("sample.sut", "玫瑰尖 7.sut")
SAMPLE_TODB = _sample("tool.todb", "EditImageTool.todb")


def _copy(src: Path, tmp_path: Path, name: str) -> Path:
    dst = tmp_path / name
    shutil.copyfile(src, dst)
    return dst


def _variant(path: Path) -> dict:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        node = dict(con.execute("SELECT * FROM Node").fetchone())
        return dict(con.execute("SELECT * FROM Variant WHERE VariantID=?", (node["NodeVariantID"],)).fetchone())
    finally:
        con.close()


# ---------------------------------------------------------------------------------------------
# Binary helpers
# ---------------------------------------------------------------------------------------------
def test_effector_roundtrip():
    blob = encode_effector(pressure=0.7, tilt=0.0, velocity=0.25, jitter=1.0)
    e = decode_effector(blob)
    assert e.enabled == 0x10 | 0x40 | 0x80
    assert (e.pressure_min, e.velocity_min, e.random_min) == (30, 75, 0)
    assert e.pressure_curve == [(0.0, 0.0), (1.0, 1.0)]
    assert len(blob) == 0x2C + 12 + 32
    neutral = decode_effector(encode_effector())
    assert neutral.enabled == 0 and neutral.pressure_curve == []


def test_effector_custom_curve_and_random_min():
    curve = [(0.0, 1.0), (0.5, 0.3), (1.0, 0.0)]
    e = decode_effector(encode_effector(pressure=1.0, jitter=1.0, pressure_curve=curve, random_min=-100))
    assert e.enabled == 0x10 | 0x80 and e.random_min == -100
    assert e.pressure_curve == [pytest.approx(p, abs=1e-4) for p in curve]


def test_effector_tilt_always_has_curve_block():
    # CSP crashes on import when an enabled tilt input has no curve block (word 9 = 0)
    import struct

    for kw in ({"tilt": 0.2}, {"tilt": 0.5, "jitter": 1.0}, {"pressure": 1.0, "tilt": 1.0}):
        blob = encode_effector(**kw)
        w = struct.unpack_from(">11i", blob, 0)
        if w[2] & 0x10:
            assert w[8] == 44
        assert w[9] == 44 and len(blob) == 0x2C + w[8] + w[9]
        e = decode_effector(blob)
        assert e.tilt_curve == [(0.0, 0.0), (1.0, 1.0)]
    curve = [(0.0, 1.0), (1.0, 0.2)]
    e = decode_effector(encode_effector(pressure=1.0, tilt=1.0, tilt_curve=curve))
    assert e.pressure_curve == [(0.0, 0.0), (1.0, 1.0)] and e.tilt_curve == [pytest.approx(p) for p in curve]


def test_color_channel_roundtrip():
    from brush_bridge.formats.csp import color_channel, read_color_channel

    # symmetric jitter only: two-sided random range
    amount, eff = color_channel(0.3, 0.3, 0.0)
    assert amount == pytest.approx(0.3) and decode_effector(eff).random_min == -100
    assert read_color_channel(amount, eff)[:4] == (pytest.approx(0.3), pytest.approx(0.3), 0.0, "")

    # pressure shift: reached at light pressure through an inverted curve
    amount, eff = color_channel(0.0, 0.0, -0.2, "pressure")
    e = decode_effector(eff)
    assert amount == pytest.approx(-0.2) and e.enabled & 0x10 and not e.enabled & 0x80
    assert e.pressure_curve == [(0.0, 1.0), (1.0, 0.0)]
    down, up, shift, source, curve = read_color_channel(amount, eff)
    assert (source, curve) == ("pressure", []) and shift == pytest.approx(-0.2) and down == up == pytest.approx(0)

    # shift + jitter around it, custom curve survives
    user = [(0.0, 0.0), (0.5, 0.8), (1.0, 1.0)]
    amount, eff = color_channel(0.1, 0.1, 0.3, "pressure", user)
    assert amount == pytest.approx(0.4) and decode_effector(eff).random_min == 50
    down, up, shift, source, curve = read_color_channel(amount, eff)
    assert shift == pytest.approx(0.3, abs=0.01) and down == pytest.approx(0.1, abs=0.01)
    assert curve == [pytest.approx(p, abs=1e-4) for p in user]

    # tilt keeps a direct mapping
    amount, eff = color_channel(0.0, 0.0, 0.5, "tilt")
    assert decode_effector(eff).enabled & 0x20 and read_color_channel(amount, eff)[3] == "tilt"


def test_edge_and_color_shift_columns(tmp_path):
    from brush_bridge.core.model import EdgeEffect

    b = Brush(name="效果", size_px=40,
              color=ColorDynamics(saturation_jitter=0.13, saturation_pressure=-0.13, brightness_pressure=-0.08,
                                  brightness_down=0.08, brightness_up=0.0, per_tip=True),
              edge=EdgeEffect(opacity=0.14, darken=0.27))
    out = CspCodec().write([b], tmp_path / "fx.sut")[0]
    v = _variant(out)
    assert v["BrushUseWaterEdge"] == 1 and v["BrushWaterEdgeAlphaPower"] == 14
    assert v["BrushWaterEdgeValuePower"] == 27 and v["BrushWaterEdgeRadius"] == pytest.approx(2.0)
    assert v["BrushChangePatternColor"] == 1 and v["BrushChangeStrokeColor"] == 0
    assert v["BrushSaturationChange"] == -26
    sat = decode_effector(v["BrushSaturationChangeEffector"])
    assert sat.enabled & 0x10 and sat.enabled & 0x80 and sat.random_min == 0
    assert sat.pressure_curve == [(0.0, 1.0), (1.0, 0.0)]
    assert v["BrushValueChange"] == -16

    r = CspCodec().read(out)[0]
    assert r.edge.is_active() and r.edge.darken == pytest.approx(0.27) and r.edge.opacity == pytest.approx(0.14)
    assert r.color.saturation_pressure == pytest.approx(-0.13, abs=0.01)
    assert r.color.saturation_jitter == pytest.approx(0.13, abs=0.01)
    assert r.color.brightness_pressure < 0 and not r.color.pressure_curves


def test_spray_area_and_particle_semantics(tmp_path):
    # Procreate-style "2 tips per dab, no positional jitter" must not become a sparse spray
    b = Brush(name="双印", size_px=43.4, spacing=0.07, count=2, angle_jitter=1.0)
    v = _variant(CspCodec().write([b], tmp_path / "count.sut")[0])
    assert v["BrushUseSpray"] == 0 and v["BrushSize"] == pytest.approx(43.4)
    assert v["BrushInterval"] == pytest.approx(3.5)

    # With scatter, BrushSize is the spray area and the particle size is synced to it
    b = Brush(name="散布", size_px=20, spacing=0.1, scatter=1.0, count=4)
    out = CspCodec().write([b], tmp_path / "spray.sut")[0]
    v = _variant(out)
    assert v["BrushUseSpray"] == 1 and v["BrushSize"] == pytest.approx(40)
    assert v["BrushSpraySizeSyncBrushSize"] == 1 and v["BrushSpraySize"] == pytest.approx(50)
    assert v["BrushSprayDensity"] == 4 and v["BrushInterval"] == pytest.approx(5)
    r = CspCodec().read(out)[0]
    assert r.size_px == pytest.approx(20) and r.scatter == pytest.approx(1.0) and r.spacing == pytest.approx(0.1)


def test_material_ref_roundtrip():
    refs = [MaterialRef(".:12:69:abc:data:material_0.layer", "笔尖", ".:12:69:abc", 2),
            MaterialRef(".:Paint019:def:data:material_0.layer", "Paper", ".:Install:Paint019:def", 3)]
    assert decode_material_refs(encode_material_refs(refs)) == refs
    assert decode_material_refs(b"") == [] and decode_material_refs(None) == []


# ---------------------------------------------------------------------------------------------
# Writing synthetic brushes
# ---------------------------------------------------------------------------------------------
def _synthetic() -> Brush:
    return Brush(name="合成笔刷", size_px=42, hardness=0.4, spacing=0.12, opacity=0.8, flow=0.6, roundness=0.5,
                 angle=30, angle_follow_stroke=True, angle_jitter=0.5, blend_mode="multiply",
                 size_dynamic=Dynamic(pressure=0.8), opacity_dynamic=Dynamic(pressure=0.5, tilt=0.3),
                 scatter=0.5, count=3, taper_start=0.2, taper_end=0.3, stabilization=0.15,
                 color=ColorDynamics(hue_jitter=0.5, brightness_jitter=0.2),
                 wet=WetMix(enabled=True, load=0.7, mix=0.3, wetness=0.4, color_stretch=0.2))


def test_write_read_synthetic(tmp_path):
    codec = CspCodec()
    out = codec.write([_synthetic()], tmp_path / "synthetic.sut")
    assert out == [tmp_path / "synthetic.sut"]
    con = sqlite3.connect(out[0])
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        mgr = con.execute("SELECT Version, RootUuid, MaxVariantID, CommonVariantID FROM Manager").fetchone()
        node = con.execute("SELECT NodeUuid, NodeVariantID, NodeInitVariantID, NodeInputOp FROM Node").fetchone()
        assert mgr[0] == S.DEFAULT_WRITE_VERSION and mgr[1] == node[0] and mgr[2] == 2 and mgr[3] == 44
        assert node[1:] == (1, 2, 10)
        assert con.execute("SELECT COUNT(*) FROM Variant").fetchone()[0] == 2
    finally:
        con.close()

    info = codec.sniff(out[0])
    assert info and info.format_id == "csp" and info.brush_count == 1 and info.version == str(S.DEFAULT_WRITE_VERSION)

    b = codec.read(out[0])[0]
    assert b.name == "合成笔刷"
    assert b.size_px == pytest.approx(42) and b.hardness == pytest.approx(0.4) and b.spacing == pytest.approx(0.12)
    assert b.opacity == pytest.approx(0.8) and b.flow == pytest.approx(0.6) and b.roundness == pytest.approx(0.5)
    assert b.angle == pytest.approx(30) and b.angle_follow_stroke and b.angle_jitter == pytest.approx(0.5)
    assert b.blend_mode == "multiply" and not b.is_eraser
    assert b.size_dynamic.pressure == pytest.approx(0.8)
    assert b.opacity_dynamic.pressure == pytest.approx(0.5) and b.opacity_dynamic.tilt == pytest.approx(0.3)
    assert b.scatter == pytest.approx(0.5) and b.count == 3
    assert b.taper_start == pytest.approx(0.2) and b.taper_end == pytest.approx(0.3)
    assert b.stabilization == pytest.approx(0.15)
    assert b.color.hue_jitter == pytest.approx(0.5, abs=0.01) and b.color.brightness_jitter == pytest.approx(0.2)
    assert b.wet.enabled and b.wet.load == pytest.approx(0.7) and b.wet.wetness == pytest.approx(0.4)

    # Writing an unmodified read brush again is lossless
    again = codec.write([b], tmp_path / "again.sut")[0]
    v1, v2 = _variant(out[0]), _variant(again)
    v1.pop("_PW_ID"), v2.pop("_PW_ID")
    assert v1 == v2


def test_eraser_and_custom_tip(tmp_path):
    codec = CspCodec()
    tip = Image.new("L", (32, 32), 0)
    tip.paste(255, (8, 8, 24, 24))
    brushes = [Brush(name="橡皮", is_eraser=True), Brush(name="方块", tips=[tip])]
    assert not loses_images(brushes[0]) and loses_images(brushes[1])
    files = codec.write(brushes, tmp_path / "multi.sut")
    assert [f.name for f in files] == ["橡皮.sut", "方块.sut"]
    er = codec.read(files[0])[0]
    assert er.is_eraser
    sq = codec.read(files[1])[0]
    assert len(sq.tips) == 1  # generated (unencrypted) CSP material, read back at full resolution
    assert np.array_equal(np.asarray(sq.tips[0].convert("L")), np.asarray(tip))
    v = _variant(files[1])
    assert v["BrushUsePatternImage"] == 1
    con = sqlite3.connect(files[1])
    (fd,) = con.execute("SELECT FileData FROM MaterialFile").fetchone()
    con.close()
    members = material_members(fd)
    assert {"catalog.zip", "info.zip", "data/material_0.layer", "thumbnail/thumbnail.png"} <= set(members)
    db = tmp_path / "layer.db"
    db.write_bytes(c2f_unwrap(members["data/material_0.layer"]))
    lcon = sqlite3.connect(db)
    assert lcon.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    lcon.close()


def test_generated_texture_and_dual(tmp_path):
    rng = np.random.default_rng(1)
    tex = Image.fromarray(rng.integers(0, 256, (70, 90), dtype=np.uint8))
    dual = Image.new("L", (40, 20), 0)
    dual.paste(200, (5, 5, 35, 15))
    b = Brush(name="纹理双重", tips=[dual.copy()], texture=Texture(image=tex, scale=0.5, depth=0.6, mode="subtract"),
              dual=DualTip(image=dual, size_px=12, spacing=0.3, mode="multiply"))
    f = CspCodec().write([b], tmp_path / "td.sut")[0]
    r = CspCodec().read(f)[0]
    assert r.texture is not None and r.texture.mode == "subtract"
    assert np.array_equal(np.asarray(r.texture.image.convert("L")), np.asarray(tex))
    assert r.dual is not None and np.array_equal(np.asarray(r.dual.image.convert("L")), np.asarray(dual))
    v = _variant(f)
    assert v["UseDualBrush"] == 1 and v["DualAutoIntervalType"] == 0


def test_downgrade_fix(tmp_path):
    codec = CspCodec()
    src = codec.write([_synthetic()], tmp_path / "new.sut")[0]
    con = sqlite3.connect(src)
    con.execute("ALTER TABLE Variant ADD COLUMN BrushFutureParam INTEGER DEFAULT NULL")
    con.execute("UPDATE Variant SET BrushFutureParam=7")
    con.execute("UPDATE Manager SET Version=999")
    con.commit()
    con.close()
    target = S.Schema(version=145, app_version="5.1.2", source=Path("x"),
                      variant_columns={"VariantID"} | {c for c, _t, _d in S.VARIANT_COLUMNS},
                      node_columns={c for c, _ in S.NODE_COLUMNS})
    problem = codec._problem(999, {"BrushFutureParam", "BrushSize"}, set(), target)
    assert problem and "更新" in problem and "BrushFutureParam" in problem
    fix = codec._downgrade_fix(target)
    out = fix.apply(src, tmp_path / "fixed")
    assert len(out) == 1 and out[0].exists()
    con = sqlite3.connect(out[0])
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(Variant)")} - {"_PW_ID"}
        assert "BrushFutureParam" not in cols and "BrushSize" in cols
        assert con.execute("SELECT Version FROM Manager").fetchone()[0] == 145
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()
    assert codec._problem(145, cols, set(), target) is None
    assert codec.read(out[0])[0].size_px == pytest.approx(42)


# ---------------------------------------------------------------------------------------------
# Real sample (skipped when not available)
# ---------------------------------------------------------------------------------------------
@pytest.mark.skipif(SAMPLE_SUT is None, reason="no sample .sut (set BRUSH_BRIDGE_SAMPLES)")
def test_read_sample_and_lossless_roundtrip(tmp_path):
    src = _copy(SAMPLE_SUT, tmp_path, "sample.sut")
    codec = CspCodec()
    info = codec.sniff(src)
    assert info.brush_count == 1 and int(info.version) > 0
    b = codec.read(src)[0]
    assert b.tips and b.tips[0].mode == "L"
    assert b.size_px > 0 and 0 < b.spacing < 5
    out = codec.write([b], tmp_path / "rt.sut")[0]
    v1, v2 = _variant(src), _variant(out)
    for v in (v1, v2):
        v.pop("_PW_ID"), v.pop("VariantID")
    assert v1 == v2
    con = sqlite3.connect(out)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT COUNT(*) FROM MaterialFile").fetchone()[0] >= 1
    finally:
        con.close()
    assert codec.read(out)[0].tips[0].tobytes() == b.tips[0].tobytes()


# ---------------------------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------------------------
def _inst(todb: Path | None = None, running: bool = False) -> AppInstall:
    return AppInstall(app_id="csp", name="Clip Studio Paint", version="5.1.2", exe=None, running=running,
                      extra={"todb": str(todb or ""), "material_root": str(S.material_root())})


def test_export_strategy_b(tmp_path):
    from brush_bridge.core.plugin import app

    tip = Image.new("L", (16, 16), 255)
    res = app("csp").install(_inst(), [_synthetic(), Brush(name="自定义", tips=[tip])], out_dir=tmp_path)
    assert res.ok and "子工具" in res.message
    assert sorted(p.suffix for p in res.files) == [".abr", ".sut", ".sut"]
    assert all(p.exists() for p in res.files)


def test_direct_requires_closed_app(tmp_path, monkeypatch):
    from brush_bridge.core.plugin import app

    todb = tmp_path / "EditImageTool.todb"
    todb.write_bytes(b"x")
    adapter = app("csp")
    monkeypatch.setattr(adapter, "is_running", lambda inst: True)
    res = adapter.install(_inst(todb, running=True), [_synthetic()], direct=True)
    assert not res.ok and res.needs_app_closed and "关闭" in res.message
    assert todb.read_bytes() == b"x"


def _reachable(path: Path) -> tuple[int, int]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        seen = [n.uuid for n in S.walk_tree(con)]
        total = con.execute("SELECT COUNT(*) FROM Node").fetchone()[0]
    finally:
        con.close()
    assert len(seen) == len(set(seen))
    return len(seen), total


@pytest.mark.skipif(SAMPLE_TODB is None, reason="no EditImageTool.todb copy (set BRUSH_BRIDGE_SAMPLES)")
def test_direct_install_into_copy(tmp_path, monkeypatch):
    from brush_bridge.core.plugin import app

    todb = _copy(SAMPLE_TODB, tmp_path, "EditImageTool.todb")
    adapter = app("csp")
    monkeypatch.setattr(adapter, "is_running", lambda inst: False)
    inst = _inst(todb)
    before = adapter.list_brushes(inst)
    reach_before, total_before = _reachable(todb)

    installed = before[0].load()  # an existing sub tool, re-inserted
    brushes = [_synthetic(), Brush(name="直装橡皮", is_eraser=True), installed]
    res = adapter.install(inst, brushes, direct=True)
    assert res.ok, res.message
    assert res.backup and Path(res.backup).exists()

    con = sqlite3.connect(todb)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        max_vid, max_row = con.execute("SELECT MaxVariantID, (SELECT MAX(VariantID) FROM Variant) FROM Manager").fetchone()
        assert max_vid >= max_row
    finally:
        con.close()
    reach_after, total_after = _reachable(todb)
    assert total_after - total_before in (3, 4)  # 3 sub tools (+ the new group)
    assert reach_after - reach_before == total_after - total_before

    after = adapter.list_brushes(inst)
    new = [r for r in after if r.group.endswith(" / brush-bridge")]
    assert [r.name for r in new] == ["合成笔刷", "直装橡皮", installed.name]
    b = new[0].load()
    assert b.size_px == pytest.approx(42) and b.blend_mode == "multiply"
    assert new[1].load().is_eraser

    # Second install appends to the same group
    res2 = adapter.install(inst, [Brush(name="第二支")], direct=True)
    assert res2.ok
    names = [r.name for r in adapter.list_brushes(inst) if r.group.endswith(" / brush-bridge")]
    assert names[-1] == "第二支" and len(names) == 4
