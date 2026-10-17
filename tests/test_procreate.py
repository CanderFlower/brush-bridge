"""Procreate (.brush / .brushset) codec and app adapter tests.

Real-file tests use public sample brushes downloaded by ``scripts/fetch_samples.py`` into
``tests/fixtures/downloaded/procreate`` (never committed); they are skipped when offline."""
import importlib.util
import math
import plistlib
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from brush_bridge.core.model import Brush, ColorDynamics, Dynamic, Texture, WetMix
from brush_bridge.core.plugin import AppInstall, app, codec, sniff
from brush_bridge.formats import procreate as pc

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def _tip(w=90, h=60):
    im = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(im)
    d.ellipse((4, 4, w - 5, h - 5), fill=180)
    d.rectangle((20, 15, 40, 45), fill=255)
    return im


def _grain(w=300, h=200):
    rng = np.random.default_rng(7)
    return Image.fromarray((rng.random((h, w)) * 255).astype(np.uint8), "L")


def _brush(name="水彩测试", **kw):
    b = Brush(
        name=name, tips=[_tip()], size_px=120, min_size_px=10, max_size_px=300, spacing=0.15,
        angle=30, roundness=0.7, angle_follow_stroke=True, angle_jitter=0.25, scatter=0.8,
        scatter_both_axes=True, count=3, count_jitter=0.5, tip_order="random", flip_x=True,
        size_dynamic=Dynamic(pressure=0.8, tilt=0.3, velocity=0.2, jitter=0.1),
        opacity_dynamic=Dynamic(pressure=0.6, jitter=0.15), flow_dynamic=Dynamic(pressure=0.4),
        opacity=0.85, flow=0.6, blend_mode="multiply", stabilization=0.3, taper_start=0.2,
        taper_end=0.4,
        texture=Texture(image=_grain(), name="纸纹", scale=0.5, depth=0.7, invert=True,
                        mode="multiply", brightness=0.1, contrast=-0.2, per_tip=True,
                        depth_dynamic=Dynamic(jitter=0.3)),
        color=ColorDynamics(fg_bg_jitter=0.2, hue_jitter=0.1, saturation_jitter=0.3,
                            brightness_jitter=0.05, per_tip=True),
        wet=WetMix(enabled=True, mix=0.5, load=0.8, wetness=0.6, color_stretch=0.3),
    )
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _main_archive(zf: zipfile.ZipFile) -> str:
    """The brush's own archive (not the Reset/ backup copy)."""
    return next(n for n in zf.namelist()
                if n.endswith("Brush.archive") and "Reset/" not in n and "__MACOSX" not in n)


def _raw_archive(data: bytes) -> dict:
    return plistlib.loads(data)


def _raw_root(pl: dict) -> dict:
    return pl["$objects"][pl["$top"]["root"].data]


def _class_name(pl: dict, obj) -> str:
    return pl["$objects"][obj["$class"].data]["$classname"]


def _class_chain(pl: dict, obj) -> list:
    return pl["$objects"][obj["$class"].data]["$classes"]


def _value_class(pl: dict, v):
    """Class name of a root value (None for inline scalars and $null)."""
    if isinstance(v, plistlib.UID):
        o = pl["$objects"][v.data]
        if isinstance(o, dict) and "$class" in o:
            return _class_name(pl, o)
        return type(o).__name__ if o != "$null" else None
    return None


# ---------------------------------------------------------------------------------------------
# archive coder
# ---------------------------------------------------------------------------------------------
def test_archive_roundtrip_template():
    params = pc.template_params("模板")
    data = pc.archive(params)
    back, chains = pc.unarchive(data)
    assert back["$class"] == "SilicaBrush"
    assert chains["SilicaBrush"] == ["SilicaBrush", "ValkyrieBrush", "NSObject"]
    assert set(back) - {"$class"} == set(params) - {"$class"} == set(pc.TEMPLATE_KEYS)
    for k, v in params.items():
        if k == "$class":
            continue
        got = back[k]
        if isinstance(v, float):
            assert got == pytest.approx(v), k
        elif isinstance(v, dict) and v.get("$class") == "ValkyrieMagnitudinalCurve":
            assert [tuple(map(float, p)) for p in got["points"]] == pytest.approx(
                [tuple(map(float, p)) for p in v["points"]]), k
        else:
            assert got == v, k


