"""PaintTool SAI Ver.2 codec and app adapter.

Everything runs on temporary folders; the user's real SAI2 settings are only ever read (copied).
"""
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from brush_bridge.apps import sai2 as app_mod
from brush_bridge.core.model import Brush, Dynamic, Texture, WetMix
from brush_bridge.core.plugin import AppInstall, app, codec, sniff
from brush_bridge.formats import sai2 as s

WATER_TOOL = """tidstr=S:water
xidstr=S:
vkc=I:67
tabflt=I:0
tfmode=I:0
bzintf=I:1
name=U:水彩笔
addt=U:
.
modstr=I:1836216174
wavno=I:4
curszscl=I:1
cursize=I:12000
minsize=I:60
curdens=I:100
mindens=I:0
fomgid=I:0
fomcat=I:3
fomnam=U:水彩洇染
blotval=I:50
blotscl=I:100
blotinvf=I:0
blotivtf=I:1
blotsharpf=I:0
brslmin=I:0
brslsiz=I:50
brslrnd=I:0
brsldir=I:1
brslturnf=I:1
scatden=I:100
scatanc=I:0
scatang=I:0
scatanj=I:100
scatscl=I:125
scatszj=I:25
scatspc=I:4
scatsct=I:15
scathqsf=I:0
texgid=I:0
texcat=I:2
texnam=U:图画纸
texval=I:95
texscl=I:100
texscr=I:0
texinvf=I:0
texivtf=I:1
blend=I:30
dilution=I:30
persist=I:50
keepaf=I:0
blurthres=I:50
hardness=I:0
densamp=I:50
v1dnprsf=I:1
flattenf=I:1
szprsf=I:1
dnprsf=I:1
blprsf=I:1
dlprsf=I:0
szsens=I:100
dnsens=I:100
blsens=I:100
sbpos_patlist=I:272
previewbg=I:1
showf_fom=I:0
showf_tex=I:0
.
.
.
--EOF--
"""

SELPEN_TOOL = WATER_TOOL.replace("tidstr=S:water", "tidstr=S:selpen").replace("name=U:水彩笔", "name=U:选区笔")

SCATTER_INI_SJIS = """;
; 初期パラメータ
;
[Parameters]
\t; [角度制御]
\tAngleControl = 1
\tAngleJitter = 50
\tShapeSize = 64
\tSizeJitter = 30
\tSpacing = 150
\tScattering = 200
\tWxHRatio = 100:50
\tCount = 4
\tCountJitter = 25
\tHueJitter = 10
"""

BRSHAPE_INI_GBK = """; 笔刷形状参数
[Parameters]
\tOpacity = 80
\tScaling = 150
\tSpacing = 10
\tScattering = 5
"""


def _tip(w=64, h=48):
    im = Image.new("L", (w, h), 0)
    ImageDraw.Draw(im).ellipse((4, 4, w - 5, h - 5), fill=255)
    return im


def _noise(n=128, seed=1):
    return Image.fromarray((np.random.default_rng(seed).random((n, n)) * 255).astype(np.uint8), "L")


def _save_bmp(path: Path, img: Image.Image) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(s.encode_bmp(img))
    return path


@pytest.fixture(autouse=True)
def _no_real_settings(monkeypatch, tmp_path):
    """Never resolve assets from (or write to) the user's real SAI2 settings during tests."""
    monkeypatch.setattr(s, "default_settings_dir", lambda: tmp_path / "no-real-settings")


