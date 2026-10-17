import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image

from brush_bridge.core import convert
from brush_bridge.core.model import Brush, BrushRef

QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def test_open_export_diagnose(tmp_path):
    src = tmp_path / "a.abr"
    convert.export([Brush(name="A", size_px=20), Brush(name="B", tips=[Image.new("L", (8, 8), 255)])], "abr", src)
    o = convert.open_file(src)
    assert not o.error and [r.name for r in o.refs] == ["A", "B"]
    brushes, errors = convert.load_brushes(o.refs)
    assert not errors and len(brushes) == 2
    files = convert.export(brushes, "images", tmp_path / "png" / "x.png")
    assert len(files) == 2 and all(f.exists() for f in files)
    info = convert.diagnose(src, detected=[])
    assert info and info.format_id == "abr"
    assert convert.open_file(tmp_path / "png").error  # a folder is not a brush file
    assert len(convert.expand_paths([tmp_path])) >= 3


def _wait(qapp, cond, timeout=5.0):
    import time

    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def _refs(n, counter=None, groups=("", "g")):
    def thumb(i):
        def f():
            if counter is not None:
                counter.append(i)
            return Image.new("L", (10, 10), 128)
        return f

    return [BrushRef(name=f"b{i}", key=str(i), group=groups[i % len(groups)],
                     loader=lambda i=i: Brush(name=f"b{i}"), thumbnail=thumb(i)) for i in range(n)]


@pytest.fixture
def win(qapp, monkeypatch):
    """Main window that does not scan the real computer (no auto-selected source racing the test)."""
    from brush_bridge.gui import app as gui

    monkeypatch.setattr(convert, "detect_all", lambda: [])
    w = gui.MainWindow()
    w.resize(1100, 700)
    w.show()
    _wait(qapp, lambda: False, 0.1)  # let the (empty) detection finish
    yield gui, w
    w.close()
    w.transfer.thumb_loader.wait()


def test_main_window_smoke(qapp, win):
    gui, w = win
    t = w.transfer
    t._show_refs(_refs(5), "file", None)
    assert t.model.rowCount() == 5 and t.proxy.rowCount() == 5
    t._check_all(True)
    assert len(t.checked_refs()) == 5
    t.search.setText("b3")
    assert t.proxy.rowCount() == 1
    pm = gui.pil_to_pixmap(Image.new("L", (300, 100), 200))
    assert pm.width() == gui.THUMB


def test_grid_checking_updates_count(qapp, win):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    gui, w = win
    t = w.transfer
    t._show_refs(_refs(20), "file", None)
    assert t.count.text() == "已选 0 个" and t.go.text() == "开始导入"

    # through the model API (what the delegate/view use)
    assert t.proxy.setData(t.proxy.index(2, 0), Qt.Checked, Qt.CheckStateRole)
    assert t.model.checked == {2}
    assert t.proxy.index(2, 0).data(Qt.CheckStateRole) == Qt.Checked
    assert t.count.text() == "已选 1 个" and "（1）" in t.go.text()
    t.proxy.setData(t.proxy.index(2, 0), Qt.Unchecked, Qt.CheckStateRole)
    assert not t.model.checked and t.count.text() == "已选 0 个"

    # a plain click toggles, a second click un-toggles, shift+click checks the range
    grid = t.grid
    qapp.processEvents()
    centre = lambda r: grid.visualRect(t.proxy.index(r, 0)).center()  # noqa: E731
    QTest.mouseClick(grid.viewport(), Qt.LeftButton, Qt.NoModifier, centre(1))
    assert t.model.checked == {1} and t.count.text() == "已选 1 个"
    QTest.mouseClick(grid.viewport(), Qt.LeftButton, Qt.ShiftModifier, centre(4))
    assert t.model.checked == {1, 2, 3, 4} and "（4）" in t.go.text()
    QTest.mouseClick(grid.viewport(), Qt.LeftButton, Qt.NoModifier, centre(3))
    assert t.model.checked == {1, 2, 4}

    # select all / none / invert act on the filtered rows only
    t.group.select_group("g")  # odd rows
    t._check_all(True)
    assert t.model.checked == {1, 2, 4} | set(range(1, 20, 2))
    t._invert()
    assert t.model.checked == {2, 4}
    t.group.select_group(None)
    t._check_all(False)
    assert not t.model.checked and t.count.text() == "已选 0 个"

    # the checked state is painted: blue check box in the corner of the cell
    t.model.set_checked([0], True)
    img = grid.viewport().grab().toImage()
    r0 = grid.visualRect(t.proxy.index(0, 0))
    on = img.pixelColor(r0.left() + 2 + 6 + 4, r0.top() + 2 + 6 + 4)
    r1 = grid.visualRect(t.proxy.index(1, 0))
    off = img.pixelColor(r1.left() + 2 + 6 + 4, r1.top() + 2 + 6 + 4)
    assert on.blue() > 200 and on.red() < 120, on.name()
    assert off.red() > 200, off.name()


