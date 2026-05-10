"""Main window: DRAGON FORMUP editor, layers, canvas, undo/redo."""
from __future__ import annotations

import io
import os
import platform
import sys
from functools import partial
import math
from typing import Callable, List, Optional

from PIL import Image, ImageDraw, ImageChops, ImageEnhance, ImageOps, ImageFilter

from PyQt6.QtCore import QByteArray, QBuffer, QIODevice, QRect, QSettings, Qt
from PyQt6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QGuiApplication,
    QImage,
    QKeySequence,
    QPainter,
    QPixmap,
)
from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QTabBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QPushButton,
    QTextEdit,
)

from canvas_widget import CanvasWidget, pil_to_qimage, subtract_rect
from document import Document, Layer
from left_tools import LEFT_TOOLS
import operations as ops
from tool_icons import icon_for_tool


def pil_from_clipboard() -> Optional[Image.Image]:
    clip = QGuiApplication.clipboard()
    if clip is None:
        return None
    qimg = clip.image()
    if qimg.isNull():
        return None
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    qimg.save(buf, "PNG")
    buf.close()
    return Image.open(io.BytesIO(data.data())).convert("RGBA")


def pil_to_clipboard(pil: Image.Image) -> None:
    b = io.BytesIO()
    pil.save(b, "PNG")
    b.seek(0)
    img = QImage()
    img.loadFromData(b.read())
    QGuiApplication.clipboard().setImage(img)


UNDO_LIMIT = 40


class PhotoEditorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DRAGON FORMUP")
        self.resize(1280, 800)
        self._settings = QSettings()
        self._doc: Optional[Document] = None
        self._undo_stack: List[dict] = []
        self._redo_stack: List[dict] = []
        self._current_path: Optional[str] = None
        self._last_filter: Optional[Callable[[Image.Image], Image.Image]] = None
        self._fg = QColor(0, 0, 0)
        self._bg = QColor(255, 255, 255)
        self._history_log: List[str] = []
        self._brush_size = 24
        self._stroke_in_progress = False
        self._clone_paint_anchor: Optional[tuple[int, int]] = None
        self._layer_move_pushed = False
        self._documents: list[dict] = []
        self._active_doc_index = -1

        self._canvas = CanvasWidget()
        self._canvas.editor = self
        self._canvas.selectionChanged.connect(self._on_selection_changed)
        self._canvas.cropApplied.connect(self._apply_crop_rect)

        self._layers_list = QListWidget()
        self._layers_list.currentRowChanged.connect(self._on_layer_row_changed)

        self._history_list = QListWidget()
        self._channels_list = QListWidget()
        self._paths_list = QListWidget()
        self._info_label = QLabel("Info: -")
        self._hist_label = QLabel("Histogram: -")
        self._props_label = QLabel("Properties: -")
        self._brush_size_spin: Optional[QSpinBox] = None

        self._navigator = QLabel()
        self._navigator.setMinimumSize(120, 120)
        self._navigator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._navigator.setStyleSheet("background:#222;color:#888;")

        self._build_toolbars()
        self._build_central()
        self._build_docks()
        self._build_menus()
        self._build_status()

        self._new_document(800, 600, as_new_tab=True)
        self._log_history("New document")

    # --- UI ---
    def _build_toolbars(self) -> None:
        # Use a docked tool list instead of huge QActionGroup:
        # this is more stable on Windows with many tools.
        tools_dock = QDockWidget("Tools", self)
        self._tools_list = QListWidget()
        self._tools_list.setStyleSheet(
            "QListWidget { background:#1f1f1f; color:#eaeaea; }"
            "QListWidget::item { padding:6px; }"
            "QListWidget::item:selected { background:#2f6fed; }"
            "QListWidget::item:hover { background:#2a2a2a; }"
        )
        self._tools_id_by_row: dict[int, str] = {}
        row = 0
        for tid, title, tip in LEFT_TOOLS:
            if tid == "separator":
                self._tools_list.addItem("────────────")
                it = self._tools_list.item(row)
                if it:
                    it.setFlags(Qt.ItemFlag.NoItemFlags)
                row += 1
                continue
            item = QListWidgetItem(title)
            item.setIcon(icon_for_tool(tid, 16))
            item.setToolTip(tip)
            item.setData(Qt.ItemDataRole.UserRole, tid)
            self._tools_list.addItem(item)
            self._tools_id_by_row[row] = tid
            row += 1
        self._tools_list.currentRowChanged.connect(self._on_left_tool_row_changed)
        tools_dock.setWidget(self._tools_list)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, tools_dock)
        self._select_tool_id("rectangular_marquee")

        top_tb = QToolBar("Colors / Options")
        top_tb.setMovable(True)
        self.addToolBar(top_tb)
        self._fg_action = QAction("Foreground", self)
        self._fg_action.triggered.connect(self._pick_fg)
        top_tb.addAction(self._fg_action)
        self._bg_action = QAction("Background", self)
        self._bg_action.triggered.connect(self._pick_bg)
        top_tb.addAction(self._bg_action)
        top_tb.addSeparator()
        top_tb.addAction(QAction("Swap FG/BG", self, triggered=self._swap_colors))
        top_tb.addAction(QAction("Default B&W", self, triggered=self._default_colors))
        top_tb.addSeparator()
        top_tb.addAction(QAction("Brush size +", self, triggered=lambda: self._nudge_brush(4)))
        top_tb.addAction(QAction("Brush size −", self, triggered=lambda: self._nudge_brush(-4)))
        top_tb.addSeparator()
        self._workspace_combo = QComboBox()
        self._workspace_combo.addItems(
            ["Essentials", "Photography", "Graphic and Web", "Painting", "Motion", "3D", "Reset Essentials"]
        )
        self._workspace_combo.currentTextChanged.connect(self._apply_workspace)
        top_tb.addWidget(self._workspace_combo)
        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("Search tools/commands...")
        self._search_box.returnPressed.connect(self._run_search)
        self._search_box.setMaximumWidth(260)
        top_tb.addWidget(self._search_box)
        top_tb.addSeparator()
        self._tool_opts = QLabel("Options: -")
        top_tb.addWidget(self._tool_opts)

    def _nudge_brush(self, d: int) -> None:
        self._brush_size = max(1, min(200, self._brush_size + d))
        self.statusBar().showMessage(f"Brush size {self._brush_size}px")

    def _swap_colors(self) -> None:
        self._fg, self._bg = self._bg, self._fg

    def _default_colors(self) -> None:
        self._fg = QColor(0, 0, 0)
        self._bg = QColor(255, 255, 255)

    def _apply_workspace(self, name: str) -> None:
        name = name.strip()
        if name == "Reset Essentials":
            for d in self.findChildren(QDockWidget):
                d.show()
            self.statusBar().showMessage("Workspace reset")
            return
        self.statusBar().showMessage(f"Workspace: {name}")

    def _run_search(self) -> None:
        q = self._search_box.text().strip().lower()
        if not q:
            return
        if "crop" in q:
            self._select_tool_id("crop")
        elif "blur" in q:
            self._select_tool_id("blur_tool")
        elif "remove background" in q:
            self._ctx_remove_bg()
        elif "select subject" in q:
            self._sel_subject()
        else:
            self.statusBar().showMessage(f"No direct command for: {q}")

    def show_home_screen(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("DRAGON FORMUP Home")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("Welcome to DRAGON FORMUP"))
        lay.addWidget(QPushButton("New File", clicked=lambda: (dlg.accept(), self._file_new())))
        lay.addWidget(QPushButton("Open File", clicked=lambda: (dlg.accept(), self._file_open())))
        rec = self._settings.value("recent", []) or []
        if rec:
            lay.addWidget(QLabel("Recent Files"))
            lst = QListWidget()
            for p in rec[:8]:
                lst.addItem(str(p))
            lst.itemDoubleClicked.connect(lambda it: (dlg.accept(), self._open_path(it.text())))
            lay.addWidget(lst)
        lay.addWidget(QPushButton("Learn / Tutorials", clicked=lambda: QMessageBox.information(self, "Learn", "Use left tools and right panels.")))
        dlg.resize(420, 520)
        dlg.exec()

    def _ctx_remove_bg(self) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        self._push_undo()
        im = self._doc.active_layer.image.convert("RGBA")
        # simple background removal using corner color similarity
        c = im.getpixel((0, 0))
        px = im.load()
        tol = 50
        for y in range(im.height):
            for x in range(im.width):
                p = px[x, y]
                if abs(p[0] - c[0]) + abs(p[1] - c[1]) + abs(p[2] - c[2]) < tol:
                    px[x, y] = (p[0], p[1], p[2], 0)
        self._doc.active_layer.image = im
        self._sync_ui_from_doc()
        self._log_history("Quick Action: Remove Background")

    def _ctx_generative_fill(self) -> None:
        # placeholder contextual action: textured fill on current selection
        if self._doc is None or self._doc.active_layer is None:
            return
        sel = self._canvas.selection_bounds()
        if sel is None:
            QMessageBox.information(self, "Generative Fill", "Select area first.")
            return
        self._push_undo()
        draw = ImageDraw.Draw(self._doc.active_layer.image)
        c1 = (self._fg.red(), self._fg.green(), self._fg.blue(), 180)
        c2 = (self._bg.red(), self._bg.green(), self._bg.blue(), 180)
        for y in range(sel.y(), sel.y() + sel.height(), 8):
            for x in range(sel.x(), sel.x() + sel.width(), 8):
                draw.rectangle([x, y, x + 7, y + 7], fill=c1 if ((x + y) // 8) % 2 == 0 else c2)
        self._sync_ui_from_doc()
        self._log_history("Quick Action: Generative Fill")

    def _on_left_tool_row_changed(self, row: int) -> None:
        tid = self._tools_id_by_row.get(row)
        if not tid:
            return
        self._select_tool_id(tid)

    def _select_tool_id(self, tid: str) -> None:
        for i in range(self._tools_list.count()):
            it = self._tools_list.item(i)
            if it and it.data(Qt.ItemDataRole.UserRole) == tid:
                if self._tools_list.currentRow() != i:
                    self._tools_list.blockSignals(True)
                    self._tools_list.setCurrentRow(i)
                    self._tools_list.blockSignals(False)
                break
        self._canvas.tool_id = tid
        self._canvas.crop_mode = tid == "crop"
        title = tid.replace("_", " ").title()
        if tid in {"brush", "pencil"}:
            self._tool_opts.setText(f"Options: size={self._brush_size}, hardness=70, opacity=100, flow=100")
        elif tid == "move":
            self._tool_opts.setText("Options: Auto-Select OFF, Transform Controls ON")
        elif tid == "type_horizontal":
            self._tool_opts.setText("Options: font=Arial, size=24, align=Left, anti-alias=On")
        elif tid == "crop":
            self._tool_opts.setText("Options: ratio=free, W/H/Res editable, straighten")
        else:
            self._tool_opts.setText("Options: default")
        self.statusBar().showMessage(f"Tool: {title}")

    def status_message(self, msg: str) -> None:
        self.statusBar().showMessage(msg)

    def show_tool_stub(self, tid: str) -> None:
        self.statusBar().showMessage(f"{tid}: advanced feature — use menus or another tool.")

    def toggle_quick_mask(self) -> None:
        self._canvas.quick_mask = not self._canvas.quick_mask
        self._canvas.update()
        self.statusBar().showMessage("Quick mask ON" if self._canvas.quick_mask else "Quick mask OFF")

    def toggle_screen_mode(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def sample_foreground(self, x: int, y: int) -> None:
        if self._doc is None:
            return
        im = self._doc.composite()
        x = max(0, min(im.width - 1, x))
        y = max(0, min(im.height - 1, y))
        r, g, b, _a = im.getpixel((x, y))
        self._fg = QColor(r, g, b)
        self.statusBar().showMessage(f"Foreground #{r:02x}{g:02x}{b:02x}")

    def apply_magic_wand(self, x: int, y: int, tolerance: int = 40) -> None:
        if self._doc is None:
            return
        im = self._doc.composite().convert("RGBA")
        w, h = im.size
        x = max(0, min(w - 1, x))
        y = max(0, min(h - 1, y))
        target = im.getpixel((x, y))

        def close(a: tuple, b: tuple) -> bool:
            return sum(abs(int(a[i]) - int(b[i])) for i in range(4)) <= tolerance

        seen: set[tuple[int, int]] = set()
        stack = [(x, y)]
        minx = maxx = x
        miny = maxy = y
        cap = 300_000
        while stack and len(seen) < cap:
            cx, cy = stack.pop()
            if (cx, cy) in seen or not (0 <= cx < w and 0 <= cy < h):
                continue
            if not close(im.getpixel((cx, cy)), target):
                continue
            seen.add((cx, cy))
            minx, maxx = min(minx, cx), max(maxx, cx)
            miny, maxy = min(miny, cy), max(maxy, cy)
            stack.extend([(cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)])
        if not seen:
            return
        self._canvas.selection_parts = [QRect(minx, miny, maxx - minx + 1, maxy - miny + 1)]
        self._canvas.selection_shape = "rect"
        self._canvas.update()
        self.statusBar().showMessage(f"Wand: {len(seen)} px")

    def apply_magic_eraser(self, x: int, y: int, tolerance: int = 40) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        self._push_undo()
        self.apply_magic_wand(x, y, tolerance)
        b = self._canvas.selection_bounds()
        if b:
            clear = Image.new("RGBA", (b.width(), b.height()), (0, 0, 0, 0))
            self._doc.active_layer.image.paste(clear, (b.x(), b.y()))
        self._canvas.selection_parts = []
        self._sync_ui_from_doc()
        self._log_history("Magic Eraser")

    def apply_paint_bucket(self, x: int, y: int, tolerance: int = 35) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        self._push_undo()
        lyr = self._doc.active_layer
        im = lyr.image.convert("RGBA")
        w, h = im.size
        x = max(0, min(w - 1, x))
        y = max(0, min(h - 1, y))
        target = im.getpixel((x, y))
        fill = (self._fg.red(), self._fg.green(), self._fg.blue(), 255)

        def close(a: tuple, b: tuple) -> bool:
            return sum(abs(int(a[i]) - int(b[i])) for i in range(4)) <= tolerance

        seen: set[tuple[int, int]] = set()
        stack = [(x, y)]
        px = im.load()
        cap = 400_000
        while stack and len(seen) < cap:
            cx, cy = stack.pop()
            if (cx, cy) in seen or not (0 <= cx < w and 0 <= cy < h):
                continue
            if not close(px[cx, cy], target):
                continue
            seen.add((cx, cy))
            px[cx, cy] = fill
            stack.extend([(cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)])
        lyr.image = im
        self._sync_ui_from_doc()
        self._log_history("Paint Bucket")

    def apply_object_selection(self) -> None:
        self._safe_subject_selection()

    def apply_quick_selection(self, x: int, y: int) -> None:
        if self._doc is None:
            return
        r = 40
        self._canvas.selection_parts = [
            QRect(max(0, x - r), max(0, y - r), min(self._doc.width, x + r) - max(0, x - r), min(self._doc.height, y + r) - max(0, y - r))
        ]
        self._canvas.selection_shape = "rect"
        self._canvas.update()

    def paste_lorem_text(self) -> None:
        self._type_lorem()

    def offset_active_layer(self, dx: int, dy: int) -> None:
        if self._doc is None or self._doc.active_layer is None or (dx == 0 and dy == 0):
            return
        if not self._layer_move_pushed:
            self._push_undo()
            self._layer_move_pushed = True
        self._doc.active_layer.image = ImageChops.offset(self._doc.active_layer.image, dx, dy)
        self._refresh_canvas()

    def paint_brush_stroke(self, x0: int, y0: int, x1: int, y1: int, tid: str, modifiers: Qt.KeyboardModifier) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        if not self._stroke_in_progress:
            self._push_undo()
            self._stroke_in_progress = True
        lyr = self._doc.active_layer
        im = lyr.image
        draw = ImageDraw.Draw(im, "RGBA")
        rad = max(1, self._brush_size // 4) if tid == "pencil" else self._brush_size
        fg = (self._fg.red(), self._fg.green(), self._fg.blue(), 255)
        dist = max(1, int(math.hypot(x1 - x0, y1 - y0)))
        if tid == "clone_stamp" and self._canvas._clone_src:
            if self._clone_paint_anchor is None:
                self._clone_paint_anchor = (x0, y0)
            ax, ay = self._clone_paint_anchor
            csx, csy = self._canvas._clone_src
            src_im = im.copy()
            for i in range(dist + 1):
                t = i / dist
                x = int(x0 + (x1 - x0) * t)
                y = int(y0 + (y1 - y0) * t)
                sx = csx + (x - ax)
                sy = csy + (y - ay)
                if 0 <= sx < im.width and 0 <= sy < im.height and 0 <= x < im.width and 0 <= y < im.height:
                    patch = src_im.crop((sx - rad, sy - rad, sx + rad, sy + rad))
                    im.paste(patch, (x - rad, y - rad), patch)
        else:
            for i in range(dist + 1):
                t = i / dist
                x = int(x0 + (x1 - x0) * t)
                y = int(y0 + (y1 - y0) * t)
                if tid == "eraser" or tid == "background_eraser":
                    draw.ellipse([x - rad, y - rad, x + rad, y + rad], fill=(0, 0, 0, 0))
                elif tid in ("spot_healing", "healing_brush", "remove"):
                    region = im.crop((x - rad, y - rad, x + rad, y + rad))
                    blurred = region.filter(ImageFilter.GaussianBlur(radius=4))
                    im.paste(blurred, (x - rad, y - rad))
                elif tid == "red_eye":
                    region = im.crop((x - rad, y - rad, x + rad, y + rad))
                    r, g, b, a = region.split()
                    r = r.point(lambda v: min(v, int(v * 0.4)))
                    im.paste(Image.merge("RGBA", (r, g, b, a)), (x - rad, y - rad))
                elif tid == "blur_tool":
                    region = im.crop((x - rad, y - rad, x + rad, y + rad))
                    im.paste(region.filter(ImageFilter.GaussianBlur(radius=2)), (x - rad, y - rad))
                elif tid == "sharpen_tool":
                    region = im.crop((x - rad, y - rad, x + rad, y + rad))
                    im.paste(region.filter(ImageFilter.SHARPEN), (x - rad, y - rad))
                elif tid == "smudge_tool" or tid == "mixer_brush":
                    if 0 <= x + 3 < im.width and 0 <= y + 3 < im.height:
                        patch = im.crop((x, y, x + rad + 2, y + rad + 2))
                        im.paste(patch, (x - 2, y - 2))
                elif tid == "dodge":
                    draw.ellipse([x - rad, y - rad, x + rad, y + rad], fill=(255, 255, 255, 40))
                elif tid == "burn":
                    draw.ellipse([x - rad, y - rad, x + rad, y + rad], fill=(0, 0, 0, 40))
                elif tid == "sponge":
                    region = im.crop((x - rad, y - rad, x + rad, y + rad))
                    f = 0.85 if modifiers & Qt.KeyboardModifier.ShiftModifier else 1.15
                    im.paste(ImageEnhance.Color(region).enhance(f), (x - rad, y - rad))
                elif tid == "color_replacement":
                    draw.ellipse([x - rad, y - rad, x + rad, y + rad], fill=fg)
                else:
                    draw.ellipse([x - rad, y - rad, x + rad, y + rad], fill=fg)
        lyr.image = im
        # Avoid deep re-entrancy during mouse event dispatch.
        self._refresh_canvas()

    def after_paint_stroke(self) -> None:
        self._stroke_in_progress = False
        self._clone_paint_anchor = None
        self._sync_ui_from_doc()
        self._log_history(f"Paint ({self._canvas.tool_id})")

    def reset_layer_move(self) -> None:
        self._layer_move_pushed = False

    def apply_linear_gradient(self, x0: int, y0: int, x1: int, y1: int) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        self._push_undo()
        lyr = self._doc.active_layer
        im = lyr.image.copy()
        w, h = im.size
        fg = (self._fg.red(), self._fg.green(), self._fg.blue(), 255)
        bg = (self._bg.red(), self._bg.green(), self._bg.blue(), 255)
        b = self._canvas.selection_bounds()
        x_min, y_min = 0, 0
        x_max, y_max = w - 1, h - 1
        if b:
            x_min, y_min = b.x(), b.y()
            x_max = b.x() + b.width() - 1
            y_max = b.y() + b.height() - 1
        dx, dy = x1 - x0, y1 - y0
        length = max(1.0, math.hypot(dx, dy))
        ux, uy = dx / length, dy / length
        px = im.load()
        for yy in range(y_min, y_max + 1):
            for xx in range(x_min, x_max + 1):
                t = ((xx - x0) * ux + (yy - y0) * uy) / length
                t = max(0.0, min(1.0, t))
                c = tuple(int(fg[i] + (bg[i] - fg[i]) * t) for i in range(3)) + (255,)
                o = im.getpixel((xx, yy))
                if o[3] > 0:
                    px[xx, yy] = c
        lyr.image = im
        self._sync_ui_from_doc()
        self._log_history("Gradient")

    def apply_shape_fill(self, rect: QRect, ellipse: bool) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        self._push_undo()
        lyr = self._doc.active_layer
        draw = ImageDraw.Draw(lyr.image, "RGBA")
        fg = (self._fg.red(), self._fg.green(), self._fg.blue(), 255)
        box = [rect.x(), rect.y(), rect.x() + rect.width() - 1, rect.y() + rect.height() - 1]
        if ellipse:
            draw.ellipse(box, fill=fg)
        else:
            draw.rectangle(box, fill=fg)
        self._sync_ui_from_doc()
        self._log_history("Shape")

    def _build_central(self) -> None:
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._doc_tabs = QTabBar()
        self._doc_tabs.setExpanding(False)
        self._doc_tabs.currentChanged.connect(self._on_doc_tab_changed)
        lay.addWidget(self._doc_tabs)
        self._scroll = QScrollArea()
        self._canvas.setMinimumSize(1600, 1000)
        self._scroll.setWidget(self._canvas)
        self._scroll.setWidgetResizable(False)
        lay.addWidget(self._scroll, 1)

        self._context_bar = QToolBar("Contextual Tasks")
        self._context_bar.addAction("Remove Background", self._ctx_remove_bg)
        self._context_bar.addAction("Select Subject", self._sel_subject)
        self._context_bar.addAction("Generative Fill", self._ctx_generative_fill)
        self._context_bar.addAction("Create Mask", self._sel_mask)
        self._context_bar.addAction("Transform", self._edit_free_transform)
        lay.addWidget(self._context_bar)
        self.setCentralWidget(root)

    def _build_docks(self) -> None:
        d_layers = QDockWidget("Layers", self)
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(self._layers_list)
        row = QHBoxLayout()
        for txt, fn in [
            ("+", self._layer_new),
            ("Dup", self._layer_duplicate),
            ("Del", self._layer_delete),
        ]:
            b = QPushButton(txt)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        d_layers.setWidget(w)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, d_layers)

        d_hist = QDockWidget("History", self)
        d_hist.setWidget(self._history_list)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, d_hist)

        d_nav = QDockWidget("Navigator", self)
        d_nav.setWidget(self._navigator)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, d_nav)

        d_panels = QDockWidget("Panels", self)
        tabs = QTabWidget()

        # Properties
        props = QWidget()
        props_l = QVBoxLayout(props)
        props_l.addWidget(self._props_label)
        b_opacity = QPushButton("Layer Opacity…")
        b_opacity.clicked.connect(self._layer_props)
        props_l.addWidget(b_opacity)
        tabs.addTab(props, "Properties")

        # Adjustments
        adj = QWidget()
        adj_l = QVBoxLayout(adj)
        adj_l.addWidget(QPushButton("Auto Tone", clicked=self._img_auto_tone))
        adj_l.addWidget(QPushButton("Auto Contrast", clicked=self._img_auto_contrast))
        adj_l.addWidget(QPushButton("Auto Color", clicked=self._img_auto_color))
        adj_l.addWidget(QPushButton("Brightness/Contrast…", clicked=self._img_adj_bc))
        tabs.addTab(adj, "Adjustments")

        # Libraries
        libs = QTextEdit()
        libs.setReadOnly(True)
        libs.setPlainText("Libraries panel\nStore reusable assets, colors, presets.")
        tabs.addTab(libs, "Libraries")

        # Channels
        self._channels_list.addItems(["Red", "Green", "Blue", "Alpha"])
        tabs.addTab(self._channels_list, "Channels")

        # Paths
        self._paths_list.addItem("No active path")
        tabs.addTab(self._paths_list, "Paths")

        # Color
        color_w = QWidget()
        color_l = QVBoxLayout(color_w)
        color_l.addWidget(QPushButton("Foreground Color…", clicked=self._pick_fg))
        color_l.addWidget(QPushButton("Background Color…", clicked=self._pick_bg))
        color_l.addWidget(QPushButton("Swap FG/BG", clicked=self._swap_colors))
        tabs.addTab(color_w, "Color")

        # Swatches
        sw = QListWidget()
        for c in ["#000000", "#ffffff", "#ff0000", "#00ff00", "#0000ff", "#ffd700", "#00ffff", "#ff00ff"]:
            it = QListWidgetItem(c)
            it.setBackground(QColor(c))
            sw.addItem(it)
        sw.itemClicked.connect(lambda it: setattr(self, "_fg", QColor(it.text())))
        tabs.addTab(sw, "Swatches")

        # Gradients
        grad_w = QWidget()
        grad_l = QVBoxLayout(grad_w)
        grad_l.addWidget(QPushButton("Apply FG->BG Gradient", clicked=lambda: self.apply_linear_gradient(0, 0, self._doc.width if self._doc else 100, 0)))
        tabs.addTab(grad_w, "Gradients")

        # Patterns
        pat_w = QWidget()
        pat_l = QVBoxLayout(pat_w)
        pat_l.addWidget(QPushButton("Checker Fill", clicked=self._panel_pattern_checker))
        pat_l.addWidget(QPushButton("Stripe Fill", clicked=self._panel_pattern_stripe))
        tabs.addTab(pat_w, "Patterns")

        # Brushes
        br_w = QWidget()
        br_l = QFormLayout(br_w)
        spin = QSpinBox()
        spin.setRange(1, 200)
        spin.setValue(self._brush_size)
        spin.valueChanged.connect(self._panel_set_brush_size)
        self._brush_size_spin = spin
        br_l.addRow("Brush Size", spin)
        tabs.addTab(br_w, "Brushes")

        # Brush Settings
        bs = QTextEdit()
        bs.setReadOnly(True)
        bs.setPlainText("Brush Settings\n- Use brush size control\n- Hardness/flow simplified in this build")
        tabs.addTab(bs, "Brush Settings")

        # Info / Histogram
        tabs.addTab(self._info_label, "Info")
        tabs.addTab(self._hist_label, "Histogram")

        # Character / Paragraph / Glyphs
        ch = QTextEdit()
        ch.setPlainText("Character panel\nUse Type > Paste Lorem Ipsum in this build.")
        tabs.addTab(ch, "Character")
        pg = QTextEdit()
        pg.setPlainText("Paragraph panel\nAlignment controls simplified.")
        tabs.addTab(pg, "Paragraph")
        gl = QTextEdit()
        gl.setPlainText("Glyphs panel\nAdvanced font glyph selection is simplified.")
        tabs.addTab(gl, "Glyphs")

        # Styles
        sty_w = QWidget()
        sty_l = QVBoxLayout(sty_w)
        sty_l.addWidget(QPushButton("Emboss Style", clicked=lambda: self._apply_filter(ops.emboss, "Style: Emboss")))
        sty_l.addWidget(QPushButton("Edge Style", clicked=lambda: self._apply_filter(ops.find_edges, "Style: Edge")))
        tabs.addTab(sty_w, "Styles")

        # Shapes
        shp_w = QWidget()
        shp_l = QVBoxLayout(shp_w)
        shp_l.addWidget(QPushButton("Rectangle Tool", clicked=lambda: self._select_tool_id("shape_rect")))
        shp_l.addWidget(QPushButton("Ellipse Tool", clicked=lambda: self._select_tool_id("shape_ellipse")))
        tabs.addTab(shp_w, "Shapes")

        # Actions
        act = QListWidget()
        for a in ["Auto Tone", "Flatten", "Quick Export", "Duplicate Layer"]:
            act.addItem(a)
        act.itemDoubleClicked.connect(self._panel_run_action)
        tabs.addTab(act, "Actions")

        # Timeline
        tl = QListWidget()
        tl.addItems(["Frame 1 (current)", "Add Frame", "Play Preview"])
        tl.itemDoubleClicked.connect(self._panel_timeline_action)
        tabs.addTab(tl, "Timeline")

        # Comments / Learn
        cm = QTextEdit()
        cm.setPlainText("Comments panel\nProject notes and review comments placeholder.")
        tabs.addTab(cm, "Comments")
        lr = QTextEdit()
        lr.setPlainText("Learn panel\nTip: Select tool -> click/drag canvas -> use right panels for fine control.")
        tabs.addTab(lr, "Learn")

        d_panels.setWidget(tabs)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, d_panels)

        d_layers.raise_()

    def _build_status(self) -> None:
        self._status_zoom = QLabel("100%")
        self._status_doc = QLabel("Doc: -")
        self._status_perf = QLabel("Efficiency: 100%")
        self.statusBar().addPermanentWidget(self._status_zoom)
        self.statusBar().addPermanentWidget(self._status_doc)
        self.statusBar().addPermanentWidget(self._status_perf)
        self.statusBar().showMessage("Ready")

    # --- Document / state ---
    def _push_undo(self) -> None:
        if self._doc is None:
            return
        self._undo_stack.append(self._doc.snapshot())
        if len(self._undo_stack) > UNDO_LIMIT:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def _edit_undo(self) -> None:
        if not self._undo_stack or self._doc is None:
            return
        self._redo_stack.append(self._doc.snapshot())
        snap = self._undo_stack.pop()
        self._doc.restore(snap)
        self._sync_ui_from_doc()
        self._log_history("Undo")

    def _edit_redo(self) -> None:
        if not self._redo_stack or self._doc is None:
            return
        self._undo_stack.append(self._doc.snapshot())
        snap = self._redo_stack.pop()
        self._doc.restore(snap)
        self._sync_ui_from_doc()
        self._log_history("Redo")

    def _sync_ui_from_doc(self) -> None:
        if self._doc is None:
            return
        self._layers_list.blockSignals(True)
        self._layers_list.clear()
        for lyr in self._doc.layers:
            self._layers_list.addItem(lyr.name + ("" if lyr.visible else " (hidden)"))
        self._layers_list.setCurrentRow(self._doc.active_index)
        self._layers_list.blockSignals(False)
        comp = self._doc.composite()
        self._canvas.set_pil_image(comp)
        self._update_navigator(comp)
        self._update_side_panels(comp)
        self._update_status_info()

    def _update_navigator(self, pil: Image.Image) -> None:
        small = pil.copy()
        small.thumbnail((120, 120))
        self._navigator.setPixmap(QPixmap.fromImage(pil_to_qimage(small)))

    def _refresh_canvas(self) -> None:
        if self._doc:
            self._canvas.set_pil_image(self._doc.composite())
            self._update_navigator(self._doc.composite())
            self._update_status_info()

    def _on_layer_row_changed(self, row: int) -> None:
        if self._doc and 0 <= row < len(self._doc.layers):
            self._doc.active_index = row

    def _on_selection_changed(self, rect) -> None:
        if rect is None:
            return
        self.statusBar().showMessage(f"Selection {rect.width()}×{rect.height()}")
        self._paths_list.clear()
        self._paths_list.addItem(f"Selection path: {rect.x()},{rect.y()}  {rect.width()}x{rect.height()}")
        self._info_label.setText(f"Info: x={rect.x()} y={rect.y()} w={rect.width()} h={rect.height()}")
        self._context_bar.setVisible(True)

    def _update_status_info(self) -> None:
        if self._doc is None:
            return
        z = int(self._canvas.zoom * 100)
        self._status_zoom.setText(f"{z}%")
        self._status_doc.setText(f"{self._doc.width}x{self._doc.height} | Layers {len(self._doc.layers)}")
        self._status_perf.setText("Timing: realtime")

    def _log_history(self, msg: str) -> None:
        self._history_log.append(msg)
        self._history_list.addItem(msg)
        self._history_list.scrollToBottom()

    def _update_side_panels(self, comp: Image.Image) -> None:
        if self._doc is None:
            return
        lyr = self._doc.active_layer
        if lyr is not None:
            self._props_label.setText(
                f"Layer: {lyr.name}\nVisible: {lyr.visible}\nOpacity: {lyr.opacity}\nSize: {lyr.image.width}x{lyr.image.height}"
            )
        hist = comp.convert("L").histogram()
        total = sum(i * v for i, v in enumerate(hist))
        count = max(1, sum(hist))
        mean = total / count
        self._hist_label.setText(f"Histogram:\nPixels: {count}\nMean Luma: {mean:.1f}")
        self._info_label.setText(f"Info: image {comp.width}x{comp.height}, layers={len(self._doc.layers)}")

    def _panel_set_brush_size(self, v: int) -> None:
        self._brush_size = int(v)
        self.statusBar().showMessage(f"Brush size {self._brush_size}px")

    def _panel_pattern_checker(self) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        self._push_undo()
        im = self._doc.active_layer.image
        draw = ImageDraw.Draw(im)
        c1 = (self._fg.red(), self._fg.green(), self._fg.blue(), 255)
        c2 = (self._bg.red(), self._bg.green(), self._bg.blue(), 255)
        s = 24
        for y in range(0, im.height, s):
            for x in range(0, im.width, s):
                draw.rectangle([x, y, x + s - 1, y + s - 1], fill=c1 if ((x // s + y // s) % 2 == 0) else c2)
        self._sync_ui_from_doc()
        self._log_history("Pattern: Checker")

    def _panel_pattern_stripe(self) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        self._push_undo()
        im = self._doc.active_layer.image
        draw = ImageDraw.Draw(im)
        c1 = (self._fg.red(), self._fg.green(), self._fg.blue(), 255)
        c2 = (self._bg.red(), self._bg.green(), self._bg.blue(), 255)
        s = 16
        for y in range(0, im.height, s):
            draw.rectangle([0, y, im.width - 1, y + s - 1], fill=c1 if (y // s) % 2 == 0 else c2)
        self._sync_ui_from_doc()
        self._log_history("Pattern: Stripe")

    def _panel_run_action(self, item: QListWidgetItem) -> None:
        t = item.text()
        if t == "Auto Tone":
            self._img_auto_tone()
        elif t == "Flatten":
            self._layer_flatten()
        elif t == "Quick Export":
            self._file_quick_export()
        elif t == "Duplicate Layer":
            self._layer_duplicate()

    def _panel_timeline_action(self, item: QListWidgetItem) -> None:
        t = item.text()
        if "Add Frame" in t:
            self._layer_duplicate()
            self._log_history("Timeline: Add Frame")
        elif "Play" in t:
            QMessageBox.information(self, "Timeline", "Preview playback placeholder in this build.")

    def _pick_fg(self) -> None:
        c = QColorDialog.getColor(self._fg, self, "Foreground")
        if c.isValid():
            self._fg = c

    def _pick_bg(self) -> None:
        c = QColorDialog.getColor(self._bg, self, "Background")
        if c.isValid():
            self._bg = c

    # --- Layer ops ---
    def _layer_new(self) -> None:
        if self._doc is None:
            return
        self._push_undo()
        w, h = self._doc.width, self._doc.height
        blank = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        self._doc.layers.append(Layer(blank, f"Layer {len(self._doc.layers) + 1}"))
        self._doc.active_index = len(self._doc.layers) - 1
        self._sync_ui_from_doc()
        self._log_history("New layer")

    def _layer_duplicate(self) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        self._push_undo()
        lyr = self._doc.active_layer
        dup = Layer(lyr.image.copy(), lyr.name + " copy")
        dup.visible = lyr.visible
        dup.opacity = lyr.opacity
        self._doc.layers.insert(self._doc.active_index + 1, dup)
        self._doc.active_index += 1
        self._sync_ui_from_doc()
        self._log_history("Duplicate layer")

    def _layer_delete(self) -> None:
        if self._doc is None or len(self._doc.layers) <= 1:
            return
        self._push_undo()
        del self._doc.layers[self._doc.active_index]
        self._doc.active_index = min(self._doc.active_index, len(self._doc.layers) - 1)
        self._sync_ui_from_doc()
        self._log_history("Delete layer")

    def _apply_crop_rect(self, rect) -> None:
        if self._doc is None:
            return
        self._push_undo()
        r = (rect.x(), rect.y(), rect.x() + rect.width(), rect.y() + rect.height())
        new_layers = []
        for lyr in self._doc.layers:
            im = Image.new("RGBA", (rect.width(), rect.height()), (0, 0, 0, 0))
            im.paste(lyr.image.crop(r), (-rect.x(), -rect.y()))
            lyr.image = im
            new_layers.append(lyr)
        self._doc.layers = new_layers
        self._doc.width, self._doc.height = rect.width(), rect.height()
        self._canvas.crop_mode = False
        self._sync_ui_from_doc()
        self._log_history("Crop")

    def _new_document(self, w: int, h: int, as_new_tab: bool = True) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._doc = Document.new_blank(w, h)
        self._current_path = None
        self._sync_ui_from_doc()
        if as_new_tab:
            self._add_or_replace_doc_tab("Untitled")

    # --- Filters ---
    def _apply_filter(self, fn: Callable[[Image.Image], Image.Image], name: str) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        self._push_undo()
        lyr = self._doc.active_layer
        lyr.image = fn(lyr.image.copy())
        self._last_filter = fn
        self._sync_ui_from_doc()
        self._log_history(name)

    def _repeat_last_filter(self) -> None:
        if self._last_filter:
            self._apply_filter(self._last_filter, "Last filter")

    # --- Menus ---
    def _build_menus(self) -> None:
        mb = self.menuBar()
        self._menu_file(mb.addMenu("&File"))
        self._menu_edit(mb.addMenu("&Edit"))
        self._menu_image(mb.addMenu("&Image"))
        self._menu_layer(mb.addMenu("&Layer"))
        self._menu_type(mb.addMenu("&Type"))
        self._menu_select(mb.addMenu("&Select"))
        self._menu_filter(mb.addMenu("&Filter"))
        self._menu_3d(mb.addMenu("&3D"))
        self._menu_plugins(mb.addMenu("&Plugins"))
        self._menu_view(mb.addMenu("&View"))
        self._menu_window(mb.addMenu("&Window"))
        self._menu_help(mb.addMenu("&Help"))

    def _add(self, menu, title: str, slot, shortcut: Optional[str] = None) -> QAction:
        a = QAction(title, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(slot)
        menu.addAction(a)
        return a

    def _menu_file(self, menu) -> None:
        self._add(menu, "&New…", self._file_new, "Ctrl+N")
        self._add(menu, "&Open…", self._file_open, "Ctrl+O")
        recent = menu.addMenu("Open &Recent")
        self._recent_menu = recent
        self._fill_recent_menu()
        menu.addSeparator()
        self._add(menu, "&Close", self._file_close, "Ctrl+W")
        self._add(menu, "Close All", self._file_close_all)
        menu.addSeparator()
        self._add(menu, "&Save", self._file_save, "Ctrl+S")
        self._add(menu, "Save &As…", self._file_save_as, "Ctrl+Shift+S")
        self._add(menu, "Save a Copy…", self._file_save_copy)
        menu.addSeparator()
        self._add(menu, "&Export…", self._file_export)
        self._add(menu, "Export As…", self._file_export_as)
        self._add(menu, "Quick Export", self._file_quick_export, "Ctrl+Shift+Alt+W")
        self._add(menu, "&Share…", self._file_share)
        menu.addSeparator()
        self._add(menu, "&Automate…", self._file_automate)
        self._add(menu, "&Scripts…", self._file_scripts)
        self._add(menu, "File &Info…", self._file_info)
        menu.addSeparator()
        self._add(menu, "&Print…", self._file_print, "Ctrl+P")
        menu.addSeparator()
        self._add(menu, "E&xit", self.close, "Ctrl+Q")

    def _fill_recent_menu(self) -> None:
        self._recent_menu.clear()
        recent = self._settings.value("recent", []) or []
        if isinstance(recent, str):
            recent = [recent]
        for path in recent[:12]:
            if path and os.path.isfile(path):
                act = QAction(os.path.basename(path), self)
                act.setData(path)
                act.triggered.connect(lambda checked=False, p=path: self._open_path(p))
                self._recent_menu.addAction(act)
        if self._recent_menu.isEmpty():
            self._recent_menu.addAction("(empty)").setEnabled(False)

    def _remember_recent(self, path: str) -> None:
        recent = list(self._settings.value("recent", []) or [])
        if path in recent:
            recent.remove(path)
        recent.insert(0, path)
        self._settings.setValue("recent", recent[:20])
        self._fill_recent_menu()

    def _file_new(self) -> None:
        w, ok1 = QInputDialog.getInt(self, "New", "Width:", 800, 1, 10000)
        if not ok1:
            return
        h, ok2 = QInputDialog.getInt(self, "New", "Height:", 600, 1, 10000)
        if ok2:
            self._new_document(w, h)
            self._log_history("File > New")

    def _file_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open", "", "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tiff);;All (*.*)"
        )
        if path:
            self._open_path(path)

    def _open_path(self, path: str) -> None:
        try:
            img = Image.open(path).convert("RGBA")
        except Exception as e:
            QMessageBox.warning(self, "Open", str(e))
            return
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._doc = Document.from_image(img, os.path.basename(path))
        self._current_path = path
        self._remember_recent(path)
        self._sync_ui_from_doc()
        self._log_history(f"Open {path}")
        self._add_or_replace_doc_tab(os.path.basename(path))

    def _save_active_doc_snapshot(self) -> None:
        if self._doc is None or self._active_doc_index < 0 or self._active_doc_index >= len(self._documents):
            return
        self._documents[self._active_doc_index]["snapshot"] = self._doc.snapshot()

    def _add_or_replace_doc_tab(self, name: str) -> None:
        snap = self._doc.snapshot() if self._doc else None
        if self._active_doc_index == -1 or self._active_doc_index >= len(self._documents):
            self._documents.append({"name": name, "snapshot": snap})
            idx = self._doc_tabs.addTab(name)
            self._active_doc_index = idx
            self._doc_tabs.setCurrentIndex(idx)
            return
        # open into current tab if unnamed, else new tab
        cur_name = self._documents[self._active_doc_index]["name"]
        if cur_name == "Untitled":
            self._documents[self._active_doc_index] = {"name": name, "snapshot": snap}
            self._doc_tabs.setTabText(self._active_doc_index, name)
        else:
            self._save_active_doc_snapshot()
            self._documents.append({"name": name, "snapshot": snap})
            idx = self._doc_tabs.addTab(name)
            self._active_doc_index = idx
            self._doc_tabs.setCurrentIndex(idx)

    def _on_doc_tab_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._documents):
            return
        self._save_active_doc_snapshot()
        self._active_doc_index = index
        snap = self._documents[index].get("snapshot")
        if snap is None:
            return
        self._doc.restore(snap)
        self._sync_ui_from_doc()

    def _file_close(self) -> None:
        self._new_document(800, 600)

    def _file_close_all(self) -> None:
        self._file_close()

    def _file_save(self) -> None:
        if self._current_path:
            self._save_to_path(self._current_path)
        else:
            self._file_save_as()

    def _file_save_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save As", "", "PNG (*.png);;JPEG (*.jpg)")
        if path:
            self._save_to_path(path)
            self._current_path = path
            self._remember_recent(path)

    def _file_save_copy(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save a Copy", "", "PNG (*.png);;JPEG (*.jpg)")
        if path:
            self._save_to_path(path)

    def _save_to_path(self, path: str) -> None:
        if self._doc is None:
            return
        comp = self._doc.composite()
        comp.save(path)
        self._log_history(f"Save {path}")

    def _file_export(self) -> None:
        self._file_export_as()

    def _file_export_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export As", "", "PNG (*.png);;JPEG (*.jpg);;WebP (*.webp)")
        if path and self._doc:
            self._doc.composite().save(path)
            self._log_history("Export")

    def _file_quick_export(self) -> None:
        if self._doc is None:
            return
        path = os.path.join(os.path.expanduser("~"), "quick_export.png")
        self._doc.composite().save(path)
        self.statusBar().showMessage(f"Exported {path}")
        self._log_history("Quick export")

    def _file_share(self) -> None:
        if self._doc is None:
            return
        pil_to_clipboard(self._doc.composite())
        QMessageBox.information(self, "Share", "Composite image copied to clipboard.")

    def _file_automate(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Automate — batch blur folder")
        if not folder or self._last_filter is None:
            QMessageBox.information(self, "Automate", "Run a filter first, then pick a folder to batch-apply the last filter.")
            return
        count = 0
        for name in os.listdir(folder):
            if name.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".webp")):
                p = os.path.join(folder, name)
                try:
                    im = Image.open(p).convert("RGBA")
                    out = self._last_filter(im)
                    base, ext = os.path.splitext(name)
                    out.save(os.path.join(folder, f"{base}_out{ext or '.png'}"))
                    count += 1
                except OSError:
                    pass
        QMessageBox.information(self, "Automate", f"Processed {count} file(s) with last filter.")

    def _file_scripts(self) -> None:
        d = os.path.join(os.path.dirname(__file__), "scripts")
        os.makedirs(d, exist_ok=True)
        QMessageBox.information(self, "Scripts", f"User scripts folder:\n{d}\n\nPlace Python scripts here for future use.")

    def _file_info(self) -> None:
        if self._doc is None:
            return
        comp = self._doc.composite()
        msg = f"Size: {comp.width} × {comp.height}\nMode: {comp.mode}\nLayers: {len(self._doc.layers)}"
        QMessageBox.information(self, "File Info", msg)

    def _file_print(self) -> None:
        if self._doc is None:
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dlg = QPrintDialog(printer, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        pix = QPixmap.fromImage(pil_to_qimage(self._doc.composite()))
        painter = QPainter(printer)
        painter.drawPixmap(0, 0, pix)
        painter.end()

    def _menu_edit(self, menu) -> None:
        self._add(menu, "&Undo", self._edit_undo, "Ctrl+Z")
        self._add(menu, "&Redo", self._edit_redo, "Ctrl+Shift+Z")
        menu.addSeparator()
        self._add(menu, "Cu&t", self._edit_cut, "Ctrl+X")
        self._add(menu, "&Copy", self._edit_copy, "Ctrl+C")
        self._add(menu, "Copy &Merged", self._edit_copy_merged, "Ctrl+Shift+C")
        self._add(menu, "&Paste", self._edit_paste, "Ctrl+V")
        self._add(menu, "Paste in Place", self._edit_paste)
        paste_sp = menu.addMenu("Paste &Special")
        self._add(paste_sp, "Paste as New Layer", self._edit_paste)
        menu.addSeparator()
        self._add(menu, "&Clear", self._edit_clear, "Del")
        menu.addSeparator()
        self._add(menu, "&Search…", self._edit_search)
        self._add(menu, "Check Spelling…", self._edit_spell)
        self._add(menu, "Find and Replace Text…", self._edit_find_text)
        menu.addSeparator()
        self._add(menu, "&Fill…", self._edit_fill)
        self._add(menu, "&Stroke…", self._edit_stroke)
        self._add(menu, "Content-Aware Fill…", self._edit_content_aware)
        menu.addSeparator()
        tr = menu.addMenu("&Transform")
        self._add(tr, "Rotate 180°", self._img_rot_180)
        self._add(tr, "Flip Horizontal", self._img_flip_h)
        self._add(tr, "Flip Vertical", self._img_flip_v)
        self._add(menu, "&Free Transform…", self._edit_free_transform)
        self._add(menu, "Puppet Warp…", self._stub_puppet)
        self._add(menu, "Perspective Warp…", self._stub_perspective)
        menu.addSeparator()
        self._add(menu, "Keyboard Shortcuts…", self._stub_shortcuts)
        self._add(menu, "Menus…", self._stub_menus)
        self._add(menu, "&Preferences…", self._edit_preferences)

    def _edit_cut(self) -> None:
        self._edit_copy()
        self._edit_clear()

    def _edit_copy(self) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        pil_to_clipboard(self._doc.active_layer.image.copy())
        self._log_history("Copy")

    def _edit_copy_merged(self) -> None:
        if self._doc is None:
            return
        pil_to_clipboard(self._doc.composite())
        self._log_history("Copy merged")

    def _edit_paste(self) -> None:
        im = pil_from_clipboard()
        if im is None or self._doc is None:
            return
        self._push_undo()
        w, h = self._doc.width, self._doc.height
        layer_im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        layer_im.paste(im, (0, 0))
        self._doc.layers.append(Layer(layer_im, "Pasted"))
        self._doc.active_index = len(self._doc.layers) - 1
        self._sync_ui_from_doc()
        self._log_history("Paste")

    def _edit_clear(self) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        self._push_undo()
        lyr = self._doc.active_layer
        parts = self._canvas.selection_parts
        if parts:
            for sel in parts:
                if sel.width() > 0 and sel.height() > 0:
                    clear_box = Image.new("RGBA", (sel.width(), sel.height()), (0, 0, 0, 0))
                    lyr.image.paste(clear_box, (sel.x(), sel.y()))
        else:
            lyr.image = Image.new("RGBA", lyr.image.size, (0, 0, 0, 0))
        self._sync_ui_from_doc()
        self._log_history("Clear")

    def _edit_search(self) -> None:
        text, ok = QInputDialog.getText(self, "Search", "Layer name contains:")
        if not ok or not text or self._doc is None:
            return
        for i, lyr in enumerate(self._doc.layers):
            if text.lower() in lyr.name.lower():
                self._layers_list.setCurrentRow(i)
                return
        QMessageBox.information(self, "Search", "No layer matched.")

    def _edit_spell(self) -> None:
        QMessageBox.information(self, "Spelling", "No text layers in this build — spell check skipped.")

    def _edit_find_text(self) -> None:
        QMessageBox.information(self, "Find Text", "Raster-only text in this version. Use Image adjustments instead.")

    def _edit_fill(self) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        self._push_undo()
        lyr = self._doc.active_layer
        c = self._fg
        rgba = (c.red(), c.green(), c.blue(), 255)
        parts = self._canvas.selection_parts
        if parts:
            draw = ImageDraw.Draw(lyr.image)
            ell = self._canvas.selection_shape == "ellipse"
            for sel in parts:
                if sel.width() > 0 and sel.height() > 0:
                    box = [sel.x(), sel.y(), sel.x() + sel.width() - 1, sel.y() + sel.height() - 1]
                    (draw.ellipse if ell else draw.rectangle)(box, fill=rgba)
        else:
            lyr.image = Image.new("RGBA", lyr.image.size, rgba)
        self._sync_ui_from_doc()
        self._log_history("Fill")

    def _edit_stroke(self) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        parts = self._canvas.selection_parts
        if not parts:
            QMessageBox.information(self, "Stroke", "Make a selection first.")
            return
        w, ok = QInputDialog.getInt(self, "Stroke", "Width (px):", 2, 1, 100)
        if not ok:
            return
        self._push_undo()
        lyr = self._doc.active_layer
        c = self._fg
        rgba = (c.red(), c.green(), c.blue(), 255)
        draw = ImageDraw.Draw(lyr.image)
        ell = self._canvas.selection_shape == "ellipse"
        for sel in parts:
            if sel.width() <= 0:
                continue
            for t in range(w):
                box = [sel.x() - t, sel.y() - t, sel.x() + sel.width() - 1 + t, sel.y() + sel.height() - 1 + t]
                (draw.ellipse if ell else draw.rectangle)(box, outline=rgba)
        self._sync_ui_from_doc()
        self._log_history("Stroke")

    def _edit_content_aware(self) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        sel = self._canvas.selection_bounds()
        if not sel or sel.width() <= 0:
            QMessageBox.information(self, "Content-Aware", "Select a region first.")
            return
        self._push_undo()
        lyr = self._doc.active_layer
        region = lyr.image.crop((sel.x(), sel.y(), sel.x() + sel.width(), sel.y() + sel.height()))
        blurred = region.filter(ImageFilter.GaussianBlur(radius=8))
        lyr.image.paste(blurred, (sel.x(), sel.y()))
        self._sync_ui_from_doc()
        self._log_history("Content-Aware Fill (approx)")

    def _edit_free_transform(self) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        s, ok = QInputDialog.getDouble(self, "Free Transform", "Uniform scale:", 1.0, 0.01, 10.0, 3)
        if not ok:
            return
        self._push_undo()
        lyr = self._doc.active_layer
        nw = max(1, int(lyr.image.width * s))
        nh = max(1, int(lyr.image.height * s))
        lyr.image = lyr.image.resize((nw, nh), Image.Resampling.LANCZOS)
        self._sync_ui_from_doc()
        self._log_history("Free Transform")

    def _stub_puppet(self) -> None:
        QMessageBox.information(self, "Puppet Warp", "Full puppet warp is not implemented. Use Edit > Transform.")

    def _stub_perspective(self) -> None:
        QMessageBox.information(self, "Perspective Warp", "Not implemented in this Python build.")

    def _stub_shortcuts(self) -> None:
        QMessageBox.information(self, "Shortcuts", "Ctrl+N/O/S, Ctrl+Z/Shift+Z, filters via Filter menu, V/H pan with Space+drag.")

    def _stub_menus(self) -> None:
        QMessageBox.information(self, "Menus", "All menus are visible; customize is not persisted.")

    def _edit_preferences(self) -> None:
        QMessageBox.information(self, "Preferences", f"Undo limit: {UNDO_LIMIT}\nPlatform: {platform.system()}")

    def _menu_image(self, menu) -> None:
        mode = menu.addMenu("&Mode")
        self._add(mode, "&RGB Color", self._img_mode_rgb)
        self._add(mode, "&Grayscale", self._img_mode_gray)
        adj = menu.addMenu("&Adjustments")
        self._add(adj, "&Brightness/Contrast…", self._img_adj_bc)
        self._add(adj, "&Hue/Saturation…", self._img_adj_hue)
        menu.addSeparator()
        self._add(menu, "&Auto Tone", self._img_auto_tone)
        self._add(menu, "Auto &Contrast", self._img_auto_contrast)
        self._add(menu, "Auto &Color", self._img_auto_color)
        menu.addSeparator()
        self._add(menu, "&Image Size…", self._img_size)
        self._add(menu, "&Canvas Size…", self._img_canvas_size)
        rot = menu.addMenu("&Image Rotation")
        self._add(rot, "90° CW", partial(self._img_rotate, -90))
        self._add(rot, "90° CCW", partial(self._img_rotate, 90))
        self._add(rot, "180°", self._img_rot_180)
        menu.addSeparator()
        self._add(menu, "&Crop", self._img_crop_tool)
        self._add(menu, "&Trim…", self._img_trim)
        self._add(menu, "Reveal All", self._img_reveal_all)
        self._add(menu, "&Duplicate", self._img_duplicate)
        menu.addSeparator()
        self._add(menu, "&Apply Image…", self._img_apply_image)
        self._add(menu, "&Calculations…", self._img_calculations)

    def _apply_to_composite_flat(self, fn: Callable[[Image.Image], Image.Image], log: str) -> None:
        if self._doc is None:
            return
        self._push_undo()
        comp = fn(self._doc.composite())
        self._doc = Document.from_image(comp, "Merged")
        self._sync_ui_from_doc()
        self._log_history(log)

    def _img_mode_rgb(self) -> None:
        if self._doc and self._doc.active_layer:
            self._push_undo()
            self._doc.active_layer.image = ops.rgb_mode(self._doc.active_layer.image)
            self._sync_ui_from_doc()
            self._log_history("Mode RGB")

    def _img_mode_gray(self) -> None:
        if self._doc and self._doc.active_layer:
            self._push_undo()
            self._doc.active_layer.image = ops.grayscale_mode(self._doc.active_layer.image)
            self._sync_ui_from_doc()
            self._log_history("Mode Grayscale")

    def _img_adj_bc(self) -> None:
        b, ok1 = QInputDialog.getDouble(self, "Brightness", "Factor:", 1.0, 0.0, 3.0, 2)
        if not ok1:
            return
        c, ok2 = QInputDialog.getDouble(self, "Contrast", "Factor:", 1.0, 0.0, 3.0, 2)
        if not ok2:
            return

        def f(im: Image.Image) -> Image.Image:
            return ops.adjust_contrast(ops.adjust_brightness(im, b), c)

        self._apply_to_composite_flat(f, "Brightness/Contrast")

    def _img_adj_hue(self) -> None:
        s, ok = QInputDialog.getDouble(self, "Saturation", "Factor:", 1.0, 0.0, 3.0, 2)
        if ok:
            self._apply_to_composite_flat(lambda im: ops.adjust_saturation(im, s), "Hue/Saturation")

    def _img_auto_tone(self) -> None:
        self._apply_to_composite_flat(ops.auto_tone, "Auto Tone")

    def _img_auto_contrast(self) -> None:
        self._apply_to_composite_flat(ops.auto_contrast, "Auto Contrast")

    def _img_auto_color(self) -> None:
        self._apply_to_composite_flat(ops.auto_color, "Auto Color")

    def _img_size(self) -> None:
        if self._doc is None:
            return
        w, ok1 = QInputDialog.getInt(self, "Image Size", "Width:", self._doc.width, 1, 20000)
        if not ok1:
            return
        h, ok2 = QInputDialog.getInt(self, "Image Size", "Height:", self._doc.height, 1, 20000)
        if ok2:
            self._push_undo()
            comp = self._doc.composite().resize((w, h), Image.Resampling.LANCZOS)
            self._doc = Document.from_image(comp, "Resized")
            self._sync_ui_from_doc()
            self._log_history("Image Size")

    def _img_canvas_size(self) -> None:
        if self._doc is None:
            return
        w, ok1 = QInputDialog.getInt(self, "Canvas Size", "Width:", self._doc.width, 1, 20000)
        if not ok1:
            return
        h, ok2 = QInputDialog.getInt(self, "Canvas Size", "Height:", self._doc.height, 1, 20000)
        if not ok2:
            return
        self._push_undo()
        comp = self._doc.composite()
        new_im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        new_im.paste(comp, (0, 0))
        self._doc = Document.from_image(new_im, "Canvas")
        self._sync_ui_from_doc()
        self._log_history("Canvas Size")

    def _img_rotate(self, angle: int) -> None:
        if self._doc is None:
            return
        self._push_undo()
        comp = self._doc.composite().rotate(angle, expand=True, fillcolor=(0, 0, 0, 0))
        self._doc = Document.from_image(comp, "Rotated")
        self._sync_ui_from_doc()
        self._log_history(f"Rotate {angle}°")

    def _img_rot_180(self) -> None:
        self._img_rotate(180)

    def _img_flip_h(self) -> None:
        if self._doc is None:
            return
        self._push_undo()
        comp = self._doc.composite().transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        self._doc = Document.from_image(comp, "Flipped")
        self._sync_ui_from_doc()
        self._log_history("Flip H")

    def _img_flip_v(self) -> None:
        if self._doc is None:
            return
        self._push_undo()
        comp = self._doc.composite().transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        self._doc = Document.from_image(comp, "Flipped")
        self._sync_ui_from_doc()
        self._log_history("Flip V")

    def _img_crop_tool(self) -> None:
        self._canvas.crop_mode = True
        self.statusBar().showMessage("Drag a crop rectangle on the canvas.")

    def _img_trim(self) -> None:
        if self._doc is None:
            return
        self._push_undo()
        comp = self._doc.composite()
        bbox = comp.getbbox()
        if bbox:
            comp = comp.crop(bbox)
            self._doc = Document.from_image(comp, "Trimmed")
            self._sync_ui_from_doc()
            self._log_history("Trim")

    def _img_reveal_all(self) -> None:
        self._canvas.fit_zoom()
        self._log_history("Reveal All (fit zoom)")

    def _img_duplicate(self) -> None:
        if self._doc is None:
            return
        self._push_undo()
        comp = self._doc.composite()
        self._doc.layers.append(Layer(comp.copy(), "Duplicate"))
        self._doc.active_index = len(self._doc.layers) - 1
        self._sync_ui_from_doc()
        self._log_history("Duplicate (layer from composite)")

    def _img_apply_image(self) -> None:
        if self._doc is None or len(self._doc.layers) < 2:
            QMessageBox.information(self, "Apply Image", "Need at least two layers.")
            return
        names = [lyr.name for lyr in self._doc.layers]
        s1, ok1 = QInputDialog.getItem(self, "Apply Image", "Source:", names, self._doc.active_index, False)
        if not ok1:
            return
        s2, ok2 = QInputDialog.getItem(self, "Apply Image", "Blend with:", names, 0, False)
        if not ok2:
            return
        i1, i2 = names.index(s1), names.index(s2)
        self._push_undo()
        a = self._doc.layers[i1].image.convert("RGBA")
        b = self._doc.layers[i2].image.convert("RGBA")
        w = max(a.width, b.width)
        h = max(a.height, b.height)
        aa = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        bb = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        aa.paste(a, (0, 0))
        bb.paste(b, (0, 0))
        blended = Image.blend(aa, bb, 0.5)
        self._doc.layers[i1].image = blended
        self._doc.active_index = i1
        self._sync_ui_from_doc()
        self._log_history("Apply Image")

    def _img_calculations(self) -> None:
        self._img_apply_image()
        self._log_history("Calculations (blend)")

    # --- Layer menu ---
    def _menu_layer(self, menu) -> None:
        self._add(menu, "&New…", self._layer_new)
        self._add(menu, "&Duplicate Layer", self._layer_duplicate)
        self._add(menu, "&Delete", self._layer_delete)
        self._add(menu, "&Rename Layer…", self._layer_rename)
        self._add(menu, "Layer &Properties…", self._layer_props)
        self._add(menu, "Layer &Style…", self._layer_style)
        menu.addSeparator()
        self._add(menu, "New &Fill Layer…", self._layer_new_fill)
        self._add(menu, "New &Adjustment Layer…", self._layer_new_adj)
        menu.addSeparator()
        self._add(menu, "Layer &Mask…", self._layer_mask_stub)
        self._add(menu, "&Vector Mask…", self._layer_vector_stub)
        self._add(menu, "Create &Clipping Mask", self._layer_clipping_stub)
        self._add(menu, "&Smart Objects", self._layer_smart_stub)
        self._add(menu, "&Video Layers", self._layer_video_stub)
        menu.addSeparator()
        self._add(menu, "&Rasterize", self._layer_rasterize)
        menu.addSeparator()
        self._add(menu, "&Group Layers", self._layer_group_stub)
        self._add(menu, "&Ungroup Layers", self._layer_ungroup_stub)
        self._add(menu, "&Arrange", self._layer_arrange_stub)
        self._add(menu, "&Align", self._layer_align_stub)
        self._add(menu, "&Distribute", self._layer_distribute_stub)
        menu.addSeparator()
        self._add(menu, "&Merge Layers", self._layer_merge)
        self._add(menu, "Merge &Visible", self._layer_merge_visible)
        self._add(menu, "&Flatten Image", self._layer_flatten)

    def _layer_rename(self) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        name, ok = QInputDialog.getText(self, "Rename", "Layer name:", self._doc.active_layer.name)
        if ok and name:
            self._push_undo()
            self._doc.active_layer.name = name
            self._sync_ui_from_doc()

    def _layer_props(self) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        lyr = self._doc.active_layer
        o, ok = QInputDialog.getInt(self, "Opacity", "0-255:", lyr.opacity, 0, 255)
        if ok:
            self._push_undo()
            lyr.opacity = o
            self._sync_ui_from_doc()

    def _layer_style(self) -> None:
        QMessageBox.information(self, "Layer Style", "Drop shadow / stroke: use Edit > Stroke on selection.")

    def _layer_new_fill(self) -> None:
        self._layer_new()
        if self._doc and self._doc.active_layer:
            c = self._bg
            self._doc.active_layer.image = Image.new(
                "RGBA",
                self._doc.active_layer.image.size,
                (c.red(), c.green(), c.blue(), 255),
            )
            self._sync_ui_from_doc()

    def _layer_new_adj(self) -> None:
        v, ok = QInputDialog.getDouble(self, "Adjustment", "Brightness factor:", 1.1, 0.1, 3.0, 2)
        if ok and self._doc and self._doc.active_layer:
            self._push_undo()
            self._doc.active_layer.image = ops.adjust_brightness(self._doc.active_layer.image, v)
            self._sync_ui_from_doc()

    def _layer_mask_stub(self) -> None:
        QMessageBox.information(self, "Layer Mask", "Use selection + Clear for simple masking in this build.")

    def _layer_vector_stub(self) -> None:
        QMessageBox.information(self, "Vector Mask", "Raster editor only.")

    def _layer_clipping_stub(self) -> None:
        QMessageBox.information(self, "Clipping Mask", "Not implemented.")

    def _layer_smart_stub(self) -> None:
        QMessageBox.information(self, "Smart Objects", "Not implemented.")

    def _layer_video_stub(self) -> None:
        QMessageBox.information(self, "Video Layers", "Not implemented.")

    def _layer_rasterize(self) -> None:
        QMessageBox.information(self, "Rasterize", "All layers are raster in this app.")

    def _layer_group_stub(self) -> None:
        QMessageBox.information(self, "Group", "Flatten or merge layers as a workaround.")

    def _layer_ungroup_stub(self) -> None:
        pass

    def _layer_arrange_stub(self) -> None:
        QMessageBox.information(self, "Arrange", "Reorder layers via the Layers panel (future: drag-drop).")

    def _layer_align_stub(self) -> None:
        QMessageBox.information(self, "Align", "Not implemented for multi-layer geometry.")

    def _layer_distribute_stub(self) -> None:
        QMessageBox.information(self, "Distribute", "Not implemented.")

    def _layer_merge(self) -> None:
        if self._doc is None or self._doc.active_index < 1:
            return
        self._push_undo()
        i = self._doc.active_index
        down = self._doc.layers[i - 1]
        up = self._doc.layers[i]
        w = max(down.image.width, up.image.width)
        h = max(down.image.height, up.image.height)
        base = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        base.paste(down.image, (0, 0))
        up_im = up.image.convert("RGBA")
        if up_im.size != base.size:
            tmp = Image.new("RGBA", base.size, (0, 0, 0, 0))
            tmp.paste(up_im, (0, 0))
            up_im = tmp
        down.image = Image.alpha_composite(base, up_im)
        del self._doc.layers[i]
        self._doc.active_index = i - 1
        self._sync_ui_from_doc()
        self._log_history("Merge layers")

    def _layer_merge_visible(self) -> None:
        if self._doc is None:
            return
        self._push_undo()
        comp = self._doc.composite()
        self._doc.layers = [Layer(comp, "Merged")]
        self._doc.active_index = 0
        self._sync_ui_from_doc()
        self._log_history("Merge visible")

    def _layer_flatten(self) -> None:
        if self._doc is None:
            return
        self._push_undo()
        comp = self._doc.composite()
        self._doc.layers = [Layer(comp, "Background")]
        self._doc.active_index = 0
        self._sync_ui_from_doc()
        self._log_history("Flatten")

    # --- Type ---
    def _menu_type(self, menu) -> None:
        self._add(menu, "&Panels…", self._type_panels)
        self._add(menu, "Font Preview Size", self._type_font_preview)
        self._add(menu, "Language Options…", self._type_lang)
        self._add(menu, "Anti-Alias", self._type_aa)
        self._add(menu, "Orientation", self._type_orient)
        menu.addSeparator()
        self._add(menu, "Convert to Shape", self._type_shape)
        self._add(menu, "Rasterize Type Layer", self._type_raster_type)
        self._add(menu, "Create Work Path", self._type_work_path)
        self._add(menu, "Warp Text…", self._type_warp)
        self._add(menu, "Update All Text Layers", self._type_update)
        self._add(menu, "Replace All Missing Fonts", self._type_fonts)
        self._add(menu, "Paste Lorem Ipsum", self._type_lorem)
        self._add(menu, "Match Font…", self._type_match)

    def _type_panels(self) -> None:
        QMessageBox.information(self, "Type Panels", "Character/Paragraph: use toolbar colors + Edit > Fill for raster text.")

    def _type_font_preview(self) -> None:
        QMessageBox.information(self, "Font Preview", "System fonts available via OS; raster text tool not in menu.")

    def _type_lang(self) -> None:
        QMessageBox.information(self, "Language", f"Locale: {platform.locale.getlocale()}")

    def _type_aa(self) -> None:
        QMessageBox.information(self, "Anti-Alias", "Canvas uses smooth scaling when zoomed.")

    def _type_orient(self) -> None:
        QMessageBox.information(self, "Orientation", "Horizontal text only in this build.")

    def _type_shape(self) -> None:
        QMessageBox.information(self, "Convert to Shape", "Not implemented.")

    def _type_raster_type(self) -> None:
        self._layer_rasterize()

    def _type_work_path(self) -> None:
        QMessageBox.information(self, "Work Path", "Not implemented.")

    def _type_warp(self) -> None:
        QMessageBox.information(self, "Warp Text", "Not implemented.")

    def _type_update(self) -> None:
        self._refresh_canvas()

    def _type_fonts(self) -> None:
        QMessageBox.information(self, "Fonts", "No missing-font tracking.")

    def _type_lorem(self) -> None:
        if self._doc is None:
            return
        from PIL import ImageFont

        self._layer_new()
        lyr = self._doc.active_layer
        draw = ImageDraw.Draw(lyr.image)
        text = "Lorem ipsum dolor sit amet, consectetur adipiscing elit."
        try:
            font = ImageFont.truetype("arial.ttf", 24)
        except OSError:
            font = ImageFont.load_default()
        draw.text((20, 20), text, fill=(0, 0, 0, 255), font=font)
        self._sync_ui_from_doc()
        self._log_history("Lorem Ipsum")

    def _type_match(self) -> None:
        QMessageBox.information(self, "Match Font", "Not implemented.")

    # --- Select ---
    def _menu_select(self, menu) -> None:
        self._add(menu, "&All", self._sel_all)
        self._add(menu, "&Deselect", self._sel_none, "Ctrl+D")
        self._add(menu, "&Reselect", self._sel_reselect, "Ctrl+Shift+D")
        self._add(menu, "&Inverse", self._sel_inverse, "Ctrl+Shift+I")
        menu.addSeparator()
        self._add(menu, "All Layers", self._sel_all_layers)
        self._add(menu, "Deselect Layers", self._sel_deselect_layers)
        self._add(menu, "Find Layers…", self._edit_search)
        menu.addSeparator()
        self._add(menu, "&Color Range…", self._sel_color_range)
        self._add(menu, "Focus Area…", self._sel_focus)
        self._add(menu, "&Subject", self._sel_subject)
        self._add(menu, "&Sky", self._sel_sky)
        self._add(menu, "Select and Mask…", self._sel_mask)
        mod = menu.addMenu("&Modify")
        self._add(mod, "&Expand…", partial(self._sel_modify, 1))
        self._add(mod, "&Contract…", partial(self._sel_modify, -1))
        self._add(menu, "&Grow…", self._sel_grow)
        self._add(menu, "&Similar…", self._sel_similar)
        self._add(menu, "&Transform Selection…", self._sel_transform)
        self._add(menu, "Edit in Quick Mask Mode", self._sel_quick_mask)
        menu.addSeparator()
        self._add(menu, "&Load Selection…", self._sel_load)
        self._add(menu, "&Save Selection…", self._sel_save)

    def _sel_all(self) -> None:
        if self._canvas._qimage is None:
            return
        self._canvas.selection_parts = [QRect(0, 0, self._canvas._qimage.width(), self._canvas._qimage.height())]
        self._canvas.update()

    def _sel_none(self) -> None:
        self._canvas.selection_parts = []
        self._canvas.update()

    def _sel_reselect(self) -> None:
        self._canvas.update()

    def _sel_inverse(self) -> None:
        if self._canvas._qimage is None:
            return
        full = QRect(0, 0, self._canvas._qimage.width(), self._canvas._qimage.height())
        b = self._canvas.selection_bounds()
        if b is None or b.isEmpty():
            self._canvas.selection_parts = [full]
        else:
            self._canvas.selection_parts = subtract_rect(full, b)
        self._canvas.update()

    def _sel_all_layers(self) -> None:
        for i in range(self._layers_list.count()):
            self._layers_list.item(i).setSelected(True)

    def _sel_deselect_layers(self) -> None:
        self._layers_list.clearSelection()

    def _sel_color_range(self) -> None:
        QMessageBox.information(self, "Color Range", "Pick a tolerance; full implementation would threshold by color.")
        self._sel_all()

    def _sel_focus(self) -> None:
        self._sel_subject()

    def _sel_subject(self) -> None:
        self._safe_subject_selection()

    def _safe_subject_selection(self) -> None:
        """Robust object/subject selection without risky native filters."""
        if self._doc is None:
            return
        comp = self._doc.composite().convert("RGBA")
        bbox = comp.getbbox()
        if bbox is None:
            # fallback: full image if all transparent / unknown
            self._canvas.selection_parts = [QRect(0, 0, self._doc.width, self._doc.height)]
        else:
            x0, y0, x1, y1 = bbox
            self._canvas.selection_parts = [QRect(x0, y0, max(1, x1 - x0), max(1, y1 - y0))]
        self._canvas.selection_shape = "rect"
        self._canvas.update()

    def _sel_sky(self) -> None:
        if self._doc is None:
            return
        h = max(1, self._doc.height // 3)
        self._canvas.selection_parts = [QRect(0, 0, self._doc.width, h)]
        self._canvas.update()

    def _sel_mask(self) -> None:
        self._canvas.quick_mask = not self._canvas.quick_mask
        self._canvas.update()

    def _sel_modify(self, delta: int) -> None:
        if not self._canvas.selection_parts:
            return
        self._canvas.selection_parts = [
            QRect(r.x() - delta, r.y() - delta, r.width() + 2 * delta, r.height() + 2 * delta).normalized()
            for r in self._canvas.selection_parts
        ]
        self._canvas.update()

    def _sel_grow(self) -> None:
        self._sel_modify(2)

    def _sel_similar(self) -> None:
        self._sel_subject()

    def _sel_transform(self) -> None:
        QMessageBox.information(self, "Transform Selection", "Resize canvas selection: use Modify > Expand/Contract.")

    def _sel_quick_mask(self) -> None:
        self._sel_mask()

    def _sel_load(self) -> None:
        QMessageBox.information(self, "Load Selection", "Not persisted; use Select > All.")

    def _sel_save(self) -> None:
        QMessageBox.information(self, "Save Selection", "Not persisted in this build.")

    # --- Filter ---
    def _menu_filter(self, menu) -> None:
        self._add(menu, "Last Filter", self._repeat_last_filter, "Ctrl+F")
        self._add(menu, "Convert for Smart Filters", self._stub_smart_filters)
        self._add(menu, "Filter Gallery…", self._filter_gallery)
        menu.addSeparator()
        self._add(menu, "Camera Raw Filter…", self._stub_camera_raw)
        self._add(menu, "Adaptive Wide Angle…", self._stub_adaptive)
        self._add(menu, "Lens Correction…", partial(self._apply_filter, ops.lens_distort, "Lens Correction"))
        self._add(menu, "Liquify…", self._filter_liquify)
        self._add(menu, "Neural Filters…", self._stub_neural)
        self._add(menu, "Vanishing Point…", self._stub_vanishing)
        blur = menu.addMenu("&Blur")
        self._add(blur, "&Gaussian Blur…", self._filter_gaussian)
        self._add(blur, "&Box Blur…", self._filter_box)
        self._add(blur, "&Motion Blur…", self._filter_motion)
        bg = menu.addMenu("Blur &Gallery")
        self._add(bg, "Field Blur (approx)", partial(self._apply_filter, lambda im: ops.gaussian_blur(im, 3), "Field Blur"))
        self._add(bg, "Iris Blur (approx)", partial(self._apply_filter, lambda im: ops.gaussian_blur(im, 5), "Iris Blur"))
        dist = menu.addMenu("&Distort")
        self._add(dist, "&Ripple (approx)", self._filter_ripple)
        self._add(dist, "&Spherize (approx)", partial(self._apply_filter, ops.lens_distort, "Spherize"))
        self._add(dist, "&Wave (approx)", self._filter_wave)
        noise = menu.addMenu("&Noise")
        self._add(noise, "&Add Noise…", self._filter_noise)
        self._add(noise, "&Despeckle", partial(self._apply_filter, lambda im: im.filter(ImageFilter.MedianFilter(size=3)), "Despeckle"))
        pix = menu.addMenu("&Pixelate")
        self._add(pix, "&Mosaic…", self._filter_mosaic)
        self._add(pix, "&Color Halftone…", self._filter_halftone)
        ren = menu.addMenu("&Render")
        self._add(ren, "&Clouds", self._filter_clouds)
        self._add(ren, "&Difference Clouds", self._filter_diff_clouds)
        sharp = menu.addMenu("&Sharpen")
        self._add(sharp, "&Sharpen", partial(self._apply_filter, ops.sharpen, "Sharpen"))
        self._add(sharp, "Sharpen &More", partial(self._apply_filter, ops.unsharp_mask, "Sharpen More"))
        self._add(sharp, "&Edge Sharpen", partial(self._apply_filter, ops.edge_enhance, "Edge Sharpen"))
        sty = menu.addMenu("&Stylize")
        self._add(sty, "&Emboss", partial(self._apply_filter, ops.emboss, "Emboss"))
        self._add(sty, "&Find Edges", partial(self._apply_filter, ops.find_edges, "Find Edges"))
        self._add(sty, "&Solarize…", self._filter_solarize)
        self._add(sty, "&Posterize…", self._filter_posterize)
        oth = menu.addMenu("&Other")
        self._add(oth, "&High Pass", self._filter_highpass)
        self._add(oth, "&Offset…", self._filter_offset)

    def _stub_smart_filters(self) -> None:
        QMessageBox.information(self, "Smart Filters", "Filters apply to the active raster layer.")

    def _filter_gallery(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Filter Gallery")
        v = QVBoxLayout(dlg)
        lst = QListWidget()
        for name, fn in [
            ("Gaussian Blur", lambda: self._apply_filter(lambda im: ops.gaussian_blur(im, 2), "Gallery: Blur")),
            ("Sharpen", lambda: self._apply_filter(ops.sharpen, "Gallery: Sharpen")),
            ("Emboss", lambda: self._apply_filter(ops.emboss, "Gallery: Emboss")),
            ("Posterize", lambda: self._apply_filter(lambda im: ops.posterize(im, 3), "Gallery: Posterize")),
            ("Pixelate", lambda: self._apply_filter(lambda im: ops.pixelate(im, 12), "Gallery: Pixelate")),
        ]:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, fn)
            lst.addItem(item)
        v.addWidget(lst)

        def run() -> None:
            it = lst.currentItem()
            if it:
                it.data(Qt.ItemDataRole.UserRole)()
                dlg.accept()

        b = QPushButton("Apply")
        b.clicked.connect(run)
        v.addWidget(b)
        dlg.exec()

    def _stub_camera_raw(self) -> None:
        self._apply_filter(lambda im: ops.adjust_contrast(ops.auto_tone(im), 1.05), "Camera Raw (approx)")

    def _stub_adaptive(self) -> None:
        QMessageBox.information(self, "Adaptive Wide Angle", "Use Lens Correction for a simple warp.")

    def _filter_liquify(self) -> None:
        self._apply_filter(lambda im: ops.gaussian_blur(ops.lens_distort(im, 0.4), 0.5), "Liquify (approx)")

    def _stub_neural(self) -> None:
        QMessageBox.information(self, "Neural Filters", "Not available — try Filter > Stylize.")

    def _stub_vanishing(self) -> None:
        QMessageBox.information(self, "Vanishing Point", "Not implemented.")

    def _filter_gaussian(self) -> None:
        r, ok = QInputDialog.getDouble(self, "Gaussian Blur", "Radius:", 2.0, 0.0, 50.0, 1)
        if ok:
            self._apply_filter(lambda im: ops.gaussian_blur(im, r), "Gaussian Blur")

    def _filter_box(self) -> None:
        r, ok = QInputDialog.getInt(self, "Box Blur", "Radius:", 2, 0, 50)
        if ok:
            self._apply_filter(lambda im: ops.box_blur(im, r), "Box Blur")

    def _filter_motion(self) -> None:
        s, ok = QInputDialog.getInt(self, "Motion Blur", "Length:", 10, 3, 99)
        if ok:
            self._apply_filter(lambda im: ops.motion_blur(im, s), "Motion Blur")

    def _filter_ripple(self) -> None:
        def ripple(im: Image.Image) -> Image.Image:
            im = ops.ensure_rgba(im)
            return ImageChops.offset(im, 4, int(4 * math.sin(0.1)))

        self._apply_filter(ripple, "Ripple")

    def _filter_wave(self) -> None:
        self._filter_ripple()

    def _filter_noise(self) -> None:
        a, ok = QInputDialog.getInt(self, "Noise", "Amount 1-50:", 15, 1, 50)
        if ok:
            self._apply_filter(lambda im: ops.add_noise_pil_only(im, a), "Noise")

    def _filter_mosaic(self) -> None:
        b, ok = QInputDialog.getInt(self, "Mosaic", "Cell size:", 10, 2, 100)
        if ok:
            self._apply_filter(lambda im: ops.pixelate(im, b), "Mosaic")

    def _filter_halftone(self) -> None:
        self._apply_filter(lambda im: ops.posterize(im, 2), "Color Halftone (approx)")

    def _filter_clouds(self) -> None:
        if self._doc is None:
            return
        self._layer_new()
        if self._doc.active_layer:
            clouds = ops.simple_clouds((self._doc.width, self._doc.height))
            self._doc.active_layer.image = clouds
            self._sync_ui_from_doc()
            self._log_history("Clouds")

    def _filter_diff_clouds(self) -> None:
        if self._doc is None or self._doc.active_layer is None:
            return
        self._push_undo()
        lyr = self._doc.active_layer
        c2 = ops.simple_clouds(lyr.image.size, seed=2)
        lyr.image = ImageChops.difference(ops.ensure_rgba(lyr.image), c2)
        self._sync_ui_from_doc()
        self._log_history("Difference Clouds")

    def _filter_solarize(self) -> None:
        t, ok = QInputDialog.getInt(self, "Solarize", "Threshold:", 128, 0, 255)
        if ok:
            self._apply_filter(lambda im: ops.solarize(im, t), "Solarize")

    def _filter_posterize(self) -> None:
        b, ok = QInputDialog.getInt(self, "Posterize", "Bits:", 4, 1, 8)
        if ok:
            self._apply_filter(lambda im: ops.posterize(im, b), "Posterize")

    def _filter_highpass(self) -> None:
        def hp(im: Image.Image) -> Image.Image:
            im = ops.ensure_rgba(im)
            low = ops.gaussian_blur(im, 2)
            return ImageChops.subtract(im, low)

        self._apply_filter(hp, "High Pass")

    def _filter_offset(self) -> None:
        dx, ok1 = QInputDialog.getInt(self, "Offset", "DX:", 10, -2000, 2000)
        if not ok1:
            return
        dy, ok2 = QInputDialog.getInt(self, "Offset", "DY:", 0, -2000, 2000)
        if ok2:
            self._apply_filter(lambda im: ImageChops.offset(ops.ensure_rgba(im), dx, dy), "Offset")

    # --- 3D ---
    def _menu_3d(self, menu) -> None:
        self._add(menu, "New 3D Layer", self._3d_new)
        self._add(menu, "New Mesh from Layer", self._3d_mesh)
        self._add(menu, "New 3D Extrusion", self._3d_extrude)
        self._add(menu, "Render 3D Layer", self._3d_render)
        self._add(menu, "3D Print Settings…", self._3d_print)
        self._add(menu, "Export 3D Layer…", self._3d_export)

    def _3d_fake(self, title: str) -> None:
        if self._doc is None:
            return
        def f(im: Image.Image) -> Image.Image:
            im = ops.ensure_rgba(im)
            shadow = Image.new("RGBA", im.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(shadow)
            d.rectangle([8, 8, im.width - 1, im.height - 1], outline=(40, 40, 40, 180), width=4)
            return Image.alpha_composite(im, shadow)

        self._apply_filter(f, title)

    def _3d_new(self) -> None:
        self._3d_fake("3D Layer (fake lighting)")

    def _3d_mesh(self) -> None:
        self._3d_fake("Mesh from Layer")

    def _3d_extrude(self) -> None:
        self._apply_filter(lambda im: ImageChops.offset(ops.ensure_rgba(im), 6, 6), "3D Extrusion (offset)")

    def _3d_render(self) -> None:
        QMessageBox.information(self, "Render 3D", "No GPU path — applied drop-outline on active layer instead.")
        self._3d_fake("Render 3D")

    def _3d_print(self) -> None:
        QMessageBox.information(self, "3D Print", "Export STL not implemented.")

    def _3d_export(self) -> None:
        self._file_export_as()

    # --- Plugins ---
    def _menu_plugins(self, menu) -> None:
        self._add(menu, "Browse Plugins…", self._plug_browse)
        self._add(menu, "Manage Plugins…", self._plug_manage)
        self._add(menu, "Plugin Panels", self._plug_panels)

    def _plug_browse(self) -> None:
        d = os.path.join(os.path.dirname(__file__), "plugins")
        os.makedirs(d, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(d)
        elif sys.platform == "darwin":
            os.system(f'open "{d}"')
        else:
            os.system(f'xdg-open "{d}"')

    def _plug_manage(self) -> None:
        QMessageBox.information(self, "Plugins", "Drop Python scripts in the plugins folder.")

    def _plug_panels(self) -> None:
        QMessageBox.information(self, "Plugin Panels", "Use Window menu for built-in panels.")

    # --- View ---
    def _menu_view(self, menu) -> None:
        self._add(menu, "Proof Setup", self._view_proof_setup)
        self._add(menu, "Proof Colors", self._view_proof_colors)
        self._add(menu, "Gamut Warning", self._view_gamut)
        menu.addSeparator()
        self._add(menu, "Zoom In", partial(self._zoom, 1.2), "Ctrl++")
        self._add(menu, "Zoom Out", partial(self._zoom, 1 / 1.2), "Ctrl+-")
        self._add(menu, "Fit on Screen", self._view_fit, "Ctrl+0")
        self._add(menu, "100%", self._view_100, "Ctrl+1")
        self._add(menu, "200%", self._view_200)
        self._add(menu, "Screen Mode", self._view_screen_mode)
        menu.addSeparator()
        self._add(menu, "Extras", self._view_extras)
        self._add(menu, "Show", self._view_show)
        self._add(menu, "Rulers", self._view_rulers)
        self._add(menu, "Snap", self._view_snap)
        self._add(menu, "Snap To", self._view_snap_to)
        menu.addSeparator()
        self._add(menu, "Lock Guides", self._view_lock_guides)
        self._add(menu, "Clear Guides", self._view_clear_guides)
        self._add(menu, "New Guide…", self._view_new_guide)
        self._add(menu, "New Guide Layout…", self._view_guide_layout)

    def _zoom(self, factor: float) -> None:
        self._canvas.zoom = max(0.05, min(32.0, self._canvas.zoom * factor))
        self._canvas.update()
        self.statusBar().showMessage(f"Zoom {int(self._canvas.zoom * 100)}%")

    def _view_fit(self) -> None:
        self._canvas.fit_zoom()

    def _view_100(self) -> None:
        self._canvas.actual_size_zoom()

    def _view_200(self) -> None:
        self._canvas.zoom = 2.0
        self._canvas.update()

    def _view_proof_setup(self) -> None:
        QMessageBox.information(self, "Proof Setup", "sRGB assumed.")

    def _view_proof_colors(self) -> None:
        QMessageBox.information(self, "Proof Colors", "Toggle not implemented.")

    def _view_gamut(self) -> None:
        QMessageBox.information(self, "Gamut Warning", "Not implemented.")

    def _view_screen_mode(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _view_extras(self) -> None:
        self._canvas.show_guides = not self._canvas.show_guides
        self._canvas.update()

    def _view_show(self) -> None:
        self._canvas.show_grid = not self._canvas.show_grid
        self._canvas.update()
        self.statusBar().showMessage("Grid ON" if self._canvas.show_grid else "Grid OFF")

    def _view_rulers(self) -> None:
        self._canvas.show_rulers = not self._canvas.show_rulers
        self._canvas.update()
        self.statusBar().showMessage("Rulers ON" if self._canvas.show_rulers else "Rulers OFF")

    def _view_snap(self) -> None:
        self._canvas.show_smart_guides = not self._canvas.show_smart_guides
        self._canvas.update()
        self.statusBar().showMessage("Smart Guides ON" if self._canvas.show_smart_guides else "Smart Guides OFF")

    def _view_snap_to(self) -> None:
        QMessageBox.information(self, "Snap To", "Snap to Guides/Grid enabled through View toggles.")

    def _view_lock_guides(self) -> None:
        QMessageBox.information(self, "Lock Guides", "Guides are static until cleared.")

    def _view_clear_guides(self) -> None:
        self._canvas.guides.clear()
        self._canvas.update()

    def _view_new_guide(self) -> None:
        ori, ok = QInputDialog.getItem(self, "Guide", "Orientation:", ["Vertical", "Horizontal"], 0, False)
        if not ok:
            return
        pos, ok2 = QInputDialog.getInt(self, "Guide", "Position (px):", 100, 0, 100000)
        if ok2:
            self._canvas.guides.append(("v" if ori.startswith("V") else "h", pos))
            self._canvas.update()

    def _view_guide_layout(self) -> None:
        cols, ok1 = QInputDialog.getInt(self, "Guide Layout", "Columns:", 3, 1, 20)
        if not ok1 or self._canvas._qimage is None:
            return
        w = self._canvas._qimage.width()
        self._canvas.guides.clear()
        for i in range(1, cols):
            self._canvas.guides.append(("v", int(i * w / cols)))
        self._canvas.update()

    # --- Window ---
    def _menu_window(self, menu) -> None:
        self._add(menu, "&Arrange", self._win_arrange)
        self._add(menu, "&Workspace", self._win_workspace)
        self._add(menu, "&Extensions", self._win_extensions)
        menu.addSeparator()
        self._add(menu, "&Actions", partial(self._win_panel_stub, "Actions"))
        self._add(menu, "&Adjustments", partial(self._win_panel_stub, "Adjustments"))
        self._add(menu, "Brush &Settings", partial(self._win_panel_stub, "Brush Settings"))
        self._add(menu, "&Brushes", partial(self._win_panel_stub, "Brushes"))
        self._add(menu, "&Channels", partial(self._win_panel_stub, "Channels"))
        self._add(menu, "&Character", partial(self._win_panel_stub, "Character"))
        self._add(menu, "&Color", self._win_color_panel)
        self._add(menu, "&Gradients", partial(self._win_panel_stub, "Gradients"))
        self._add(menu, "&History", partial(self._raise_dock, "History"))
        self._add(menu, "&Layers", partial(self._raise_dock, "Layers"))
        self._add(menu, "&Libraries", partial(self._win_panel_stub, "Libraries"))
        self._add(menu, "&Navigator", partial(self._raise_dock, "Navigator"))
        self._add(menu, "&Paragraph", partial(self._win_panel_stub, "Paragraph"))
        self._add(menu, "&Paths", partial(self._win_panel_stub, "Paths"))
        self._add(menu, "&Patterns", partial(self._win_panel_stub, "Patterns"))
        self._add(menu, "&Properties", partial(self._win_panel_stub, "Properties"))
        self._add(menu, "&Shapes", partial(self._win_panel_stub, "Shapes"))
        self._add(menu, "&Styles", partial(self._win_panel_stub, "Styles"))
        self._add(menu, "&Swatches", partial(self._win_panel_stub, "Swatches"))
        self._add(menu, "&Timeline", partial(self._win_panel_stub, "Timeline"))
        self._add(menu, "&Tools", partial(self._win_panel_stub, "Tools (see toolbar)"))

    def _win_panel_stub(self, name: str) -> None:
        QMessageBox.information(
            self,
            name,
            f"The {name} panel is not a separate window in this build.\n"
            "Use the top menus and the Layers / History / Navigator docks.",
        )

    def _win_color_panel(self) -> None:
        self._pick_fg()

    def _raise_dock(self, name: str) -> None:
        for d in self.findChildren(QDockWidget):
            if d.windowTitle() == name:
                d.show()
                d.raise_()
                break

    def _win_arrange(self) -> None:
        docks = [d for d in self.findChildren(QDockWidget) if d.isVisible()]
        if len(docks) >= 2:
            self.tabifyDockWidget(docks[0], docks[1])
        for d in docks:
            d.show()

    def _win_workspace(self) -> None:
        QMessageBox.information(self, "Workspace", "Default workspace: Layers + History + Navigator.")

    def _win_extensions(self) -> None:
        self._plug_browse()

    # --- Help ---
    def _menu_help(self, menu) -> None:
        self._add(menu, "Help Center", self._help_ps)
        self._add(menu, "Learn Tools", self._help_learn)
        self._add(menu, "What's New", self._help_new)
        self._add(menu, "System Info…", self._help_sys)
        self._add(menu, "Manage Account", self._help_account)
        self._add(menu, "Updates", self._help_updates)
        self._add(menu, "Sign Out", self._help_signout)
        self._add(menu, "About DRAGON FORMUP", self._help_about)

    def _help_ps(self) -> None:
        QMessageBox.information(self, "Help", "DRAGON FORMUP — use menus and left tools. Middle-mouse or Space+drag to pan.")

    def _help_learn(self) -> None:
        QMessageBox.information(self, "Learn", "Open an image, add layers, apply filters from the Filter menu.")

    def _help_new(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("What's New")
        v = QVBoxLayout(dlg)
        t = QTextEdit()
        t.setReadOnly(True)
        t.setPlainText(
            "• Full professional menu bar\n"
            "• Layers, undo/redo, filters, export, print\n"
            "• Guides, crop, selection, clipboard\n"
        )
        v.addWidget(t)
        dlg.resize(400, 200)
        dlg.exec()

    def _help_sys(self) -> None:
        inst = QApplication.instance()
        ver = inst.applicationVersion() if inst else ""
        QMessageBox.information(self, "System Info", f"Python {sys.version}\n{platform.platform()}\nQt {ver}")

    def _help_account(self) -> None:
        QMessageBox.information(self, "Account", "No cloud account in this desktop app.")

    def _help_updates(self) -> None:
        QMessageBox.information(self, "Updates", "Check this project folder for newer versions.")

    def _help_signout(self) -> None:
        QMessageBox.information(self, "Sign Out", "N/A")

    def _help_about(self) -> None:
        QMessageBox.about(
            self,
            "About",
            "<b>DRAGON FORMUP</b><br>"
            "Advanced image editor built with Pillow + PyQt6.<br>"
            "Creative desktop suite.",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        event.accept()