@pytest.fixture
def settings(tmp_path) -> Path:
    """A small synthetic SAI2 settings folder (2020-01-07+ layout)."""
    root = tmp_path / "settings"
    nrm = root / "custool" / "nrm"
    nrm.mkdir(parents=True)
    (nrm / "bak").mkdir()
    (nrm / "1.saitdat").write_bytes(WATER_TOOL.encode("utf-8"))
    (nrm / "2.saitdat").write_bytes(SELPEN_TOOL.encode("utf-8"))
    (nrm / "bak" / "7.saitdat").write_bytes(WATER_TOOL.encode("utf-8"))  # SAI2 keeps old versions here
    (nrm / "_0.saitset").write_bytes(b"curgid=I:1\ntabno=I:0\nnewgid=I:2\n.\n0=1\n.\n--EOF--\n")
    (nrm / "_1.saitgrp").write_bytes("vsbpos=I:0\nname=U:基本\ncsrbid0=I:-1879048191\ncsrbid1=I:0\ncsrbid2=I:0\n.\n"
                                     "0=1\n1=2\n.\n--EOF--\n".encode("utf-8"))
    (root / "custool" / "ink").mkdir()
    _save_bmp(root / "brushfom" / "brshape" / "水彩洇染.bmp", _tip(80, 80))
    (root / "brushfom" / "brshape" / "水彩洇染.ini").write_bytes(BRSHAPE_INI_GBK.encode("gbk"))
    (root / "brushfom" / "blotmap").mkdir(parents=True)
    _save_bmp(root / "brushtex" / "图画纸.bmp", _noise(512))
    _save_bmp(root / "papertex" / "画布.bmp", _noise(512, 2))
    _save_bmp(root / "scatter" / "星.bmp", _tip(40, 40))
    (root / "scatter" / "星.ini").write_bytes(SCATTER_INI_SJIS.encode("cp932"))
    return root


def _inst(root: Path, **extra) -> AppInstall:
    return AppInstall("sai2", "PaintTool SAI Ver.2", "2021-02-28", Path(r"Z:\no-such\sai2.exe"), root,
                      extra={"build": "2021-02-28", **extra})


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------------------------
# Text formats
# ---------------------------------------------------------------------------------------------
def test_sait_roundtrip_is_lossless():
    secs = s.parse_sait(WATER_TOOL)
    assert len(secs) == 4 and secs[2] == [] and secs[3] == []
    assert s.sait_values(secs[0])["name"] == "水彩笔"
    assert s.sait_values(secs[1])["cursize"] == 12000
    assert s.dump_sait(secs) == WATER_TOOL


def test_ini_decoding_utf8_sjis_gbk():
    assert "初期" in s.decode_text(SCATTER_INI_SJIS.encode("cp932"))
    assert "笔刷形状" in s.decode_text(BRSHAPE_INI_GBK.encode("gbk"))
    assert "初期" in s.decode_text(SCATTER_INI_SJIS.encode("utf-8"))
    p = s.parse_ini(s.decode_text(SCATTER_INI_SJIS.encode("cp932")))
    assert p["Count"] == "4" and p["WxHRatio"] == "100:50"


def test_format_ini_parses_back():
    values = {"AngleControl": "0", "Angle": "+15", "Spacing": "120", "WxHRatio": "100:50"}
    text = s.format_ini(values)
    assert "[Parameters]" in text and "\r" not in text
    assert s.parse_ini(text) == values


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------
def test_read_tool_resolves_shape_and_texture(settings):
    [b] = codec("sai2").read(settings / "custool" / "nrm" / "1.saitdat")
    assert b.name == "水彩笔" and b.group == "基本"
    assert b.size_px == pytest.approx(1200 * 1.25)  # cursize 0.1 px x shape scaling 125 %
    assert b.min_size_px == pytest.approx(b.size_px * 0.6)
    assert b.size_dynamic.pressure == pytest.approx(0.4)
    assert b.tips and b.tips[0].size == (80, 80) and b.tips[0].mode == "L"
    assert b.spacing == pytest.approx(0.04) and b.scatter == pytest.approx(0.15)
    assert b.angle_jitter == pytest.approx(1.0) and b.size_dynamic.jitter == pytest.approx(0.25)
    assert b.texture is not None and b.texture.name == "图画纸" and b.texture.depth == pytest.approx(0.95)
    assert b.wet.enabled and b.wet.mix == pytest.approx(0.3) and b.wet.wetness == pytest.approx(0.3)
    assert b.source.native["sai2_tid"] == "water"


def test_read_settings_skips_selection_tools_and_adds_loose_shapes(settings):
    brushes = codec("sai2").read(settings)
    names = [b.name for b in brushes]
    assert "选区笔" not in names
    assert names.count("水彩笔") == 1
    star = next(b for b in brushes if b.name == "星")
    assert star.count == 4 and star.count_jitter == pytest.approx(0.25)
    assert star.spacing == pytest.approx(1.5) and star.scatter == pytest.approx(2.0)
    assert star.size_px == 64 and star.roundness == pytest.approx(0.5)
    assert star.color.hue_jitter == pytest.approx(0.1)


