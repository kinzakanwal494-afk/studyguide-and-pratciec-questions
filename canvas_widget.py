"""Viewport: zoom, pan, selections, crop, tool behaviors."""
from __future__ import annotations

import traceback
from typing import TYPE_CHECKING, List, Optional, Tuple

from PIL import Image
from PyQt6.QtCore import QPoint, QPointF, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QImage, QPainter, QPen, QPolygon
from PyQt6.QtWidgets import QWidget

if TYPE_CHECKING:
    pass


def pil_to_qimage(pil: Image.Image) -> QImage:
    im = pil.convert("RGBA")
    return QImage(
        im.tobytes("raw", "RGBA"),
        im.width,
        im.height,
        QImage.Format.Format_RGBA8888,
    ).copy()


def subtract_rect(outer: QRect, inner: QRect) -> List[QRect]:
    inner = inner.normalized()
    outer = outer.normalized()
    if not outer.intersects(inner):
        return [outer]
    i = outer.intersected(inner)
    o = outer
    rects: List[QRect] = []
    if i.top() > o.top():
        rects.append(QRect(o.left(), o.top(), o.width(), i.top() - o.top()))
    if i.bottom() < o.bottom():
        rects.append(QRect(o.left(), i.bottom() + 1, o.width(), o.bottom() - i.bottom()))
    mid_top = max(i.top(), o.top())
    mid_h = min(i.bottom(), o.bottom()) - mid_top + 1
    if mid_h > 0:
        if i.left() > o.left():
            rects.append(QRect(o.left(), mid_top, i.left() - o.left(), mid_h))
        if i.right() < o.right():
            rects.append(QRect(i.right() + 1, mid_top, o.right() - i.right(), mid_h))
    return [r for r in rects if r.width() > 0 and r.height() > 0]


MARQUEE_TOOLS = frozenset(
    {
        "rectangular_marquee",
        "elliptical_marquee",
        "single_row_marquee",
        "single_column_marquee",
    }
)
PAINT_TOOLS = frozenset(
    {
        "brush",
        "pencil",
        "eraser",
        "blur_tool",
        "sharpen_tool",
        "smudge_tool",
        "dodge",
        "burn",
        "sponge",
        "color_replacement",
        "mixer_brush",
        "spot_healing",
        "healing_brush",
        "red_eye",
        "remove",
        "clone_stamp",
        "background_eraser",
    }
)
SHAPE_TOOLS = frozenset({"shape_rect", "shape_ellipse"})


