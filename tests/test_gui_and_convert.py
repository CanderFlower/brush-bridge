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


def test_main_window_smoke(qapp):
    from brush_bridge.gui import app as gui

    w = gui.MainWindow()
    refs = [BrushRef(name=f"b{i}", key=str(i), group="g" if i % 2 else "",
                     loader=lambda i=i: Brush(name=f"b{i}"), thumbnail=lambda: Image.new("L", (10, 10), 128))
            for i in range(5)]
    w.transfer._show_refs(refs, "file", None)
    assert w.transfer.grid.count() == 5
    w.transfer._check_all(True)
    assert len(w.transfer.checked_refs()) == 5
    w.transfer.search.setText("b3")
    assert sum(not w.transfer.grid.item(i).isHidden() for i in range(5)) == 1
    pm = gui.pil_to_pixmap(Image.new("L", (300, 100), 200))
    assert pm.width() == gui.THUMB
    w.close()
