"""Build a clean, traceable Windows portable ZIP; never copy personal data."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.version import APP_NAME, APP_VERSION
REPOSITORY = 'Auth3r/ok-leiming'
MANAGED_ROOTS = ['_internal', 'templates', 'icons', 'i18n', 'licenses']
LICENSE_ASSETS = ('Apache-2.0.txt', 'GPL-3.0.txt', 'LGPL-3.0.txt',
                  'Mesa-11.2.2-COPYING.txt', 'MIT-standard.txt', 'PyDirectInput-MIT.txt',
                  'PyInstaller-6.22.3-COPYING.txt', 'Qt-6.11.1-THIRD-PARTY-LICENSES.zip',
                  'Microsoft-Visual-C-Runtime-2015-2022-License.txt',
                  'Microsoft-Visual-Studio-2022-Community-License.txt',
                  'LLVM-3.6.2-LICENSE.txt', 'LLVM-3.6.2-ADDITIONAL-NOTICES.zip',
                  'Mesa-11.2.2-LICENSE-NOTICE.txt',
                  'Qt-6.11.1-THIRD-PARTY-NOTICES.md', 'THIRD-PARTY-NOTICES.md',
                  'LICENSE-PROVENANCE.json')


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args], text=True, encoding='utf-8').strip()


def windows_file_version(version):
    """Give Beta, RC and final builds distinct, increasing Windows versions."""
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.[1-9]\d*)?', version):
        raise ValueError('Use vMAJOR.MINOR.PATCH[-alpha.N|-beta.N|-rc.N] in src/version.py')
    from packaging.version import Version
    parsed = Version(version)
    if any(part > 65535 for part in parsed.release) or (parsed.pre and parsed.pre[1] >= 10000):
        raise ValueError('Application version exceeds the Windows version resource limits')
    stage = {'a': 10000, 'b': 20000, 'rc': 30000}
    revision = stage[parsed.pre[0]] + parsed.pre[1] if parsed.pre else 65535
    return (*parsed.release, revision)


def validate_version_source(root=ROOT):
    """A second hard-coded project version would make release identity drift."""
    project = tomllib.loads((root/'pyproject.toml').read_text(encoding='utf-8'))
    if ('version' in project['project'] or 'version' not in project['project'].get('dynamic', [])
            or project.get('tool', {}).get('setuptools', {}).get('dynamic', {}).get('version')
            != {'attr': 'src.version.APP_VERSION'}):
        raise ValueError('pyproject.toml must derive its version from src.version.APP_VERSION')
    return windows_file_version(APP_VERSION)


def require_unused_release_tag(version, root=ROOT):
    """Never create a different binary under an already used release identity."""
    local = subprocess.run(['git', '-C', str(root), 'show-ref', '--verify', '--quiet',
                            'refs/tags/' + version], check=False)
    if local.returncode == 0:
        raise RuntimeError(f'Release tag {version} already exists; choose a new application version')
    if local.returncode != 1:
        raise RuntimeError('Could not check local release tags')
    remote = subprocess.run(['git', '-C', str(root), 'ls-remote', '--exit-code', '--tags',
                             'origin', 'refs/tags/' + version], capture_output=True, text=True,
                            encoding='utf-8', timeout=30, check=False)
    if remote.returncode == 0:
        raise RuntimeError(f'Remote release tag {version} already exists; choose a new application version')
    if remote.returncode != 2:
        raise RuntimeError('Could not verify unused remote release tag: ' + remote.stderr.strip())


def validate_dependencies(root=ROOT):
    from packaging.requirements import Requirement
    versions = {}
    for line in (root/'requirements.txt').read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        requirement = Requirement(line)
        actual = metadata.version(requirement.name)
        if actual not in requirement.specifier:
            raise RuntimeError(f'Dependency differs from requirements.txt: {requirement.name}=={actual}')
        versions[requirement.name] = actual
    subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True)
    return versions


def zip_tree(directory, target, prefix=''):
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(directory.rglob('*')):
            if path.is_file():
                archive.write(path, prefix + path.relative_to(directory).as_posix())


def copy_tracked_tree(folder, target):
    """Ship only repository-owned assets; untracked local files are not releases."""
    names = git('ls-files', '-z', '--', folder).split('\0')
    for name in filter(None, names):
        relative = Path(name).relative_to(folder)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)


def require_new_destination(destination, version, dirty):
    target = destination / 'Leiming-Automation'
    archive = destination / f'Leiming-Automation-{version}-win-x64{"-TRIAL" if dirty else ""}.zip'
    if (target.exists() or archive.exists() or archive.with_suffix('.zip.sha256').exists()
            or (destination/'release-manifest.json').exists()):
        raise FileExistsError('Build destination already contains a portable install or ZIP. '
                              'Choose a new --output-dir to preserve it: ' + str(destination))
    return target, archive


def write_release_assets(archive, stamp):
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    published = {**stamp, 'asset_name': archive.name, 'asset_bytes': archive.stat().st_size,
                 'asset_sha256': digest}
    (archive.parent/'release-manifest.json').write_text(
        json.dumps(published, ensure_ascii=False, indent=2), encoding='utf-8')
    return published


def runtime_hashes(target):
    return {path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(target.rglob('*')) if path.is_file()}


def licenses(target):
    directory = target/'licenses'
    directory.mkdir(exist_ok=True)
    records = []
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        name = dist.metadata['Name']
        if name.lower() in ('pip', 'pytest', 'pluggy', 'iniconfig', 'pygments', 'colorama',
                            'setuptools', 'altgraph', 'pefile', 'pyinstaller',
                            'pyinstaller-hooks-contrib', 'pywin32-ctypes'):
            continue
        folder = directory / (name + '-' + dist.version)
        folder.mkdir(exist_ok=True)
        (folder/'METADATA.txt').write_text(dist.read_text('METADATA') or '', encoding='utf-8')
        for file in dist.files or []:
            relative = Path(str(file))
            if '__pycache__' in relative.parts or relative.suffix.lower() in ('.py', '.pyc', '.pyo', '.pyd', '.dll'):
                continue  # SPDX resolver code/caches are not license notices.
            if any('license' in part.lower() or 'copying' in part.lower() or
                   'third-party' in part.lower() for part in relative.parts):
                original = Path(dist.locate_file(file))
                if original.is_file():
                    safe = Path(*[p for p in relative.parts if p not in ('.', '..')])
                    output = folder / safe
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original, output)
        records.append({'name': name, 'version': dist.version,
                        'license': dist.metadata.get('License-Expression') or dist.metadata.get('License')
                        or [value for value in dist.metadata.get_all('Classifier', []) if value.startswith('License ::')],
                        'source': f'https://pypi.org/project/{name}/{dist.version}/'})
    for name in LICENSE_ASSETS:
        file = ROOT/'tools'/'release-licenses'/name
        shutil.copy2(file, directory/file.name)
    shutil.copy2(ROOT/'docs'/'OK_SCRIPT_LICENSE.txt', directory/'AGPL-3.0.txt')
    python_license = Path(sys.base_prefix)/'LICENSE.txt'
    if python_license.exists(): shutil.copy2(python_license, directory/'Python-LICENSE.txt')
    (directory/'dependency-manifest.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--allow-dirty', action='store_true', help='Local trial only; cannot be published')
    parser.add_argument('--output-dir', type=Path, help='Separate build destination; preserves an installed portable folder')
    parser.add_argument('--work-dir', type=Path, help='Separate intermediate build directory; preserves previous build evidence')
    args = parser.parse_args()
    if (ROOT/'RELEASE_HOLD').exists():
        raise RuntimeError('P0 发布冻结：肉鸽实机验收尚未通过；禁止打包（包括 dirty 试构建）。参见 RELEASE_HOLD。')
    if os.name != 'nt' or sys.maxsize <= 2**32 or sys.version_info[:2] != (3, 12):
        raise RuntimeError('Build requires Windows x64 / Python 3.12')
    dirty = bool(git('status', '--porcelain', '--untracked-files=normal'))
    if dirty and not args.allow_dirty:
        raise RuntimeError('Commit all release changes before production build')
    numeric_version = validate_version_source()
    dependency_versions = validate_dependencies()
    if not args.allow_dirty:
        require_unused_release_tag(APP_VERSION)
    # Even a clean source rebuild of an existing tag is a TRIAL, never an
    # alternative archive eligible for upload under that published identity.
    trial = dirty or args.allow_dirty
    destination = (args.output_dir or ROOT/'dist').resolve()
    target, archive = require_new_destination(destination, APP_VERSION, trial)
    destination.mkdir(parents=True, exist_ok=True)
    from packaging.version import Version
    stamp = {'schema_version': 1, 'repository': REPOSITORY, 'platform': 'win-x64',
             'app_version': APP_VERSION, 'release_tag': APP_VERSION, 'trial': trial,
             'pep440_version': str(Version(APP_VERSION)), 'exe_file_version': list(numeric_version),
             'managed_roots': MANAGED_ROOTS, 'dependency_versions': dependency_versions,
             'git_commit': git('rev-parse', 'HEAD'),
             'git_branch': git('branch', '--show-current'), 'git_dirty': dirty,
             'python': sys.version, 'pyinstaller': metadata.version('pyinstaller')}
    work = (args.work_dir or ROOT/'output'/'release-build').resolve()
    work.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    Image.open(ROOT/'icons'/'icon.png').save(work/'icon.ico', sizes=[(16,16),(32,32),(48,48),(256,256)])
    version = APP_VERSION.removeprefix('v')
    flags = 2 if Version(version).is_prerelease else 0
    info = f"""VSVersionInfo(ffi=FixedFileInfo(filevers={numeric_version!r}, prodvers={numeric_version!r}, mask=0x3f, flags={flags}, OS=0x40004, fileType=1, subtype=0, date=(0,0)), kids=[StringFileInfo([StringTable('040904B0', [StringStruct('CompanyName', 'Auth3r'), StringStruct('FileDescription', '{APP_NAME}'), StringStruct('FileVersion', '{version}'), StringStruct('ProductName', '{APP_NAME}'), StringStruct('ProductVersion', '{version}'), StringStruct('OriginalFilename', 'Leiming-Automation.exe')])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])"""
    (work/'version-info.txt').write_text(info, encoding='utf-8')
    build_environment = dict(os.environ)
    build_environment['LEIMING_BUILD_WORK_DIR'] = str(work)
    build_environment['LEIMING_TRACKED_SOURCE_FILES'] = json.dumps(
        list(filter(None, git('ls-files', '-z', '--', 'src').split('\0'))))
    build_environment['PATH'] = os.pathsep.join((str(Path(sys.executable).parent),
                                                str(Path(sys.base_prefix)),
                                                str(Path(os.environ['SystemRoot'])/'System32'),
                                                os.environ['SystemRoot']))
    for key in tuple(build_environment):
        if key.upper() in ('PYTHONHOME', 'PYTHONPATH'):
            build_environment.pop(key)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                    '--distpath', str(destination), '--workpath', str(work/'pyinstaller'),
                    str(ROOT/'tools'/'Leiming-Automation.spec')], cwd=ROOT, env=build_environment, check=True)
    for folder in ('templates', 'icons', 'i18n'):
        copy_tracked_tree(folder, target/folder)
    shutil.copy2(ROOT/'docs'/'使用说明.md', target/'使用说明.md')
    shutil.copy2(ROOT/'docs'/'releases'/f'{APP_VERSION}.md', target/'更新公告.md')
    shutil.copy2(ROOT/'LICENSE', target/'LICENSE')
    (target/'PROJECT-NOTICES.txt').write_text(
        '镭闪自动化的 MIT License 仅适用于 Auth3r 有权授权的原创部分。\n'
        '第三方框架、运行库、图标、翻译及游戏图像不被改授 MIT。\n'
        '第三方原始许可、通知和对应源码义务见 licenses/THIRD-PARTY-NOTICES.md。\n'
        '完整项目权利范围见 licenses/corresponding-source.zip 内 docs/THIRD_PARTY_NOTICES.md。\n'
        '第三方材料保留原始权利；许可证适用范围见上述声明。\n', encoding='utf-8')
    licenses(target)
    # Corresponding first-party source and copyleft dependency source travel with
    # the binary. Exclude development evidence, tests, credentials and user files.
    source = work/'corresponding-source'
    if source.exists():
        if source.resolve().parent != work.resolve(): raise RuntimeError('Unsafe source staging path')
        shutil.rmtree(source)
    source.mkdir()
    for folder in ('src', 'templates', 'icons', 'i18n'):
        copy_tracked_tree(folder, source/folder)
    for file in ('main.py', 'main_debug.py', '.gitignore', 'pyproject.toml', 'requirements.txt',
                 'settings.example.json', 'README.md', 'LICENSE', 'AGENTS.md',
                 'Setup.cmd', 'Setup.ps1', 'Start.cmd', 'Start.ps1', 'Debug.cmd'):
        shutil.copy2(ROOT/file, source/file)
    (source/'tools').mkdir()
    for file in ('build_release.py', 'verify_release.py', 'requirements-build.txt', 'Leiming-Automation.spec', 'windows.manifest',
                 'validate_module_navigation.py', 'season_live_test.py', 'roguelike_gui_acceptance.py',
                 'daily_gui_acceptance.py'):
        shutil.copy2(ROOT/'tools'/file, source/'tools'/file)
    (source/'tools'/'release-licenses').mkdir()
    for name in LICENSE_ASSETS:
        shutil.copy2(ROOT/'tools'/'release-licenses'/name, source/'tools'/'release-licenses'/name)
    (source/'docs'/'releases').mkdir(parents=True)
    for file in ('使用说明.md', 'OK_SCRIPT_LICENSE.txt', 'THIRD_PARTY_NOTICES.md', 'UPSTREAM.md', 'UPSTREAM_README.md', 'RELEASING.md', 'PUBLICATION_CHECK.md'):
        shutil.copy2(ROOT/'docs'/file, source/'docs'/file)
    shutil.copy2(ROOT/'docs'/'releases'/f'{APP_VERSION}.md', source/'docs'/'releases'/f'{APP_VERSION}.md')
    # This nested copy identifies source provenance only. A source archive
    # cannot contain its own digest; the outer manifest hashes the final ZIP.
    (source/'release-manifest.json').write_text(
        json.dumps({**stamp, 'manifest_scope': 'source-provenance'}, ensure_ascii=False, indent=2),
        encoding='utf-8')
    if not dirty:
        # Preserve canonical Git bytes, including line endings, for source proof.
        for path in source.rglob('*'):
            if path.is_file() and path.name != 'release-manifest.json':
                name = path.relative_to(source).as_posix()
                path.write_bytes(subprocess.check_output(['git', '-C', str(ROOT), 'show',
                                                         stamp['git_commit'] + ':' + name]))
    for package in ('ok', 'qfluentwidgets', 'qframelesswindow', 'pyappify', 'pynput'):
        import importlib.util
        package_root = Path(importlib.util.find_spec(package).origin).parent
        shutil.copytree(package_root, source/'third-party-source'/package,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyd', '*.dll'))
    (source/'BUILDING.txt').write_text(
        '对应源码不含仓库历史或个人配置。重建需要 Windows x64、Python 3.12 和 Git。\n'
        '在此目录 git init，git add .，git commit -m "local source rebuild"。\n'
        'python -m venv .venv\n'
        '.venv\\Scripts\\python.exe -m pip install --no-deps -r requirements.txt\n'
        '.venv\\Scripts\\python.exe -m pip install -r tools/requirements-build.txt\n'
        '.venv\\Scripts\\python.exe tools/build_release.py --allow-dirty\n'
        '重建包标记 TRIAL，不可覆盖原发布资产或复用原标签。\n'
        '此重建记录本地提交 SHA；原发布 SHA 见 release-manifest.json。\n'
        '第三方原始版本、获取地址及许可证见二进制包 licenses；copyleft 框架源码位于 third-party-source。\n',
        encoding='utf-8')
    zip_tree(source, target/'licenses'/'corresponding-source.zip')
    stamp['runtime_files_sha256'] = runtime_hashes(target)
    (target/'release-manifest.json').write_text(json.dumps(stamp, ensure_ascii=False, indent=2), encoding='utf-8')
    zip_tree(target, archive, 'Leiming-Automation/')
    if not dirty and (git('rev-parse', 'HEAD') != stamp['git_commit'] or
                      git('status', '--porcelain', '--untracked-files=normal')):
        raise RuntimeError('Source changed during build; do not publish this archive')
    published = write_release_assets(archive, stamp)
    print(json.dumps({'zip': str(archive), **published}, ensure_ascii=False))


if __name__ == '__main__':
    main()