class CanvasWidget(QWidget):
    selectionChanged = pyqtSignal(object)
    cropApplied = pyqtSignal(QRect)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.editor = None  # PhotoEditorWindow set after init
        self.tool_id = "rectangular_marquee"
        self._pil: Optional[Image.Image] = None
        self._qimage: Optional[QImage] = None
        self.zoom = 1.0
        self._pan = QPointF(0, 0)
        self._drag_start: Optional[QPoint] = None
        self._pan_start: Optional[QPointF] = None
        self.selection_parts: List[QRect] = []
        self.selection_shape = "rect"  # rect | ellipse
        self._select_start: Optional[QPoint] = None
        self.crop_mode = False
        self.quick_mask = False
        self.show_guides = True
        self.show_grid = False
        self.show_rulers = False
        self.show_smart_guides = False
        self.guides: list[tuple[str, int]] = []
        self.lasso_points: List[QPoint] = []
        self._lasso_active = False
        self._paint_active = False
        self._paint_prev: Optional[Tuple[int, int]] = None
        self._clone_src: Optional[Tuple[int, int]] = None
        self._grad_start: Optional[Tuple[int, int]] = None
        self._shape_origin: Optional[Tuple[int, int]] = None
        self._layer_move_origin: Optional[Tuple[int, int]] = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(400, 300)

    def event(self, event) -> bool:
        """Never allow Python exceptions to escape into Qt event loop."""
        try:
            return super().event(event)
        except Exception as exc:
            traceback.print_exc()
            if self.editor is not None:
                self.editor.status_message(f"Tool runtime error: {exc}")
            return True

    def selection_bounds(self) -> Optional[QRect]:
        if not self.selection_parts:
            return None
        u = self.selection_parts[0]
        for r in self.selection_parts[1:]:
            u = u.united(r)
        return u.normalized()

    def set_pil_image(self, pil: Optional[Image.Image]) -> None:
        self._pil = pil.copy() if pil is not None else None
        self._qimage = pil_to_qimage(pil) if pil is not None else None
        self.update()

    def image_size(self) -> Tuple[int, int]:
        if self._pil is None:
            return (0, 0)
        return self._pil.size

    def screen_to_image(self, p: QPointF) -> QPointF:
        if self._qimage is None:
            return QPointF(0, 0)
        cx = self.width() / 2 + self._pan.x()
        cy = self.height() / 2 + self._pan.y()
        z = max(0.05, self.zoom)
        ix = (p.x() - cx) / z + self._qimage.width() / 2
        iy = (p.y() - cy) / z + self._qimage.height() / 2
        return QPointF(ix, iy)

    def _clamp_img(self, x: float, y: float) -> Tuple[int, int]:
        if self._qimage is None:
            return (0, 0)
        xi = int(max(0, min(self._qimage.width() - 1, x)))
        yi = int(max(0, min(self._qimage.height() - 1, y)))
        return (xi, yi)

    def _defer(self, fn) -> None:
        QTimer.singleShot(0, fn)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(60, 60, 60))
        if self._qimage is None:
            painter.end()
            return
        painter.translate(self.width() / 2 + self._pan.x(), self.height() / 2 + self._pan.y())
        painter.scale(self.zoom, self.zoom)
        painter.translate(-self._qimage.width() / 2, -self._qimage.height() / 2)
        painter.drawImage(0, 0, self._qimage)
        if self.show_grid:
            painter.setPen(QPen(QColor(120, 120, 120, 90), 0))
            step = 32
            for x in range(0, self._qimage.width(), step):
                painter.drawLine(x, 0, x, self._qimage.height())
            for y in range(0, self._qimage.height(), step):
                painter.drawLine(0, y, self._qimage.width(), y)
        if self.quick_mask:
            painter.fillRect(self._qimage.rect(), QColor(255, 0, 0, 60))
        if self.show_guides and self.guides:
            painter.setPen(QPen(QColor(0, 200, 255), 0))
            for orient, pos in self.guides:
                if orient == "v":
                    painter.drawLine(pos, 0, pos, self._qimage.height())
                else:
                    painter.drawLine(0, pos, self._qimage.width(), pos)
        painter.setPen(QPen(QColor(255, 255, 255), 1, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.lasso_points and len(self.lasso_points) > 1:
            painter.drawPolyline(QPolygon(self.lasso_points))
        for r in self.selection_parts:
            if r.width() > 0 and r.height() > 0:
                if self.selection_shape == "ellipse":
                    painter.drawEllipse(r)
                else:
                    painter.drawRect(r)
        if self.show_smart_guides and self.selection_parts:
            b = self.selection_bounds()
            if b:
                cx = b.x() + b.width() // 2
                cy = b.y() + b.height() // 2
                painter.setPen(QPen(QColor(255, 110, 0), 0))
                painter.drawLine(cx, 0, cx, self._qimage.height())
                painter.drawLine(0, cy, self._qimage.width(), cy)
        painter.end()

        if self.show_rulers:
            rp = QPainter(self)
            rp.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            rp.fillRect(0, 0, self.width(), 22, QColor(30, 30, 30))
            rp.fillRect(0, 0, 22, self.height(), QColor(30, 30, 30))
            rp.setPen(QPen(QColor(170, 170, 170), 1))
            step = 50
            for x in range(22, self.width(), step):
                rp.drawLine(x, 0, x, 10)
            for y in range(22, self.height(), step):
                rp.drawLine(0, y, 10, y)
            rp.end()

    def wheelEvent(self, event) -> None:
        if self.tool_id == "zoom" and self._qimage is not None:
            ip = self.screen_to_image(event.position())
            xi, yi = self._clamp_img(ip.x(), ip.y())
            delta = event.angleDelta().y()
            old_z = self.zoom
            if delta > 0:
                self.zoom = min(32.0, self.zoom * 1.15)
            else:
                self.zoom = max(0.05, self.zoom / 1.15)
            # zoom toward cursor (simplified)
            self.update()
            return
        delta = event.angleDelta().y()
        if delta > 0:
            self.zoom = min(32.0, self.zoom * 1.15)
        else:
            self.zoom = max(0.05, self.zoom / 1.15)
        self.update()

    def mousePressEvent(self, event) -> None:
        ed = self.editor
        if self._qimage is None or ed is None:
            return
        if event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.SpaceModifier
        ):
            self._drag_start = event.position().toPoint()
            self._pan_start = QPointF(self._pan)
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        ip = self.screen_to_image(event.position())
        ix, iy = self._clamp_img(ip.x(), ip.y())
        tid = self.tool_id

        if tid == "hand":
            self._drag_start = event.position().toPoint()
            self._pan_start = QPointF(self._pan)
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return

        if tid == "zoom":
            if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                self.zoom = max(0.05, self.zoom / 1.25)
            else:
                self.zoom = min(32.0, self.zoom * 1.25)
            self.update()
            return

        if tid == "eyedropper":
            self._defer(lambda: ed.sample_foreground(ix, iy))
            return

        if tid == "magic_wand":
            self._defer(lambda: ed.apply_magic_wand(ix, iy))
            return

        if tid == "magic_eraser":
            self._defer(lambda: ed.apply_magic_eraser(ix, iy))
            return

        if tid == "paint_bucket":
            self._defer(lambda: ed.apply_paint_bucket(ix, iy))
            return

        if tid == "object_selection":
            # Keep object selection on safe path (no heavy native filters in event phase).
            self._defer(ed.apply_object_selection)
            return

        if tid == "quick_selection":
            self._defer(lambda: ed.apply_quick_selection(ix, iy))
            return

        if tid in ("quick_mask",):
            self._defer(ed.toggle_quick_mask)
            return

        if tid == "screen_mode":
            self._defer(ed.toggle_screen_mode)
            return

        if tid in ("artboard", "perspective_crop", "slice", "slice_select", "frame", "eyedropper_3d", "color_sampler", "ruler", "note", "count", "patch", "pattern_stamp", "history_brush", "art_history_brush", "material_drop_3d", "pen", "freeform_pen", "curvature_pen", "add_anchor", "delete_anchor", "convert_point", "type_vertical", "type_mask_v", "type_mask_h", "path_selection", "direct_selection", "shape_triangle", "shape_polygon", "shape_line", "shape_custom", "rotate_view", "edit_toolbar", "polygonal_lasso", "magnetic_lasso", "content_aware_move"):
            self._defer(lambda: ed.show_tool_stub(tid))
            return

        if tid == "type_horizontal":
            self._defer(ed.paste_lorem_text)
            return

        if tid == "move":
            ed.reset_layer_move()
            self._layer_move_origin = (ix, iy)
            self._drag_start = event.position().toPoint()
            return

        if tid in PAINT_TOOLS:
            if tid == "clone_stamp" and event.modifiers() & Qt.KeyboardModifier.AltModifier:
                self._clone_src = (ix, iy)
                ed.status_message(f"Clone source {ix},{iy}")
                return
            self._paint_active = True
            self._paint_prev = (ix, iy)
            ed.paint_brush_stroke(ix, iy, ix, iy, tid, event.modifiers())
            return

        if tid == "gradient":
            self._grad_start = (ix, iy)
            return

        if tid in SHAPE_TOOLS:
            self._shape_origin = (ix, iy)
            self._select_start = QPoint(ix, iy)
            return

        if tid == "lasso":
            self.lasso_points = [QPoint(ix, iy)]
            self._lasso_active = True
            return

        self.crop_mode = tid == "crop"
        self.selection_shape = "ellipse" if tid == "elliptical_marquee" else "rect"

        if tid == "single_row_marquee":
            self.selection_parts = [QRect(0, iy, self._qimage.width(), 1)]
            self.selection_shape = "rect"
            self.selectionChanged.emit(self.selection_bounds())
            self.update()
            return
        if tid == "single_column_marquee":
            self.selection_parts = [QRect(ix, 0, 1, self._qimage.height())]
            self.selection_shape = "rect"
            self.selectionChanged.emit(self.selection_bounds())
            self.update()
            return

        if tid in MARQUEE_TOOLS or self.crop_mode:
            self._select_start = QPoint(ix, iy)
            self.selection_parts = [QRect(self._select_start, self._select_start)]
            self.update()

    def mouseMoveEvent(self, event) -> None:
        ed = self.editor
        if self._qimage is None:
            return
        if self._drag_start is not None and self._pan_start is not None:
            d = event.position().toPoint() - self._drag_start
            self._pan = QPointF(self._pan_start.x() + d.x(), self._pan_start.y() + d.y())
            self.update()
            return

        if event.buttons() & Qt.MouseButton.LeftButton and self._layer_move_origin and ed and self.tool_id == "move":
            ip = self.screen_to_image(event.position())
            ix, iy = self._clamp_img(ip.x(), ip.y())
            ox, oy = self._layer_move_origin
            dx, dy = ix - ox, iy - oy
            if dx != 0 or dy != 0:
                ed.offset_active_layer(dx, dy)
                self._layer_move_origin = (ix, iy)
            return

        if self._paint_active and self._paint_prev and ed and event.buttons() & Qt.MouseButton.LeftButton:
            ip = self.screen_to_image(event.position())
            ix, iy = self._clamp_img(ip.x(), ip.y())
            x0, y0 = self._paint_prev
            ed.paint_brush_stroke(x0, y0, ix, iy, self.tool_id, event.modifiers())
            self._paint_prev = (ix, iy)
            return

        if self._lasso_active and event.buttons() & Qt.MouseButton.LeftButton:
            ip = self.screen_to_image(event.position())
            ix, iy = self._clamp_img(ip.x(), ip.y())
            self.lasso_points.append(QPoint(ix, iy))
            self.selectionChanged.emit(self.selection_bounds())
            self.update()
            return

        if self._select_start is not None and event.buttons() & Qt.MouseButton.LeftButton:
            ip = self.screen_to_image(event.position())
            cur = QPoint(int(ip.x()), int(ip.y()))
            self.selection_parts = [QRect(self._select_start, cur).normalized()]
            self.selectionChanged.emit(self.selection_bounds())
            self.update()

        if self._shape_origin and self._select_start is not None and event.buttons() & Qt.MouseButton.LeftButton:
            ip = self.screen_to_image(event.position())
            cur = QPoint(int(ip.x()), int(ip.y()))
            self.selection_parts = [QRect(self._select_start, cur).normalized()]
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        ed = self.editor
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton):
            if self.tool_id != "move":
                self._drag_start = None
                self._pan_start = None
                self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

        if event.button() == Qt.MouseButton.LeftButton:
            if self.tool_id == "move":
                self._layer_move_origin = None
                self._drag_start = None
                self._pan_start = None
                self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
                return

            if self._paint_active:
                self._paint_active = False
                self._paint_prev = None
                if ed:
                    ed.after_paint_stroke()
                return

            if self._lasso_active:
                self._lasso_active = False
                if self.lasso_points:
                    poly = QPolygon(self.lasso_points)
                    b = poly.boundingRect()
                    self.selection_parts = [b]
                    self.selection_shape = "rect"
                self.lasso_points = []
                self.selectionChanged.emit(self.selection_bounds())
                self.update()
                return

            if self.tool_id == "gradient" and self._grad_start and ed:
                ip = self.screen_to_image(event.position())
                ix, iy = self._clamp_img(ip.x(), ip.y())
                x0, y0 = self._grad_start
                ed.apply_linear_gradient(x0, y0, ix, iy)
                self._grad_start = None
                return

            if self.tool_id in SHAPE_TOOLS and self._shape_origin and self.selection_bounds() and ed:
                r = self.selection_bounds()
                if r and r.width() > 1 and r.height() > 1:
                    ed.apply_shape_fill(r, self.tool_id == "shape_ellipse")
                self._shape_origin = None
                self._select_start = None
                self.selection_parts = []
                self.update()
                return

            if self._select_start is not None:
                self._select_start = None
                if self.crop_mode and self.selection_bounds() and self._qimage:
                    r = self.selection_bounds()
                    if r and r.width() > 2 and r.height() > 2:
                        r = r.intersected(self._qimage.rect())
                        self.cropApplied.emit(r)
                self.selectionChanged.emit(self.selection_bounds())

    def fit_zoom(self) -> None:
        if self._qimage is None:
            return
        zw = (self.width() - 40) / max(1, self._qimage.width())
        zh = (self.height() - 40) / max(1, self._qimage.height())
        self.zoom = max(0.05, min(zw, zh))
        self._pan = QPointF(0, 0)
        self.update()

    def actual_size_zoom(self) -> None:
        self.zoom = 1.0
        self._pan = QPointF(0, 0)
        self.update()
