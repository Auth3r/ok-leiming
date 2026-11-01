"""Real Qt clicks and real ZIP verification with explicitly simulated GitHub HTTP bodies."""
import hashlib
import io
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import zipfile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from ok.ui.qt.about.UpdateCard import UpdateCard
from src.update_api import UpdateApi, configure_update_card, install_update_api, automation_active
from src.update_client import (API_ROOT, GitHubUpdateClient, UpdateAccessError, UpdateDataError,
                               UpdateError, UpdateNetworkError, ReleaseRedirectHandler,
                               extract_verified_archive, validate_package)

CURRENT = 'v1.0.0-beta.6'
TARGET = 'v1.0.0-beta.7'
ZIP_NAME = f'Leiming-Automation-{TARGET}-win-x64.zip'


def release(version=TARGET, **changes):
    value = {'tag_name': version, 'draft': False, 'prerelease': '-beta.' in version or '-rc.' in version,
             'published_at': '2026-10-28T01:00:00Z', 'body': 'Updater test fixture', 'assets': []}
    value.update(changes)
    return value


class SimulatedGitHub:
    """Replaces only HTTPS I/O; production parsing, downloading and validation run unchanged."""
    def __init__(self, releases, assets=None, tag_responses=None):
        self.releases = releases
        self.assets = assets or {}
        self.requests = []
        self.tag_responses = tag_responses

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.releases, Exception):
            raise self.releases
        if '/releases?' in request.full_url:
            return io.BytesIO(json.dumps(self.releases).encode())
        if '/git/' in request.full_url:
            if self.tag_responses is not None:
                response = self.tag_responses[request.full_url.removeprefix(API_ROOT)]
            else:
                response = {'ref': f'refs/tags/{TARGET}', 'object': {'type': 'commit', 'sha': 'a' * 40}}
            return io.BytesIO(json.dumps(response).encode())
        return io.BytesIO(self.assets[int(request.full_url.rsplit('/', 1)[-1])])


def client(response, assets=None):
    return GitHubUpdateClient(token='', opener=SimulatedGitHub(response, assets))


@pytest.mark.parametrize('target,current,newer', [
    ('v1.0.0-beta.7', 'v1.0.0-beta.6', True),
    ('v1.0.0-beta.10', 'v1.0.0-beta.6', True),
    ('v1.0.0-beta.6', 'v1.0.0-beta.10', False),
    ('v1.0.0-rc.1', 'v1.0.0-beta.6', True),
    ('v1.0.0', 'v1.0.0-beta.6', True),
    ('v1.0.0-beta.6', 'v1.0.0', False),
    ('1.0.0b6', 'v1.0.0-beta.6', False),
    ('v1.0.0-beta.6', '1.0.0b6', False),
    ('v1.0.0-beta.5', 'v1.0.0-beta.6', False),
])
def test_semantic_beta_rc_and_release_order(target, current, newer):
    assert UpdateApi.is_greater_version(target, current) is newer


@pytest.mark.parametrize('response', [None, {}, [None], [{}], [{'version': None}],
                                     [{'version': 7}], [{'version': ''}],
                                     [release('not-a-version')]])
def test_invalid_remote_data_is_an_error_not_no_update(response):
    with pytest.raises(UpdateDataError):
        client(response).get_version_list()


def test_release_selection_excludes_drafts_and_invalid_tags_and_sorts_beta_rc_final():
    service = client([release('v1.0.0-beta.20'), release('v1.0.0-rc.1'), release('v1.0.0'),
                      release('v2.0.0', draft=True), release('v1.0.0-beta.6-rc.20261023'),
                      release('nonsense'), release('v1.0.0-beta.6')])
    assert [v['version'] for v in service.get_version_list()] == [
        'v1.0.0', 'v1.0.0-rc.1', 'v1.0.0-beta.20', 'v1.0.0-beta.6']
    assert [v['version'] for v in service.get_version_list(release_only=True)] == ['v1.0.0']


@pytest.mark.parametrize('code', [401, 403, 404])
def test_private_access_errors_never_look_like_no_updates(code):
    error = urllib.error.HTTPError(API_ROOT, code, 'HTTP fixture', {}, None)
    with pytest.raises(UpdateAccessError, match=f'HTTP {code}'):
        client(error).get_version_list()


