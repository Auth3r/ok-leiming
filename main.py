if __name__ == '__main__':
    import sys
    # The copied updater must run before portable preparation, mutex, or Qt.
    if '--apply-update' in sys.argv:
        from src.update_installer import apply_update
        sys.exit(apply_update(sys.argv[sys.argv.index('--apply-update') + 1]))
    from src.app_paths import APP_ROOT
    from src.portable_runtime import prepare, report_startup_error
    import sys
    try:
        prepare(APP_ROOT)
        from src.app_paths import DATA_ROOT
        from src.runtime_identity import get_runtime_identity, log_runtime_start
        get_runtime_identity()
        from src.config_migration import migrate
        migrate(DATA_ROOT)
        from ok import OK
        from src.release_limits import install_log_limit
        install_log_limit()
        from src.config import config
        if '--validate-navigation' in sys.argv:
            from tools.validate_module_navigation import main as validate_navigation
            sys.exit(validate_navigation([value for value in sys.argv[1:] if value != '--validate-navigation']))
        elif '--release-smoke' in sys.argv:
            from src.release_smoke import run
            run(config)
        elif '--validate-roguelike' in sys.argv:
            # Explicit QA only; the harness additionally requires --live and
            # starts the same production Qt panel after fresh home recognition.
            sys.argv.remove('--validate-roguelike')
            import tools.roguelike_gui_acceptance
        elif '--validate-daily' in sys.argv:
            sys.argv.remove('--validate-daily')
            import tools.daily_gui_acceptance
        else:
            app = OK(config)
            log_runtime_start()
            app.start()
    except Exception:
        from src.app_paths import DATA_ROOT
        report_startup_error(DATA_ROOT)
        if getattr(sys, 'frozen', False):
            sys.exit(1)  # The friendly message already explains the error.
        raise
