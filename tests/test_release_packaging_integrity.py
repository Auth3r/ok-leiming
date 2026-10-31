"""Release builds preserve old installs and only copy tracked resource files."""
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tools import build_release


def test_release_destination_cannot_overwrite_existing_install_or_archive():
    with tempfile.TemporaryDirectory() as temporary:
        destination = Path(temporary)
        target, archive = build_release.require_new_destination(destination, 'v1', False)
        target.mkdir()
        old = target / 'settings.json'
        old.write_text('private configuration')
        with pytest.raises(FileExistsError):
            build_release.require_new_destination(destination, 'v1', False)
        assert old.read_text() == 'private configuration'
        other = destination / 'second'
        other.mkdir()
        (other / archive.name).write_bytes(b'old archive')
        with pytest.raises(FileExistsError):
            build_release.require_new_destination(other, 'v1', False)


def test_resource_copy_excludes_untracked_local_files():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / 'templates').mkdir()
        (root / 'templates/public.png').write_bytes(b'public template')
        (root / 'templates/private-capture.png').write_bytes(b'private evidence')
        target = root / 'package/templates'
        with patch.object(build_release, 'ROOT', root), \
             patch.object(build_release, 'git', return_value='templates/public.png\0'):
            build_release.copy_tracked_tree('templates', target)
        assert (target / 'public.png').read_bytes() == b'public template'
        assert not (target / 'private-capture.png').exists()


def test_runtime_manifest_detects_changed_executable_bytes():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        executable = root / 'Leiming-Automation.exe'
        executable.write_bytes(b'original build')
        first = build_release.runtime_hashes(root)
        executable.write_bytes(b'different build')
        second = build_release.runtime_hashes(root)
        assert first['Leiming-Automation.exe'] != second['Leiming-Automation.exe']
