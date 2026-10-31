"""Inspect actual ZIP bytes, nested corresponding source and provenance."""
import argparse
import ast
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.build_release import windows_file_version
FORBIDDEN = {'.git', '.venv', 'venv', 'config-backups', 'screenshots', 'acceptance',
             'evidence', 'reports', 'cache', 'logs', '.pytest_cache', '__pycache__'}
SECRETS = re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|'
                     rb'AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|'
                     rb'sk-proj-[A-Za-z0-9_-]{30,})')


def inspect(archive):
    names = archive.namelist()
    assert len({name.casefold() for name in names}) == len(names), 'Duplicate ZIP paths'
    for item in archive.infolist():
        path = PurePosixPath(item.filename)
        assert not path.is_absolute() and '..' not in path.parts, item.filename
        assert not (set(path.parts) & FORBIDDEN), item.filename
        assert path.name not in ('settings.json', 'config_version.json', '.env', 'ui_config.json'), item.filename
        if path.suffix.lower() in ('.json', '.txt', '.md', '.py', '.toml', '.ps1', '.cmd', '.yml', '.yaml',
                                   '.pem', '.ini', '.cfg', '.csv'):
            assert not SECRETS.search(archive.read(item)), 'Secret pattern in ' + item.filename
    assert archive.testzip() is None, 'ZIP CRC failure'
    return names


def verify_release_identity(archive, archive_path):
    prefix = 'Leiming-Automation/'
    manifest = json.loads(archive.read(prefix+'release-manifest.json'))
    assert manifest.get('schema_version') == 1, 'Unsupported manifest schema'
    assert manifest.get('repository') == 'Auth3r/ok-leiming', 'Wrong release repository'
    assert manifest.get('platform') == 'win-x64', 'Wrong release platform'
    assert manifest['git_dirty'] is False and manifest.get('trial') is False, 'Trial build is not a release'
    assert re.fullmatch('[0-9a-f]{40}', manifest['git_commit']), 'Invalid source commit'
    version = manifest['app_version']
    assert manifest['release_tag'] == version, 'Release tag differs from application version'
    assert manifest['exe_file_version'] == list(windows_file_version(version)), 'Windows version differs'
    assert archive_path.name == f'Leiming-Automation-{version}-win-x64.zip', 'Archive name differs from version'
    with archive_path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    checksum = archive_path.with_suffix('.zip.sha256').read_text(encoding='ascii').strip()
    assert checksum == f'{digest}  {archive_path.name}', 'ZIP SHA-256 sidecar differs'
    published = json.loads((archive_path.parent/'release-manifest.json').read_text(encoding='utf-8'))
    assert all(published.get(key) == value for key, value in manifest.items()), 'Published manifest differs'
    assert (published.get('asset_name') == archive_path.name and published.get('asset_sha256') == digest
            and published.get('asset_bytes') == archive_path.stat().st_size), 'Published asset identity differs'
    hashes = manifest['runtime_files_sha256']
    payload = {name[len(prefix):] for name in archive.namelist()
               if name.startswith(prefix) and not name.endswith('/')
               and name != prefix+'release-manifest.json'}
    assert set(hashes) == payload, 'Manifest does not cover exactly every package file'
    for name, expected_hash in hashes.items():
        assert hashlib.sha256(archive.read(prefix+name)).hexdigest() == expected_hash, \
            'Runtime file differs from build manifest: ' + name
    return manifest


def verify_executable_version(data, manifest):
    import pefile
    pe = pefile.PE(data=data, fast_load=True)
    try:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']])
        fixed = pe.VS_FIXEDFILEINFO[0]
        actual = [fixed.FileVersionMS >> 16, fixed.FileVersionMS & 0xffff,
                  fixed.FileVersionLS >> 16, fixed.FileVersionLS & 0xffff]
        assert actual == manifest['exe_file_version'], 'EXE numeric version differs from manifest'
        strings = {}
        for section in pe.FileInfo:
            for item in section:
                for table in getattr(item, 'StringTable', []):
                    strings.update(table.entries)
        version = manifest['app_version'].removeprefix('v').encode('utf-8')
        assert strings.get(b'ProductVersion') == version, 'EXE ProductVersion differs from manifest'
        assert strings.get(b'FileVersion') == version, 'EXE FileVersion differs from manifest'
    finally:
        pe.close()