def test_unarchive_rejects_garbage():
    with pytest.raises(pc.ArchiveError):
        pc.unarchive(b"not a plist")


def test_scale_and_jitter_converters_invert():
    for f in (0.0, 0.05, 0.3, 1.0, 1.7):
        assert pc.jitter_raw_to_fraction(pc.jitter_fraction_to_raw(f)) == pytest.approx(f, abs=1e-6)
    for f in (0.05, 0.25, 0.6, 1.0):
        assert pc.grain_scale_raw_to_fraction(pc.grain_scale_fraction_to_raw(f)) == pytest.approx(f, rel=1e-6)


# ---------------------------------------------------------------------------------------------
# synthetic round trips
# ---------------------------------------------------------------------------------------------
def _compare(a: Brush, b: Brush):
    assert b.name == a.name
    assert b.size_px == pytest.approx(a.size_px, rel=1e-3)
    assert b.min_size_px == pytest.approx(a.min_size_px, rel=1e-3, abs=1e-3)
    if a.max_size_px <= max(a.size_px * 3, 50):  # larger maxima are narrowed to a usable slider range
        assert b.max_size_px == pytest.approx(a.max_size_px, rel=1e-3)
    assert b.spacing == pytest.approx(a.spacing, abs=1e-4)
    assert b.angle == pytest.approx(a.angle, abs=1e-3)
    assert b.roundness == pytest.approx(a.roundness, abs=1e-4)
    assert b.angle_follow_stroke == a.angle_follow_stroke
    assert b.angle_jitter == pytest.approx(a.angle_jitter, abs=1e-4)
    assert b.scatter == pytest.approx(a.scatter, abs=1e-4)
    assert b.count == a.count
    assert b.count_jitter == pytest.approx(a.count_jitter, abs=1e-4)
    assert b.tip_order == a.tip_order
    assert (b.flip_x, b.flip_y) == (a.flip_x, a.flip_y)
    for dyn in ("size_dynamic", "opacity_dynamic"):
        for f in ("pressure", "tilt", "velocity", "jitter"):
            assert getattr(getattr(b, dyn), f) == pytest.approx(getattr(getattr(a, dyn), f), abs=1e-4), (dyn, f)
    assert b.flow_dynamic.pressure == pytest.approx(a.flow_dynamic.pressure, abs=1e-4)
    assert b.opacity == pytest.approx(a.opacity, abs=1e-4)
    assert b.flow == pytest.approx(a.flow, abs=1e-4)
    assert b.blend_mode == a.blend_mode
    assert b.stabilization == pytest.approx(a.stabilization, abs=1e-4)
    assert b.taper_start == pytest.approx(a.taper_start, abs=1e-4)
    assert b.taper_end == pytest.approx(a.taper_end, abs=1e-4)
    if a.texture is None:
        assert b.texture is None
    else:
        ta, tb = a.texture, b.texture
        assert tb is not None
        assert tb.scale == pytest.approx(ta.scale, rel=1e-3)
        assert tb.depth == pytest.approx(ta.depth, abs=1e-4)
        assert tb.invert == ta.invert
        assert tb.mode == ta.mode
        assert tb.brightness == pytest.approx(ta.brightness, abs=1e-4)
        assert tb.contrast == pytest.approx(ta.contrast, abs=1e-4)
        assert tb.per_tip == ta.per_tip
        assert tb.depth_dynamic.jitter == pytest.approx(ta.depth_dynamic.jitter, abs=1e-4)
    for f in ("fg_bg_jitter", "hue_jitter", "saturation_jitter", "brightness_jitter"):
        assert getattr(b.color, f) == pytest.approx(getattr(a.color, f), abs=1e-4), f
    assert b.color.per_tip == a.color.per_tip
    assert b.wet.enabled == a.wet.enabled
    if a.wet.enabled:
        for f in ("mix", "load", "wetness", "color_stretch"):
            assert getattr(b.wet, f) == pytest.approx(getattr(a.wet, f), abs=1e-4), f