@pytest.mark.parametrize('error', [TimeoutError(), urllib.error.URLError('offline')])
def test_network_error_is_distinct_from_empty_releases(error):
    with pytest.raises(UpdateNetworkError):
        client(error).get_version_list()
    assert client([]).get_version_list() == []


def test_private_token_is_not_forwarded_to_asset_redirect():
    transport = SimulatedGitHub([])
    service = GitHubUpdateClient(token='test-only-placeholder', opener=transport)
    service.get_version_list()
    request = transport.requests[0]
    redirected = ReleaseRedirectHandler().redirect_request(
        request, None, 302, 'redirect', {}, 'https://release-assets.githubusercontent.com/signed-fixture')
    assert request.get_header('Authorization') == 'Bearer test-only-placeholder'
    assert redirected.get_header('Authorization') is None


@pytest.mark.parametrize('url', ['http://release-assets.githubusercontent.com/payload',
                               'https://example.com/payload', 'https://github.com@evil.test/payload'])
def test_redirect_cannot_leave_trusted_https_asset_hosts(url):
    with pytest.raises(UpdateDataError):
        ReleaseRedirectHandler().redirect_request(urllib.request.Request(API_ROOT), None, 302, '', {}, url)


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('version,channel', [
    ('v1.0.0-beta.8', '预发布'),
    ('v1.0.0', '正式版'),
])
def test_actual_version_card_labels_release_channel(app, monkeypatch, version, channel):
    from types import SimpleNamespace
    from PySide6.QtGui import QIcon
    from ok.ui.qt.about.VersionCard import VersionCard
    monkeypatch.setattr('src.update_api.APP_VERSION', version)
    card = UpdateCard(version, UpdateApi(client([]), version))
    identity = VersionCard({}, QIcon(), 'App', version, False)
    try:
        install_update_api(SimpleNamespace(about_tab=SimpleNamespace(update_card=card, version_card=identity)))
        assert identity.contentLabel.text() == f'{version} · {channel}'
    finally:
        card.deleteLater()
        identity.deleteLater()
        app.processEvents()


@pytest.mark.parametrize('response,expected', [
    ([release(TARGET)], 'new'),
    ([release(CURRENT)], 'same'),
    ([release('v1.0.0-beta.5')], 'older'),
    ([], 'empty'),
    ([release('not-a-version')], 'error'),
    (urllib.error.URLError('simulated offline'), 'error'),
])
def test_check_button_shows_correct_update_outcome(app, response, expected):
    api = UpdateApi(client(response), CURRENT)
    card = UpdateCard(CURRENT, api)
    configure_update_card(card, api)
    card.show()
    app.processEvents()
    try:
        assert card.test_version_checkbox.isChecked()
        QTest.mouseClick(card.check_button, Qt.MouseButton.LeftButton)
        deadline = time.monotonic() + 4
        while card._busy and time.monotonic() < deadline:
            QTest.qWait(10)
        QTest.qWait(10)
        assert not card._busy and card.check_button.isEnabled()
        assert card.update_button.isEnabled() is (expected == 'new')
        if expected == 'error':
            assert card.status_label.styleSheet()
        if expected == 'same':
            assert card.update_button.text() == '当前版本'
        if expected == 'older':
            assert '不降级' in card.update_button.text()
        if expected == 'empty':
            assert 'No versions' in card.status_label.text()
    finally:
        card.close()
        card.deleteLater()
        app.processEvents()


def package_bytes(*, tamper=False, extra=None, manifest_changes=None):
    files = {'Leiming-Automation.exe': b'MZ-fake-for-unit-test-not-an-executable',
             '_internal/library.dll': b'unit-test-library', 'icons/icon.png': b'unit-test-icon'}
    manifest = {'schema_version': 1, 'repository': 'Auth3r/ok-leiming',
                'platform': 'win-x64', 'release_tag': TARGET, 'app_version': TARGET,
                'git_commit': 'a' * 40, 'git_dirty': False, 'trial': False,
                'runtime_files_sha256': {name: hashlib.sha256(data).hexdigest()
                                         for name, data in files.items()}}
    manifest.update(manifest_changes or {})
    if tamper:
        files['Leiming-Automation.exe'] = b'corrupted-test-executable'
    files['release-manifest.json'] = json.dumps(manifest).encode()
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        for name, content in files.items():
            archive.writestr('Leiming-Automation/' + name, content)
        if extra:
            archive.writestr(extra, b'unlisted')
    return data.getvalue()


