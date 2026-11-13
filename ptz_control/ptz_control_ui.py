# -*- coding: utf-8 -*-
#
# ⚠️ 手動維護，請勿用 pyuic5 重新產生！
# 這個檔原本是 pyuic5 從 ptz_control.ui 產生的，但之後大量手改：
#   - 右側改成 right_layout（QVBoxLayout），內含 ptz_groupBox + record_groupBox
#   - record_layout 內加了錄影紅點 recording_status_label、record_time_label
#   - camera_tab_control.py 會往 right_layout / record_layout 動態插入「存檔位置」面板
# 舊的 ptz_control.ui 已與此檔完全脫節並移除；重生會把上述元件全部蓋掉。
# 要改版面直接編輯這個檔。

from PyQt5 import QtCore, QtGui, QtWidgets


class Ui_ptz_control(object):
    def setupUi(self, ptz_control):
        ptz_control.setObjectName("ptz_control")
        ptz_control.resize(1600, 900)
        
        # === 主佈局 ===
        self.gridLayout_2 = QtWidgets.QGridLayout(ptz_control)
        self.gridLayout_2.setObjectName("gridLayout_2")
        
        # === 左側：影像顯示區 ===
        self.gridLayout_3 = QtWidgets.QGridLayout()
        self.gridLayout_3.setObjectName("gridLayout_3")
        self.main_view = QtWidgets.QGraphicsView(ptz_control)
        self.main_view.setMinimumSize(QtCore.QSize(480, 360))
        self.main_view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.main_view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.main_view.setObjectName("main_view")
        self.gridLayout_3.addWidget(self.main_view, 0, 0, 1, 1)
        
        self.gridLayout_2.addLayout(self.gridLayout_3, 0, 0, 1, 1)

        # === 右側：控制面板總容器 ===
        self.right_widget = QtWidgets.QWidget(ptz_control)
        self.right_layout = QtWidgets.QVBoxLayout(self.right_widget)
        self.right_layout.setContentsMargins(0, 0, 0, 0)
        self.right_layout.setSpacing(15)
        self.right_layout.setObjectName("right_layout")

        # ---------------------------------------------------------
        # 1. 上方區塊：設定
        # ---------------------------------------------------------
        self.ptz_groupBox = QtWidgets.QGroupBox(self.right_widget)
        font = QtGui.QFont()
        font.setPointSize(12)
        self.ptz_groupBox.setFont(font)
        self.ptz_groupBox.setObjectName("ptz_groupBox")
        
        self.ptz_layout = QtWidgets.QGridLayout(self.ptz_groupBox)
        self.ptz_layout.setObjectName("ptz_layout")
        self.ptz_layout.setVerticalSpacing(8)

        # 選擇相機
        self.camer_select_label = QtWidgets.QLabel(self.ptz_groupBox)
        self.camer_select_label.setMinimumSize(QtCore.QSize(75, 0))
        self.camer_select_label.setObjectName("camer_select_label")
        self.ptz_layout.addWidget(self.camer_select_label, 0, 0, 1, 1)

        self.camera_select_combobox = QtWidgets.QComboBox(self.ptz_groupBox)
        self.camera_select_combobox.setObjectName("camera_select_combobox")
        self.ptz_layout.addWidget(self.camera_select_combobox, 0, 1, 1, 2)

        # 連線按鈕
        self.connect_camera_button = QtWidgets.QPushButton(self.ptz_groupBox)
        self.connect_camera_button.setMinimumSize(QtCore.QSize(150, 32)) 
        self.connect_camera_button.setObjectName("connect_camera_button")
        self.ptz_layout.addWidget(self.connect_camera_button, 1, 0, 1, 3)

        # 方向鍵與縮放
        btn_h = 30
        btn_w = 50

        self.move_up_button = QtWidgets.QPushButton(self.ptz_groupBox)
        self.move_up_button.setEnabled(False)
        self.move_up_button.setMinimumSize(QtCore.QSize(btn_w, btn_h))
        self.move_up_button.setObjectName("move_up_button")
        self.ptz_layout.addWidget(self.move_up_button, 2, 1, 1, 1)

        self.move_left_button = QtWidgets.QPushButton(self.ptz_groupBox)
        self.move_left_button.setEnabled(False)
        self.move_left_button.setMinimumSize(QtCore.QSize(btn_w, btn_h))
        self.move_left_button.setObjectName("move_left_button")
        self.ptz_layout.addWidget(self.move_left_button, 3, 0, 1, 1)

        self.move_down_button = QtWidgets.QPushButton(self.ptz_groupBox)
        self.move_down_button.setEnabled(False)
        self.move_down_button.setMinimumSize(QtCore.QSize(btn_w, btn_h))
        self.move_down_button.setObjectName("move_down_button")
        self.ptz_layout.addWidget(self.move_down_button, 4, 1, 1, 1)

        self.move_right_button = QtWidgets.QPushButton(self.ptz_groupBox)
        self.move_right_button.setEnabled(False)
        self.move_right_button.setMinimumSize(QtCore.QSize(btn_w, btn_h))
        self.move_right_button.setObjectName("move_right_button")
        self.ptz_layout.addWidget(self.move_right_button, 3, 2, 1, 1)

        self.zoom_in_button = QtWidgets.QPushButton(self.ptz_groupBox)
        self.zoom_in_button.setEnabled(False)
        self.zoom_in_button.setMinimumSize(QtCore.QSize(0, btn_h))
        self.zoom_in_button.setObjectName("zoom_in_button")
        self.ptz_layout.addWidget(self.zoom_in_button, 5, 0, 1, 1)

        self.zoom_out_button = QtWidgets.QPushButton(self.ptz_groupBox)
        self.zoom_out_button.setEnabled(False)
        self.zoom_out_button.setMinimumSize(QtCore.QSize(0, btn_h))
        self.zoom_out_button.setObjectName("zoom_out_button")
        self.ptz_layout.addWidget(self.zoom_out_button, 5, 2, 1, 1)

        self.ptz_layout.setColumnStretch(0, 1)
        self.ptz_layout.setColumnStretch(1, 1)
        self.ptz_layout.setColumnStretch(2, 1)
        self.right_layout.addWidget(self.ptz_groupBox)


        # ---------------------------------------------------------
        # 2. 下方區塊：錄影功能
        # ---------------------------------------------------------
        self.record_groupBox = QtWidgets.QGroupBox(self.right_widget)
        self.record_groupBox.setFont(font)
        self.record_groupBox.setObjectName("record_groupBox")
        
        self.record_layout = QtWidgets.QGridLayout(self.record_groupBox)
        self.record_layout.setObjectName("record_layout")
        self.record_layout.setVerticalSpacing(8)

        # [Row 0] ★ 時間顯示 (移到最上方，靠左)
        self.record_time_label = QtWidgets.QLabel(self.record_groupBox)
        # 設定靠左對齊 (AlignLeft) | 垂直置中 (AlignVCenter)
        self.record_time_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        self.record_time_label.setObjectName("record_time_label")
        self.record_time_label.setText("00:00") # 預設改為 MM:SS
        self.record_layout.addWidget(self.record_time_label, 0, 0, 1, 3) # 跨 3 欄

        # [Row 1] 按鈕與狀態燈 (下移一列)
        self.start_record_button = QtWidgets.QPushButton(self.record_groupBox)
        self.start_record_button.setMinimumSize(QtCore.QSize(0, 30)) 
        self.start_record_button.setObjectName("start_record_button")
        self.record_layout.addWidget(self.start_record_button, 1, 0, 1, 1)

        self.recording_status_label = QtWidgets.QLabel(self.record_groupBox)
        self.recording_status_label.setFixedSize(16, 16)
        self.recording_status_label.setStyleSheet("background-color: red; border-radius: 8px;")
        self.recording_status_label.setVisible(False)
        self.recording_status_label.setAlignment(QtCore.Qt.AlignCenter)
        self.recording_status_label.setObjectName("recording_status_label")
        self.record_layout.addWidget(self.recording_status_label, 1, 1, 1, 1,
                                     QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)

        self.stop_record_button = QtWidgets.QPushButton(self.record_groupBox)
        self.stop_record_button.setMinimumSize(QtCore.QSize(0, 30))
        self.stop_record_button.setObjectName("stop_record_button")
        self.record_layout.addWidget(self.stop_record_button, 1, 2, 1, 1)

        self.record_layout.setColumnStretch(0, 2)
        self.record_layout.setColumnStretch(1, 1)
        self.record_layout.setColumnStretch(2, 2)

        self.right_layout.addWidget(self.record_groupBox)

        # 彈簧
        spacerItem = QtWidgets.QSpacerItem(20, 40, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Expanding)
        self.right_layout.addItem(spacerItem)

        # 加入右側
        self.gridLayout_2.addWidget(self.right_widget, 0, 1, 1, 1)
        self.gridLayout_2.setColumnStretch(0, 8)
        self.gridLayout_2.setColumnStretch(1, 2)

        self.retranslateUi(ptz_control)
        QtCore.QMetaObject.connectSlotsByName(ptz_control)

    def retranslateUi(self, ptz_control):
        _translate = QtCore.QCoreApplication.translate
        ptz_control.setWindowTitle(_translate("ptz_control", "Ptz_Control_20240918_V1B3"))
        
        self.ptz_groupBox.setTitle(_translate("ptz_control", "設定"))
        self.record_groupBox.setTitle(_translate("ptz_control", "錄影"))
        
        self.camer_select_label.setText(_translate("ptz_control", "選擇相機 :"))
        self.connect_camera_button.setText(_translate("ptz_control", "讀取串流"))
        self.move_up_button.setText(_translate("ptz_control", "向上"))
        self.move_left_button.setText(_translate("ptz_control", "向左"))
        self.move_right_button.setText(_translate("ptz_control", "向右"))
        self.move_down_button.setText(_translate("ptz_control", "向下"))
        self.zoom_in_button.setText(_translate("ptz_control", "放大"))
        self.zoom_out_button.setText(_translate("ptz_control", "縮小"))
        
        self.start_record_button.setText(_translate("ptz_control", "開始錄影"))
        self.stop_record_button.setText(_translate("ptz_control", "停止錄影"))


if __name__ == "__main__":
    import sys
    app = QtWidgets.QApplication(sys.argv)
    ptz_control = QtWidgets.QWidget()
    ui = Ui_ptz_control()
    ui.setupUi(ptz_control)
    ptz_control.show()
    sys.exit(app.exec_())