def test_read_brush_shape_with_gbk_ini(settings):
    [b] = codec("sai2").read(settings / "brushfom" / "brshape" / "水彩洇染.bmp")
    assert b.flow == pytest.approx(0.8) and b.spacing == pytest.approx(0.1)
    assert b.size_px == pytest.approx(80 * 1.5)


def test_sniff_claims_only_sai2_bmps(settings, tmp_path):
    plain = _save_bmp(tmp_path / "loose" / "photo.bmp", _noise(64))
    info = sniff(plain)
    assert info is None or info.format_id != "sai2"
    assert sniff(settings / "scatter" / "星.bmp").format_id == "sai2"
    assert sniff(settings / "scatter" / "星.ini").format_id == "sai2"
    # a loose BMP with a SAI2 parameter INI next to it is a scatter shape
    loose = _save_bmp(tmp_path / "dl" / "leaf.bmp", _tip())
    (tmp_path / "dl" / "leaf.ini").write_bytes(SCATTER_INI_SJIS.encode("cp932"))
    info = sniff(loose)
    assert info.format_id == "sai2" and info.version == "scatter"
    assert info.details["min_build"] == "2018-09-17"
    tool = sniff(settings / "custool" / "nrm" / "1.saitdat")
    assert tool.format_id == "sai2" and tool.brush_count == 1
    assert sniff(settings).format_id == "sai2"


def test_sniff_reports_non_grey_bmp_with_fix(settings, tmp_path):
    rgb = settings / "scatter" / "彩色.bmp"
    Image.new("RGB", (32, 32), (255, 0, 0)).save(rgb)
    info = sniff(rgb)
    assert info.format_id == "sai2" and info.problems and info.fixes
    out = info.fixes[0].apply(rgb, tmp_path / "fixed")
    with Image.open(out[0]) as im:
        assert im.mode in ("L", "P") and im.size == (32, 32)


def test_compatibility_by_build():
    from brush_bridge.core.plugin import FileInfo

    c = codec("sai2")
    info = FileInfo(Path("x.bmp"), "sai2", "", "brshape", 1, {"min_build": "2019-05-18"})
    assert c.compatibility(info, "sai2", "2018-10-01")
    assert "2019-05-18" in c.compatibility(info, "sai2", "2018-10-01")
    assert c.compatibility(info, "sai2", "2021-02-28") is None
    assert c.compatibility(info, "krita", "5.0") is None


# ---------------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------------
def _brushes():
    scat = Brush(name="测试散布", tips=[_tip()], size_px=40, spacing=1.2, scatter=1.5, count=3, count_jitter=0.2,
                 angle_jitter=0.5, size_dynamic=Dynamic(pressure=0.5, jitter=0.3))
    shaped = Brush(name="测试形状", tips=[_tip(50, 30)], size_px=30, spacing=0.1, opacity=0.8,
                   size_dynamic=Dynamic(pressure=1.0), texture=Texture(image=_noise(100), name="噪点", depth=0.6))
    wet = Brush(name="湿画笔", size_px=60, hardness=0.5, wet=WetMix(enabled=True, mix=0.4, load=0.5, wetness=0.5))
    pen = Brush(name="圆头", size_px=12, hardness=1.0)
    eraser = Brush(name="橡皮", size_px=25, is_eraser=True)
    return [scat, shaped, wet, pen, eraser]