def download_service(data, *, corrupt=False, omit_checksum=False):
    digest = hashlib.sha256(data).hexdigest()
    assets = [{'id': 1, 'name': ZIP_NAME, 'state': 'uploaded', 'size': len(data), 'digest': 'sha256:' + digest}]
    if not omit_checksum:
        assets.append({'id': 2, 'name': ZIP_NAME + '.sha256', 'state': 'uploaded', 'size': 140})
    return client([release(assets=assets)], {1: b'broken' if corrupt else data,
                                           2: f'{digest}  {ZIP_NAME}\n'.encode()})


def test_download_hash_and_runtime_manifest_prepare_installable_stage(tmp_path):
    service = download_service(package_bytes())
    root = service.prepare_update(TARGET, CURRENT, tmp_path)
    assert root.name == 'Leiming-Automation'
    assert validate_package(root, TARGET)['app_version'] == TARGET
    assert (root / 'Leiming-Automation.exe').read_bytes().startswith(b'MZ-fake')


def test_download_annotated_tag_binds_package_to_actual_commit(tmp_path):
    service = download_service(package_bytes())
    service._opener.tag_responses = {
        f'/git/ref/tags/{TARGET}': {'ref': f'refs/tags/{TARGET}', 'object': {'type': 'tag', 'sha': 'b' * 40}},
        '/git/tags/' + 'b' * 40: {'sha': 'b' * 40, 'object': {'type': 'tag', 'sha': 'c' * 40}},
        '/git/tags/' + 'c' * 40: {'sha': 'c' * 40, 'object': {'type': 'commit', 'sha': 'a' * 40}},
    }
    root = service.prepare_update(TARGET, CURRENT, tmp_path)
    assert validate_package(root, TARGET)['git_commit'] == 'a' * 40


