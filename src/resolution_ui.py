"""Visible selector persisted in the existing Runtime width/height fields."""
import json
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel
from qfluentwidgets import ComboBox
from src.settings import ROOT, load_settings
from src.config_migration import _atomic, _json
from src.resolution import OPTIONS, SIZES, selected_size


class ResolutionPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = ComboBox(self)
        self.mode.addItems(list(OPTIONS))
        self.mode.setCurrentIndex(SIZES.index(selected_size(load_settings())))
        self.message = QLabel('下次运行生效；与实际客户区不符时停止。', self)
        self.message.setWordWrap(True)
        layout = QHBoxLayout(self)
        layout.addWidget(QLabel('游戏分辨率'))
        layout.addWidget(self.mode)
        layout.addWidget(self.message)
        self.mode.currentIndexChanged.connect(self.save)

    def save(self, index):
        path = ROOT/'settings.json'
        values = json.loads(path.read_text(encoding='utf-8-sig'))
        values['resolution_width'], values['resolution_height'] = SIZES[index]
        _atomic(path, _json(values))
        self.message.setText('已保存：'+OPTIONS[index]+'；下次运行生效。')
