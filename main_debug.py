import os
from pathlib import Path

if __name__ == '__main__':
    from src.app_paths import APP_ROOT
    from src.portable_runtime import prepare
    prepare(APP_ROOT)
    from src.app_paths import DATA_ROOT
    from src.runtime_identity import get_runtime_identity, log_runtime_start
    get_runtime_identity()
    from src.config_migration import migrate
    migrate(DATA_ROOT)
    from ok import OK
    from src.config import config
    config['debug'] = True
    app = OK(config)
    log_runtime_start()
    app.start()