def test_download_rejects_valid_hashes_built_from_wrong_tag_commit(tmp_path):
    service = download_service(package_bytes())
    service._opener.tag_responses = {
        f'/git/ref/tags/{TARGET}': {'ref': f'refs/tags/{TARGET}', 'object': {'type': 'commit', 'sha': 'b' * 40}},
    }
    with pytest.raises(UpdateDataError, match='源码提交与 GitHub 发布标签不一致'):
        service.prepare_update(TARGET, CURRENT, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('object_data', [None, {}, {'type': 'tree', 'sha': 'a' * 40},
                                        {'type': 'commit', 'sha': 'not-a-sha'}])
def test_invalid_tag_target_cannot_establish_provenance(object_data):
    service = client([])
    service._opener.tag_responses = {
        f'/git/ref/tags/{TARGET}': {'ref': f'refs/tags/{TARGET}', 'object': object_data}}
    with pytest.raises(UpdateDataError):
        service.get_tag_commit(TARGET)


def test_tag_reference_name_and_object_identity_must_match():
    service = client([])
    service._opener.tag_responses = {
        f'/git/ref/tags/{TARGET}': {'ref': 'refs/tags/other', 'object': {'type': 'commit', 'sha': 'a' * 40}}}
    with pytest.raises(UpdateDataError, match='所选版本不一致'):
        service.get_tag_commit(TARGET)
    service._opener.tag_responses = {
        f'/git/ref/tags/{TARGET}': {'ref': f'refs/tags/{TARGET}', 'object': {'type': 'tag', 'sha': 'b' * 40}},
        '/git/tags/' + 'b' * 40: {'sha': 'c' * 40, 'object': {'type': 'commit', 'sha': 'a' * 40}}}
    with pytest.raises(UpdateDataError, match='标签对象摘要不一致'):
        service.get_tag_commit(TARGET)


@pytest.mark.parametrize('cycle', [True, False])
def test_tag_resolution_rejects_cycles_and_excessive_indirection(cycle):
    service = client([])
    service._opener.tag_responses = {
        f'/git/ref/tags/{TARGET}': {'ref': f'refs/tags/{TARGET}', 'object': {'type': 'tag', 'sha': '1' * 40}}}
    for number in range(1, 8):
        sha = str(number) * 40
        service._opener.tag_responses['/git/tags/' + sha] = {
            'sha': sha, 'object': {'type': 'tag', 'sha': '1' * 40 if cycle else str(number + 1) * 40}}
    with pytest.raises(UpdateDataError, match='循环引用' if cycle else '引用层级过多'):
        service.get_tag_commit(TARGET)


@pytest.mark.parametrize('case', ['download-corrupt', 'runtime-corrupt', 'checksum-missing', 'same', 'older'])
def test_failed_download_or_nonupgrade_never_changes_installed_data(tmp_path, case):
    install = tmp_path / 'existing'
    install.mkdir()
    (install / 'settings.json').write_text('personal', encoding='utf-8')
    (install / 'Leiming-Automation.exe').write_bytes(b'old-working-exe')
    service = download_service(package_bytes(tamper=case == 'runtime-corrupt'),
                               corrupt=case == 'download-corrupt', omit_checksum=case == 'checksum-missing')
    current = TARGET if case == 'same' else 'v1.0.0-beta.9' if case == 'older' else CURRENT
    with pytest.raises(UpdateDataError):
        service.prepare_update(TARGET, current, tmp_path / 'staging')
    assert (install / 'settings.json').read_text(encoding='utf-8') == 'personal'
    assert (install / 'Leiming-Automation.exe').read_bytes() == b'old-working-exe'


@pytest.mark.parametrize('name', ['../outside', 'Leiming-Automation/../../outside',
    'Leiming-Automation/C:stream', 'Leiming-Automation/CON.txt',
    'Leiming-Automation/_internal/evil.dll', 'Leiming-Automation/settings.json',
    'Leiming-Automation/Leiming-Automation.EXE', 'Leiming-Automation/logs/private.txt'])
def test_untrusted_zip_paths_and_user_data_are_rejected(tmp_path, name):
    archive = tmp_path / 'update.zip'
    archive.write_bytes(package_bytes(extra=name))
    with pytest.raises(UpdateDataError):
        extract_verified_archive(archive, tmp_path / 'extracted', TARGET)
    assert not (tmp_path / 'outside').exists()


@pytest.mark.parametrize('change', [{'trial': True}, {'git_dirty': True}, {'git_commit': 'wrong'},
                                    {'app_version': CURRENT}, {'release_tag': CURRENT},
                                    {'repository': 'upstream/template'}, {'platform': 'linux-x64'}])
def test_provenance_mismatch_cannot_be_installed(tmp_path, change):
    service = download_service(package_bytes(manifest_changes=change))
    with pytest.raises(UpdateDataError):
        service.prepare_update(TARGET, CURRENT, tmp_path)


def test_cancelled_download_does_not_touch_disk(tmp_path):
    event = threading.Event()
    event.set()
    with pytest.raises(InterruptedError):
        download_service(package_bytes()).prepare_update(TARGET, CURRENT, tmp_path, event)
    assert list(tmp_path.iterdir()) == []


def test_running_task_blocks_only_installation_not_checking():
    api = UpdateApi(client([release()]), CURRENT, task_active=lambda: True)
    assert api.get_version_list()[0]['version'] == TARGET
    with pytest.raises(UpdateError, match='请先停止'):
        api.update_to_version(TARGET)


def test_independent_720p_worker_blocks_install_while_running_or_paused():
    from types import SimpleNamespace
    stopped = threading.Event()
    worker = threading.Thread(target=stopped.wait)
    panel = SimpleNamespace(worker=worker)
    main = SimpleNamespace(start_tab=SimpleNamespace(runtime_720p_panel=panel), grouped_task_tabs=[])
    executor = SimpleNamespace(current_task=None, onetime_tasks=[], trigger_tasks=[])
    assert not automation_active(main, executor)
    worker.start()
    try:
        assert automation_active(main, executor)
    finally:
        stopped.set()
        worker.join(timeout=2)
    assert not automation_active(main, executor)


@pytest.mark.parametrize('kind', ['current_task', 'onetime_tasks', 'trigger_tasks'])
def test_current_or_queued_framework_tasks_block_install(kind):
    from types import SimpleNamespace
    main = SimpleNamespace(start_tab=None, grouped_task_tabs=[])
    executor = SimpleNamespace(current_task=None, onetime_tasks=[], trigger_tasks=[])
    assert not automation_active(main, executor)
    setattr(executor, kind, object() if kind == 'current_task' else [SimpleNamespace(enabled=True)])
    assert automation_active(main, executor)