def test_single_brush_roundtrip(tmp_path):
    src = _brush()
    out = codec("procreate").write([src], tmp_path / "one.brush")
    assert out == [tmp_path / "one.brush"]
    with zipfile.ZipFile(out[0]) as zf:
        names = set(zf.namelist())
    assert {"Brush.archive", "Shape.png", "Grain.png", "QuickLook/Thumbnail.png"} <= names
    assert not any(n.endswith(".part") for n in names)

    info = sniff(out[0])
    assert info is not None and info.format_id == "procreate"
    assert info.brush_count == 1
    assert info.problems == []

    back = codec("procreate").read(out[0])
    assert len(back) == 1
    _compare(src, back[0])
    b = back[0]
    assert b.source.app == "procreate" and b.source.native["archive"]
    assert b.preview is not None and b.preview.mode == "RGBA"
    # tip: same white-paint polarity, square
    t = b.tips[0]
    assert t.mode == "L" and t.width == t.height
    arr = np.asarray(t, dtype=np.float32)
    assert arr[0, 0] == 0 and arr.max() == 255


@pytest.mark.parametrize("blend", ["normal", "multiply", "screen", "overlay", "darken", "lighten",
                                   "color_dodge", "color_burn", "linear_burn", "difference",
                                   "hard_light", "soft_light", "add"])
def test_blend_modes_roundtrip(tmp_path, blend):
    src = Brush(name=f"b-{blend}", blend_mode=blend, size_px=40)
    if pc._blend_codes(blend) is None:
        pytest.skip(f"{blend} not available in Procreate")
    out = codec("procreate").write([src], tmp_path / "b.brush")[0]
    assert codec("procreate").read(out)[0].blend_mode == blend


