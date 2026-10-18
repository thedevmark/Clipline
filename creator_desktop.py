"""Run the persistent Creator Library companion during the native rebuild."""
import argparse
from pathlib import Path
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from native.services.paths import APP_STATE_DIR, INTERNAL_DIR
from native.ui.creator_library import CreatorLibraryWindow


def main(argv=None):
    parser = argparse.ArgumentParser(description='Clipline Creator Library')
    parser.add_argument('--workspace', type=Path, default=APP_STATE_DIR / 'creator')
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('Clipline')
    app.setWindowIcon(QIcon(str(INTERNAL_DIR / 'static' / 'favicon.ico')))
    window = CreatorLibraryWindow(args.workspace)
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
