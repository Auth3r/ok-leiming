"""Release artifacts must carry one verifiable, unused version identity."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from tools import build_release, verify_release


def test_python_metadata_follows_the_only_source_version(tmp_path):
    shutil.copy2(build_release.ROOT/'pyproject.toml', tmp_path/'pyproject.toml')
    (tmp_path/'src').mkdir()
    (tmp_path/'src/__init__.py').touch()
    (tmp_path/'src/version.py').write_text("APP_VERSION = 'v9.8.7-rc.2'\n", encoding='utf-8')
    output = tmp_path/'metadata'
    output.mkdir()
    subprocess.run([sys.executable, '-c',
                    'import setuptools.build_meta as b; b.prepare_metadata_for_build_wheel("metadata")'],
                   cwd=tmp_path, check=True, capture_output=True, text=True)
    metadata = next(output.glob('*.dist-info/METADATA')).read_text(encoding='utf-8')
    assert '\nVersion: 9.8.7rc2\n' in metadata


def test_second_version_source_is_rejected(tmp_path):
    original = (build_release.ROOT/'pyproject.toml').read_text(encoding='utf-8')
    (tmp_path/'pyproject.toml').write_text(original.replace('dynamic = ["version"]', 'version = "1.0.0b6"'),
                                         encoding='utf-8')
    with pytest.raises(ValueError, match='derive its version'):
        build_release.validate_version_source(tmp_path)
    (tmp_path/'pyproject.toml').write_text(original, encoding='utf-8')
    build_release.validate_version_source(tmp_path)


def test_windows_file_version_orders_beta_rc_and_final():
    versions = ['v1.0.0-alpha.9', 'v1.0.0-beta.6', 'v1.0.0-beta.10', 'v1.0.0-rc.1', 'v1.0.0']
    numeric = [build_release.windows_file_version(version) for version in versions]
    assert all(left < right for left, right in zip(numeric, numeric[1:]))


@pytest.mark.parametrize('version', ['v1.0.0-beta.6-rc.20261023', 'v1.0.0-beta.10000', 'v65536.0.0', 'broken'])
def test_invalid_or_ambiguous_release_identity_is_rejected(version):
    with pytest.raises(ValueError):
        build_release.windows_file_version(version)


def test_existing_local_and_remote_tag_cannot_be_reused(tmp_path):
    remote = tmp_path/'remote.git'
    checkout = tmp_path/'checkout'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    subprocess.run(['git', 'init', str(checkout)], check=True, capture_output=True)

    def git(*args):
        return subprocess.run(['git', '-C', str(checkout), *args], check=True, capture_output=True)

    git('remote', 'add', 'origin', str(remote))
    git('-c', 'user.name=Release identity test', '-c', 'user.email=test@example.invalid',
        'commit', '--allow-empty', '-m', 'Test source identity')
    build_release.require_unused_release_tag('v9.8.7-beta.1', checkout)
    git('tag', 'v9.8.7-beta.1')
    with pytest.raises(RuntimeError, match='already exists'):
        build_release.require_unused_release_tag('v9.8.7-beta.1', checkout)
    git('push', 'origin', 'refs/tags/v9.8.7-beta.1')
    git('tag', '-d', 'v9.8.7-beta.1')
    with pytest.raises(RuntimeError, match='Remote release tag'):
        build_release.require_unused_release_tag('v9.8.7-beta.1', checkout)


def make_package(tmp_path, *, altered_bytes=False, missing_hash=False, trial=False):
    payload = {'Leiming-Automation.exe': b'executable bytes', 'licenses/notice.txt': b'license bytes'}
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in payload.items()}
    if missing_hash:
        del hashes['licenses/notice.txt']
    if altered_bytes:
        payload['Leiming-Automation.exe'] = b'different executable'
    stamp = {'schema_version': 1, 'repository': 'Auth3r/ok-leiming', 'platform': 'win-x64',
             'app_version': 'v9.8.7-beta.1', 'release_tag': 'v9.8.7-beta.1',
             'git_commit': '1'*40, 'git_dirty': False, 'trial': trial,
             'exe_file_version': [9, 8, 7, 20001], 'runtime_files_sha256': hashes}
    archive_path = tmp_path/'Leiming-Automation-v9.8.7-beta.1-win-x64.zip'
    with zipfile.ZipFile(archive_path, 'w') as archive:
        for name, data in payload.items():
            archive.writestr('Leiming-Automation/'+name, data)
        archive.writestr('Leiming-Automation/release-manifest.json', json.dumps(stamp))
    build_release.write_release_assets(archive_path, stamp)
    return archive_path


def test_release_manifest_and_external_assets_verify_actual_bytes(tmp_path):
    path = make_package(tmp_path)
    with zipfile.ZipFile(path) as archive:
        manifest = verify_release.verify_release_identity(archive, path)
    assert manifest['app_version'] == 'v9.8.7-beta.1'


@pytest.mark.parametrize(('kwargs', 'error'), [
    ({'altered_bytes': True}, 'Runtime file differs'),
    ({'missing_hash': True}, 'every package file'),
    ({'trial': True}, 'Trial build'),
])
def test_modified_incomplete_and_trial_packages_are_rejected(tmp_path, kwargs, error):
    path = make_package(tmp_path, **kwargs)
    with zipfile.ZipFile(path) as archive, pytest.raises(AssertionError, match=error):
        verify_release.verify_release_identity(archive, path)


def test_replaced_checksum_or_published_manifest_is_rejected(tmp_path):
    path = make_package(tmp_path)
    checksum_path = path.with_suffix('.zip.sha256')
    original = checksum_path.read_text(encoding='ascii')
    checksum_path.write_text('0'*64+'  '+path.name, encoding='ascii')
    with zipfile.ZipFile(path) as archive, pytest.raises(AssertionError, match='sidecar differs'):
        verify_release.verify_release_identity(archive, path)
    checksum_path.write_text(original, encoding='ascii')
    published_path = path.parent/'release-manifest.json'
    published = json.loads(published_path.read_text(encoding='utf-8'))
    published['git_commit'] = '2'*40
    published_path.write_text(json.dumps(published), encoding='utf-8')
    with zipfile.ZipFile(path) as archive, pytest.raises(AssertionError, match='Published manifest differs'):
        verify_release.verify_release_identity(archive, path)


def test_partial_previous_release_outputs_are_never_overwritten(tmp_path):
    (tmp_path/'release-manifest.json').write_text('previous release', encoding='utf-8')
    with pytest.raises(FileExistsError):
        build_release.require_new_destination(tmp_path, 'v9.8.7-beta.1', False)
    assert (tmp_path/'release-manifest.json').read_text(encoding='utf-8') == 'previous release'