def test_grid_large_library_is_virtualised(qapp, win):
    import time

    gui, w = win
    t = w.transfer
    calls: list[int] = []
    refs = _refs(5000, calls)
    t0 = time.perf_counter()
    t._show_refs(refs, "file", None)
    qapp.processEvents()
    assert time.perf_counter() - t0 < 1.0
    assert t.proxy.rowCount() == 5000

    # only on-screen (and a screen of prefetch) thumbnails get requested
    assert _wait(qapp, lambda: len(t.thumb_cache) > 0)
    _wait(qapp, lambda: t.thumb_loader.pending_count() == 0, 3)
    assert 0 < t.thumb_loader.requested < 300, t.thumb_loader.requested
    assert len(calls) < 300 and max(calls) < 400
    assert t.model.pixmap(0, request=False) is not None

    # scrolling to the bottom loads the bottom rows, not everything in between
    before = t.thumb_loader.requested
    t.grid.scrollToBottom()
    assert _wait(qapp, lambda: t.model.pixmap(4999, request=False) is not None)
    assert t.thumb_loader.requested - before < 300

    # filtering is cheap and does not rebuild anything
    t0 = time.perf_counter()
    t.search.setText("b4999")
    assert t.proxy.rowCount() == 1
    t.search.setText("b1")
    n = t.proxy.rowCount()
    t.search.setText("")
    t.group.select_group("g")
    assert t.proxy.rowCount() == 2500
    t.group.select_group(None)
    assert time.perf_counter() - t0 < 0.5
    assert n == 1111 and t.proxy.rowCount() == 5000
    t0 = time.perf_counter()
    t._check_all(True)
    assert len(t.checked_refs()) == 5000 and t.count.text() == "已选 5000 个"
    t._invert()
    assert not t.checked_refs()
    assert time.perf_counter() - t0 < 0.5


def test_thumbnail_loader_dedup_hold_and_retain(qapp):
    from PySide6.QtGui import QImage

    from brush_bridge.gui.brush_grid import ThumbnailLoader

    seen = []
    loader = ThumbnailLoader(lambda img: QImage(4, 4, QImage.Format_RGBA8888), max_threads=1)

    def job(k):
        def f():
            seen.append(k)
            return Image.new("L", (4, 4), 255)
        return f

    loader.hold(30)  # e.g. the user is scrolling: nothing starts
    for k in range(10):
        loader.request(k, job(k))
        loader.request(k, job(k))  # duplicates are ignored
    assert loader.requested == 10
    _wait(qapp, lambda: False, 0.1)
    assert seen == [] and loader.pending_count() == 10
    assert loader.retain({7, 8, 9}) == 7  # rows scrolled away are cancelled
    got = []
    loader.ready.connect(lambda k, q: got.append(k))
    loader.resume()
    assert _wait(qapp, lambda: len(got) == 3)
    assert seen == [9, 8, 7]  # most recently requested (= currently on screen) first
    loader.shutdown()
    assert loader.wait()


def test_targets_offer_csp(qapp, win):
    from brush_bridge.core.convert import DetectedApp
    from brush_bridge.core.plugin import AppInstall, app

    gui, w = win
    t = w.transfer
    csp = DetectedApp(app("csp"), AppInstall("csp", "Clip Studio Paint", "4.0.0"))
    t.set_detected([csp])
    items = [(t.target.itemText(i), t.target.itemData(i)) for i in range(t.target.count())]
    kinds = [(d[0], getattr(d[1], "id", None)) for _, d in items]
    assert ("app", None) in kinds and ("app_direct", None) in kinds
    assert ("codec", "csp") in kinds
    assert "💾 导出为文件：Clip Studio Paint 笔刷 (.sut)" in [x for x, _ in items]
    writable = {c.id for c in convert.writable_codecs()}
    assert {d[1].id for _, d in items if d[0] == "codec"} == writable
    # the artist's choice survives a re-detection (e.g. the refresh after a transfer)
    i = [x for x, _ in items].index("💾 导出为文件：Clip Studio Paint 笔刷 (.sut)")
    t.target.setCurrentIndex(i)
    t.set_detected([DetectedApp(app("csp"), AppInstall("csp", "Clip Studio Paint", "4.0.0"))])
    assert t.target.currentData()[0] == "codec" and t.target.currentData()[1].id == "csp"


def test_group_tree_nested(qapp, win):
    gui, w = win
    t = w.transfer
    groups = ["笔刷包 / B / 常用", "", "笔刷包 / B / 叠加", "笔刷包 / A", "基础", "笔刷包 / B / 常用"]
    refs = [BrushRef(name=f"n{i}", key=str(i), group=g) for i, g in enumerate(groups)]
    t._show_refs(refs, "file", None)
    assert not t.group.isHidden()
    root = t.group.topLevelItem(0)
    assert root.text(0) == "全部笔刷（6）"
    # the app's own order is kept (not sorted), sub groups nest, counts include sub groups
    assert [root.child(i).text(0) for i in range(root.childCount())] == ["笔刷包（4）", "基础（1）", "未分组（1）"]
    pack = root.child(0)
    assert [pack.child(i).text(0) for i in range(pack.childCount())] == ["B（3）", "A（1）"]
    assert [pack.child(0).child(i).text(0) for i in range(2)] == ["常用（2）", "叠加（1）"]

    t.group.select_group("笔刷包 / B")
    assert sorted(t.proxy.source_rows()) == [0, 2, 5]
    t.group.select_group("笔刷包")
    assert sorted(t.proxy.source_rows()) == [0, 2, 3, 5]
    t.group.select_group("")
    assert t.proxy.source_rows() == [1]
    t.group.select_group(None)
    assert t.proxy.rowCount() == 6

    # flat sources hide the tree
    t._show_refs([BrushRef(name="x", key="0")], "file", None)
    assert t.group.isHidden()