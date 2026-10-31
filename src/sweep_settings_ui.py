"""Visible shortcut to the existing persisted task configuration."""
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel
from qfluentwidgets import SpinBox, PrimaryPushButton


class SweepSettingsPanel(QWidget):
    def __init__(self, tasks, parent=None):
        super().__init__(parent)
        self.tasks=[t for t in tasks if type(t).__name__ in ('DailyTask','DarkTideTask')]
        self.settings=self.tasks[0].executor.global_config.get_config('Tasks')
        self.count=SpinBox(self)
        self.count.setRange(0,50)
        self.count.setValue(self.settings['dark_tide_sweep_count'])
        self.save_button=PrimaryPushButton('保存次数',self)
        self.summary=QLabel()
        self.summary.setWordWrap(True)
        self.saved=QLabel('完整日常路线和暗涌单项共用此次数；保存后下次执行生效。')
        self.saved.setWordWrap(True)
        row=QHBoxLayout();row.addWidget(QLabel('04-3 扫荡次数'));row.addWidget(self.count);row.addWidget(self.save_button);row.addStretch()
        layout=QVBoxLayout(self);layout.addLayout(row);layout.addWidget(self.summary);layout.addWidget(self.saved)
        self.count.valueChanged.connect(self.update_summary)
        self.save_button.clicked.connect(self.save)
        self.update_summary()

    def update_summary(self,*args):
        n=self.count.value()
        self.summary.setText(f'计划 {n} 次，共需 {n*60} 体力。每次领取材料并返回关卡后，再进行下一次。'
                             if n else '0 次：跳过暗涌扫荡。')

    def save(self):
        count=self.count.value()
        self.settings['dark_tide_sweep_count']=count
        self.saved.setText(f'已保存：完整日常和暗涌单项均为 {count} 次；下次执行生效。体力不足会停止，不自动补充。')