def read_source_version(data):
    for node in ast.parse(data).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'APP_VERSION'
                                                for target in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('Corresponding source has no APP_VERSION')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('zip', type=Path)
    args = parser.parse_args()
    prefix = 'Leiming-Automation/'
    with zipfile.ZipFile(args.zip) as archive:
        names = inspect(archive)
        required = ['Leiming-Automation.exe', 'release-manifest.json', '使用说明.md', '更新公告.md',
                    'LICENSE', 'PROJECT-NOTICES.txt',
                    'icons/icon.png', 'templates/manifest.json', 'templates/coco_annotations.json',
                    '_internal/python312.dll', '_internal/onnxocr/models/ppocrv5/det/det.onnx',
                    '_internal/onnxocr/models/ppocrv5/rec/rec.onnx',
                    '_internal/onnxocr/models/ppocrv5/cls/cls.onnx',
                    '_internal/onnxocr/models/ppocrv5/ppocrv5_dict.txt',
                    'licenses/AGPL-3.0.txt', 'licenses/GPL-3.0.txt', 'licenses/LGPL-3.0.txt',
                    'licenses/THIRD-PARTY-NOTICES.md', 'licenses/corresponding-source.zip']
        assert all(prefix + name in names for name in required), 'Missing required runtime/notice'
        manifest = verify_release_identity(archive, args.zip)
        hashes = manifest['runtime_files_sha256']
        assert 'Leiming-Automation.exe' in hashes, 'Missing executable integrity record'
        verify_executable_version(archive.read(prefix+'Leiming-Automation.exe'), manifest)
        sha = manifest['git_commit']
        project_license = subprocess.check_output(['git', '-C', str(ROOT), 'show', f'{sha}:LICENSE'])
        assert archive.read(prefix+'LICENSE').splitlines() == project_license.splitlines(), \
            'Project license differs from Git commit'
        with zipfile.ZipFile(io.BytesIO(archive.read(prefix+'licenses/corresponding-source.zip'))) as source:
            source_names = inspect(source)
            assert read_source_version(source.read('src/version.py')) == manifest['app_version'], \
                'Corresponding source version differs from executable version'
            source_manifest = json.loads(source.read('release-manifest.json'))
            assert source_manifest['git_commit'] == sha and source_manifest['app_version'] == manifest['app_version'], \
                'Corresponding source provenance differs from package'
            verified = []
            for name in source_names:
                if name.startswith(('src/', 'templates/', 'icons/', 'i18n/', 'tools/')) or name in (
                        'main.py', 'main_debug.py', '.gitignore', 'pyproject.toml', 'requirements.txt',
                        'settings.example.json', 'README.md', 'LICENSE', 'AGENTS.md',
                        'Setup.cmd', 'Setup.ps1', 'Start.cmd', 'Start.ps1', 'Debug.cmd',
                        'docs/THIRD_PARTY_NOTICES.md', 'docs/UPSTREAM.md', 'docs/UPSTREAM_README.md',
                        'docs/RELEASING.md', 'docs/PUBLICATION_CHECK.md', 'docs/使用说明.md', f"docs/releases/{manifest['app_version']}.md"):
                    # All first-party shipped source/resources must be exact commit bytes.
                    expected = subprocess.check_output(['git', '-C', str(ROOT), 'show', f'{sha}:{name}'])
                    assert source.read(name) == expected, 'Source differs from Git commit: ' + name
                    verified.append(name)
        result = {'status': 'passed', 'app_version': manifest['app_version'], 'git_commit': sha,
                  'zip_entries': len(names), 'source_entries': len(source_names),
                  'first_party_files_verified_against_git': len(verified),
                  'bytes': args.zip.stat().st_size,
                  'sha256': hashlib.file_digest(args.zip.open('rb'), 'sha256').hexdigest(),
                  'runtime_files_verified': len(hashes),
                  'excluded_sensitive_runtime_data': True, 'zip_crc_valid': True}
    output = args.zip.with_suffix('.audit.json')
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
