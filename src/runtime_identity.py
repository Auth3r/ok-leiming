"""Process-frozen provenance of the code actually loaded; Git is optional."""
import json
import logging
import os
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from src.version import APP_VERSION

# Keep provenance tied to the installation, independent of the writable data root.
from src.app_paths import APP_ROOT
_identity = None
_identity_lock = Lock()


@dataclass(frozen=True)
class RuntimeIdentity:
    app_version: str
    git_branch: str | None
    git_commit: str | None
    git_dirty: bool | None
    app_root: str
    python_executable: str
    python_version: str
    started_at: str
    git_error: str | None
    data_root: str | None = None
    working_directory: str | None = None

    def as_dict(self):
        return asdict(self)

    @property
    def display_version(self):
        label = f'{self.app_version} · {self.git_commit[:7] if self.git_commit else "Git unavailable"}'
        if self.git_dirty is True:
            label += ' · dirty'
        if self.git_error and self.git_commit:
            label += ' · Git incomplete'
        return label


def capture_identity(root, app_version):
    """Read this checkout only, with bounded Git calls and explicit missing fields."""
    root = Path(root).resolve()
    from src.app_paths import DATA_ROOT
    started_at = datetime.now(timezone.utc).isoformat()
    branch = commit = dirty = error = None
    if getattr(sys, 'frozen', False):
        # A release must identify its exact clean build without requiring Git.
        manifest = json.loads((root / 'release-manifest.json').read_text(encoding='utf-8'))
        commit = manifest['git_commit']
        if (manifest['app_version'] != app_version or type(manifest['git_dirty']) is not bool
                or len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit)):
            raise ValueError('Invalid portable release provenance')
        return RuntimeIdentity(app_version, manifest['git_branch'], commit, manifest['git_dirty'], str(root),
                               str(Path(sys.executable).resolve()), platform.python_version(),
                               started_at, None, str(DATA_ROOT), str(Path.cwd()))
    # An inherited GIT_DIR/WORK_TREE/INDEX_FILE must not redirect provenance.
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith('GIT_')}

    def git(*args):
        result = subprocess.run(['git', '--no-optional-locks', '-C', str(root), *args],
                                capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=2, env=environment,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise RuntimeError(f'git {args[0]}: {result.stderr.strip() or result.returncode}')
        return result.stdout.strip()

    try:
        if Path(git('rev-parse', '--show-toplevel')).resolve() != root:
            raise RuntimeError('Loaded application root is not the Git worktree root')
        commit = git('rev-parse', '--verify', 'HEAD')
        if len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit.lower()):
            commit = None
            raise RuntimeError('Git did not return a 40-character commit SHA')
        branch = git('branch', '--show-current') or None  # detached HEAD is valid
        dirty = bool(git('status', '--porcelain', '--untracked-files=normal'))
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        error = f'{type(exc).__name__}: {exc}'
    return RuntimeIdentity(app_version, branch, commit, dirty, str(root),
                           str(Path(sys.executable).resolve()), platform.python_version(),
                           started_at, error, str(DATA_ROOT), str(Path.cwd()))


def get_runtime_identity():
    global _identity
    with _identity_lock:
        if _identity is None:
            _identity = capture_identity(APP_ROOT, APP_VERSION)
        return _identity


def log_runtime_start():
    """Call after ok-script configures its main log, before app.start()."""
    identity = get_runtime_identity()
    payload = json.dumps({'event': 'run_start', 'runtime_identity': identity.as_dict()},
                         ensure_ascii=False)
    logger = logging.getLogger('ok')
    if identity.git_dirty or identity.git_error:
        logger.warning(payload)
    else:
        logger.info(payload)
