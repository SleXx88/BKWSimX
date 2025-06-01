from PyQt6 import QtWidgets, QtGui, QtCore
import math
"""Widget‑Sammlung für die visuelle Darstellung der PV‑Generator‑Orientierung
(optimiert für 121 × 121 px Frames)."""

# Farbpalette
CLR_WALL   = QtGui.QColor("#B0BEC5")   # Light Blue‑Grey
CLR_MODULE = QtGui.QColor("#0288D1")   # Light Blue 600
CLR_ARROW  = QtGui.QColor("#FF8F00")   # Amber 800
CLR_SCALE  = QtGui.QColor("#424242")   # Dunkelgrau für Skala
CLR_SUN    = QtGui.QColor("#FFC107")   # Sonnengelb

class TiltWidget(QtWidgets.QFrame):

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._angle        = 0.0   # Grad
        self._mod_offset   = 0.0   # px
        self.setMinimumSize(131, 101)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

    # -------------------- Public API -----------------------------------
    def setAngle(self, angle: float) -> None:
        self._angle = angle % 360
        self.update()

    def setModuleOffset(self, offset_px: float) -> None:
        self._mod_offset = offset_px
        self.update()

    def setPivotOffset(self, offset_px: float) -> None:
        """Verschiebt die Drehachse vertikal relativ zur Standardposition."""
        self._pivot_offset = offset_px
        self.update()

    # -------------------- Painting -------------------------------------
    def paintEvent(self, _: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        
        # ------------------------------------- Sonne oben links ---------
        sun_center = QtCore.QPointF(16, 16)
        sun_r      = 8
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QBrush(CLR_SUN))
        p.drawEllipse(sun_center, sun_r, sun_r)
        # Sonnenstrahlen
        p.setPen(QtGui.QPen(CLR_SUN, 2))
        for a in range(0, 360, 45):
            rad = math.radians(a)
            inner = QtCore.QPointF(
                sun_center.x() + sun_r * 0.9 * math.cos(rad),
                sun_center.y() + sun_r * 0.9 * math.sin(rad),
            )
            outer = QtCore.QPointF(
                sun_center.x() + (sun_r + 5) * math.cos(rad),
                sun_center.y() + (sun_r + 5) * math.sin(rad),
            )
            p.drawLine(inner, outer)

        # Grund‑Pivot (leicht rechts & oben) + user‑Offset
        pivot_x = w * 0.70
        pivot_y = h * 0.60
        p.translate(QtCore.QPointF(pivot_x, pivot_y/2))

        # Hauswand
        wall_h = h * 0.60
        p.setPen(QtGui.QPen(CLR_WALL, 12, QtCore.Qt.PenStyle.SolidLine,
                            QtCore.Qt.PenCapStyle.SquareCap))
        p.drawLine(QtCore.QPointF(5, wall_h), QtCore.QPointF(5, wall_h - 80))
        
        # ------------------ Winkelskala --------------------------------
        scale_r = w * 0.32   # Radius der Skala
        p.setPen(QtGui.QPen(CLR_SCALE, 1.5))
        for deg, label in ((0, "0°"), (45, "45°"), (90, "90°")):
            p.save()
            p.rotate(-deg)
            # Tick‑Marke
            p.drawLine(QtCore.QPointF(0, 0), QtCore.QPointF(-scale_r, 0))
            # Text leicht links neben Tick
            font = p.font(); font.setPointSize(8); p.setFont(font)
            font.setPointSize(8)
            font.setWeight(QtGui.QFont.Weight.Bold)
            p.setFont(font)
            fm = QtGui.QFontMetricsF(font)
            text_pt = QtCore.QPointF(-scale_r - fm.horizontalAdvance(label) - 3,
                                      fm.height()/4)
            p.drawText(text_pt, label)
            p.restore()

        # Modul
        p.rotate(-self._angle)
        p.translate(0, self._mod_offset)  # Modul‑Versatz nach Rotation
        mod_len = w * 0.50
        mod_thk = h * 0.06
        rect = QtCore.QRectF(-mod_len, -mod_thk, mod_len, mod_thk)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QBrush(CLR_MODULE))
        p.drawRoundedRect(rect, 2, 2)
            
        p.end()


class AzimuthWidget(QtWidgets.QFrame):
    """Draufsicht‑Widget: Modul rotiert innerhalb eines Kreises."""
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._azimuth = 0.0
        self.setMinimumSize(121, 121)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

    # -------------------- Public API -----------------------------------
    def setAzimuth(self, az_deg: float) -> None:
        self._azimuth = az_deg % 360
        self.update()

    # -------------------- Painting -------------------------------------
    def paintEvent(self, _: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        # Grund‑Pivot (leicht rechts & oben) + user‑Offset
        pivot_x = w * 0.50
        pivot_y = h * 0.55
        p.translate(QtCore.QPointF(pivot_x, pivot_y))

        radius = min(w, h) * 0.30

        # Kreis
        p.setPen(QtGui.QPen(CLR_WALL, 2))
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.drawEllipse(QtCore.QPointF(0, 0), radius, radius)

        # Labels
        font = p.font()
        font.setPointSize(8)
        font.setWeight(QtGui.QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(QtCore.Qt.GlobalColor.black)
        fm  = QtGui.QFontMetricsF(font)
        htx = fm.height()
        def _lbl(t, dx, dy):
            bw = fm.horizontalAdvance(t)
            p.drawText(QtCore.QPointF(dx - bw/2, dy + htx/4), t)
        _lbl("N", 0, -radius - 8)
        _lbl("E", radius + 8, 0)
        _lbl("S", 0, radius + 8)
        _lbl("W", -radius - 8, 0)

        # Modul
        p.save()
        p.rotate(self._azimuth)
        mod_w, mod_h = radius * 0.85, radius * 0.16
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QBrush(CLR_MODULE))
        p.drawRoundedRect(QtCore.QRectF(-mod_w/2, -mod_h/2, mod_w, mod_h), 2, 2)
        p.restore()

        # Pfeil
        p.save()
        p.rotate(self._azimuth)
        p.setPen(QtGui.QPen(CLR_ARROW, 2))
        p.drawLine(QtCore.QPointF(0, 0), QtCore.QPointF(0, -radius + 6))
        head = QtGui.QPolygonF([
            QtCore.QPointF(0, -radius + 6),
            QtCore.QPointF(-4, -radius + 14),
            QtCore.QPointF(4, -radius + 14),
        ])
        p.setBrush(QtGui.QBrush(CLR_ARROW))
        p.drawPolygon(head)
        p.restore()
        p.end()
