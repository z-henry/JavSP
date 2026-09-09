"""Console entry point for the frozen Windows application."""
import sys


def run():
    try:
        # Include import/configuration errors in the console lifetime as well.
        from javsp.__main__ import entry
        entry()
    except Exception:
        sys.excepthook(*sys.exc_info())
        raise SystemExit(1)
    finally:
        if (getattr(sys, 'frozen', False) and sys.platform == 'win32'
                and sys.stdin is not None and sys.stdin.isatty()):
            import msvcrt
            print('\n按任意键退出 . . .', flush=True)
            try:
                msvcrt.getwch()
            except KeyboardInterrupt:
                pass


if __name__ == '__main__':
    run()
