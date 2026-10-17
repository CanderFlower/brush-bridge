"""Brush Bridge desktop GUI (PySide6). Artist-first: pick brushes, pick a target, click once."""
from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from .. import __version__
from ..core import convert
from ..core.convert import DetectedApp, OpenedFile
from ..core.model import BrushRef
from ..core.plugin import FileInfo, codecs, load_plugins
from ..core.system import export_dir, open_in_explorer
from .brush_grid import THUMB, BrushGridView, BrushListModel, PixmapCache, ThumbnailLoader, pil_to_qimage

log = logging.getLogger(__name__)

ROLE_SRC = Qt.UserRole + 2

# Friendlier names for export targets (codec names come from the format plugins).
CODEC_LABELS = {
    "csp": "Clip Studio Paint 笔刷 (.sut)",
}


def codec_label(c) -> str:
    return CODEC_LABELS.get(c.id, c.name)

STYLE = """
QWidget { font-size: 13px; }
QMainWindow, QTabWidget::pane { background: #f6f6f8; }
QTabBar::tab { padding: 8px 18px; font-size: 14px; }
QTabBar::tab:selected { font-weight: bold; }
QListWidget#sources { background: white; border: 1px solid #ddd; border-radius: 6px; padding: 4px; }
QListWidget#sources::item { padding: 7px 6px; border-radius: 4px; }
QListWidget#sources::item:selected { background: #2f6fed; color: white; }
QListView#grid { background: white; border: 1px solid #ddd; border-radius: 6px; }
QPushButton { padding: 6px 14px; border-radius: 5px; border: 1px solid #c8c8d0; background: white; }
QPushButton:hover { background: #eef2fb; }
QPushButton#primary { background: #2f6fed; color: white; border: none; font-size: 15px; font-weight: bold; padding: 10px 26px; }
QPushButton#primary:hover { background: #255bd0; }
QPushButton#primary:disabled { background: #a9bde8; }
QLabel#title { font-size: 16px; font-weight: bold; }
QLabel#hint { color: #777; }
QFrame#drop { border: 2px dashed #9aa7c2; border-radius: 12px; background: white; }
"""


# ---------------------------------------------------------------------------------------------
# background work
# ---------------------------------------------------------------------------------------------
class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Job(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self):
        try:
            try:
                res = self.fn()
            except Exception as e:
                log.exception("background job failed")
                self.signals.failed.emit(f"{e}\n\n{traceback.format_exc(limit=3)}")
            else:
                self.signals.done.emit(res)
        except RuntimeError:  # receiver already gone (app shutting down)
            log.debug("background job finished after shutdown")


_jobs: set = set()


def run_bg(fn: Callable[[], Any], on_done: Callable[[Any], None], on_fail: Optional[Callable[[str], None]] = None):
    job = _Job(fn)
    _jobs.add(job.signals)

    def finish(*_):
        _jobs.discard(job.signals)

    job.signals.done.connect(on_done)
    job.signals.done.connect(finish)
    job.signals.failed.connect(on_fail or (lambda msg: QMessageBox.warning(None, "出错了", msg)))
    job.signals.failed.connect(finish)
    QThreadPool.globalInstance().start(job)


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def pil_to_pixmap(img: Image.Image, size: int = THUMB) -> QPixmap:
    """GUI thread only; workers use ``pil_to_qimage``."""
    return QPixmap.fromImage(pil_to_qimage(img, size))


def result_dialog(parent, ok: bool, message: str, files: list[Path]):
    box = QMessageBox(parent)
    box.setWindowTitle("完成" if ok else "没有完全成功")
    box.setIcon(QMessageBox.Information if ok else QMessageBox.Warning)
    box.setText(message)
    if files:
        box.setDetailedText("\n".join(str(f) for f in files))
        btn = box.addButton("打开文件所在位置", QMessageBox.ActionRole)
    else:
        btn = None
    box.addButton("好的", QMessageBox.AcceptRole)
    box.exec()
    if btn is not None and box.clickedButton() is btn:
        open_in_explorer(Path(files[0]))


