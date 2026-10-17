"""Virtualised brush thumbnail grid for the transfer page.

* ``BrushListModel``  – flat model over ``BrushRef`` objects; owns the checked set (O(1) counting).
* ``BrushFilterProxy`` – name search + group filter without touching any widget.
* ``BrushDelegate``   – paints thumbnail, name and a big check mark; no per-item widgets.
* ``BrushGridView``   – QListView (icon mode): click toggles, Shift+click checks a range.
* ``ThumbnailLoader`` – lazy, deduplicated, prioritised thumbnail rendering on a private thread pool.
  PIL → QImage happens in the worker; QPixmap is only created on the GUI thread.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import OrderedDict
from typing import Callable, Hashable, Iterable, Optional

from PIL import Image, ImageOps
from PySide6.QtCore import (
    QAbstractListModel, QModelIndex, QObject, QPointF, QRect, QRectF, QRunnable, QSize, QSortFilterProxyModel, Qt,
    QThreadPool, QTimer, Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QApplication, QListView, QStyle, QStyledItemDelegate

from ..core.model import BrushRef

log = logging.getLogger(__name__)

THUMB = 96
GRID = QSize(THUMB + 34, THUMB + 58)
ACCENT = QColor("#2f6fed")
ROLE_REF = Qt.UserRole + 1  # source row == index into model.refs
ROLE_GROUP = Qt.UserRole + 3


# ---------------------------------------------------------------------------------------------
# imaging
# ---------------------------------------------------------------------------------------------
def pil_to_qimage(img: Image.Image, size: int = THUMB) -> QImage:
    """Square white thumbnail. Safe to call from worker threads (QImage only, no QPixmap)."""
    if img.mode == "L":  # tip mask: 255 = paint → show dark paint on white
        a = ImageOps.autocontrast(img, cutoff=0.5) if img.getextrema()[1] < 250 else img
        img = Image.new("RGBA", a.size, (30, 30, 36, 0))
        img.putalpha(a)
    img = img.convert("RGBA")
    img.thumbnail((size - 8, size - 8), Image.LANCZOS)
    bg = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    bg.alpha_composite(img, ((size - img.width) // 2, (size - img.height) // 2))
    data = bg.tobytes("raw", "RGBA")
    return QImage(data, size, size, QImage.Format_RGBA8888).copy()


# ---------------------------------------------------------------------------------------------
# thumbnail loading
# ---------------------------------------------------------------------------------------------
class PixmapCache:
    """Small LRU of ready thumbnails (GUI thread only)."""

    def __init__(self, capacity: int = 2000):
        self.capacity = capacity
        self._d: OrderedDict[Hashable, QPixmap] = OrderedDict()
        self.failed: set = set()

    def get(self, key) -> Optional[QPixmap]:
        pm = self._d.get(key)
        if pm is not None:
            self._d.move_to_end(key)
        return pm

    def put(self, key, pm: QPixmap) -> None:
        self._d[key] = pm
        self._d.move_to_end(key)
        while len(self._d) > self.capacity:
            self._d.popitem(last=False)

    def __contains__(self, key) -> bool:
        return key in self._d

    def __len__(self) -> int:
        return len(self._d)


class _ThumbWorker(QRunnable):
    def __init__(self, loader: "ThumbnailLoader"):
        super().__init__()
        self.loader = loader

    def run(self):
        loader = self.loader
        while True:
            job = loader._take()
            if job is None:
                return
            key, fn = job
            qimg = None
            try:
                img = fn()
                if img is not None:
                    qimg = loader.render(img)
            except Exception:
                log.debug("thumbnail %r failed", key, exc_info=True)
            finally:
                loader._finish(key)
            try:
                loader.ready.emit(key, qimg)
            except RuntimeError:  # loader deleted while the app shuts down
                return


class ThumbnailLoader(QObject):
    """Renders thumbnails off the GUI thread.

    Requests are deduplicated by key; priority 0 (painted right now) is served before priority 1
    (prefetch), most recent first. ``retain`` drops queued work for rows that scrolled away.
    """

    ready = Signal(object, object)  # key, QImage | None — emitted from worker threads

    def __init__(self, render: Callable[[Image.Image], QImage] = pil_to_qimage, max_threads: int = 2,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.render = render
        self._lock = threading.Lock()
        self._pending: dict[Hashable, tuple[int, int, Callable]] = {}
        self._running: set = set()
        self._seq = itertools.count()
        self._workers = 0
        self._max = max(1, max_threads)
        self._closed = False
        self._hold_until = 0.0
        self.requested = 0  # number of jobs ever queued (diagnostics / tests)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(self._max)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def request(self, key: Hashable, fn: Callable[[], Optional[Image.Image]], priority: int = 0) -> None:
        with self._lock:
            if self._closed or key in self._running:
                return
            cur = self._pending.get(key)
            if cur is None:
                self.requested += 1
            else:
                priority = min(priority, cur[0])
            self._pending[key] = (priority, next(self._seq), fn)
            start = self._workers < self._max
            if start:
                self._workers += 1
        if start:
            self.pool.start(_ThumbWorker(self))

    def is_queued(self, key: Hashable) -> bool:
        with self._lock:
            return key in self._pending or key in self._running

    def retain(self, keep: set) -> int:
        """Drop queued (not yet started) work whose key is not in ``keep``."""
        with self._lock:
            drop = [k for k in self._pending if k not in keep]
            for k in drop:
                del self._pending[k]
        return len(drop)

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def busy(self) -> bool:
        with self._lock:
            return bool(self._pending or self._running)

    def hold(self, seconds: float) -> None:
        """Don't start new jobs for a moment (called while the user scrolls).

        Thumbnail decoders are mostly pure Python; running them while the GUI thread paints makes
        the two fight over the GIL and scrolling stutters. Started jobs still finish.
        """
        self._hold_until = max(self._hold_until, time.monotonic() + seconds)

    def resume(self) -> None:
        self._hold_until = 0.0

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            self._pending.clear()

    def wait(self, msecs: int = 5000) -> bool:
        return self.pool.waitForDone(msecs)

    # worker side ---------------------------------------------------------------------------------
    def _take(self):
        while True:
            with self._lock:
                if not self._pending or self._closed:
                    self._workers -= 1
                    return None
                wait = self._hold_until - time.monotonic()
                if wait <= 0:
                    key = min(self._pending, key=lambda k: (self._pending[k][0], -self._pending[k][1]))
                    fn = self._pending.pop(key)[2]
                    self._running.add(key)
                    return key, fn
            time.sleep(min(wait, 0.05))  # GUI is busy scrolling: stay off the GIL

    def _finish(self, key) -> None:
        with self._lock:
            self._running.discard(key)


# ---------------------------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------------------------
def _is_checked(value) -> bool:
    if isinstance(value, Qt.CheckState):
        return value == Qt.Checked
    if isinstance(value, bool):
        return value
    try:
        return int(value) == Qt.CheckState.Checked.value
    except (TypeError, ValueError):
        return False


class BrushListModel(QAbstractListModel):
    checkedChanged = Signal(int)  # number of checked brushes

    def __init__(self, loader: Optional[ThumbnailLoader] = None, cache: Optional[PixmapCache] = None,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.loader = loader if loader is not None else ThumbnailLoader(parent=self)
        self.cache = cache if cache is not None else PixmapCache()
        self.refs: list[BrushRef] = []
        self.names_lower: list[str] = []
        self.groups: list[str] = []
        self.checked: set[int] = set()
        self.ns: Hashable = None
        self.loader.ready.connect(self._on_thumb, Qt.QueuedConnection)

    # data ---------------------------------------------------------------------------------------
    def set_refs(self, refs: list[BrushRef], ns: Hashable = None) -> None:
        self.beginResetModel()
        self.loader.clear()
        self.refs = list(refs)
        self.names_lower = [(r.name or "").lower() for r in self.refs]
        self.groups = [r.group or "" for r in self.refs]
        self.checked = set()
        self.ns = ns if ns is not None else object()
        self.endResetModel()
        self.checkedChanged.emit(0)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.refs)

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable | Qt.ItemNeverHasChildren

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.refs):
            return None
        row = index.row()
        if role == Qt.DisplayRole:
            return self.refs[row].name
        if role == Qt.CheckStateRole:
            return Qt.Checked if row in self.checked else Qt.Unchecked
        if role == Qt.ToolTipRole:
            r = self.refs[row]
            return "\n".join(x for x in (r.name, r.group, r.detail) if x)
        if role == Qt.DecorationRole:
            return self.pixmap(row)
        if role == ROLE_REF:
            return row
        if role == ROLE_GROUP:
            return self.groups[row]
        return None

    def setData(self, index, value, role=Qt.EditRole) -> bool:
        if role != Qt.CheckStateRole or not index.isValid():
            return False
        self.set_checked([index.row()], _is_checked(value))
        return True

    # checking -----------------------------------------------------------------------------------
    def is_checked(self, row: int) -> bool:
        return row in self.checked

    def set_checked(self, rows: Iterable[int], on: bool) -> None:
        rows = [r for r in rows if 0 <= r < len(self.refs)]
        changed = [r for r in rows if (r in self.checked) != on]
        if not changed:
            return
        if on:
            self.checked.update(changed)
        else:
            self.checked.difference_update(changed)
        self._emit_changed(min(changed), max(changed), [Qt.CheckStateRole])
        self.checkedChanged.emit(len(self.checked))

    def toggle(self, row: int) -> bool:
        on = row not in self.checked
        self.set_checked([row], on)
        return on

    def invert(self, rows: Iterable[int]) -> None:
        rows = [r for r in rows if 0 <= r < len(self.refs)]
        if not rows:
            return
        self.checked.symmetric_difference_update(rows)
        self._emit_changed(min(rows), max(rows), [Qt.CheckStateRole])
        self.checkedChanged.emit(len(self.checked))

    def checked_refs(self) -> list[BrushRef]:
        return [self.refs[i] for i in sorted(self.checked)]

    def _emit_changed(self, first: int, last: int, roles) -> None:
        self.dataChanged.emit(self.index(first), self.index(last), roles)

    # thumbnails ---------------------------------------------------------------------------------
    def thumb_key(self, row: int):
        r = self.refs[row]
        return (self.ns, row, r.key, r.name)

    def pixmap(self, row: int, request: bool = True) -> Optional[QPixmap]:
        """Cached thumbnail, or None after queueing it for rendering (high priority)."""
        key = self.thumb_key(row)
        pm = self.cache.get(key)
        if pm is None and request:
            self._request(row, key, 0)
        return pm

    def prefetch(self, rows: Iterable[int]) -> None:
        for row in rows:
            if 0 <= row < len(self.refs):
                key = self.thumb_key(row)
                if key not in self.cache:
                    self._request(row, key, 1)

    def _request(self, row: int, key, priority: int) -> None:
        fn = self.refs[row].thumbnail
        if fn is None or key in self.cache.failed:
            return
        self.loader.request(key, fn, priority)

    def _on_thumb(self, key, qimg) -> None:
        if qimg is None or qimg.isNull():
            self.cache.failed.add(key)
            return
        self.cache.put(key, QPixmap.fromImage(qimg))
        ns, row = key[0], key[1]
        if ns == self.ns and row < len(self.refs) and self.thumb_key(row) == key:
            self._emit_changed(row, row, [Qt.DecorationRole])


class BrushFilterProxy(QSortFilterProxyModel):
    """Cheap search/group filter that reads the source model's pre-lowered name list directly."""

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.setDynamicSortFilter(False)
        self._text = ""
        self._group: Optional[str] = None

    @property
    def active(self) -> bool:
        return bool(self._text) or self._group is not None

    def set_filter(self, text: str = "", group: Optional[str] = None) -> None:
        text = (text or "").strip().lower()
        if text == self._text and group == self._group:
            return
        if hasattr(self, "beginFilterChange"):
            self.beginFilterChange()
            self._text, self._group = text, group
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:  # pragma: no cover - older Qt
            self._text, self._group = text, group
            self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent) -> bool:
        m = self.sourceModel()
        if self._group is not None and m.groups[row] != self._group:
            return False
        return not self._text or self._text in m.names_lower[row]

    def source_rows(self) -> list[int]:
        """Source rows currently visible, in display order."""
        n = self.rowCount()
        if not self.active:
            return list(range(n))
        return [self.mapToSource(self.index(r, 0)).row() for r in range(n)]


