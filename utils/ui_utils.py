from typing import Optional

from PyQt5.QtWidgets import QGraphicsScene, QGraphicsView, QGraphicsPixmapItem
from PyQt5.QtCore import Qt


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
