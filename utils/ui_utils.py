from typing import Optional

from PyQt5.QtWidgets import QGraphicsScene, QGraphicsView, QGraphicsPixmapItem, QWidget
from PyQt5.QtCore import Qt, QSize


def _ensure_pixitem(scene: QGraphicsScene, view: QGraphicsView) -> QGraphicsPixmapItem:
    item: Optional[QGraphicsPixmapItem] = getattr(view, "_pixitem", None)
    if item is None:
        item = QGraphicsPixmapItem()
        item.setTransformationMode(Qt.SmoothTransformation)
        scene.addItem(item)
        setattr(view, "_pixitem", item)
        view.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    return item


GRID_CELL_MIN_W = 200   # 16:9 格子最小寬度（200×112），再小就看不清楚


def _layout_of(container):
    # MultiCameraTab 把 self.layout 蓋成屬性，不能直接呼叫 container.layout()
    return QWidget.layout(container)


def _grid_overhead(container, cell_widgets, rows: int, cols: int) -> QSize:
    """格子以外（控制列、標籤、間距、邊界）佔掉的寬高，第一次算完就快取。

    不能每次即時算：格子 setFixedSize 之後 Qt 的 layout.minimumSize() 會有一拍
    還是舊值，拿舊值減新格子尺寸會算錯（實測視窗被撐回 1700px 寬）。
    第一次呼叫時格子還沒被改過，layout 最小尺寸是準的。
    """
    cached = getattr(container, "_grid_overhead_cache", None)
    if cached is not None:
        return cached
    layout = _layout_of(container)
    if layout is None or not cell_widgets:
        return QSize(0, 0)
    m = layout.minimumSize()
    cur = cell_widgets[0].size()
    oh = QSize(max(0, m.width() - cols * cur.width()),
               max(0, m.height() - rows * cur.height()))
    container._grid_overhead_cache = oh
    return oh


def fit_grid_cell_size(container, cell_widgets, rows: int, cols: int,
                       min_w: int = GRID_CELL_MIN_W):
    """算出 rows×cols 16:9 格子在 container 內放得下的 (cell_w, cell_h)。

    原本只看寬度：cell_w = (寬 - 80) / 4、最小 280。小筆電（1366×768、
    或 1920×1080 開 125%/150% 縮放）高度不夠，兩排畫面 + 上方控制列 + 下方
    按鈕塞不下 → 整個分頁被擠壓、底部被切掉。這裡寬高都考慮，取較小者。
    """
    if not cell_widgets:
        return None
    oh = _grid_overhead(container, cell_widgets, rows, cols)
    by_w = (container.width() - oh.width()) // cols
    by_h = ((container.height() - oh.height()) // rows) * 16 // 9
    cell_w = max(min_w, min(by_w, by_h))
    return cell_w, cell_w * 9 // 16


def grid_minimum_size_hint(container, cell_widgets, rows: int, cols: int,
                           min_w: int = GRID_CELL_MIN_W) -> QSize:
    """container 的 minimumSizeHint：格子縮到最小時，整個分頁最小要多大。

    格子用 setFixedSize，Qt 會把「目前」格子大小當成整個視窗的最小尺寸。
    原本初始 460×258 → 視窗最小寬 ≈ 1920，小筆電螢幕放不下，視窗被撐出
    螢幕外、看起來就是被擠壓 / 切掉。覆寫 minimumSizeHint 回報格子最小時的
    尺寸，視窗就能縮小，resizeEvent 再把格子調到剛好塞滿。
    """
    if not cell_widgets:
        return QSize(0, 0)
    oh = _grid_overhead(container, cell_widgets, rows, cols)
    return QSize(oh.width() + cols * min_w, oh.height() + rows * (min_w * 9 // 16))
