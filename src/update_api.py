"""Connect the real About-page controls to this project's portable updater."""
import os
from pathlib import Path
import sys
from types import MethodType

from packaging.version import InvalidVersion, Version
from src.update_client import GitHubUpdateClient, UpdateError, cancelled
from src.version import APP_VERSION


def automation_active(main_window, executor):
    """Installation must not close active, paused or queued automation."""
    if executor is not None:
        if getattr(executor, 'current_task', None) is not None:
            return True
        tasks = list(getattr(executor, 'onetime_tasks', [])) + list(getattr(executor, 'trigger_tasks', []))
        if any(task.enabled for task in tasks):
            return True
    panels = [getattr(getattr(main_window, 'start_tab', None), 'runtime_720p_panel', None)]
    panels.extend(getattr(tab, 'runtime_720p_panel', None)
                  for tab in getattr(main_window, 'grouped_task_tabs', []))
    return any(getattr(panel, 'worker', None) is not None and panel.worker.is_alive()
               for panel in panels)


class UpdateApi:
    def __init__(self, backend=None, current_version=APP_VERSION, task_active=None):
        self.backend = backend if backend is not None else GitHubUpdateClient()
        self.app_version = current_version
        self.task_active = task_active or (lambda: False)

    def get_version_list(self, *args, **kwargs):
        versions = self.backend.get_version_list(*args, **kwargs)
        if not isinstance(versions, list):
            raise ValueError('更新版本列表格式无效')
        for item in versions:
            version = item.get('version') if isinstance(item, dict) else None
            if not isinstance(version, str):
                raise ValueError('更新数据缺少有效的版本字符串')
            try:
                Version(version)
            except InvalidVersion as error:
                raise ValueError(f'更新版本号无效：{version!r}') from error
        return versions

    @staticmethod
    def is_greater_version(left, right):
        try:
            return Version(left) > Version(right)
        except (InvalidVersion, TypeError):
            return False

    @staticmethod
    def calculate_update_notes(versions, current_version, target_version):
        return [str(note) for item in versions if item.get('version') == target_version
                for note in item.get('update_note', [])]

    def update_to_version(self, version, timeout=600, exit_event=None):
        cancelled(exit_event)
        if not self.is_greater_version(version, self.app_version):
            raise UpdateError('所选版本不高于当前版本，不执行降级或覆盖')
        if self.task_active():
            raise UpdateError('请先停止正在运行的自动化任务，再安装更新')
        if os.name != 'nt' or not getattr(sys, 'frozen', False):
            raise UpdateError('自动安装只支持 Windows 便携 EXE；源码开发环境请使用 Git')
        from src.app_paths import APP_ROOT
        from src.update_installer import start_update_install
        stage = self.backend.prepare_update(version, self.app_version,
                                            Path(APP_ROOT).parent / '.leiming-update-downloads',
                                            exit_event=exit_event, timeout=timeout)
        cancelled(exit_event)
        if self.task_active():
            raise UpdateError('下载已校验，但有自动化任务正在运行；请停止任务后重试安装')
        return start_update_install(stage, APP_ROOT, version, exit_event=exit_event)


def configure_update_card(card, api):
    """Keep upstream UI/threading; replace only transport, channel and downgrade policy."""
    if getattr(card, '_leiming_update_configured', False):
        return
    card.pyappify_module = api
    card.current_version = api.app_version
    card.test_version_checkbox.setChecked(Version(api.app_version).is_prerelease)
    card.test_version_checkbox.setToolTip('包含本项目已发布的 Beta 和 RC；草稿不会用于更新')
    card.status_label.setWordWrap(True)
    original = card._selection_changed

    def select(self, index=None):
        original(index)
        if self.version_combo.currentIndex() >= 0:
            target = self.version_combo.currentText()
            if not api.is_greater_version(target, self.current_version):
                self.update_button.setEnabled(False)
                self.update_button.setText('当前版本' if Version(target) == Version(self.current_version)
                                           else '旧版本（不降级）')

    card.version_combo.currentIndexChanged.disconnect(original)
    card._selection_changed = MethodType(select, card)
    card.version_combo.currentIndexChanged.connect(card._selection_changed)
    card._leiming_update_configured = True


def install_update_api(main_window):
    card = getattr(getattr(main_window, 'about_tab', None), 'update_card', None)
    if card is None or getattr(card, '_leiming_update_configured', False):
        return

    def task_active():
        from ok import og
        return automation_active(main_window, getattr(og, 'executor', None))

    configure_update_card(card, UpdateApi(task_active=task_active))
    version_card = getattr(main_window.about_tab, 'version_card', None)
    if version_card is not None:
        channel = '预发布' if Version(APP_VERSION).is_prerelease else '正式版'
        version_card.setContent(f'{APP_VERSION} · {channel}')

    def quit_when_prepared(result):
        successful, value = result
        if not successful or not isinstance(value, dict) or value.get('prepared') is not True:
            return
        if task_active():
            from src.update_installer import cancel_update_install
            cancel_update_install(value['job'])
            card._show_error('安装准备后启动了自动化任务，未退出；请停止任务后重试更新')
            return
        from ok import og
        og.ok.quit()

    card._update_finished.connect(quit_when_prepared)