def test_write_package_and_read_back(tmp_path):
    files = codec("sai2").write(_brushes(), tmp_path / "pkg.saitdat")
    pkg = tmp_path / "pkg"
    rel = {str(f.relative_to(pkg)) for f in files}
    assert {"scatter\\测试散布.bmp", "scatter\\测试散布.ini", "brushfom\\brshape\\测试形状.bmp",
            "brushtex\\噪点.bmp", "安装说明.txt"} <= rel
    assert len(list((pkg / "custool" / "nrm").glob("*.saitdat"))) == 5
    with Image.open(pkg / "brushtex" / "噪点.bmp") as im:
        assert im.size == (512, 512) and im.mode in ("L", "P")
    raw = (pkg / "scatter" / "测试散布.bmp").read_bytes()
    assert raw[:2] == b"BM" and int.from_bytes(raw[28:30], "little") == 8
    # tips keep model polarity (255 = paint) when read back
    with Image.open(pkg / "scatter" / "测试散布.bmp") as im:
        assert np.array_equal(np.asarray(im.convert("L")), np.asarray(_tip()))

    back = {b.name: b for b in codec("sai2").read(pkg)}
    scat = back["测试散布"]
    assert scat.source.native["sai2_tid"] == "scatter"
    assert scat.count == 3 and scat.spacing == pytest.approx(1.2) and scat.scatter == pytest.approx(1.5)
    assert scat.size_px == pytest.approx(40, rel=0.05)
    shaped = back["测试形状"]
    assert shaped.tips and shaped.texture is not None and shaped.texture.name == "噪点"
    assert shaped.opacity == pytest.approx(0.8) and shaped.size_dynamic.pressure == pytest.approx(1.0)
    assert back["湿画笔"].wet.enabled
    assert back["圆头"].hardness == pytest.approx(1.0)
    assert back["橡皮"].is_eraser
    # writing again never overwrites: a second package folder is created
    files2 = codec("sai2").write(_brushes()[:1], tmp_path / "pkg")
    assert all(pkg not in f.parents for f in files2)


def test_native_roundtrip_keeps_tool_bytes(settings, tmp_path):
    [b] = codec("sai2").read(settings / "custool" / "nrm" / "1.saitdat")
    codec("sai2").write([b], tmp_path / "out")
    [dat] = (tmp_path / "out" / "custool" / "nrm").glob("*.saitdat")
    assert dat.read_bytes() == WATER_TOOL.encode("utf-8")
    assert (tmp_path / "out" / "brushfom" / "brshape" / "水彩洇染.ini").read_bytes() == \
        BRSHAPE_INI_GBK.encode("gbk")


# ---------------------------------------------------------------------------------------------
# App adapter
# ---------------------------------------------------------------------------------------------
def test_history_and_pe_build(tmp_path):
    (tmp_path / "history.txt").write_bytes("\ufeff2021-02-28版\n  修正\n2020-01-07b版\n".encode("utf-8"))
    assert app_mod.history_build(tmp_path) == "2021-02-28"
    d = app_mod.pe_build(Path(sys.executable))
    assert d is None or (len(d) == 10 and d[4] == "-")


def test_install_closed_creates_group_with_backups(settings, monkeypatch):
    ad = app("sai2")
    monkeypatch.setattr(ad, "is_running", lambda inst: False)
    # an unrelated user file with the same name must survive
    _save_bmp(settings / "scatter" / "测试散布.bmp", _noise(20))
    before = _snapshot(settings)
    res = ad.install(_inst(settings), _brushes())
    assert res.ok and not res.needs_app_closed
    assert "导入笔刷" in res.message and "散布形状" in res.message
    nrm = settings / "custool" / "nrm"
    assert (settings / "scatter" / "测试散布 (2).bmp").is_file()
    assert (settings / "scatter" / "测试散布.bmp").read_bytes() == before["scatter\\测试散布.bmp"]
    new_ids = sorted(int(f.stem) for f in nrm.glob("*.saitdat") if f.name not in ("1.saitdat", "2.saitdat"))
    assert new_ids[0] == 8  # after the highest id in custool and custool\bak
    groups = s.tool_groups(nrm)
    assert [g[1] for g in groups] == ["基本", "导入笔刷"]
    assert [t for _s, t in groups[1][2]] == new_ids
    # existing files are only modified after a backup, all others untouched
    changed = {k for k, v in before.items() if (settings / k).read_bytes() != v}
    assert changed == {"custool\\nrm\\_0.saitset"}
    assert res.backup and res.backup.read_bytes() == before["custool\\nrm\\_0.saitset"]

    refs = ad.list_brushes(_inst(settings))
    group_refs = [r.name for r in refs if r.group.endswith("导入笔刷")]
    assert group_refs == ["测试散布", "测试形状", "湿画笔", "圆头", "橡皮"]
    assert refs[0].thumbnail() is not None
    assert next(r for r in refs if r.name == "测试散布").load().count == 3

    # installing the same brushes again adds nothing new
    res2 = ad.install(_inst(settings), _brushes())
    assert res2.ok and "没有重复添加" in res2.message
    assert len(list(nrm.glob("*.saitdat"))) == 2 + 5


