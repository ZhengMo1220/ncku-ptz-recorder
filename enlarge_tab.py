from PyQt5.QtWidgets import QWidget, QVBoxLayout, QGraphicsView, QGraphicsScene
from PyQt5.QtCore import Qt
from utils.stream_thread import StreamThread

class EnlargeTab(QWidget):
    def __init__(self, src, title="PTZ", parent=None):
        super().__init__(parent)
        self.src = src
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        layout = QVBoxLayout(self)
        self.scene = QGraphicsScene(self)
        self.view  = QGraphicsView(self)
        self.view.setScene(self.scene)
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.view)

        self.thread = StreamThread(self.scene, self.view, self.src)
        self.thread.start()

    def stop_stream(self):
        if getattr(self, "thread", None):
            self.thread.stop()
            self.thread = None

    def closeEvent(self, e):
        self.stop_stream()
        e.accept()
