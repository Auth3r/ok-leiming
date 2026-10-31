"""GitHub Release transport and verified portable payload preparation.

No launcher, developer Git credentials, or token files are required. Private
repositories require an explicitly supplied process-only LEIMING_GITHUB_TOKEN.
"""
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from packaging.version import InvalidVersion, Version

REPOSITORY = 'Auth3r/ok-leiming'
API_ROOT = f'https://api.github.com/repos/{REPOSITORY}'
RELEASES_URL = f'{API_ROOT}/releases'
EXE_NAME = 'Leiming-Automation.exe'
MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_EXPANDED_BYTES = 16 * 1024**3
MAX_JSON_BYTES = 8 * 1024**2
logger = logging.getLogger(__name__)


class UpdateError(RuntimeError):
    """An actionable update error that never prevents normal startup."""


class UpdateAccessError(UpdateError):
    pass


class UpdateNetworkError(UpdateError):
    pass


class UpdateDataError(UpdateError):
    pass


class ReleaseRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urllib.parse.urlsplit(newurl)
        allowed = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
                   'objects.githubusercontent.com'}
        if (destination.scheme != 'https' or destination.netloc not in allowed
                or destination.username or destination.password):
            raise UpdateDataError('更新下载重定向到了不受信任的地址')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def cancelled(exit_event):
    if exit_event is not None and exit_event.is_set():
        raise InterruptedError('更新已取消，原程序未改动')


def release_version(value):
    """Only the project's documented release tags identify distributable builds."""
    if not isinstance(value, str) or not re.fullmatch(
            r'v?\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?', value):
        raise UpdateDataError('发布标签不是有效的应用版本')
    try:
        return Version(value)
    except InvalidVersion as error:
        raise UpdateDataError('发布标签不是有效的应用版本') from error


def sha256_file(path, exit_event=None):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        while block := source.read(1024 * 1024):
            cancelled(exit_event)
            digest.update(block)
    return digest.hexdigest()


def safe_member(name):
    """Reject Windows aliases, drive paths, streams, traversal and device names."""
    path = PurePosixPath(name)
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {
        f'{prefix}{number}' for prefix in ('COM', 'LPT') for number in range(1, 10)}
    if (not name or '\\' in name or path.is_absolute() or not path.parts
            or any(part in ('.', '..') or part.endswith((' ', '.'))
                   or any(c in part for c in ':<>"|?*')
                   or any(ord(c) < 32 for c in part)
                   or part.split('.')[0].upper() in reserved for part in path.parts)):
        raise UpdateDataError('更新 ZIP 含不安全的文件路径')
    return path


