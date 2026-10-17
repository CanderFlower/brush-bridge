"""Krita (.bundle/.kpp) and GIMP (.gbr/.gih/.vbr) codec and app adapter tests.

Real-file tests use reference files downloaded by ``scripts/fetch_samples.py`` (sections
"krita" and "gimp") into ``tests/fixtures/downloaded/`` (git-ignored, never committed); they are
skipped when offline."""
import hashlib
import importlib.util
import io
import re
import sqlite3
import struct
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from brush_bridge.core.model import Brush, ColorDynamics, DualTip, Dynamic, Texture
from brush_bridge.core.plugin import AppInstall, app, codec, sniff
from brush_bridge.formats import gimp as gp
from brush_bridge.formats import krita as kr

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}"
KNOWN_SENSORS = {"pressure", "pressurein", "xtilt", "ytilt", "tiltdirection", "tiltelevation", "speed",
                 "drawingangle", "rotation", "distance", "time", "fuzzy", "fuzzystroke", "fade", "perspective",
                 "tangentialpressure", "sensorslist"}


# ---------------------------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------------------------
def _tip(w=90, h=60):
    im = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(im)
    d.ellipse((w // 20, h // 20, w - 1 - w // 20, h - 1 - h // 20), fill=180)
    d.rectangle((w // 4, h // 4, w // 2, 3 * h // 4), fill=255)
    return im


def _grain(w=128, h=96):
    rng = np.random.default_rng(3)
    return Image.fromarray((rng.random((h, w)) * 255).astype(np.uint8), "L")


def _rich_brush(name="水彩 测试"):
    return Brush(
        name=name, tips=[_tip()], size_px=120, spacing=0.15, angle=30, roundness=1.0,
        size_dynamic=Dynamic(pressure=0.7), opacity_dynamic=Dynamic(pressure=0.5),
        opacity=0.8, flow=0.6, angle_jitter=0.25, scatter=1.5, blend_mode="multiply",
        texture=Texture(image=_grain(), name="纸纹", scale=0.5, depth=0.7, invert=True, mode="subtract"),
        dual=DualTip(image=_tip(40, 40), size_px=25, mode="darken"),
        color=ColorDynamics(hue_jitter=0.1),
    )


def _load_fetcher():
    spec = importlib.util.spec_from_file_location("fetch_samples", ROOT / "scripts" / "fetch_samples.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def krita_samples():
    fs = _load_fetcher()
    paths = {p.name: p for p in fs.fetch_krita()}
    if not paths:
        pytest.skip("Krita reference files unavailable (offline?)")
    return paths


@pytest.fixture(scope="session")
def gimp_samples():
    fs = _load_fetcher()
    paths = {p.name: p for p in fs.fetch_gimp()}
    if not paths:
        pytest.skip("GIMP reference brushes unavailable (offline?)")
    return paths


def _need(samples, name):
    if name not in samples:
        pytest.skip(f"{name} not downloaded")
    return samples[name]


def _mask_diff(a: Image.Image, b: Image.Image) -> int:
    return int(np.abs(np.asarray(a.convert("L"), int) - np.asarray(b.convert("L"), int)).max())


# =============================================================================================
# GIMP: binary format
# =============================================================================================
def test_gbr_v2_binary_layout_matches_spec():
    tip = _tip(7, 5)
    data = gp.encode_gbr(tip, "Brush ü", spacing=37)
    name = "Brush ü".encode("utf-8") + b"\0"
    header_size, version, w, h, nbytes, magic, spacing = struct.unpack(">7I", data[:28])
    assert (version, w, h, nbytes, spacing) == (2, 7, 5, 1, 37)
    assert magic == 0x47494D50 and data[20:24] == b"GIMP"
    assert header_size == 28 + len(name)
    assert data[28:header_size] == name
    # GIMP brush masks: one byte per pixel, row-major, 255 = full paint (no inversion)
    assert data[header_size:] == tip.tobytes()
    b, end = gp.parse_gbr(data)
    assert end == len(data) and b.name == "Brush ü" and b.spacing == 37 and _mask_diff(b.mask, tip) == 0


def test_gbr_v1_and_cinepaint_v3_are_read_and_fixable(tmp_path):
    tip = _tip(6, 4)
    name = b"old\0"
    v1 = struct.pack(">5I", 20 + len(name), 1, 6, 4, 1) + name + tip.tobytes()
    p1 = tmp_path / "old.gbr"
    p1.write_bytes(v1)
    info = sniff(p1)
    assert info.format_id == "gimp" and info.version == "GBR v1"
    assert info.fixes and _mask_diff(codec("gimp").read(p1)[0].main_tip(), tip) == 0
    fixed = info.fixes[0].apply(p1, tmp_path / "fixed")
    assert sniff(fixed[0]).version == "GBR v2"

    # CinePaint: 16-bit "half" values = upper 16 bits of float32
    f = (np.asarray(tip, np.float32) / 255.0).astype(">f4").view(">u4") >> 16
    v3 = struct.pack(">7I", 28 + len(name), 3, 6, 4, 18, 0x47494D50, 25) + name + f.astype(">u2").tobytes()
    p3 = tmp_path / "cine.gbr"
    p3.write_bytes(v3)
    info = sniff(p3)
    assert info.version == "GBR v3" and info.problems
    assert _mask_diff(codec("gimp").read(p3)[0].main_tip(), tip) <= 2
    assert "Krita" in codec("gimp").compatibility(info, "krita", "5.2")


def test_v1_without_gbr_extension_is_not_claimed(tmp_path):
    p = tmp_path / "random.bin"
    p.write_bytes(struct.pack(">5I", 24, 1, 2, 2, 1) + b"abc\0" + b"\0" * 4)
    info = codec("gimp").sniff(p)
    assert info is None


def test_gih_round_trip_and_header():
    tips = [_tip(30, 20), _tip(20, 30), _tip(25, 25)]
    data = gp.encode_gih(tips, "管道", spacing=40, selection="random")
    lines = data.split(b"\n", 2)
    assert lines[0].decode("utf-8") == "管道"
    assert lines[1].startswith(b"3 ") and b"ncells:3" in lines[1] and b"sel0:random" in lines[1]
    name, params, cells = gp.parse_gih(data)
    assert name == "管道" and params["sel0"] == "random" and len(cells) == 3
    for c, t in zip(cells, tips):
        assert c.spacing == 40 and _mask_diff(c.mask, t) == 0


def test_vbr_text_round_trip_and_render():
    v = gp.VbrBrush(name="Diamond", version="1.5", shape="diamond", spacing=15, radius=20, spikes=4,
                    hardness=0.5, aspect=2.0, angle=30)
    text = gp.encode_vbr(v).decode("utf-8")
    assert text.startswith("GIMP-VBR\n1.5\nDiamond\n")
    back = gp.parse_vbr(text.encode())
    assert (back.shape, back.spikes, back.radius, back.hardness, back.aspect, back.angle) == \
           ("diamond", 4, 20, 0.5, 2.0, 30)
    img = gp.render_vbr(back)
    assert img.mode == "L" and max(img.size) <= 41 and np.asarray(img).max() == 255
    v10 = gp.parse_vbr(b"GIMP-VBR\n1.0\nOld\n25.0\n10.0\n0.75\n1.0\n0.0\n")
    assert v10.version == "1.0" and v10.shape == "circle" and v10.radius == 10 and v10.hardness == 0.75


# =============================================================================================
# GIMP: codec
# =============================================================================================
def test_gimp_codec_round_trips(tmp_path):
    g = codec("gimp")
    single = Brush("Brush A", tips=[_tip()], size_px=90, spacing=0.2)
    multi = Brush("Multi", tips=[_tip(), _tip(60, 90)], tip_order="random", size_px=90, spacing=0.3)
    computed = Brush("Round", size_px=40, hardness=0.5, roundness=0.5, angle=30)

    out = g.write([single], tmp_path / "a.gbr")
    assert out == [tmp_path / "a.gbr"] and sniff(out[0]).version == "GBR v2"
    r = g.read(out[0])[0]
    assert r.name == "Brush A" and abs(r.spacing - 0.2) < 1e-6 and _mask_diff(r.main_tip(), _tip()) == 0

    out = g.write([multi], tmp_path / "m.gih")
    r = g.read(out[0])[0]
    assert out[0].suffix == ".gih" and len(r.tips) == 2 and r.tip_order == "random"
    assert abs(r.spacing - 0.3) < 1e-6

    out = g.write([computed], tmp_path / "r.vbr")
    r = g.read(out[0])[0]
    info = sniff(out[0])
    assert info.version == "VBR 1.5" and not r.tips
    assert (r.size_px, r.hardness, r.roundness, r.angle) == (40, 0.5, 0.5, 30)
    assert any(f.title == "转换为 GBR" for f in info.fixes)
    assert "Krita" in g.compatibility(info, "krita", "5.2")

    out = g.write([single, multi, computed], tmp_path / "set" / "x.gbr")
    assert [p.name for p in out] == ["Brush A.gbr", "Multi.gih", "Round.gbr"]
    assert all(sniff(p) is not None for p in out)


def test_gimp_writer_resamples_tips_to_brush_size(tmp_path):
    b = Brush("Big", tips=[_tip(400, 200)], size_px=100)
    out = codec("gimp").write([b], tmp_path / "big.gbr")
    assert max(gp.parse_gbr(out[0].read_bytes())[0].size) == 100
    out = codec("gimp").write([b], tmp_path / "raw.gbr", resample=False)
    assert gp.parse_gbr(out[0].read_bytes())[0].size == (400, 200)


def test_gimp_real_brushes(gimp_samples):
    g = codec("gimp")
    expect = {"Bristles-01.gbr": "GBR v2", "Smoke.gbr": "GBR v2", "pixel.gbr": "GBR v2", "pepper.gbr": "GBR v2",
              "confetti.gih": "GIH", "Pencil-03.gih": "GIH", "Hardness-075.vbr": "VBR 1.0", "Star.vbr": "VBR 1.5"}
    for fname, version in expect.items():
        p = _need(gimp_samples, fname)
        info = sniff(p)
        assert info and info.format_id == "gimp" and info.version == version, fname
        bs = g.read(p)
        assert len(bs) == 1 and bs[0].name
    assert len(g.read(gimp_samples["confetti.gih"])[0].tips) == 10
    assert g.read(gimp_samples["Hardness-075.vbr"])[0].hardness == 0.75
    pepper = g.read(gimp_samples["pepper.gbr"])[0]
    assert pepper.notes  # colour brush -> shape only, with a warning
    # re-encode a real brush and compare the bitmaps
    smoke = g.read(gimp_samples["Smoke.gbr"])[0]
    data = gp.encode_gbr(smoke.main_tip(), smoke.name, 50)
    assert data == gimp_samples["Smoke.gbr"].read_bytes()


# =============================================================================================
# Krita: preset/bundle reading
# =============================================================================================
def test_krita_reads_reference_presets(krita_samples):
    k = codec("krita")
    p = kr.parse_kpp(_need(krita_samples, "b)_Basic-5_Size_default.kpp").read_bytes())
    assert p.paintop == "paintbrush" and p.version == "2.2" and "brush_definition" in p.params
    b = k.read(krita_samples["b)_Basic-5_Size_default.kpp"])[0]
    assert b.size_px == 40 and b.size_dynamic.pressure > 0.9 and not b.tips

    eraser = k.read(_need(krita_samples, "a)_Eraser_Circle.kpp"))[0]
    assert eraser.is_eraser

    info = sniff(_need(krita_samples, "test-embedded-resources-5.0.kpp"))
    assert info.format_id == "krita" and info.version == "kpp 5.0" and info.problems
    emb = k.read(krita_samples["test-embedded-resources-5.0.kpp"])[0]
    assert len(emb.tips) == 1 and emb.texture is not None and emb.texture.name == "test_pattern"


def test_krita_reads_reference_bundles(krita_samples):
    k = codec("krita")
    p = _need(krita_samples, "Krita_4_Default_Resources.bundle")
    info = sniff(p)
    assert info.version == "Krita 4 资源包" and info.brush_count == 117
    brushes = k.read(p)
    assert len(brushes) == 117 and all(b.name for b in brushes)
    assert sum(1 for b in brushes if b.tips) > 30
    refs = k.list_refs(p)
    # Same display name as Krita 5.3.4's resourcecache.sqlite ('_' shown as ' ').
    assert len(refs) == 117 and refs[0].name == "a) Eraser Circle"
    assert refs[1].load().name == refs[1].name and refs[1].thumbnail().mode == "RGBA"

    rgba = k.read(_need(krita_samples, "RGBA_brushes.bundle"))
    assert len(rgba) == 6 and any(b.texture is not None for b in rgba)
    assert k.read(_need(krita_samples, "test1.bundle")) == []


# =============================================================================================
# Krita: writing
# =============================================================================================
def _bundle_presets(path):
    with zipfile.ZipFile(path) as z:
        return {n: kr.parse_kpp(z.read(n), n) for n in z.namelist() if n.endswith(".kpp")}


def test_krita_bundle_structure(tmp_path):
    out = codec("krita").write([_rich_brush(), Brush("Round soft", size_px=33, hardness=0.4)],
                               tmp_path / "out.bundle", title="测试包")
    path = out[0]
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        assert infos[0].filename == "mimetype" and infos[0].compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/x-krita-resourcebundle"
        manifest = ET.fromstring(z.read("META-INF/manifest.xml"))
        assert manifest.tag == f"{MANIFEST}manifest"
        entries = {e.get(f"{MANIFEST}full-path"): e for e in manifest.iter(f"{MANIFEST}file-entry")}
        assert entries["/"].get(f"{MANIFEST}media-type") == "application/x-krita-resourcebundle"
        names = set(z.namelist())
        for fp, e in entries.items():
            if fp == "/":
                continue
            assert fp in names
            assert fp.split("/")[0] == e.get(f"{MANIFEST}media-type")
            assert hashlib.md5(z.read(fp)).hexdigest() == e.get(f"{MANIFEST}md5sum")
        payload = {n for n in names if "/" in n and not n.startswith("META-INF/")}
        assert payload == set(entries) - {"/"}
        meta = z.read("meta.xml").decode("utf-8")
        assert re.search(r"<meta:meta\b", meta) and "测试包" in meta
        assert all(n.isascii() for n in names)
        Image.open(io.BytesIO(z.read("preview.png"))).verify()
    presets = _bundle_presets(path)
    assert len(presets) == 2
    for p in presets.values():
        assert p.version == "2.2" and p.paintop == "paintbrush"
    assert {p.name for p in presets.values()} == {"水彩 测试", "Round soft"}


def test_krita_png_tip_uses_krita_convention(tmp_path):
    out = codec("krita").write([Brush("Tip", tips=[_tip()], size_px=90)], tmp_path / "t.bundle")
    with zipfile.ZipFile(out[0]) as z:
        png = [n for n in z.namelist() if n.startswith("brushes/")]
        img = Image.open(io.BytesIO(z.read(png[0])))
        assert img.mode == "L"  # grayscale, no alpha -> Krita uses it as a mask: black = paint
        assert _mask_diff(Image.eval(img, lambda v: 255 - v), _tip()) == 0
        p = next(iter(_bundle_presets(out[0]).values()))
        bd = ET.fromstring(p.params["brush_definition"])
        assert bd.tag == "Brush" and bd.get("type") == "png_brush"
        assert bd.get("filename") == Path(png[0]).name
        assert bd.get("md5sum") == hashlib.md5(z.read(png[0])).hexdigest()
        assert abs(float(bd.get("scale")) * 90 - 90) < 1e-3


def test_krita_round_trip_keeps_settings(tmp_path):
    src = [_rich_brush(), Brush("Round soft", size_px=33, hardness=0.4, roundness=0.6, angle=45, is_eraser=True),
           Brush("Multi", tips=[_tip(), _tip(60, 90)], tip_order="random", size_px=50, angle_follow_stroke=True)]
    out = codec("krita").write(src, tmp_path / "rt.bundle")
    back = codec("krita").read(out[0])
    assert [b.name for b in back] == [b.name for b in src]
    a, r, m = back
    assert (round(a.size_px, 3), round(a.spacing, 3), round(a.angle, 1)) == (120, 0.15, 30)
    assert _mask_diff(a.main_tip(), _tip()) == 0
    assert a.size_dynamic.pressure == pytest.approx(0.7) and a.opacity_dynamic.pressure == pytest.approx(0.5)
    assert (a.opacity, a.flow, a.blend_mode) == (pytest.approx(0.8), pytest.approx(0.6), "multiply")
    assert a.angle_jitter == pytest.approx(0.25) and a.scatter == pytest.approx(1.5)
    assert a.texture and (a.texture.mode, a.texture.invert) == ("subtract", True)
    assert a.texture.depth == pytest.approx(0.7) and a.texture.scale == pytest.approx(0.5)
    assert _mask_diff(a.texture.image, _grain()) == 0
    assert a.dual and a.dual.mode == "darken" and a.dual.size_px == pytest.approx(25)
    assert a.color.hue_jitter > 0
    assert (r.hardness, r.roundness, r.angle, r.is_eraser, r.tips) == (pytest.approx(0.4), pytest.approx(0.6),
                                                                      pytest.approx(45), True, [])
    assert len(m.tips) == 2 and m.tip_order == "random" and m.angle_follow_stroke


def test_krita_preset_xml_matches_reference_structure(tmp_path, krita_samples):
    ref = kr.parse_kpp(_need(krita_samples, "b)_Basic-5_Size_default.kpp").read_bytes())
    out = codec("krita").write([_rich_brush()], tmp_path / "s.bundle")
    ours = next(iter(_bundle_presets(out[0]).values()))
    # every setting key a stock Krita preset has is also present in ours
    missing = {k for k in ref.params if not k.startswith("MaskingBrush/Preset/")} - set(ours.params)
    assert not missing
    for key, val in ours.params.items():
        if key.endswith("Sensor") or key.endswith("/Sensor"):
            if not val:
                continue
            el = ET.fromstring(re.sub(r"<!DOCTYPE[^>]*>", "", val))
            assert el.tag == "params" and el.get("id") in KNOWN_SENSORS, key
            for c in el.iter("curve"):
                assert kr.parse_curve(c.text)
            for c in el.iter("ChildSensor"):
                assert c.get("id") in KNOWN_SENSORS
    assert ours.params["paintop"] == "paintbrush"
    assert ET.fromstring(ours.params["brush_definition"]).tag == "Brush"
    # Krita 4 readers need the pattern embedded as bytearray; Krita 5 uses md5 + file name
    assert ours.types["Texture/Pattern/Pattern"] == "bytearray"
    assert re.fullmatch(r"[0-9a-f]{32}", ours.params["Texture/Pattern/PatternMD5Sum"])
    xml = Image.open(io.BytesIO(kr.encode_kpp(kr.build_preset_xml(ours.name, ours.params, ours.types),
                                              Image.new("RGBA", (8, 8))))).text["preset"]
    ET.fromstring(xml)  # well-formed


def test_standalone_kpp_embeds_resources(tmp_path):
    k = codec("krita")
    out = k.write([_rich_brush()], tmp_path / "one.kpp")
    p = kr.parse_kpp(out[0].read_bytes())
    assert p.version == "5.0" and {r.type for r in p.resources} >= {"brushes", "patterns"}
    for r in p.resources:
        assert hashlib.md5(r.data).hexdigest() == r.md5
    b = k.read(out[0])[0]
    assert b.name == "水彩 测试" and len(b.tips) == 1 and b.texture is not None
    assert sniff(out[0]).problems  # Krita 4 cannot import it
    out = k.write([Brush("Plain", size_px=20)], tmp_path / "plain.kpp")
    assert kr.parse_kpp(out[0].read_bytes()).version == "2.2"  # nothing to embed -> Krita 4 compatible


def test_krita_rewrites_stock_bundle(tmp_path, krita_samples):
    k = codec("krita")
    src = k.read(_need(krita_samples, "Krita_4_Default_Resources.bundle"))[:25]
    out = k.write(src, tmp_path / "again.bundle")
    back = k.read(out[0])
    assert len(back) == 25
    for a, b in zip(src, back):
        assert a.name == b.name and len(a.tips) == len(b.tips)
        assert b.size_px == pytest.approx(a.size_px, rel=0.02)
        assert b.spacing == pytest.approx(a.spacing, rel=0.02, abs=1e-3)


# =============================================================================================
# App adapters
# =============================================================================================
def test_detect_does_not_crash():
    for a in (app("krita"), app("gimp")):
        assert isinstance(a.detect(), list)


def _fake_program(monkeypatch, module, name, version, location):
    from brush_bridge.core.system import InstalledProgram

    monkeypatch.setattr(module, "find_programs", lambda rx: [InstalledProgram(name, version, location)]
                        if re.search(rx, name, re.I) else [])
    for env in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.setenv(env, str(location.parent / "nowhere"))


def test_krita_detect_and_install(tmp_path, monkeypatch):
    import brush_bridge.apps.krita as ka

    loc = tmp_path / "Krita (x64)"
    (loc / "bin").mkdir(parents=True)
    (loc / "bin" / "krita.exe").write_bytes(b"MZ")
    _fake_program(monkeypatch, ka, "Krita (x64) 5.2.6", "5.2.6", loc)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(ka, "export_dir", lambda sub="": tmp_path / "export")
    (tmp_path / "export").mkdir()
    monkeypatch.setattr(ka, "IS_WINDOWS", True)
    insts = app("krita").detect()
    assert len(insts) == 1
    inst = insts[0]
    assert inst.version == "5.2.6" and inst.data_dir == tmp_path / "Roaming" / "krita" and inst.extra["major"] == 5

    # custom resource folder from kritarc (root group)
    (tmp_path / "Local").mkdir()
    (tmp_path / "Local" / "kritarc").write_text(
        f"ResourceDirectory={(tmp_path / 'res').as_posix()}\n[General]\nResourceDirectory=C:/wrong\n", "utf-8")
    assert ka.resource_dir() == tmp_path / "res"
    inst = app("krita").detect()[0]
    assert inst.data_dir == tmp_path / "res"

    monkeypatch.setattr(type(app("krita")), "is_running", lambda self, i: False)
    res = app("krita").install(inst, [_rich_brush(), Brush("Round", size_px=30)], title="我的笔刷")
    assert res.ok and len(res.files) == 1 and "重新启动" not in res.message and "启动 Krita" in res.message
    installed = res.files[0]
    assert installed.parent == tmp_path / "res" / "bundles" and installed.suffix == ".bundle"
    assert len(codec("krita").read(installed)) == 2

    monkeypatch.setattr(type(app("krita")), "is_running", lambda self, i: True)
    kpp = codec("krita").write([Brush("Loose", size_px=12)], tmp_path / "loose.kpp")[0]
    abr = tmp_path / "x.abr"
    abr.write_bytes(b"\0\x02")
    res = app("krita").install_files(inst, [kpp, abr, tmp_path / "ignored.txt"])
    assert res.ok and "正在运行" in res.message
    assert (tmp_path / "res" / "paintoppresets" / "loose.kpp").exists()
    assert (tmp_path / "res" / "bundles" / "x.abr").exists()
    inst.extra["major"] = 4
    app("krita").install_files(inst, [abr])
    assert (tmp_path / "res" / "brushes" / "x.abr").exists()
    assert not app("krita").install_files(inst, [tmp_path / "ignored.txt"]).ok

    refs = app("krita").list_brushes(inst)  # no resourcecache.sqlite -> file scan
    assert {r.name for r in refs} >= {"水彩 测试", "Round", "Loose"}
    assert refs[0].load().name


def test_krita_list_brushes_from_resource_db(tmp_path):
    resdir = tmp_path / "krita"
    (resdir / "bundles").mkdir(parents=True)
    (resdir / "paintoppresets").mkdir()
    bundle = codec("krita").write([Brush("In bundle", size_px=10), Brush("Hidden", size_px=11)],
                                  resdir / "bundles" / "b.bundle")[0]
    codec("krita").write([Brush("Loose one", size_px=12)], resdir / "paintoppresets" / "loose.kpp")
    with zipfile.ZipFile(bundle) as z:
        in_bundle = [Path(n).name for n in z.namelist() if n.endswith(".kpp")]
    db = sqlite3.connect(resdir / "resourcecache.sqlite")
    db.executescript("""
        CREATE TABLE resource_types (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE storage_types (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE storages (id INTEGER PRIMARY KEY, storage_type_id INTEGER, location TEXT, timestamp INTEGER,
                               pre_installed INTEGER, active INTEGER, thumbnail BLOB);
        CREATE TABLE resources (id INTEGER PRIMARY KEY, resource_type_id INTEGER, storage_id INTEGER, name TEXT,
                                filename TEXT, tooltip TEXT, thumbnail BLOB, status INTEGER, temporary INTEGER,
                                md5sum TEXT);
        INSERT INTO resource_types VALUES (1, 'paintoppresets'), (2, 'brushes');
        INSERT INTO storage_types VALUES (1, 'Folder'), (2, 'Bundle'), (3, 'Memory');
        INSERT INTO storages VALUES (1, 1, '', 0, 0, 1, NULL), (2, 2, 'bundles/b.bundle', 0, 0, 1, NULL),
                                    (3, 3, 'memory', 0, 0, 1, NULL);
    """)
    db.execute("INSERT INTO resources VALUES (1, 1, 1, 'Loose one', 'loose.kpp', '', NULL, 1, 0, '')")
    db.execute("INSERT INTO resources VALUES (2, 1, 2, 'In bundle', ?, '', NULL, 1, 0, '')", (in_bundle[0],))
    db.execute("INSERT INTO resources VALUES (3, 1, 2, 'Hidden', ?, '', NULL, 0, 0, '')", (in_bundle[1],))
    db.execute("INSERT INTO resources VALUES (4, 1, 3, 'Temp', 'tmp.kpp', '', NULL, 1, 1, '')")
    db.commit()
    db.close()
    inst = AppInstall("krita", "Krita", "5.2.6", None, resdir, extra={"major": 5})
    refs = app("krita").list_brushes(inst)
    assert [r.name for r in refs] == ["Loose one", "In bundle"]
    assert refs[1].load().name == "In bundle" and refs[0].load().name == "Loose one"


def test_gimp_detect_and_install(tmp_path, monkeypatch):
    import brush_bridge.apps.gimp as ga

    loc = tmp_path / "GIMP 3"
    (loc / "bin").mkdir(parents=True)
    for exe in ("gimp-3.0.exe", "gimp-console-3.0.exe"):
        (loc / "bin" / exe).write_bytes(b"MZ")
    _fake_program(monkeypatch, ga, "GIMP 3.0.4", "3.0.4", loc)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.delenv("GIMP3_DIRECTORY", raising=False)
    monkeypatch.setattr(ga, "export_dir", lambda sub="": tmp_path / "export")
    monkeypatch.setattr(ga, "IS_WINDOWS", True)
    insts = app("gimp").detect()
    assert len(insts) == 1
    inst = insts[0]
    assert inst.exe.name == "gimp-3.0.exe" and inst.version == "3.0.4"
    assert inst.data_dir == tmp_path / "Roaming" / "GIMP" / "3.0" / "brushes"

    monkeypatch.setattr(type(app("gimp")), "is_running", lambda self, i: True)
    res = app("gimp").install(inst, [Brush("One", tips=[_tip()], size_px=60),
                                     Brush("Two", tips=[_tip(), _tip(40, 40)], size_px=60),
                                     Brush("One", size_px=20)])
    assert res.ok and "刷新" in res.message
    names = sorted(p.name for p in res.files)
    assert names == ["One (2).gbr", "One.gbr", "Two.gih"] or len(set(names)) == 3
    assert all(p.parent == inst.data_dir for p in res.files)
    abr = tmp_path / "ps.abr"
    abr.write_bytes(b"\0\x02")
    assert app("gimp").install_files(inst, [abr]).ok and (inst.data_dir / "ps.abr").exists()
    assert not app("gimp").install_files(inst, [tmp_path / "a.kpp"]).ok

    refs = app("gimp").list_brushes(inst)
    gimp_refs = [r for r in refs if r.detail.startswith("GIMP")]
    assert {r.name for r in gimp_refs} == {"One", "Two"} and len(gimp_refs) == 3
    assert gimp_refs[0].load().tips and gimp_refs[0].thumbnail().mode == "RGBA"


def test_gimp_detect_210_folder(tmp_path, monkeypatch):
    import brush_bridge.apps.gimp as ga

    loc = tmp_path / "GIMP 2"
    (loc / "bin").mkdir(parents=True)
    (loc / "bin" / "gimp-2.10.exe").write_bytes(b"MZ")
    _fake_program(monkeypatch, ga, "GIMP 2.10.38", "2.10.38", loc)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.delenv("GIMP2_DIRECTORY", raising=False)
    monkeypatch.setattr(ga, "IS_WINDOWS", True)
    inst = app("gimp").detect()[0]
    assert inst.data_dir == tmp_path / "Roaming" / "GIMP" / "2.10" / "brushes"
