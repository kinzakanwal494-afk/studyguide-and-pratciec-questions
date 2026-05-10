"""Tiny icon set for left toolbar tools (no external assets)."""
from __future__ import annotations

from functools import lru_cache

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap


_COLOR_BY_GROUP: dict[str, QColor] = {
    "select": QColor(80, 170, 255),
    "move": QColor(120, 220, 120),
    "crop": QColor(255, 200, 80),
    "paint": QColor(255, 120, 160),
    "erase": QColor(220, 220, 220),
    "shape": QColor(180, 120, 255),
    "type": QColor(255, 255, 120),
    "view": QColor(120, 220, 220),
    "other": QColor(200, 200, 200),
}


def _group(tool_id: str) -> str:
    if tool_id in {"move"}:
        return "move"
    if "marquee" in tool_id or "lasso" in tool_id or tool_id in {"magic_wand", "object_selection", "quick_selection"}:
        return "select"
    if "crop" in tool_id or tool_id in {"slice", "slice_select"}:
        return "crop"
    if tool_id in {
        "brush",
        "pencil",
        "blur_tool",
        "sharpen_tool",
        "smudge_tool",
        "dodge",
        "burn",
        "sponge",
        "clone_stamp",
        "spot_healing",
        "healing_brush",
        "remove",
        "red_eye",
        "gradient",
        "paint_bucket",
        "color_replacement",
        "mixer_brush",
    }:
        return "paint"
    if "eraser" in tool_id:
        return "erase"
    if tool_id.startswith("shape_"):
        return "shape"
    if tool_id.startswith("type"):
        return "type"
    if tool_id in {"hand", "zoom", "rotate_view", "screen_mode", "quick_mask"}:
        return "view"
    return "other"


def _glyph(tool_id: str) -> str:
    # 1–2 letter glyphs to keep icons readable at 16px
    overrides = {
        "move": "MV",
        "hand": "HD",
        "zoom": "Z",
        "crop": "CR",
        "magic_wand": "W",
        "paint_bucket": "BK",
        "gradient": "GR",
        "clone_stamp": "CL",
        "eraser": "ER",
        "magic_eraser": "ME",
        "background_eraser": "BE",
        "brush": "BR",
        "pencil": "PN",
        "lasso": "LS",
        "rectangular_marquee": "RM",
        "elliptical_marquee": "EM",
        "single_row_marquee": "SR",
        "single_column_marquee": "SC",
        "eyedropper": "EY",
        "shape_rect": "▭",
        "shape_ellipse": "◯",
        "type_horizontal": "T",
        "quick_mask": "QM",
        "screen_mode": "FS",
    }
    if tool_id in overrides:
        return overrides[tool_id]
    # default from words
    parts = [p for p in tool_id.split("_") if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


@lru_cache(maxsize=256)
def icon_for_tool(tool_id: str, size: int = 16) -> QIcon:
    pix = QPixmap(QSize(size, size))
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    try:
        g = _group(tool_id)
        base = _COLOR_BY_GROUP.get(g, _COLOR_BY_GROUP["other"])

        # background
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(base)
        p.drawRoundedRect(0, 0, size - 1, size - 1, 3, 3)

        # border
        p.setPen(QPen(QColor(0, 0, 0, 90), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(0, 0, size - 1, size - 1, 3, 3)

        # glyph
        p.setPen(QPen(QColor(20, 20, 20), 1))
        font = QFont()
        font.setBold(True)
        font.setPointSize(max(7, int(size * 0.45)))
        p.setFont(font)
        p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, _glyph(tool_id))
    finally:
        p.end()
    return QIcon(pix)