def test_install_running_only_copies_assets(settings, monkeypatch):
    ad = app("sai2")
    monkeypatch.setattr(ad, "is_running", lambda inst: True)
    before = _snapshot(settings)
    res = ad.install(_inst(settings), _brushes())
    assert res.ok and res.needs_app_closed
    assert "关闭 SAI2" in res.message and "重新启动" in res.message
    after = _snapshot(settings)
    assert all(after[k] == v for k, v in before.items())
    assert not any(k.startswith("custool") for k in set(after) - set(before))
    assert "scatter\\测试散布.bmp" in after and "brushtex\\噪点.bmp" in after


def test_install_running_nothing_but_tools_is_refused(settings, monkeypatch):
    ad = app("sai2")
    monkeypatch.setattr(ad, "is_running", lambda inst: True)
    res = ad.install(_inst(settings), [Brush(name="圆头", size_px=12, hardness=1.0)])
    assert not res.ok and res.needs_app_closed


def test_install_legacy_layout(tmp_path, monkeypatch):
    ad = app("sai2")
    monkeypatch.setattr(ad, "is_running", lambda inst: False)
    exe_dir = tmp_path / "SAI2-2019"
    (exe_dir / "scatter").mkdir(parents=True)
    (exe_dir / "brshape").mkdir()
    res = ad.install(_inst(exe_dir, legacy_layout=True, build="2019-07-28"), _brushes()[:2])
    assert res.ok
    assert (exe_dir / "scatter" / "测试散布.bmp").is_file()
    assert (exe_dir / "brshape" / "测试形状.bmp").is_file()
    assert not (exe_dir / "custool").exists()


def test_install_missing_settings(tmp_path):
    res = app("sai2").install(_inst(tmp_path / "missing"), _brushes())
    assert not res.ok and "启动" in res.message


def test_install_files_from_package(settings, tmp_path, monkeypatch):
    ad = app("sai2")
    monkeypatch.setattr(ad, "is_running", lambda inst: False)
    codec("sai2").write(_brushes()[:2], tmp_path / "pkg")
    res = ad.install_files(_inst(settings), [tmp_path / "pkg"])
    assert res.ok
    names = [r.name for r in ad.list_brushes(_inst(settings)) if r.group.endswith("导入笔刷")]
    assert names == ["测试散布", "测试形状"]
    res = ad.install_files(_inst(settings), [tmp_path / "pkg" / "scatter" / "测试散布.bmp"])
    assert res.ok


def test_detect_runs():
    for inst in app("sai2").detect():
        assert inst.exe.name.lower() == "sai2.exe" and inst.data_dir is not None


@pytest.mark.skipif(not (Path.home() / "Documents" / "SYSTEMAX Software Development" / "SAIv2" / "settings").is_dir(),
                    reason="no local SAI2 settings")
def test_real_settings_copy(tmp_path, monkeypatch):
    real = Path.home() / "Documents" / "SYSTEMAX Software Development" / "SAIv2" / "settings"
    copy = tmp_path / "real-copy"
    shutil.copytree(real, copy)
    brushes = codec("sai2").read(copy)
    assert brushes and all(b.size_px > 0 for b in brushes)
    refs = s.list_settings(copy)
    assert any(r.group.startswith("自定义工具") for r in refs)
    for f in list(copy.glob("custool/*/*.saitdat"))[:50]:
        text = s.decode_text(f.read_bytes())
        assert s.dump_sait(s.parse_sait(text)) == text
    ad = app("sai2")
    monkeypatch.setattr(ad, "is_running", lambda inst: False)
    res = ad.install(_inst(copy), _brushes())
    assert res.ok
    assert [r.name for r in ad.list_brushes(_inst(copy)) if r.group.endswith("导入笔刷")][:5] == \
        ["测试散布", "测试形状", "湿画笔", "圆头", "橡皮"]
