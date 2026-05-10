
"""
یہی فائل چلائیں — Single entry for DRAGON FORMUP editor.

پہلے:
  pip install PyQt6 Pillow

پھر:
  python COMPLETE_RUN_THIS.py
"""
from __future__ import annotations

import math
import os
import sys
import traceback

from PyQt6.QtCore import QObject, QPointF, QTimer, Qt
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget

# Full app lives in editor_app.py
from editor_app import PhotoEditorWindow

# Mitigate Windows native Qt/GPU crashes (0xC0000409) by forcing software backend.
os.environ.setdefault("QT_OPENGL", "software")
os.environ.setdefault("QT_ANGLE_PLATFORM", "software")


class SafeApplication(QApplication):
    """Catches exceptions raised in Qt callbacks/events."""

    def notify(self, receiver: QObject, event) -> bool:  # type: ignore[override]
        try:
            return super().notify(receiver, event)
        except Exception:
            traceback.print_exc()
            QMessageBox.critical(
                None,
                "Runtime Error",
                "Tool runtime error:\n\n" + traceback.format_exc(),
            )
            return True


class DragonSplash(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setFixedSize(860, 480)
        self._progress = 0
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(24)
        self._on_done = None

    def start(self, on_done) -> None:
        self._on_done = on_done
        self.show()

    def _tick(self) -> None:
        self._phase += 0.09
        self._progress = min(100, self._progress + 1)
        self.update()
        if self._progress >= 100:
            self._timer.stop()
            self.close()
            if self._on_done:
                self._on_done()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # background gradient
        bg = QLinearGradient(0, 0, 0, self.height())
        bg.setColorAt(0.0, QColor(8, 20, 40))
        bg.setColorAt(0.6, QColor(14, 32, 55))
        bg.setColorAt(1.0, QColor(6, 12, 22))
        p.fillRect(self.rect(), bg)

        cx = self.width() * 0.50
        cy = self.height() * 0.46
        bob = math.sin(self._phase * 0.8) * 6.0
        wing = math.sin(self._phase * 1.6) * 22.0
        tail = math.sin(self._phase * 1.2) * 16.0

        # glow
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(40, 120, 190, 65))
        p.drawEllipse(QPointF(cx, cy + 30 + bob), 210, 95)

        body = QPainterPath()
        body.moveTo(cx - 70, cy + bob)
        body.cubicTo(cx - 40, cy - 45 + bob, cx + 45, cy - 45 + bob, cx + 75, cy + bob)
        body.cubicTo(cx + 42, cy + 38 + bob, cx - 35, cy + 38 + bob, cx - 70, cy + bob)
        p.setBrush(QColor(26, 170, 120))
        p.setPen(QPen(QColor(8, 55, 35), 2))
        p.drawPath(body)

        # wings
        left = QPainterPath()
        left.moveTo(cx - 25, cy - 8 + bob)
        left.lineTo(cx - 170, cy - 60 - wing + bob)
        left.lineTo(cx - 145, cy + 42 + bob)
        left.closeSubpath()
        p.setBrush(QColor(24, 130, 210, 210))
        p.drawPath(left)

        right = QPainterPath()
        right.moveTo(cx + 25, cy - 8 + bob)
        right.lineTo(cx + 170, cy - 60 + wing + bob)
        right.lineTo(cx + 145, cy + 42 + bob)
        right.closeSubpath()
        p.drawPath(right)

        # head and eye
        p.setBrush(QColor(40, 200, 145))
        p.drawEllipse(QPointF(cx + 86, cy - 14 + bob), 23, 18)
        p.setBrush(QColor(255, 220, 90))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx + 94, cy - 16 + bob), 3.5, 3.5)

        # tail
        p.setPen(QPen(QColor(30, 185, 130), 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(cx - 70, cy + 4 + bob), QPointF(cx - 180 - tail, cy + 28 + bob))

        # title
        p.setPen(QColor(230, 244, 255))
        title_font = QFont("Segoe UI", 31, QFont.Weight.Bold)
        p.setFont(title_font)
        p.drawText(self.rect().adjusted(0, 36, 0, 0), Qt.AlignmentFlag.AlignHCenter, "DRAGON FORMUP")
        p.setFont(QFont("Segoe UI", 11))
        p.setPen(QColor(180, 205, 225))
        p.drawText(self.rect().adjusted(0, 95, 0, 0), Qt.AlignmentFlag.AlignHCenter, "Launching creative workspace...")

        # progress bar
        bar_w = 520
        bar_h = 20
        x = (self.width() - bar_w) // 2
        y = self.height() - 72
        p.setPen(QPen(QColor(90, 125, 150), 1))
        p.setBrush(QColor(10, 22, 32))
        p.drawRoundedRect(x, y, bar_w, bar_h, 9, 9)

        fill_w = int((bar_w - 4) * self._progress / 100)
        fg = QLinearGradient(x + 2, y + 2, x + bar_w, y + 2)
        fg.setColorAt(0.0, QColor(50, 205, 255))
        fg.setColorAt(1.0, QColor(60, 230, 130))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fg)
        p.drawRoundedRect(x + 2, y + 2, max(1, fill_w), bar_h - 4, 7, 7)

        p.setPen(QColor(210, 235, 255))
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        p.drawText(self.rect().adjusted(0, 0, 0, 44), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, f"Loading {self._progress}%")


def main() -> None:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseSoftwareOpenGL, True)
    app = SafeApplication(sys.argv)
    app.setApplicationName("DRAGON FORMUP")
    app.setOrganizationName("DragonFormup")
    try:
        win = PhotoEditorWindow()
        splash = DragonSplash()
        splash.start(on_done=lambda: (win.show(), win.show_home_screen()))
        sys.exit(app.exec())
    except Exception:
        QMessageBox.critical(
            None,
            "Startup Error",
            "App start nahi ho saka.\n\n" + traceback.format_exc(),
        )
        raise


if __name__ == "__main__":
    main()