class GitHubUpdateClient:
    def __init__(self, token=None, opener=None):
        # A developer's git credential store is intentionally never consulted.
        self._token = os.environ.get('LEIMING_GITHUB_TOKEN', '') if token is None else token
        self._opener = opener or urllib.request.build_opener(ReleaseRedirectHandler())
        self._releases = {}

    def _open(self, url, *, binary=False, timeout=20):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or parsed.netloc != 'api.github.com'
                or not parsed.path.startswith(f'/repos/{REPOSITORY}/')
                or parsed.username or parsed.password):
            raise UpdateDataError('更新源地址不属于本项目 GitHub API')
        request = urllib.request.Request(url, headers={
            'Accept': 'application/octet-stream' if binary else 'application/vnd.github+json',
            'User-Agent': 'Leiming-Automation-Updater', 'X-GitHub-Api-Version': '2022-11-28'})
        if self._token:
            # urllib redirects copy normal headers, but not unredirected headers.
            # Thus signed asset CDN redirects never receive the private token.
            request.add_unredirected_header('Authorization', 'Bearer ' + self._token)
        try:
            return self._opener.open(request, timeout=min(max(timeout, 1), 30))
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 404):
                raise UpdateAccessError(
                    f'GitHub 返回 HTTP {error.code}：仓库可能为私有、访问权限不足或请求受限。'
                    '私有测试需设置仅当前进程使用的 LEIMING_GITHUB_TOKEN（仓库 Contents 只读权限）；'
                    '正常启动不受影响。') from None
            raise UpdateNetworkError(f'GitHub 更新请求失败（HTTP {error.code}）') from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise UpdateNetworkError('无法连接 GitHub 更新源或请求超时，请检查网络后重试') from None

    def _read(self, url, *, limit=MAX_JSON_BYTES, binary=False, timeout=20, exit_event=None):
        cancelled(exit_event)
        try:
            with self._open(url, binary=binary, timeout=timeout) as response:
                data = response.read(limit + 1)
                cancelled(exit_event)
                if len(data) > limit:
                    raise UpdateDataError('更新服务返回的数据过大')
                return data
        except (TimeoutError, socket.timeout, OSError):
            raise UpdateNetworkError('读取更新数据失败或超时，请重试') from None

    def get_version_list(self, number_versions=10, release_only=False, timeout=20, exit_event=None):
        if isinstance(number_versions, bool) or not isinstance(number_versions, int) or number_versions < 1:
            raise ValueError('number_versions must be a positive integer')
        releases = []
        self._releases = {}  # A failed refresh must not leave an installable stale selection.
        for page in range(1, 21):
            url = f'{RELEASES_URL}?per_page=100&page={page}'
            raw = self._read(url, timeout=timeout, exit_event=exit_event)
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                raise UpdateDataError('GitHub 返回了无效的发布数据') from None
            if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                raise UpdateDataError('GitHub 发布列表格式错误')
            releases.extend(data)
            if len(data) < 100:
                break
        else:
            raise UpdateDataError('发布记录过多，未能取得完整版本列表')
        rows = []
        malformed = 0
        for release in releases:
            if release.get('draft') is True:
                continue
            if (release.get('draft') is not False or type(release.get('prerelease')) is not bool
                    or not release.get('published_at')):
                malformed += 1
                continue
            try:
                parsed = release_version(release.get('tag_name'))
            except UpdateDataError:
                malformed += 1
                continue
            if release_only and (release['prerelease'] or parsed.is_prerelease):
                continue
            version = release['tag_name']
            if version in self._releases:
                raise UpdateDataError('GitHub 返回了重复的发布版本')
            self._releases[version] = release
            rows.append({'version': version, 'update_note': [str(release.get('body') or '无更新说明')],
                         'published_at': release['published_at']})
        if not rows and malformed:
            raise UpdateDataError('发布记录中没有有效的应用版本，请联系发布维护者')
        rows.sort(key=lambda row: release_version(row['version']), reverse=True)
        logger.info('GitHub releases checked: repository=%s, published_versions=%s, ignored_invalid=%s',
                    REPOSITORY, [row['version'] for row in rows], malformed)
        return rows[:number_versions]

    def _asset(self, release, name):
        assets = release.get('assets')
        if not isinstance(assets, list):
            raise UpdateDataError('该版本的更新资产列表无效')
        matches = [item for item in assets if isinstance(item, dict) and item.get('name') == name
                   and item.get('state') == 'uploaded']
        if len(matches) != 1:
            raise UpdateDataError(f'该版本缺少唯一的已上传更新资产：{name}')
        asset = matches[0]
        if not isinstance(asset.get('id'), int) or isinstance(asset['id'], bool) or asset['id'] <= 0:
            raise UpdateDataError('更新资产编号无效')
        return asset

    @staticmethod
    def _asset_url(asset):
        # Construct from the fixed repository, never trust supplied remote URLs.
        return f'{API_ROOT}/releases/assets/{asset["id"]}'

    def get_tag_commit(self, version, exit_event=None):
        """Resolve lightweight or annotated tags through the fixed repository API."""
        release_version(version)

        def read_object(url):
            try:
                data = json.loads(self._read(url, exit_event=exit_event))
            except (ValueError, UnicodeError):
                raise UpdateDataError('GitHub 发布标签数据无效') from None
            if not isinstance(data, dict):
                raise UpdateDataError('GitHub 发布标签数据格式错误')
            return data

        ref = read_object(f'{API_ROOT}/git/ref/tags/{urllib.parse.quote(version, safe="")}')
        if ref.get('ref') != f'refs/tags/{version}':
            raise UpdateDataError('GitHub 返回的标签与所选版本不一致')
        obj = ref.get('object')
        seen = set()
        for _ in range(6):
            if (not isinstance(obj, dict) or not isinstance(obj.get('sha'), str)
                    or not re.fullmatch('[0-9a-f]{40}', obj['sha'])
                    or obj.get('type') not in ('tag', 'commit')):
                raise UpdateDataError('发布标签没有指向有效的源码提交')
            if obj['type'] == 'commit':
                return obj['sha']
            if obj['sha'] in seen:
                raise UpdateDataError('发布标签包含循环引用')
            seen.add(obj['sha'])
            tag = read_object(f'{API_ROOT}/git/tags/{obj["sha"]}')
            if tag.get('sha') != obj['sha']:
                raise UpdateDataError('GitHub 返回的标签对象摘要不一致')
            obj = tag.get('object')
        raise UpdateDataError('发布标签引用层级过多，无法确认源码提交')

    def prepare_update(self, version, current_version, staging_root, exit_event=None, timeout=600):
        if release_version(version) <= Version(current_version):
            raise UpdateDataError('所选版本不高于当前版本；不会降级或覆盖当前程序')
        # Recheck publication status at click time (a deleted/replaced draft cannot be installed).
        self.get_version_list(number_versions=2000, release_only=False, exit_event=exit_event)
        release = self._releases.get(version)
        if release is None:
            raise UpdateDataError('所选版本已撤回或尚未正式发布')
        name = f'Leiming-Automation-{version}-win-x64.zip'
        asset = self._asset(release, name)
        checksum_asset = self._asset(release, name + '.sha256')
        checksum = self._read(self._asset_url(checksum_asset), binary=True, limit=4096,
                              exit_event=exit_event).decode('utf-8-sig').strip()
        checksum_match = re.fullmatch(r'([0-9a-fA-F]{64})[ \t]+\*?' + re.escape(name), checksum)
        if not checksum_match:
            raise UpdateDataError('更新 SHA-256 文件内容或资产文件名无效')
        expected = checksum_match[1].lower()
        if asset.get('digest') and asset['digest'] != 'sha256:' + expected:
            raise UpdateDataError('GitHub 资产摘要与校验文件不一致')
        size = asset.get('size')
        if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
            raise UpdateDataError('更新 ZIP 大小无效')
        stage = Path(staging_root).resolve() / ('download-' + uuid.uuid4().hex)
        stage.mkdir(parents=True, exist_ok=False)
        archive = stage / name
        deadline = time.monotonic() + timeout
        try:
            digest = hashlib.sha256()
            received = 0
            with self._open(self._asset_url(asset), binary=True) as response, archive.open('xb') as output:
                while True:
                    cancelled(exit_event)
                    if time.monotonic() > deadline:
                        raise UpdateNetworkError('下载更新超时，原程序未改动')
                    try:
                        block = response.read(1024 * 1024)
                    except (TimeoutError, socket.timeout, OSError):
                        raise UpdateNetworkError('更新下载中断，请重试') from None
                    if not block:
                        break
                    received += len(block)
                    if received > size:
                        raise UpdateDataError('更新下载大小超过发布记录')
                    digest.update(block)
                    output.write(block)
            if received != size or digest.hexdigest() != expected:
                raise UpdateDataError('更新 ZIP 大小或 SHA-256 校验失败；原程序未改动')
            result = extract_verified_archive(archive, stage / 'unpacked', version, exit_event)
            manifest = json.loads((result / 'release-manifest.json').read_text(encoding='utf-8'))
            # A plausible SHA in a package is not provenance. Bind it to the
            # actual repository tag after download, including annotated tags.
            if manifest['git_commit'] != self.get_tag_commit(version, exit_event):
                raise UpdateDataError('更新包源码提交与 GitHub 发布标签不一致；原程序未改动')
            logger.info('Update payload verified: version=%s, zip_sha256=%s, bytes=%s', version, expected, received)
            return result
        except Exception:
            # Only our UUID-owned new download folder is removable, never an installation.
            shutil.rmtree(stage, ignore_errors=True)
            raise