# ---------------------------------------------------------------------------------------------
# Page 1: move brushes
# ---------------------------------------------------------------------------------------------
class TransferPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.detected: list[DetectedApp] = []
        self.files: list[OpenedFile] = []
        self.source_token = 0
        self.thumb_loader = ThumbnailLoader(pil_to_qimage, max_threads=2, parent=self)
        self.thumb_cache = PixmapCache(2000)
        self.model = BrushListModel(self.thumb_loader, self.thumb_cache, self)
        self.model.checkedChanged.connect(self._update_count)

        root = QHBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split)

        # --- left: sources
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        t = QLabel("① 选择笔刷来源")
        t.setObjectName("title")
        lv.addWidget(t)
        self.sources = QListWidget()
        self.sources.setObjectName("sources")
        self.sources.currentItemChanged.connect(self._source_changed)
        lv.addWidget(self.sources, 1)
        add = QPushButton("＋ 打开笔刷文件…")
        add.clicked.connect(self._open_dialog)
        lv.addWidget(add)
        h = QLabel("提示：也可以把笔刷文件或文件夹\n直接拖进这个窗口（比如从 iPad\n传过来的 Procreate 笔刷）。")
        h.setObjectName("hint")
        lv.addWidget(h)
        split.addWidget(left)

        # --- right: brushes + target
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.title = QLabel("② 勾选要搬运的笔刷")
        self.title.setObjectName("title")
        head.addWidget(self.title, 1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 搜索笔刷名称")
        self.search.textChanged.connect(self._filter)
        self.search.setMaximumWidth(220)
        head.addWidget(self.search)
        self.group = QComboBox()
        self.group.setMinimumWidth(160)
        self.group.currentIndexChanged.connect(self._filter)
        head.addWidget(self.group)
        rv.addLayout(head)

        self.grid = BrushGridView(self.model, parent=self)
        self.grid.setObjectName("grid")
        self.grid.checkAllRequested.connect(lambda: self._check_all(True))
        self.proxy = self.grid.proxy
        rv.addWidget(self.grid, 1)
        self.status = QLabel("")
        self.status.setObjectName("hint")
        rv.addWidget(self.status)

        sel = QHBoxLayout()
        for text, fn, tip in (("全选", lambda: self._check_all(True), "勾选当前显示的全部笔刷"),
                              ("全不选", lambda: self._check_all(False), "取消勾选当前显示的全部笔刷"),
                              ("反选", self._invert, "把当前显示的笔刷勾选状态反过来")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            sel.addWidget(b)
        sel.addStretch(1)
        self.count = QLabel("已选 0 个")
        sel.addWidget(self.count)
        rv.addLayout(sel)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        rv.addWidget(line)
        tgt = QHBoxLayout()
        t3 = QLabel("③ 导入到")
        t3.setObjectName("title")
        tgt.addWidget(t3)
        self.target = QComboBox()
        self.target.setMinimumWidth(320)
        self.target.setMinimumHeight(34)
        tgt.addWidget(self.target, 1)
        self.go = QPushButton("开始导入")
        self.go.setObjectName("primary")
        self.go.clicked.connect(self._go)
        tgt.addWidget(self.go)
        rv.addLayout(tgt)
        split.addWidget(right)
        split.setSizes([260, 900])

    # sources ---------------------------------------------------------------------------------
    def set_detected(self, detected: list[DetectedApp]):
        self.detected = detected
        self._rebuild_sources()
        self._rebuild_targets()

    def _header(self, text: str):
        it = QListWidgetItem(text)
        it.setFlags(Qt.NoItemFlags)
        f = it.font()
        f.setBold(True)
        it.setFont(f)
        it.setForeground(QColor("#666"))
        self.sources.addItem(it)

    def _rebuild_sources(self):
        cur = self.sources.currentItem().data(ROLE_SRC) if self.sources.currentItem() else None
        self.sources.blockSignals(True)
        self.sources.clear()
        self._header("电脑上的绘画软件")
        for d in self.detected:
            if d.install is None:
                continue
            status = "（运行中）" if d.install.running else ""
            label = d.install.label if d.adapter.desktop else f"{d.adapter.name}（电脑里找到的 iPad 笔刷文件）"
            it = QListWidgetItem(f"{label}{status}")
            it.setData(ROLE_SRC, ("app", d))
            it.setToolTip(str(d.install.data_dir or d.install.exe or ""))
            self.sources.addItem(it)
        if self.files:
            self._header("打开的文件")
            for f in self.files:
                n = f.info.brush_count if f.info else 0
                it = QListWidgetItem(f"📄 {f.path.name}" + (f"（{n}）" if n >= 0 else ""))
                it.setData(ROLE_SRC, ("file", f))
                it.setToolTip(str(f.path))
                self.sources.addItem(it)
        self.sources.blockSignals(False)
        for i in range(self.sources.count()):
            if cur is not None and self.sources.item(i).data(ROLE_SRC) is not None and \
                    self.sources.item(i).data(ROLE_SRC)[1] is cur[1]:
                self.sources.setCurrentRow(i)
                return

    @staticmethod
    def _target_key(data) -> Optional[tuple]:
        if not data:
            return None
        kind, obj = data
        if kind == "codec":
            return kind, obj.id
        return kind, obj.adapter.id, obj.label

    def _rebuild_targets(self):
        prev = self._target_key(self.target.currentData())
        self.target.blockSignals(True)
        self.target.clear()
        for d in self.detected:
            if not d.adapter.desktop:
                self.target.addItem(f"→ {d.adapter.name}（生成文件，传到 iPad 上点开）", ("app", d))
            elif d.install is not None and d.adapter.id == "csp":
                self.target.addItem(f"→ {d.install.label}（生成文件，拖进「子工具」面板）", ("app", d))
                self.target.addItem(f"→ {d.install.label}（实验：直接写入，需先关闭 CSP，会自动备份）", ("app_direct", d))
            elif d.install is not None:
                self.target.addItem(f"→ {d.install.label}（自动导入）", ("app", d))
        for c in codecs():
            if c.can_write:
                self.target.addItem(f"💾 导出为文件：{codec_label(c)}", ("codec", c))
        if prev is not None:  # keep the artist's choice across re-detection (e.g. after a transfer)
            for i in range(self.target.count()):
                if self._target_key(self.target.itemData(i)) == prev:
                    self.target.setCurrentIndex(i)
                    break
        self.target.blockSignals(False)

    def add_files(self, paths: list[Path]):
        paths = convert.expand_paths(paths)
        if not paths:
            return
        self.win.busy(f"正在读取 {len(paths)} 个文件…")

        def work():
            return [convert.open_file(p) for p in paths]

        def done(opened: list[OpenedFile]):
            self.win.idle()
            bad = [o for o in opened if o.error]
            good = [o for o in opened if not o.error]
            known = {str(f.path) for f in self.files}
            self.files += [g for g in good if str(g.path) not in known]
            self._rebuild_sources()
            if good:
                for i in range(self.sources.count()):
                    src = self.sources.item(i).data(ROLE_SRC)
                    if src and src[0] == "file" and src[1].path == good[-1].path:
                        self.sources.setCurrentRow(i)
            if bad:
                QMessageBox.warning(self, "有文件无法打开", "\n".join(f"{b.path.name}：{b.error}" for b in bad))

        run_bg(work, done, lambda m: (self.win.idle(), QMessageBox.warning(self, "出错了", m)))

    def _open_dialog(self):
        exts = " ".join(f"*{e}" for c in codecs() for e in c.extensions)
        files, _ = QFileDialog.getOpenFileNames(self, "选择笔刷文件", str(Path.home() / "Downloads"),
                                                f"笔刷文件 ({exts});;所有文件 (*)")
        if files:
            self.add_files([Path(f) for f in files])

    def _source_changed(self, cur: QListWidgetItem, _prev=None):
        if cur is None or cur.data(ROLE_SRC) is None:
            return
        kind, obj = cur.data(ROLE_SRC)
        self.source_token += 1
        token = self.source_token
        self.model.set_refs([], None)
        self.title.setText(f"② 勾选要搬运的笔刷 — {cur.text()}")
        self.status.setText("正在读取笔刷列表…")

        def work():
            if kind == "app":
                return obj.adapter.list_brushes(obj.install)
            return obj.refs

        def done(refs):
            if token != self.source_token:
                return
            self._show_refs(refs, kind, obj)

        run_bg(work, done, lambda m: self.status.setText("读取失败：" + m.splitlines()[0]))

    @property
    def refs(self) -> list[BrushRef]:
        return self.model.refs

    @staticmethod
    def _source_ns(kind: str, obj) -> tuple:
        """Stable identity of a brush source, so thumbnails stay cached when switching back."""
        if kind == "app" and obj is not None:
            inst = obj.install
            return ("app", obj.adapter.id, inst.label if inst else "", str(inst.data_dir) if inst else "")
        if kind == "file" and obj is not None:
            try:
                mtime = obj.path.stat().st_mtime_ns
            except OSError:
                mtime = 0
            return ("file", str(obj.path), mtime)
        return ("other", id(obj))

    def _show_refs(self, refs: list[BrushRef], kind: str, obj):
        ns = self._source_ns(kind, obj) if obj is not None else None
        self.model.set_refs(refs, ns)
        log.info("showing %d brushes from %s", len(refs), ns)
        groups = sorted({r.group for r in refs if r.group})
        self.group.blockSignals(True)
        self.group.clear()
        self.group.addItem("全部分组", None)
        for g in groups:
            self.group.addItem(g, g)
        self.group.setVisible(bool(groups))
        self.group.blockSignals(False)
        self.empty_msg = "没有找到笔刷。"
        if not refs and kind == "app" and obj is not None and obj.adapter.id == "procreate":
            self.empty_msg = "Procreate 只在 iPad 上运行：请在 iPad 上分享笔刷，把 .brushset / .brush 文件传到电脑后拖进来。"
        self._update_count()
        self._filter()
        self.grid.scrollToTop()

    # selection -------------------------------------------------------------------------------
    def _filter(self, *_):
        self.proxy.set_filter(self.search.text(), self.group.currentData())
        total, shown = len(self.refs), self.proxy.rowCount()
        if not total:
            self.status.setText(getattr(self, "empty_msg", "没有找到笔刷。"))
        else:
            head = f"共 {total} 个笔刷" + (f"，当前显示 {shown} 个" if shown != total else "")
            self.status.setText(f"{head}。单击笔刷即可勾选 / 取消，按住 Shift 单击可连续勾选一串。")
        self.grid.schedule_visible()

    def _check_all(self, on: bool):
        self.model.set_checked(self.proxy.source_rows(), on)

    def _invert(self):
        self.model.invert(self.proxy.source_rows())

    def checked_refs(self) -> list[BrushRef]:
        return self.model.checked_refs()

    def _update_count(self, *_):
        n = len(self.model.checked)
        self.count.setText(f"已选 {n} 个")
        self.go.setText(f"开始导入（{n}）" if n else "开始导入")

    # go --------------------------------------------------------------------------------------
    def _go(self):
        refs = self.checked_refs()
        if not refs:
            QMessageBox.information(self, "还没选笔刷", "请先在中间勾选要搬运的笔刷。")
            return
        data = self.target.currentData()
        if not data:
            return
        kind, obj = data
        if kind == "codec":
            ext = obj.extensions[0] if obj.extensions else ""
            default = export_dir("导出") / (refs[0].name if len(refs) == 1 else f"BrushBridge_{len(refs)}笔刷")
            path, _ = QFileDialog.getSaveFileName(self, "导出为", str(default) + ext, f"{codec_label(obj)} (*{ext})")
            if not path:
                return
            self._run_transfer(refs, lambda brushes: self._export(obj, brushes, Path(path)))
        else:
            target: DetectedApp = obj
            if target.install and target.adapter.is_running(target.install) and \
                    getattr(target.adapter, "requires_closed", False):
                QMessageBox.information(self, "请先关闭软件",
                                        f"导入 {target.label} 需要先关闭它。\n请保存作品并关闭 {target.adapter.name}，然后再点「开始导入」。")
                return
            opts = {"direct": True} if kind == "app_direct" else {}
            if opts and QMessageBox.question(
                    self, "实验功能",
                    f"将直接修改 {target.label} 的工具设置（会先自动备份）。\n请确认已经保存作品并关闭了软件。继续吗？"
            ) != QMessageBox.Yes:
                return
            self._run_transfer(refs, lambda brushes: self._install(target, brushes, **opts))

    def _export(self, c, brushes, path: Path):
        files = convert.export(brushes, c.id, path)
        notes = sorted({n for b in brushes for n in b.notes})
        msg = f"已导出 {len(brushes)} 个笔刷。"
        if notes:
            msg += "\n\n转换提示：\n· " + "\n· ".join(notes)
        return True, msg, files, False

    def _install(self, target: DetectedApp, brushes, **opts):
        res = convert.install(target, brushes, **opts)
        return res.ok, res.message, res.files, res.needs_app_closed

    def _run_transfer(self, refs, action):
        self.win.busy(f"正在搬运 {len(refs)} 个笔刷…")
        self.go.setEnabled(False)

        def work():
            brushes, errors = convert.load_brushes(refs)
            if not brushes:
                return False, "所选笔刷都读取失败：\n" + "\n".join(errors), [], False
            ok, msg, files, needs_closed = action(brushes)
            if errors:
                msg += "\n\n以下笔刷读取失败：\n" + "\n".join(errors)
            return ok, msg, files, needs_closed

        def done(res):
            self.win.idle()
            self.go.setEnabled(True)
            ok, msg, files, needs_closed = res
            if needs_closed:
                QMessageBox.information(self, "请先关闭软件", msg)
                return
            result_dialog(self, ok, msg, [Path(f) for f in files])
            self.win.refresh_apps(quiet=True)

        def fail(m):
            self.win.idle()
            self.go.setEnabled(True)
            QMessageBox.warning(self, "出错了", m)

        run_bg(work, done, fail)


# ---------------------------------------------------------------------------------------------
# Page 2: installed software
# ---------------------------------------------------------------------------------------------
class AppsPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        v = QVBoxLayout(self)
        head = QHBoxLayout()
        t = QLabel("电脑上的绘画软件")
        t.setObjectName("title")
        head.addWidget(t, 1)
        b = QPushButton("重新检测")
        b.clicked.connect(lambda: win.refresh_apps())
        head.addWidget(b)
        v.addLayout(head)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["软件", "版本", "状态", "笔刷保存位置 / 说明"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.cellDoubleClicked.connect(self._open)
        v.addWidget(self.table, 1)
        h = QLabel("双击一行可以打开该软件的笔刷文件夹。")
        h.setObjectName("hint")
        v.addWidget(h)
        self.rows: list[DetectedApp] = []

    def set_detected(self, detected: list[DetectedApp]):
        self.rows = detected
        self.table.setRowCount(len(detected))
        for r, d in enumerate(detected):
            i = d.install
            name = i.name if i else d.adapter.name
            ver = (i.version or "未知") if i else "—"
            if not d.adapter.desktop:
                status = "📱 iPad"
            elif i:
                status = "🟢 运行中" if i.running else "已安装"
            else:
                status = "未安装"
            where = str(i.data_dir) if i and i.data_dir else d.note
            for c, text in enumerate((name, ver, status, where)):
                item = QTableWidgetItem(text)
                if not i and d.adapter.desktop:
                    item.setForeground(QColor("#999"))
                self.table.setItem(r, c, item)

    def _open(self, row: int, _col: int):
        d = self.rows[row]
        if d.install and d.install.data_dir and Path(d.install.data_dir).exists():
            os.startfile(str(d.install.data_dir))  # noqa: S606


# ---------------------------------------------------------------------------------------------
# Page 3: diagnose & fix
# ---------------------------------------------------------------------------------------------
class DropZone(QFrame):
    dropped = Signal(list)

    def __init__(self, text: str):
        super().__init__()
        self.setObjectName("drop")
        self.setAcceptDrops(True)
        self.setMinimumHeight(140)
        lay = QVBoxLayout(self)
        lab = QLabel(text)
        lab.setAlignment(Qt.AlignCenter)
        f = lab.font()
        f.setPointSize(13)
        lab.setFont(f)
        lay.addWidget(lab)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.dropped.emit([Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()])
        e.acceptProposedAction()


class DiagnosePage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.info: Optional[FileInfo] = None
        v = QVBoxLayout(self)
        t = QLabel("笔刷导入不进去？在这里检查并自动修复")
        t.setObjectName("title")
        v.addWidget(t)
        self.zone = DropZone("把导入失败的笔刷文件拖到这里\n（或点击下面的按钮选择文件）")
        self.zone.dropped.connect(lambda ps: ps and self.check(ps[0]))
        v.addWidget(self.zone)
        pick = QPushButton("选择文件…")
        pick.clicked.connect(self._pick)
        v.addWidget(pick, 0, Qt.AlignLeft)
        self.report = QTextBrowser()
        v.addWidget(self.report, 1)
        self.fixes = QHBoxLayout()
        v.addLayout(self.fixes)

    def _pick(self):
        f, _ = QFileDialog.getOpenFileName(self, "选择笔刷文件", str(Path.home() / "Downloads"))
        if f:
            self.check(Path(f))

    def check(self, path: Path):
        self.report.setHtml(f"<p>正在检查 {path.name} …</p>")
        detected = self.win.detected

        def work():
            return convert.diagnose(path, detected)

        run_bg(work, lambda info: self._show(path, info))

    def _show(self, path: Path, info: Optional[FileInfo]):
        while self.fixes.count():
            w = self.fixes.takeAt(0).widget()
            if w:
                w.deleteLater()
        if info is None:
            self.report.setHtml(f"<h3>{path.name}</h3><p>❌ 无法识别这个文件。支持的格式：</p><ul>" +
                                "".join(f"<li>{c.name}</li>" for c in codecs()) + "</ul>")
            return
        rows = "".join(f"<tr><td style='color:#666;padding-right:12px'>{k}</td><td>{v}</td></tr>" for k, v in
                       [("格式", info.format_name), ("版本", info.version or "—"),
                        ("笔刷数量", str(info.brush_count) if info.brush_count >= 0 else "—"), *info.details.items()])
        html = f"<h3>{path.name}</h3><table>{rows}</table>"
        if info.problems:
            html += "<h4>⚠️ 发现的问题</h4><ul>" + "".join(f"<li>{p}</li>" for p in info.problems) + "</ul>"
        else:
            html += "<p>✅ 没有发现兼容性问题。如果仍然导入失败，请确认是拖到了正确的面板（例如 CSP 要拖到「辅助工具」面板）。</p>"
        if info.fixes:
            html += "<p>可以点击下面的按钮自动修复：</p>"
        self.report.setHtml(html)
        for fx in info.fixes:
            b = QPushButton(f"🔧 {fx.title}")
            b.setToolTip(fx.description)
            b.clicked.connect(lambda _=False, fx=fx: self._apply(path, fx))
            self.fixes.addWidget(b)
        self.fixes.addStretch(1)

    def _apply(self, path: Path, fx):
        out = export_dir("修复后的笔刷")
        self.win.busy("正在修复…")

        def done(files):
            self.win.idle()
            result_dialog(self, True, f"已生成修复后的文件，请导入这个新文件：\n{files[0].name}" if files else "完成", files)

        run_bg(lambda: fx.apply(path, out), done, lambda m: (self.win.idle(), QMessageBox.warning(self, "修复失败", m)))


# ---------------------------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Brush Bridge 笔刷桥 {__version__} — 在绘画软件之间搬运笔刷")
        self.resize(1200, 760)
        self.setAcceptDrops(True)
        self.detected: list[DetectedApp] = []
        self.tabs = QTabWidget()
        self.transfer = TransferPage(self)
        self.apps_page = AppsPage(self)
        self.diag = DiagnosePage(self)
        self.tabs.addTab(self.transfer, "🖌 笔刷搬家")
        self.tabs.addTab(self.apps_page, "💻 我的软件")
        self.tabs.addTab(self.diag, "🔧 修复导入问题")
        self.setCentralWidget(self.tabs)
        self.statusBar().showMessage("正在检测电脑上的绘画软件…")
        QTimer.singleShot(50, self.refresh_apps)

    def busy(self, text: str):
        self.statusBar().showMessage(text)
        QApplication.setOverrideCursor(Qt.BusyCursor)

    def idle(self, text: str = "就绪"):
        self.statusBar().showMessage(text)
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()

    def refresh_apps(self, quiet: bool = False):
        if not quiet:
            self.statusBar().showMessage("正在检测电脑上的绘画软件…")

        def done(detected):
            self.detected = detected
            self.transfer.set_detected(detected)
            self.apps_page.set_detected(detected)
            n = sum(1 for d in detected if d.install)
            self.statusBar().showMessage(f"找到 {n} 个绘画软件。")
            if not quiet and self.transfer.sources.currentRow() < 0:
                for i in range(self.transfer.sources.count()):
                    if self.transfer.sources.item(i).data(ROLE_SRC):
                        self.transfer.sources.setCurrentRow(i)
                        break

        run_bg(convert.detect_all, done)

    def closeEvent(self, e):
        self.transfer.thumb_loader.shutdown()  # don't make the app wait for queued thumbnails
        super().closeEvent(e)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        if self.tabs.currentWidget() is self.diag:
            self.diag.check(paths[0])
        else:
            self.tabs.setCurrentWidget(self.transfer)
            self.transfer.add_files(paths)
        e.acceptProposedAction()


def _setup_logging():
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "BrushBridge"
    base.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=base / "brush-bridge.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv: Optional[list[str]] = None) -> int:
    _setup_logging()
    # Brush decoders used for thumbnails are mostly pure Python and run on worker threads; a short
    # GIL switch interval keeps painting/scrolling responsive while they work (default is 5 ms).
    sys.setswitchinterval(0.0005)
    argv = sys.argv if argv is None else argv
    app = QApplication(argv)
    app.setApplicationName("Brush Bridge")
    f = QFont("Microsoft YaHei UI")
    f.setPointSize(10)
    app.setFont(f)
    app.setStyleSheet(STYLE)
    icon = Path(__file__).with_name("icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    load_plugins()
    w = MainWindow()
    w.show()
    files = [Path(a) for a in argv[1:] if Path(a).exists()]
    if files:
        QTimer.singleShot(300, lambda: w.transfer.add_files(files))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
