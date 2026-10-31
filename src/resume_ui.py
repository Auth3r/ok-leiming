"""User correction of history that a single current game screen cannot reveal."""
import json,hashlib
from PySide6.QtWidgets import QDialog,QVBoxLayout,QFormLayout,QSpinBox,QLineEdit,QLabel,QPushButton,QScrollArea,QWidget,QMessageBox,QComboBox
from src.settings import ROOT
from src.crafting_resume import load_resume,read_adjustment
from src.crafting_policy import normalize_record


def edit_resume(parent):
    try:
        data,picture=load_resume(ROOT/'logs',ROOT/'logs/not-a-run')
        source=picture.parent/'resume.json'
        if not source.exists():raise ValueError('请先运行一次新版续跑，以建立恢复记录')
        digest=hashlib.sha256(source.read_bytes()).hexdigest()
    except Exception as error:
        QMessageBox.warning(parent,'无法读取记录',str(error));return
    record=read_adjustment(source) or normalize_record(data['record'])
    dialog=QDialog(parent);dialog.setWindowTitle('编辑制作记录');dialog.resize(560,650)
    layout=QVBoxLayout(dialog)
    note=QLabel('未知次数不会当成0；可选地校正具体字段。只填写已核实的次数，其他字段保留未知。')
    note.setWordWrap(True);layout.addWidget(note)
    scroll=QScrollArea();scroll.setWidgetResizable(True);body=QWidget();form=QFormLayout(body);scroll.setWidget(body);layout.addWidget(scroll)
    def spin(label,value,maximum=99999):
        widget=QSpinBox();widget.setObjectName(label);widget.setRange(0,maximum);widget.setValue(value);form.addRow(label,widget);return widget
    boosts={key:spin('激增次数 · '+key,record['boosts'].get(key,0),100) for key in
            sorted(set(record['boosts'])|{'火力攻击','重盾防御','重盾攻击','工程最大生命'})}
    for key,widget in boosts.items():
        widget.setMinimum(-1);widget.setSpecialValueText('未知')
        known=key in record.get('known_boosts',[]) or (record.get('boosts_complete',record.get('complete',False)) and key not in record.get('unknown_boosts',[]))
        if not known:
            widget.setValue(-1);widget.setToolTip('旧记录下界：'+str(record['boosts'].get(key,'未知'))+'；不能据此证明未满次数')
    skills={key:spin('技能等级 · '+key,record['skills'].get(key,0),100) for key in
            sorted(set(record['skills'])|{'连携增援','指挥增效','快速整备','电磁旋涡','强击','先制打击'})}
    gold=spin('累计金技能获得次数',record['gold_picks'],100)
    gold.setMinimum(skills['连携增援'].value())
    skills['连携增援'].valueChanged.connect(gold.setMinimum)
    gold.setToolTip('连携增援为金技能，累计次数至少等于其等级；其他金技能次数可手动补充。')
    factors=QLineEdit('，'.join(record['factors']));form.addRow('已选择因子（逗号分隔）',factors)
    missions=[]
    def add_mission(m=None):
        m=m or {'index':len(missions),'group':'火力','attr':'攻击','goal':0,'current':0}
        group=QComboBox();group.addItems(['火力','重盾','突击','工程','医疗']);group.setCurrentText(m['group'])
        attr=QComboBox();attr.addItems(['攻击','防御','最大生命']);attr.setCurrentText(m['attr'])
        form.addRow('研发目标 · 职业',group);form.addRow('属性',attr)
        goal=spin('要求数值（0表示删除）',m['goal']);current=spin('当前累计数值',m['current'])
        missions.append((m,group,attr,goal,current))
    for m in record['missions']:add_mission(m)
    add=QPushButton('补充研发目标');add.clicked.connect(lambda:add_mission());layout.addWidget(add)
    save=QPushButton('保存记录');layout.addWidget(save)
    def accept():
        if hashlib.sha256(source.read_bytes()).hexdigest()!=digest:
            QMessageBox.warning(dialog,'记录已变化','请关闭此窗口，重新打开校正记录。');return
        for k,w in boosts.items():
            if w.value()>=0:record['boosts'][k]=w.value()
        record['known_boosts']=[k for k,w in boosts.items() if w.value()>=0]
        record['unknown_boosts']=[k for k,w in boosts.items() if w.value()<0]
        record['skills']={k:w.value() for k,w in skills.items() if w.value()}
        record['gold_picks']=gold.value()
        record['factors']=[v.strip() for v in factors.text().replace('，',',').split(',') if v.strip()]
        record['missions']=[{**m,'group':g.currentText(),'attr':a.currentText(),'goal':goal.value(),'current':cur.value()}
                            for m,g,a,goal,cur in missions if goal.value()]
        path=picture.parent/'manual-adjustment.json';temp=path.with_suffix('.tmp')
        try:
            temp.write_text(json.dumps({'source_hash':digest,'record':normalize_record(record)},ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)
        except OSError as error:
            QMessageBox.warning(dialog,'保存失败',str(error));return
        if parent is not None and hasattr(parent,'resume_note'):
            parent.resume_note.setText('记录已保存；未核实的字段仍按未知处理。')
        dialog.accept()
    save.clicked.connect(accept);dialog.exec()