# ---------------------------------------------------------------------------------------------
# painting
# ---------------------------------------------------------------------------------------------
class BrushDelegate(QStyledItemDelegate):
    def __init__(self, model: BrushListModel, proxy: QSortFilterProxyModel, parent=None):
        super().__init__(parent)
        self.model = model
        self.proxy = proxy
        self._lines: dict[tuple, tuple[str, str]] = {}

    def sizeHint(self, option, index) -> QSize:
        return QSize(GRID.width() - 4, GRID.height() - 4)

    def editorEvent(self, event, model, option, index) -> bool:
        return False  # clicks are handled by BrushGridView

    def _split(self, fm: QFontMetrics, text: str, width: int) -> tuple[str, str]:
        key = (text, width, fm.height())
        got = self._lines.get(key)
        if got is not None:
            return got
        if fm.horizontalAdvance(text) <= width:
            res = (text, "")
        else:
            cut = 1
            while cut < len(text) and fm.horizontalAdvance(text[:cut + 1]) <= width:
                cut += 1
            sp = text.rfind(" ", 0, cut + 1)
            if sp > 0:
                cut = sp
            res = (text[:cut].rstrip(), fm.elidedText(text[cut:].strip(), Qt.ElideRight, width))
        if len(self._lines) > 5000:
            self._lines.clear()
        self._lines[key] = res
        return res

    def paint(self, painter: QPainter, option, index) -> None:
        row = self.proxy.mapToSource(index).row() if index.model() is self.proxy else index.row()
        if row < 0 or row >= len(self.model.refs):
            return
        checked = self.model.is_checked(row)
        hover = bool(option.state & QStyle.State_MouseOver)
        focus = bool(option.state & QStyle.State_HasFocus)
        r = option.rect.adjusted(2, 2, -2, -2)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        # card
        if checked:
            bg, pen = QColor("#e3ecff"), QPen(ACCENT, 2.5)
        elif hover:
            bg, pen = QColor("#f3f6fc"), QPen(QColor("#b8c8ec"), 1.2)
        else:
            bg, pen = QColor("#ffffff"), QPen(QColor("#e6e6ec"), 1)
        if focus and not checked:
            pen = QPen(QColor("#8fa9e6"), 1.2, Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(bg)
        painter.drawRoundedRect(QRectF(r).adjusted(1, 1, -1, -1), 8, 8)

        # thumbnail
        tr = QRect(r.center().x() - THUMB // 2 + 1, r.top() + 6, THUMB, THUMB)
        pm = self.model.pixmap(row)
        if pm is not None:
            painter.drawPixmap(tr, pm)
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#f2f2f5"))
            painter.drawRoundedRect(QRectF(tr), 6, 6)
            painter.setPen(QColor("#b5b5bd"))
            painter.drawText(tr, Qt.AlignCenter, "…")
        if checked:  # tint the thumbnail a little so the state reads even at a glance
            painter.fillRect(tr, QColor(47, 111, 237, 28))

        # check box (top-left corner, always visible so it's obvious cells are clickable)
        cb = QRectF(r.left() + 6, r.top() + 6, 22, 22)
        if checked:
            painter.setPen(QPen(QColor("white"), 1.5))
            painter.setBrush(ACCENT)
            painter.drawRoundedRect(cb, 5, 5)
            path = QPainterPath(QPointF(cb.left() + 5.5, cb.top() + 11.5))
            path.lineTo(cb.left() + 9.5, cb.top() + 15.5)
            path.lineTo(cb.left() + 16.5, cb.top() + 7)
            painter.setPen(QPen(QColor("white"), 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
        else:
            painter.setPen(QPen(QColor("#6f7a90") if hover else QColor("#a3abbb"), 1.6))
            painter.setBrush(QColor(255, 255, 255, 235))
            painter.drawRoundedRect(cb, 5, 5)

        # name (up to two lines)
        font = QFont(option.font)
        if checked:
            font.setBold(True)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text_w = r.width() - 8
        l1, l2 = self._split(fm, self.model.refs[row].name or "", text_w)
        painter.setPen(QColor("#1d4fc4") if checked else QColor("#222"))
        y = tr.bottom() + 5
        painter.drawText(QRect(r.left() + 4, y, text_w, fm.height()), Qt.AlignHCenter | Qt.AlignVCenter, l1)
        if l2:
            painter.drawText(QRect(r.left() + 4, y + fm.height(), text_w, fm.height()),
                             Qt.AlignHCenter | Qt.AlignVCenter, l2)
        painter.restore()


# ---------------------------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------------------------
class BrushGridView(QListView):
    """Icon grid; a click toggles a brush, Shift+click applies that state to the whole range."""

    checkAllRequested = Signal()

    def __init__(self, model: BrushListModel, proxy: Optional[BrushFilterProxy] = None, parent=None):
        super().__init__(parent)
        self.brush_model = model
        self.proxy = proxy if proxy is not None else BrushFilterProxy(self)
        self.proxy.setSourceModel(model)
        self.setModel(self.proxy)
        self.setItemDelegate(BrushDelegate(model, self.proxy, self))
        self.setViewMode(QListView.IconMode)
        self.setMovement(QListView.Static)
        self.setResizeMode(QListView.Adjust)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setGridSize(GRID)
        self.setIconSize(QSize(THUMB, THUMB))
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setDragEnabled(False)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(32)
        self._anchor: Optional[int] = None  # source row of the last click

        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(60)
        self._settle.timeout.connect(self.update_visible)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.proxy.modelReset.connect(self._reset)
        self.proxy.layoutChanged.connect(self.schedule_visible)
        self.proxy.rowsRemoved.connect(self.schedule_visible)
        self.proxy.rowsInserted.connect(self.schedule_visible)

    # visibility / prefetch ------------------------------------------------------------------------
    def _reset(self):
        self._anchor = None
        self.schedule_visible()

    def schedule_visible(self, *_):
        self._settle.start()

    def _on_scroll(self, *_):
        self.brush_model.loader.hold(0.15)
        self._settle.start()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.schedule_visible()

    def visible_proxy_rows(self, margin: int = 0) -> range:
        """Proxy rows whose cells intersect the viewport (± ``margin`` px). O(log n + visible)."""
        n = self.proxy.rowCount()
        if n == 0:
            return range(0)
        vp = self.viewport().rect().adjusted(0, -margin, 0, margin)
        idx = self.proxy.index
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if self.visualRect(idx(mid, 0)).bottom() < vp.top():
                lo = mid + 1
            else:
                hi = mid
        last = lo
        while last < n and self.visualRect(idx(last, 0)).top() <= vp.bottom():
            last += 1
        return range(lo, last)

    def update_visible(self):
        """After scrolling settles: prefetch one screen around the viewport, drop stale queued work."""
        if self.proxy.rowCount() == 0:
            self.brush_model.loader.clear()
            return
        h = max(self.viewport().height(), GRID.height())
        rows = self.visible_proxy_rows(margin=h)
        src = [self.proxy.mapToSource(self.proxy.index(r, 0)).row() for r in rows]
        m = self.brush_model
        m.loader.retain({m.thumb_key(s) for s in src})
        m.prefetch(src)

    # interaction --------------------------------------------------------------------------------
    def _click(self, e) -> None:
        if e.button() != Qt.LeftButton:
            e.ignore()
            return
        idx = self.indexAt(e.position().toPoint())
        e.accept()
        if not idx.isValid():
            return
        self.setCurrentIndex(idx)
        row = self.proxy.mapToSource(idx).row()
        m = self.brush_model
        if e.modifiers() & Qt.ShiftModifier and self._anchor is not None:
            a = self.proxy.mapFromSource(m.index(self._anchor))
            if a.isValid():
                lo, hi = sorted((a.row(), idx.row()))
                rows = [self.proxy.mapToSource(self.proxy.index(r, 0)).row() for r in range(lo, hi + 1)]
                m.set_checked(rows, m.is_checked(self._anchor))
                return
        m.toggle(row)
        self._anchor = row

    def mousePressEvent(self, e):
        self._click(e)

    def mouseDoubleClickEvent(self, e):
        self._click(e)  # a quick second click is still a click

    def mouseReleaseEvent(self, e):
        e.accept()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Space, Qt.Key_Select) and self.currentIndex().isValid():
            row = self.proxy.mapToSource(self.currentIndex()).row()
            self.brush_model.toggle(row)
            self._anchor = row
            e.accept()
            return
        if e.key() == Qt.Key_A and e.modifiers() & Qt.ControlModifier:
            self.checkAllRequested.emit()
            e.accept()
            return
        super().keyPressEvent(e)
