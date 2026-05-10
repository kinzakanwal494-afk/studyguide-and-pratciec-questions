"""Entry point for DRAGON FORMUP."""
from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from editor_app import PhotoEditorWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("DRAGON FORMUP")
    app.setOrganizationName("DragonFormup")
    win = PhotoEditorWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