def test_computed_tip_and_minimal_brush(tmp_path):
    src = Brush(name="Round", size_px=30, hardness=0.5, roundness=0.5, angle=45)
    out = codec("procreate").write([src], tmp_path / "r.brush")[0]
    back = codec("procreate").read(out)[0]
    assert back.texture is None and not back.wet.enabled
    assert back.roundness == pytest.approx(0.5, abs=1e-4)
    assert back.angle == pytest.approx(45, abs=1e-3)
    assert back.size_px == pytest.approx(30, rel=1e-3)
    with zipfile.ZipFile(out) as zf:
        shape = Image.open(zf.open("Shape.png"))
        shape.load()
    a = np.asarray(shape.convert("L"))
    # computed tips are drawn circular; roundness/angle live in the archive (not baked twice)
    assert a.shape[0] == a.shape[1]
    assert a[a.shape[0] // 2].max() == 255 and a[:, a.shape[1] // 2].max() == 255
    assert a[0, 0] == 0


def test_brushset_roundtrip_order_and_name(tmp_path):
    brushes = [_brush("一号"), _brush("二号", texture=None, wet=WetMix()),
               Brush(name="三号", size_px=8, blend_mode="screen")]
    out = codec("procreate").write(brushes, tmp_path / "set.brushset", set_name="我的笔刷集")[0]
    with zipfile.ZipFile(out) as zf:
        meta = plistlib.loads(zf.read("brushset.plist"))
        names = zf.namelist()
    assert meta["name"] == "我的笔刷集"
    assert len(meta["brushes"]) == 3
    for uid in meta["brushes"]:
        assert f"{uid}/Brush.archive" in names and f"{uid}/Shape.png" in names
    info = sniff(out)
    assert info.format_id == "procreate" and info.brush_count == 3
    assert info.details["笔刷集名称"] == "我的笔刷集"

    back = codec("procreate").read(out)
    assert [b.name for b in back] == ["一号", "二号", "三号"]
    assert all(b.group == "我的笔刷集" for b in back)
    for a, b in zip(brushes, back):
        _compare(a, b)

    refs = codec("procreate").list_refs(out)
    assert [r.name for r in refs] == ["一号", "二号", "三号"]
    assert refs[0].detail  # has grain
    assert refs[1].load().name == "二号"
    assert refs[2].thumbnail() is not None


def test_multi_brush_to_brush_path_becomes_brushset(tmp_path):
    out = codec("procreate").write([Brush(name="a"), Brush(name="b")], tmp_path / "x.brush")[0]
    assert out.suffix == ".brushset" and out.exists()
    assert len(codec("procreate").read(out)) == 2


def test_native_rewrite_is_lossless_and_rename(tmp_path):
    first = codec("procreate").write([_brush()], tmp_path / "a.brush")[0]
    b = codec("procreate").read(first)[0]
    second = codec("procreate").write([b], tmp_path / "b.brush")[0]
    with zipfile.ZipFile(first) as z1, zipfile.ZipFile(second) as z2:
        assert sorted(z1.namelist()) == sorted(z2.namelist())
        for n in z1.namelist():
            assert z1.read(n) == z2.read(n), n
    b.name = "改名"
    third = codec("procreate").write([b], tmp_path / "c.brush")[0]
    c = codec("procreate").read(third)[0]
    assert c.name == "改名"
    assert c.size_px == pytest.approx(b.size_px)


def test_shape_and_grain_image_rules():
    shape = pc.make_shape_png(_tip(90, 60))
    assert shape.mode == "L" and shape.width == shape.height == 128
    big = pc.make_shape_png(Image.new("L", (5000, 3000), 255))
    assert big.size == (pc.MAX_SHAPE_SIDE, pc.MAX_SHAPE_SIDE)
    tiny = pc.make_shape_png(Image.new("L", (1, 1), 255))
    assert tiny.size == (64, 64)

    # a seamless (periodic) source must stay seamless after squaring/resampling
    x = np.arange(300)
    y = np.arange(150)
    src = (127 + 120 * np.sin(2 * np.pi * x[None, :] / 300) * np.cos(2 * np.pi * y[:, None] / 150))
    g = pc.make_grain_png(Image.fromarray(src.astype(np.uint8), "L"))
    assert g.mode == "L" and g.width == g.height and g.width & (g.width - 1) == 0
    a = np.asarray(g, dtype=np.float32)
    inner = np.abs(np.diff(a, axis=1)).mean()
    seam_x = np.abs(a[:, 0] - a[:, -1]).mean()
    seam_y = np.abs(a[0, :] - a[-1, :]).mean()
    assert seam_x < inner * 3 + 1 and seam_y < np.abs(np.diff(a, axis=0)).mean() * 3 + 1


def test_effects_mapping_roundtrip():
    p = pc.template_params("效果")
    p.update({"burntEdgesAmount": 0.3, "wetEdgesAmount": 0.1, "dynamicsPressureSaturation": 0.2,
              "dynamicsPressureBrightness": -0.1, "dynamicsPressureHue": 0.25, "dynamicsTiltSaturation": 0.4,
              "dynamicsTiltBleed": 0.3, "dynamicsPressureSize": 0.9})
    b = pc.params_to_brush(p, shape=None, grain=None, thumb=None)
    assert b.edge.is_active() and b.edge.darken == pytest.approx(0.3) and b.edge.blur_px > 0
    # Procreate: positive saturation = paler at light pressure; hue 100% = full spectrum
    assert b.color.saturation_pressure == pytest.approx(-0.2) and b.color.saturation_tilt == pytest.approx(-0.4)
    assert b.color.brightness_pressure == pytest.approx(-0.1) and b.color.hue_pressure == pytest.approx(0.5)
    assert b.color.has_shift() and not b.color.has_jitter()
    assert b.flow_dynamic.tilt > 0

    q = pc.brush_to_params(b, has_grain=False)
    for k in ("burntEdgesAmount", "wetEdgesAmount", "dynamicsPressureSaturation", "dynamicsPressureBrightness",
              "dynamicsPressureHue", "dynamicsTiltSaturation"):
        assert q[k] == pytest.approx(p[k], abs=1e-6), k


def test_unsupported_features_warn():
    b = _brush(tips=[_tip(), _tip(40, 40)], roundness_jitter=0.3, is_eraser=True,
               blend_mode="hue_shift_of_doom")
    pc.brush_to_params(b, has_grain=True)
    assert len(b.notes) >= 4
    assert all(isinstance(n, str) and n for n in b.notes)


def test_sniff_rejects_other_zip(tmp_path):
    p = tmp_path / "x.brush"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("readme.txt", "hi")
    assert codec("procreate").sniff(p) is None
    q = tmp_path / "y.brush"
    q.write_bytes(b"8BIM not a zip")
    assert codec("procreate").sniff(q) is None


def test_bundled_assets_reported(tmp_path):
    params = pc.template_params("内置形状")
    params["bundledShapePath"] = "Some-Builtin-Shape.png"
    params["bundledGrainPath"] = "Some-Builtin-Grain.png"
    p = tmp_path / "bundled.brush"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("Brush.archive", pc.archive(params))
    info = sniff(p)
    assert info is not None and info.format_id == "procreate"
    assert len(info.problems) == 2
    b = codec("procreate").read(p)[0]
    assert b.tips == [] and b.texture is None
    assert len(b.notes) >= 2
    # a write without original files falls back to a synthesized round shape
    out = codec("procreate").write([b], tmp_path / "out.brush")[0]
    with zipfile.ZipFile(out) as zf:
        assert "Shape.png" in zf.namelist() or "Brush.archive" in zf.namelist()
    assert codec("procreate").read(out)[0].name == "内置形状"


# ---------------------------------------------------------------------------------------------
# real public samples
# ---------------------------------------------------------------------------------------------
def _fetch_module():
    spec = importlib.util.spec_from_file_location("fetch_samples", ROOT / "scripts" / "fetch_samples.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def samples():
    try:
        paths = _fetch_module().fetch_procreate(timeout=15)
    except Exception:  # noqa: BLE001
        paths = []
    if not paths:
        pytest.skip("no Procreate sample brushes available (offline?)")
    return paths


def test_real_samples_sniff_and_read(samples):
    for p in samples:
        info = sniff(p)
        assert info is not None and info.format_id == "procreate", p.name
        assert info.brush_count >= 1
        brushes = codec("procreate").read(p)
        assert len(brushes) == info.brush_count, p.name
        for b in brushes:
            assert b.name
            assert 1 <= b.size_px <= 16 * pc.SIZE_UNIT_PX
            assert 0.01 <= b.spacing <= 2
            assert 0 < b.opacity <= 1
            assert b.source.native["archive"] and isinstance(b.source.native["params"], dict)
            if b.tips:
                assert b.tips[0].mode == "L"


def test_real_samples_lossless_rewrite(samples, tmp_path):
    for p in samples:
        b = codec("procreate").read(p)[0]
        out = codec("procreate").write([b], tmp_path / f"{p.stem}.brush")[0]
        with zipfile.ZipFile(p) as zs, zipfile.ZipFile(out) as zo:
            arch = _main_archive(zs)
            assert zo.read("Brush.archive") == zs.read(arch), p.name
            src_files = {n for n in zs.namelist() if not n.endswith("/") and "__MACOSX" not in n}
            if arch == "Brush.archive":  # brush stored at the zip root: every member must survive
                assert src_files == set(zo.namelist()), p.name
                for n in src_files:
                    assert zo.read(n) == zs.read(n), (p.name, n)
        again = codec("procreate").read(out)[0]
        assert again.name == b.name and again.size_px == pytest.approx(b.size_px)


def test_real_samples_rename(samples, tmp_path):
    b = codec("procreate").read(samples[0])[0]
    b.name = "重命名后的笔刷"
    out = codec("procreate").write([b], tmp_path / "renamed.brush")[0]
    c = codec("procreate").read(out)[0]
    assert c.name == "重命名后的笔刷"
    assert c.spacing == pytest.approx(b.spacing)


def test_real_samples_synthesized_rewrite(samples, tmp_path):
    for p in samples:
        b = codec("procreate").read(p)[0]
        out = codec("procreate").write([b], tmp_path / f"{p.stem}.brush", prefer_native=False)[0]
        c = codec("procreate").read(out)[0]
        assert c.name == b.name
        assert c.size_px == pytest.approx(b.size_px, rel=1e-3), p.name
        assert c.spacing == pytest.approx(b.spacing, abs=1e-4), p.name
        assert c.blend_mode == b.blend_mode, p.name
        assert c.opacity == pytest.approx(b.opacity, abs=1e-3), p.name


def test_synthesized_archive_matches_real_structure(samples):
    """Our template archive must look like Procreate's: same archiver header, root class
    hierarchy, a superset of the keys of every modern real brush and the same value classes."""
    synth = _raw_archive(pc.archive(pc.template_params("结构")))
    sroot = _raw_root(synth)
    checked = 0
    for p in samples:
        with zipfile.ZipFile(p) as zf:
            arch = _main_archive(zf)
            real = _raw_archive(zf.read(arch))
        assert synth["$archiver"] == real["$archiver"] == "NSKeyedArchiver"
        assert synth["$version"] == real["$version"]
        assert set(synth["$top"]) == set(real["$top"])
        rroot = _raw_root(real)
        assert _class_name(synth, sroot) == _class_name(real, rroot) == "SilicaBrush", p.name
        rkeys = set(rroot) - {"$class"}
        if len(rkeys) < 200:  # Procreate 4-era or third-party (e.g. barakbl) archives
            continue
        checked += 1
        assert _class_chain(synth, sroot) == _class_chain(real, rroot), p.name
        missing = rkeys - set(sroot)
        # keys from pre-5.2 files that newer Procreate dropped are not expected
        assert missing <= {"cloneSize", "cloneOpacity"}, (p.name, sorted(missing))
        for k in rkeys & set(sroot):
            rv, sv = rroot[k], sroot[k]
            rc, sc = _value_class(real, rv), _value_class(synth, sv)
            if rc is not None and sc is not None:
                assert rc == sc, (p.name, k, rc, sc)
                ro, so = real["$objects"][rv.data], synth["$objects"][sv.data]
                if isinstance(ro, dict) and "$class" in ro:
                    assert _class_chain(real, ro) == _class_chain(synth, so), (p.name, k)
            elif not isinstance(rv, plistlib.UID) and not isinstance(sv, plistlib.UID):
                assert type(rv) is type(sv) or {type(rv), type(sv)} <= {int, float}, (p.name, k)
    assert checked >= 1


def test_sample_with_grain_reads_texture(samples):
    with_grain = [p for p in samples if "Grain.png" in " ".join(zipfile.ZipFile(p).namelist())]
    if not with_grain:
        pytest.skip("no sample with an embedded grain")
    b = codec("procreate").read(with_grain[0])[0]
    assert b.texture is not None and b.texture.image.mode == "L"


# ---------------------------------------------------------------------------------------------
# app adapter
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def adapter(tmp_path, monkeypatch):
    from brush_bridge.apps import procreate as ap

    home = tmp_path / "home"
    for d in ("Desktop", "Downloads", "Documents"):
        (home / d).mkdir(parents=True)
    monkeypatch.setattr(ap, "desktop_dir", lambda: home / "Desktop")
    monkeypatch.setattr(ap, "downloads_dir", lambda: home / "Downloads")
    monkeypatch.setattr(ap, "documents_dir", lambda: home / "Documents")
    monkeypatch.setattr(ap, "icloud_drive_dir", lambda: None)
    return app("procreate"), home


def test_adapter_detect(adapter):
    a, home = adapter
    insts = a.detect()
    assert len(insts) == 1
    inst = insts[0]
    assert inst.app_id == "procreate" and inst.version == "无法在电脑上检测"
    assert inst.data_dir == home / "Desktop" / "brush-bridge 导出"
    assert not inst.data_dir.exists()  # detect must not create anything
    assert a.desktop is False and "abr" in a.import_formats and a.native_format == "procreate"
    assert a.is_running(inst) is False


def test_adapter_detect_prefers_icloud(adapter, monkeypatch, tmp_path):
    from brush_bridge.apps import procreate as ap

    a, _home = adapter
    icloud = tmp_path / "iCloudDrive"
    icloud.mkdir()
    monkeypatch.setattr(ap, "icloud_drive_dir", lambda: icloud)
    inst = a.detect()[0]
    assert inst.data_dir == icloud / "brush-bridge 导出"


def test_adapter_install_and_list(adapter):
    a, home = adapter
    inst = a.detect()[0]
    res = a.install(inst, [_brush("甲"), Brush(name="乙", size_px=20)], set_name="测试集")
    assert res.ok, res.message
    assert len(res.files) == 1 and res.files[0].suffix == ".brushset"
    assert res.files[0].parent == inst.data_dir and res.files[0].exists()
    assert "Procreate" in res.message and "iPad" in res.message
    assert [b.name for b in codec("procreate").read(res.files[0])] == ["甲", "乙"]
    # second install does not overwrite
    res2 = a.install(inst, [Brush(name="丙")], set_name="测试集")
    assert res2.ok and res2.files[0] != res.files[0]

    # list_brushes finds files on the desktop export folder and inside "brush" folders
    deep = home / "Downloads" / "art" / "My Brushes" / "inner"
    deep.mkdir(parents=True)
    codec("procreate").write([Brush(name="深层")], deep / "deep.brush")
    too_deep = home / "Documents" / "a" / "b" / "c"
    too_deep.mkdir(parents=True)
    codec("procreate").write([Brush(name="太深")], too_deep / "hidden.brush")
    refs = a.list_brushes(inst)
    names = {r.name for r in refs}
    assert {"甲", "乙", "丙", "深层"} <= names
    assert "太深" not in names
    r = next(r for r in refs if r.name == "甲")
    assert r.group == "测试集.brushset" and "::" in r.key
    assert r.load().name == "甲"


def test_adapter_install_empty_and_files(adapter, tmp_path):
    a, _home = adapter
    inst = a.detect()[0]
    assert not a.install(inst, []).ok
    src = codec("procreate").write([Brush(name="x")], tmp_path / "x.brush")[0]
    other = tmp_path / "notes.txt"
    other.write_text("x")
    res = a.install_files(inst, [src, other])
    assert res.ok and len(res.files) == 1 and res.files[0].exists()
    assert not a.install_files(inst, [other]).ok


def test_adapter_scan_time_budget(tmp_path):
    from brush_bridge.apps import procreate as ap

    root = tmp_path / "brushes"
    for i in range(30):
        d = root / f"brush{i}" / "brush"
        d.mkdir(parents=True)
        (d / f"{i}.brush").write_bytes(b"PK")
    found = ap.scan_brush_files([root], budget_s=5)
    assert len(found) == 30
    assert ap.scan_brush_files([root], budget_s=0) == []