def extract_verified_archive(archive, destination, version, exit_event=None):
    """Validate every path and runtime hash before any existing installation is touched."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive) as package:
            members = package.infolist()
            if len(members) > 100000 or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
                raise UpdateDataError('更新 ZIP 解压后过大')
            seen = set()
            for item in members:
                cancelled(exit_event)
                relative = safe_member(item.filename.rstrip('/'))
                if relative.parts[0] != 'Leiming-Automation':
                    raise UpdateDataError('更新 ZIP 根目录无效')
                folded = str(relative).casefold()
                if folded in seen or stat.S_ISLNK(item.external_attr >> 16):
                    raise UpdateDataError('更新 ZIP 含重复路径或符号链接')
                seen.add(folded)
                if item.flag_bits & 1:
                    raise UpdateDataError('更新 ZIP 不得加密')
            for item in members:
                cancelled(exit_event)
                package.extract(item, destination)
        root = destination / 'Leiming-Automation'
        validate_package(root, version, exit_event)
        return root
    except (zipfile.BadZipFile, KeyError, OSError, ValueError, TypeError) as error:
        raise UpdateDataError('更新 ZIP 或构建清单损坏') from error


def validate_package(root, expected_version, exit_event=None):
    """Revalidate a staged directory immediately before the installer touches it."""
    root = Path(root).resolve()
    version = expected_version
    manifest = json.loads((root / 'release-manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict):
        raise UpdateDataError('更新构建清单必须是对象')
    commit = manifest.get('git_commit', '')
    if (type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1
            or manifest.get('repository') != REPOSITORY
            or manifest.get('platform') != 'win-x64' or manifest.get('release_tag') != version
            or manifest.get('app_version') != version or manifest.get('git_dirty') is not False or manifest.get('trial') is not False
            or not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit)):
        raise UpdateDataError('更新包的版本、源码提交或构建清单不匹配')
    hashes = manifest.get('runtime_files_sha256')
    if not isinstance(hashes, dict) or EXE_NAME not in hashes or not hashes:
        raise UpdateDataError('更新包缺少可验证的运行文件清单')
    for path in (root, *root.rglob('*')):
        if path.is_symlink() or path.is_junction():
            raise UpdateDataError('更新包不得包含符号链接或目录联接')
    for name, expected in hashes.items():
        relative = safe_member(name)
        path = root.joinpath(*relative.parts)
        if (not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)
                or not path.is_file() or sha256_file(path, exit_event) != expected):
            raise UpdateDataError('更新包运行文件校验失败')
    # Data must be carried forward from the old install, never supplied by a release.
    forbidden = {'settings.json', 'config_version.json', 'configs', 'logs', 'cache', 'output',
                 'config-backups', 'reports', 'screenshots', 'portable-data-origin.json'}
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.split('/')[0].casefold() in forbidden:
            raise UpdateDataError('更新包含个人配置或运行数据，不允许安装')
        if relative != 'release-manifest.json' and relative not in hashes:
            raise UpdateDataError('更新包含构建清单未登记的文件')
    return manifest
