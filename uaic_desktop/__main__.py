from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from uaic_desktop.backend import create_backend
from uaic_desktop.window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Universal AI Coprocessor")
    window = MainWindow(create_backend)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
