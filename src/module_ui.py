from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox,QToolButton,QApplication,QCheckBox
from PySide6.QtCore import Qt,QTimer,QPoint,QSettings,Signal
from qfluentwidgets import ComboBox, PushButton
from src.crafting_policy import PROFILES,PROFILE_NOTES


class SkillRecordWindow(QWidget):
    mode_changed=Signal(bool)
    opacity_changed=Signal(float)
    HUD_WIDTH=960
    HUD_MARGIN=24
    DEFAULT_OPACITY=.75

    def __init__(self,parent=None,task=None,settings=None):
        flags=Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus|Qt.WindowTransparentForInput
        super().__init__(parent,flags)
        self.task=task
        # UI preferences have no connection to the crafting configuration or evidence.
        self.settings=settings if settings is not None else QSettings('LeimingAutomation','CraftingHUD')
        self.edit_mode=False
        self._drag_offset=None
        self._position_initialized=False
        self._saved_position=None
        try:
            if self.settings.contains('overlay_x') and self.settings.contains('overlay_y'):
                self._saved_position=QPoint(int(self.settings.value('overlay_x')),int(self.settings.value('overlay_y')))
            self.hud_opacity=max(.5,min(.95,float(self.settings.value('overlay_opacity',self.DEFAULT_OPACITY))))
        except (TypeError,ValueError,OverflowError):
            self._saved_position=None
            self.hud_opacity=self.DEFAULT_OPACITY
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self.setWindowTitle('模组制作记录')
        font=self.font()
        if font.pointSizeF()>9:font.setPointSizeF(9);self.setFont(font)
        self.setWindowOpacity(self.hud_opacity)
        self.setStyleSheet('SkillRecordWindow{background:#18232e;border:1px solid #607080;}'
                           'QLabel{color:#f5f7fa;} QToolButton{color:#ffffff;background:#364656;padding:3px;}')
        self.resize(self.HUD_WIDTH,120)
        layout=QVBoxLayout(self)
        layout.setContentsMargins(10,5,10,5);layout.setSpacing(1)
        layout.setSizeConstraint(QVBoxLayout.SetNoConstraint)
        row=QHBoxLayout();self.summary=QLabel('模组制作记录');row.addWidget(self.summary,1)
        self.reset_button=QToolButton(self);self.reset_button.setText('复位')
        self.reset_button.clicked.connect(self.reset_position);row.addWidget(self.reset_button)
        self.close_button=QToolButton(self);self.close_button.setText('×');self.close_button.setToolTip('结束调整并隐藏制作记录')
        self.close_button.clicked.connect(self.hide_record);row.addWidget(self.close_button)
        for button in (self.reset_button,self.close_button):
            button.setFocusPolicy(Qt.NoFocus);button.hide()
        layout.addLayout(row)
        self.label=QLabel('等待开始制作');layout.addWidget(self.label)
        self.detail=QLabel('');layout.addWidget(self.detail)
        self.decision=QLabel('');layout.addWidget(self.decision)
        layout.removeWidget(self.detail);layout.addWidget(self.detail)
        self.candidates=QLabel('');layout.addWidget(self.candidates)
        self.retry=QLabel('');layout.addWidget(self.retry)
        for label in (self.label,self.detail,self.decision,self.candidates,self.retry):
            label.setWordWrap(True)
            label.setTextFormat(Qt.PlainText)
            label.setMinimumSize(0,0)
            label.setMaximumHeight(label.fontMetrics().height()*2+4)
        self.summary.setTextFormat(Qt.PlainText)
        self.summary.setWordWrap(True)
        self.retry.setMaximumHeight(self.retry.fontMetrics().height()*3+4)
        self.timer=QTimer(self);self.timer.timeout.connect(self.update_record);self.timer.start(500)

    def _available_geometry(self):
        if self._position_initialized:
            return self.screen().availableGeometry()
        screen=self.parentWidget().screen() if self.parentWidget() else QApplication.primaryScreen()
        if self._saved_position is not None:
            screen=next((s for s in QApplication.screens() if s.availableGeometry().contains(self._saved_position)),screen)
            return screen.availableGeometry()
        try:
            import win32api
            hwnd=self.task.executor.device_manager.hwnd_window.hwnd
            name=win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd,2))['Device']
            screen=next((s for s in QApplication.screens() if s.name()==name),screen)
        except Exception:pass  # Monitor disappeared: retain the Qt screen fallback.
        return screen.availableGeometry()

    def _clamp_position(self,area):
        x=max(area.left(),min(self.x(),area.right()-self.width()+1))
        y=max(area.top(),min(self.y(),area.bottom()-self.height()+1))
        if QPoint(x,y)!=self.pos():self.move(x,y)

    def _fit_geometry(self):
        area=self._available_geometry()
        # Qt sizes are logical pixels. Keep the HUD compact at Windows 150%/200% scaling too.
        scale=max(1.,self.devicePixelRatioF())
        margin=round(self.HUD_MARGIN/scale)
        width=min(round(self.HUD_WIDTH/scale),max(1,area.width()-margin*2))
        self.resize(width,self.height())
        self.layout().activate()
        desired=max(round(120/scale),self.layout().totalHeightForWidth(width))
        self.resize(width,min(desired,round(200/scale),max(1,area.height()-margin*2)))
        if not self._position_initialized:
            position=self._saved_position or QPoint(area.left()+margin,area.bottom()-self.height()+1-margin)
            self.move(position)
            self._position_initialized=True
        self._clamp_position(area)

    def showEvent(self,event):
        self._fit_geometry()
        super().showEvent(event)

    def save_preferences(self):
        if self._position_initialized:
            self.settings.setValue('overlay_x',self.x())
            self.settings.setValue('overlay_y',self.y())
            self._saved_position=QPoint(self.pos())
        self.settings.setValue('overlay_opacity',self.hud_opacity)
        self.settings.sync()

    def set_hud_opacity(self,value):
        self.hud_opacity=max(.5,min(.95,float(value)))
        if not self.edit_mode:self.setWindowOpacity(self.hud_opacity)
        self.save_preferences()
        self.opacity_changed.emit(self.hud_opacity)

    def set_edit_mode(self,enabled):
        enabled=bool(enabled)
        if enabled==self.edit_mode:return
        if not enabled:self.save_preferences()
        position=QPoint(self.pos());visible=self.isVisible()
        self.edit_mode=enabled;self._drag_offset=None
        self.setAttribute(Qt.WA_TransparentForMouseEvents,not enabled)
        self.setWindowFlag(Qt.WindowTransparentForInput,not enabled)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowOpacity(.9 if enabled else self.hud_opacity)
        self.setCursor(Qt.OpenHandCursor if enabled else Qt.ArrowCursor)
        for button in (self.reset_button,self.close_button):button.setVisible(enabled)
        self.move(position)
        self.update_record()
        if visible:self.show()
        self.mode_changed.emit(enabled)

    def reset_position(self):
        self._saved_position=None;self._position_initialized=False
        self.hud_opacity=self.DEFAULT_OPACITY
        self.setWindowOpacity(.9 if self.edit_mode else self.hud_opacity)
        self._fit_geometry();self.save_preferences()
        self.opacity_changed.emit(self.hud_opacity)

    def hide_record(self):
        self.set_edit_mode(False);self.hide()

    def mousePressEvent(self,event):
        if self.edit_mode and event.button()==Qt.LeftButton:
            self._drag_offset=event.globalPosition().toPoint()-self.pos()
            self.setCursor(Qt.ClosedHandCursor);event.accept()
        else:event.ignore()

    def mouseMoveEvent(self,event):
        if self.edit_mode and self._drag_offset is not None and event.buttons()&Qt.LeftButton:
            self.move(event.globalPosition().toPoint()-self._drag_offset)
            self._clamp_position(self._available_geometry());event.accept()
        else:event.ignore()

    def mouseReleaseEvent(self,event):
        if self.edit_mode and self._drag_offset is not None and event.button()==Qt.LeftButton:
            self._drag_offset=None;self.setCursor(Qt.OpenHandCursor)
            self.save_preferences();event.accept()
        else:event.ignore()

    def line(self,label,text,limit=None):
        label.setToolTip(text)
        label.setText(text if limit is None or len(text)<=limit else text[:limit]+'…（详见日志）')
        label.setVisible(bool(text))

    def update_record(self):
        from src.crafting_runner import status
        from src.task_controls import control_for,control_status
        d=dict(status)
        task=self.task;control=control_for(task) if task is not None else None
        info=getattr(task,'info',{}) if task is not None else {}
        if control is not None and control.state in control.TERMINAL:
            # A policy SELECT remains useful evidence, but never describes
            # the execution result after Stop/Error/Success.
            self.summary.setText('模组制作 · '+control_status(task))
            if d:
                scope='历史完成数未知；本次' if d.get('历史完成数')=='未知' else f"目标 {d.get('目标',1)} 个"
                self.summary.setText(self.summary.text()+' ｜ '+str(d.get('制作配置',''))+' ｜ '+scope+
                                     f" ｜ 正常 {d.get('正常完成',0)} ｜ 快速 {d.get('快速结束',0)}")
                if d.get('分类未知完成',0):self.summary.setText(self.summary.text()+f" ｜ 分类未知 {d['分类未知完成']}")
            if self.edit_mode:self.summary.setText('调整模式：拖动窗口，完成后再运行制作 ｜ '+self.summary.text())
            skills=' ｜ '.join(f'{k}：{v}级' for k,v in d.get('已选技能',{}).items())
            self.line(self.label,skills)
            from src.task_hud import STOP_REASONS
            reason=str(info.get('停止原因') or info.get('结果') or STOP_REASONS.get(control.stop_source,control.stop_source) or '')
            self.line(self.detail,('结果：' if control.state=='SUCCESS' else '停止原因：')+reason if reason else '')
            for label in (self.decision,self.candidates,self.retry):
                label.clear();label.setToolTip('');label.hide()
            if self.isVisible():self._fit_geometry()
            return
        if not d:
            execution=str(info.get('执行阶段') or '')
            self.summary.setText('调整模式：拖动窗口；完成调整后再运行制作' if self.edit_mode else
                                 ('模组制作 · '+control_status(task)+(' ｜ '+execution if execution else '')
                                  if control is not None else '模组制作记录 HUD'))
            for label in (self.label,self.detail,self.decision,self.candidates,self.retry):
                label.clear();label.setToolTip('');label.hide()
            if self.isVisible():self._fit_geometry()
            return
        skills=' ｜ '.join(f'{k}：{v}级' for k,v in d.get('已选技能',{}).items()) or '尚未选择技能'
        boosts=' / '.join(f'{k} {v}次' for k,v in d.get('激增次数',{}).items())
        scope='历史完成数未知；本次' if d.get('历史完成数')=='未知' else f"目标 {d.get('目标',1)} 个"
        self.summary.setText(f"{scope} ｜ 正常 {d.get('正常完成',0)} ｜ 快速 {d.get('快速结束',0)}")
        stages={'prepared':'实验准备','hub':'研发进度','attribute':'属性刻录','skill':'技能研习',
                'mission':'研发目标','factor':'因子选择','core_attribute':'战术核心·属性',
                'core_skill':'战术核心·技能','rest':'快速休整','overclock':'超频动画',
                'refresh_confirm':'刷新确认','quick_dialog':'快速研发确认','result':'制作结果','map':'研发地图'}
        stage=d.get('研发阶段');profile=d.get('制作配置','')
        context=' ｜ '.join(value for value in (profile,stages.get(stage,stage or '')) if value)
        mode=d.get('技能模式')
        if profile=='先攻' or mode:
            context+=' ｜ '+str(mode or '未知')+'模式'
        if context:self.summary.setText(context+' ｜ '+self.summary.text())
        if d.get('分类未知完成',0):self.summary.setText(self.summary.text()+f" ｜ 分类未知 {d['分类未知完成']}")
        execution=str(info.get('执行阶段') or '')
        if control is not None or execution:
            lifecycle=control_status(task) if control is not None else ''
            self.summary.setText(' ｜ '.join(value for value in (lifecycle,execution,self.summary.text()) if value))
        if self.edit_mode:self.summary.setText('调整模式：拖动窗口，完成后再运行制作 ｜ '+self.summary.text())
        self.line(self.label,skills)
        self.line(self.detail,f"{d.get('当前状态','')}  {boosts}",100)
        decision=d.get('属性决策',{}) if stage in (None,'attribute') else {}
        if decision:
            self.label.hide()
            action=decision.get('decision_action','').upper()
            target=decision.get('selected_index')
            rule=decision.get('decision_rule','')
            rules={'shield_attack_highest':'激增重盾攻击最高效能比较','ordinary_shield_attack_highest':'普通重盾攻击最高效能比较',
                   'people_threshold':'刷新人数门槛','off_target_highest':'安全候选最高效能',
                   'fire_surge':'激增火力攻击','fire_reward_task':'火攻奖励任务例外',
                   'historical_evidence_unavailable':'当前动作所需历史未知','boost_dependency':'核验影响决策的激增标记',
                   'candidate_identity':'补齐候选身份'}
            self.line(self.decision,f"属性刻录 ｜ 策略 {action}"+
                      (f' ｜ 目标：#{target}' if target is not None else '')+
                      (f" ｜ 规则：{rules.get(rule,rule)}" if action!='SELECT' else ''))
            reason_limit=min(80,max(28,(self.width()-32)//max(1,self.fontMetrics().horizontalAdvance('字'))-12))
            self.line(self.detail,'原因：'+decision.get('decision_reason',''),reason_limit)
            if mode and d.get('模式原因'):
                reason=d['模式原因']
                shown_reason=reason[:reason_limit]+('…' if len(reason)>reason_limit else '')
                self.detail.setToolTip('原因：'+decision.get('decision_reason','')+'\n模式原因：'+reason)
                self.detail.setText(self.detail.text()+'\n模式原因：'+shown_reason)
            shown=decision.get('candidates',[])
            if action=='SELECT':shown=[o for o in shown if o['index']==target]
            candidates=' ｜ '.join(f"#{o['index']} "+
                        (f"{o.get('group','')}/{o.get('attr','')}+{o.get('value',0)} " if o['index']==target else '')+
                        ('激增 ' if o.get('boost') and o.get('boost_known') else '激增? ' if not o.get('boost_known',True) else '')+
                        (f"{o.get('score')}（可信）" if o.get('score_known') else '?（未读）') for o in shown)
            self.line(self.candidates,'候选：'+candidates)
            if action=='SELECT' and shown:
                self.line(self.decision,self.decision.text()+' ｜ '+candidates.split(' ',1)[1])
            available=decision.get('refresh_available')
            evidence=decision.get('retry',{})
            self.line(self.retry,f"人数：{decision.get('remaining_people','?')} ｜ 门槛：{decision.get('refresh_threshold','?')} ｜ "+
                      f"刷新：{'可用' if available is True else '已用／不可用' if available is False else '未知'} ｜ 卡片：{'完整' if decision.get('cards_complete') else '待补齐'}"+
                      (f"\n缺失：{', '.join(decision.get('missing',[])) or '无'} ｜ 卡位：{evidence.get('targeted_slot','—')} ｜ "+
                       f"producer：{evidence.get('producer','—')} ｜ 重读：{evidence.get('attempt',0)}/{evidence.get('max_attempts',0)}"
                       if action in ('OBSERVE','STOP') else ''))
            self.candidates.setVisible(action in ('OBSERVE','STOP'))
        else:
            for label in (self.decision,self.candidates,self.retry):label.clear();label.hide()
            if mode:
                reason=d.get('模式原因','')
                if reason:
                    limit=min(80,max(28,(self.width()-32)//max(1,self.fontMetrics().horizontalAdvance('字'))-8))
                    self.line(self.detail,'模式原因：'+reason,limit)
                gold=d.get('金技能分类')
                if gold:
                    self.line(self.decision,f"金技能：目标 {gold.get('target',0)} ｜ 目标外 {gold.get('outside',0)} ｜ 分类未知 {gold.get('unclassified',0)}")
        if self.isVisible():
            self._fit_geometry()


class ModuleStrategyPanel(QWidget):
    def __init__(self,task,parent=None):
        super().__init__(parent)
        self.task=task
        crafting=task.executor.global_config.get_config('ModuleCrafting')
        layout=QVBoxLayout(self)
        self.strategy_note=QLabel(self)
        self.strategy_note.setWordWrap(True)
        self.strategy_note.setTextFormat(Qt.RichText)
        self.strategy_note.setStyleSheet('QLabel { background: #eef4ff; color: #243b60; border: 1px solid #cdddf5; border-radius: 10px; padding: 14px; }')
        layout.addWidget(self.strategy_note)
        row=QHBoxLayout();row.addWidget(QLabel('制作配置'))
        self.profile=ComboBox(self);self.profile.addItems(list(PROFILES))
        self.profile.setCurrentText(crafting.get('制作配置') or '旧地联合')
        def change_profile(value):
            crafting['制作配置']=value;self.update_strategy_note(value)
        self.profile.currentTextChanged.connect(change_profile)
        self.update_strategy_note(self.profile.currentText());row.addWidget(self.profile)
        row.addWidget(QLabel('制作数量'))
        self.count=QSpinBox(self);self.count.setRange(1,100);self.count.setValue(int(crafting.get('制作数量') or 1))
        self.count.valueChanged.connect(lambda v:crafting.__setitem__('制作数量',v));row.addWidget(self.count)
        row.addStretch();layout.addLayout(row)
        row=QHBoxLayout();row.addWidget(QLabel('属性刷新方式'))
        self.refresh=ComboBox(self);self.refresh.addItems(['自动','不刷新'])
        mode=crafting.get('属性刷新方式') or '自动'
        self.refresh.setCurrentText('自动' if mode=='优先刷新' else mode)
        self.refresh.currentTextChanged.connect(lambda v:crafting.__setitem__('属性刷新方式',v));row.addWidget(self.refresh)
        row.addStretch();layout.addLayout(row)
        row=QHBoxLayout();row.addWidget(QLabel('属性刷新人数门槛'))
        self.threshold=QSpinBox(self);self.threshold.setRange(4,9)
        self.threshold.setValue(int(crafting.get('属性刷新人数门槛',6)))
        self.threshold.valueChanged.connect(lambda v:crafting.__setitem__('属性刷新人数门槛',v))
        row.addWidget(self.threshold);row.addWidget(QLabel('默认6；自动刷新遵守此门槛'));row.addStretch();layout.addLayout(row)
        self.mission_priority=QCheckBox('优先火力攻击奖励任务',self)
        self.mission_priority.setChecked(crafting.get('优先火力攻击奖励任务',True))
        self.mission_priority.setToolTip('仅在未完成火攻奖励任务对应盾防／盾攻／工程生命激增且未满次数时提升并阻止刷新。不会突破次数上限。')
        self.mission_priority.toggled.connect(lambda v:crafting.__setitem__('优先火力攻击奖励任务',v))
        layout.addWidget(self.mission_priority)
        # Parent the one primary overlay to the main window, rather than a
        # particular settings panel. Re-entering a tab keeps its preferences
        # and the current craft/resume task binding.
        owner=self.window()
        from shiboken6 import isValid
        self.record=getattr(owner,'module_record_window',None)
        if self.record is None or not isValid(self.record):
            self.record=SkillRecordWindow(owner,task)
            owner.module_record_window=self.record
        self.record_button=PushButton('显示 / 隐藏制作记录',self)
        self.record_button.clicked.connect(self.toggle_record);layout.addWidget(self.record_button)
        row=QHBoxLayout()
        self.hud_adjust_button=PushButton('调整制作记录浮窗',self)
        self.hud_adjust_button.clicked.connect(self.toggle_hud_edit);row.addWidget(self.hud_adjust_button)
        self.hud_reset_button=PushButton('重置浮窗位置',self)
        self.hud_reset_button.clicked.connect(self.reset_hud);row.addWidget(self.hud_reset_button)
        row.addWidget(QLabel('HUD透明度'))
        self.hud_opacity=QSpinBox(self);self.hud_opacity.setRange(50,95);self.hud_opacity.setSuffix('%')
        self.hud_opacity.setValue(round(self.record.hud_opacity*100))
        self.hud_opacity.valueChanged.connect(lambda v:self.record.set_hud_opacity(v/100))
        row.addWidget(self.hud_opacity);row.addStretch();layout.addLayout(row)
        self.hud_note=QLabel('HUD默认鼠标穿透。调整模式接收鼠标，可拖动；请完成调整后再运行制作。')
        self.hud_note.setWordWrap(True);layout.addWidget(self.hud_note)
        self.record.mode_changed.connect(self.hud_mode_changed)
        self.record.opacity_changed.connect(self.hud_opacity_changed)
        self.adjust_button=PushButton('查看 / 校正制作记录',self)
        from src.resume_ui import edit_resume
        self.adjust_button.clicked.connect(lambda:edit_resume(self));layout.addWidget(self.adjust_button)
        self.resume_note=QLabel('“继续当前模组”从当前已识别的制作页面直接接管。无法证明的历史字段保持未知；只有当前动作确实依赖缺失证据时才停止。历史完成数未知时仅继续当前一件；不可逆输入结果不确定时不会自动重放。')
        self.resume_note.setWordWrap(True);layout.addWidget(self.resume_note)
        self.was_running=False
        self.timer=QTimer(self);self.timer.timeout.connect(self.show_running);self.timer.start(500)

    def update_strategy_note(self,profile):
        from html import escape
        from src.crafting_runner import status
        goal,priority,rules=PROFILE_NOTES[profile]
        live=''
        if profile=='先攻' and status.get('制作配置')==profile and status.get('技能模式'):
            mode=escape(str(status['技能模式']))
            reason=escape(str(status.get('模式原因','')))
            live=f'<br><b>当前</b>　{mode}模式'+(f'：{reason}' if reason else '')
            if status['技能模式']=='狗粮':live+='；优先当前最高效能安全候选。'
        self.strategy_note.setText(f'<b style="font-size:16px">{profile}</b><br>{goal}<br>'
                                   f'<b>优先</b>　{priority}<br><b>规则</b>　{rules}'+live)

    def crafting_running(self):
        return any(t.enabled for t in self.task.executor.onetime_tasks if getattr(t,'only',None) in ('MODULE_CRAFT','MODULE_RESUME'))

    def active_crafting_task(self):
        executor=self.task.executor
        current=getattr(executor,'current_task',None)
        if getattr(current,'only',None) in ('MODULE_CRAFT','MODULE_RESUME') and current.enabled:return current
        return next((t for t in executor.onetime_tasks if getattr(t,'only',None) in ('MODULE_CRAFT','MODULE_RESUME') and t.enabled),None)

    def toggle_record(self):
        if self.record.isVisible():self.record.hide_record()
        else:self.record.show()

    def toggle_hud_edit(self):
        if self.crafting_running():return
        self.record.show()
        self.record.set_edit_mode(not self.record.edit_mode)

    def hud_mode_changed(self,editing):
        self.hud_adjust_button.setText('完成调整' if editing else '调整制作记录浮窗')

    def reset_hud(self):
        if self.crafting_running():return
        self.record.reset_position()

    def hud_opacity_changed(self,value):
        blocked=self.hud_opacity.blockSignals(True)
        self.hud_opacity.setValue(round(value*100));self.hud_opacity.blockSignals(blocked)

    def show_running(self):
        self.update_strategy_note(self.profile.currentText())
        active=self.active_crafting_task();running=active is not None
        if active is not None:self.record.task=active
        self.adjust_button.setEnabled(not running)
        for control in (self.hud_adjust_button,self.hud_reset_button,self.hud_opacity):control.setEnabled(not running)
        if running:self.record.set_edit_mode(False)
        if running and not self.was_running:self.record.show()
        self.record.update_record()
        self.was_running=running
