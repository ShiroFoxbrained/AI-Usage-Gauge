#!/usr/bin/env python3
"""AI-Usage-Gauge — a single-process tray icon plus dashboard
window. Live plan limits (session/weekly %) as speedometer dials and a local
token/cost history breakdown. See claude_usage_core.py for the data layer.

Run with no arguments to open the window; --tray starts minimised to the tray.
Only one copy ever runs: a second launch hands its request to the first over a
local socket and exits."""
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Run under XWayland/X11: native Wayland clients cannot reliably place popups
# and windows. Delete this line if your system has no XWayland.
os.environ["QT_QPA_PLATFORM"] = "xcb"

sys.path.insert(0, str(Path(__file__).parent))
import claude_usage_core as core

from PyQt6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow, QMenu,
    QPushButton, QScrollArea, QStackedWidget, QSystemTrayIcon, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView, QAbstractItemView,
)
from PyQt6.QtCore import QObject, QPointF, QRectF, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFont, QFontMetricsF, QIcon, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QPolygonF, QRadialGradient,
)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

ICON_PATH = str(Path(__file__).parent / "icon.png")
SOCKET_NAME = f"claude-usage-{os.getuid()}"
HISTORY_MIN_S = 120  # don't rescan every log on rapid open/close
VERSION = "1.4"

# Catppuccin Mocha surfaces and text.
CRUST, BASE, CARD, LINE = "#11111b", "#1e1e2e", "#181825", "#313244"
TEXT, TEXT2, MUTED = "#cdd6f4", "#a6adc8", "#9399b2"
# Data series (never a status): one accent for every chart.
ACCENT = "#cba6f7"
# Status: validated for colour-blind separation on CARD (dataviz validator,
# CVD ΔE >= 15.6), and always shown with a glyph + word, never colour alone.
OK, WARN, CRIT = "#74c7ec", "#f9e2af", "#e64553"
WARN_GLYPH = "▲"

LIMIT_LABELS = {"session": ("Session", "5-hour window"), "weekly_all": ("Weekly", "7-day window")}


def severity(pct):
    """(colour, glyph, word) for a usage percentage."""
    if pct >= 90:
        return CRIT, "■", "Near limit"
    if pct >= 70:
        return WARN, WARN_GLYPH, "High"
    return OK, "●", "OK"


def fmt_reset(resets_at):
    if not resets_at:
        return ""
    try:
        dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = int((dt - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "Resets now"
    h, m = secs // 3600, secs % 3600 // 60
    if h >= 24:
        return f"Resets in {h // 24}d {h % 24}h"
    return f"Resets in {h}h {m}m" if h else f"Resets in {m}m"


def fmt_tokens(n):
    """1234 -> 1.2K, 9_600_000 -> 9.6M, 10_000_000 -> 10M, 2_194_700_000 -> 2.2B."""
    for div, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            return f"{n / div:.1f}".removesuffix(".0") + unit
    return str(int(n))


def fmt_ago(ts):
    mins = int((time.time() - ts) / 60)
    return "just now" if mins < 1 else f"{mins}m ago" if mins < 60 else f"{mins // 60}h ago"


def ui_font(px, weight=QFont.Weight.Normal):
    f = QFont(QApplication.font())
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def label(text, px=12, color=TEXT2, weight=QFont.Weight.Normal):
    lbl = QLabel(text)
    lbl.setFont(ui_font(px, weight))
    lbl.setStyleSheet(f"color:{color}; background:transparent;")
    return lbl


def nice_step(raw):
    """Round a tick step up to 1 / 2 / 2.5 / 5 × 10^k."""
    mag = 10 ** max(0, len(str(int(raw))) - 1)
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


class Worker(QThread):
    """Runs one function off the GUI thread and emits its result."""
    done = pyqtSignal(dict)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        self.done.emit(self.fn())


class UsageController(QObject):
    """The one place plan-limit data is fetched. A single timer and a single
    in-flight request feed both the tray icon and the dashboard, so the app
    makes one call per interval no matter how many views are listening —
    the endpoint 429s if polled faster than MIN_REFRESH_INTERVAL_S."""

    updated = pyqtSignal(dict)
    busy_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.latest = None
        self._thread = None
        self._busy = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(core.MIN_REFRESH_INTERVAL_S * 1000)

    def refresh(self, force=False):
        if self._busy:
            return  # a fetch is already running; every listener gets its result
        self._busy = True
        self.busy_changed.emit(True)
        # Held on self so the QThread outlives this call — a garbage-collected
        # QThread that is still running takes the process down with it.
        self._thread = Worker(lambda: core.fetch_usage(force=force))
        self._thread.done.connect(self._on_done)
        self._thread.start()

    def _on_done(self, result):
        self._busy = False
        self.latest = result
        self.busy_changed.emit(False)
        self.updated.emit(result)


def limits_of(usage):
    """{kind: entry} from the stable `usage["limits"]` list. The sibling
    top-level keys (five_hour, seven_day, internal codenames) are not a
    stable schema — never iterate them."""
    return {e.get("kind"): e for e in ((usage or {}).get("limits") or [])}


# ── Tray / app icon ─────────────────────────────────────────────────────────

def gauge_pixmap(size, session, weekly, tile=False):
    """The dashboard's speedometer in miniature: outer arc = session, inner
    arc = weekly, both sweeping the same 240° and tinted by severity, and the
    needle on the session reading. None = no data (grey, needle at zero).
    `tile` puts it on the launcher's rounded tile instead of the disc."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 64, size / 64)
    p.setPen(Qt.PenStyle.NoPen)
    if tile:
        grad = QLinearGradient(0, 2, 0, 62)
        grad.setColorAt(0, QColor(LINE))
        grad.setColorAt(1, QColor(CRUST))
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    else:
        p.setBrush(QColor(CRUST))
        p.drawEllipse(QRectF(2, 2, 60, 60))
    p.setBrush(Qt.BrushStyle.NoBrush)
    for pct, r, width in ((session, 23, 7), (weekly, 13.5, 5.5)):
        color = QColor(MUTED if pct is None else severity(pct)[0])
        rect = QRectF(32 - r, 35 - r, 2 * r, 2 * r)  # pivot a little low: the dial is open at the bottom
        p.setPen(QPen(zone_color(color.name(), 0.25), width, cap=Qt.PenCapStyle.FlatCap))
        p.drawArc(rect, 210 * 16, -240 * 16)
        if pct:
            p.setPen(QPen(color, width, cap=Qt.PenCapStyle.FlatCap))
            p.drawArc(rect, 210 * 16, int((gauge_angle(pct) - 210) * 16))
    p.translate(32, 35)
    p.rotate(-gauge_angle(session or 0))  # the needle is drawn pointing at 3 o'clock
    p.setPen(QPen(QColor(CRUST), 1.6))
    p.setBrush(QColor(TEXT))
    p.drawPolygon(QPolygonF([QPointF(25, 0), QPointF(0, 3), QPointF(-6, 2), QPointF(-6, -2), QPointF(0, -3)]))
    p.setBrush(QColor(ACCENT))
    p.drawEllipse(QPointF(0, 0), 4.5, 4.5)
    p.end()
    return pm


class UsageTray(QSystemTrayIcon):
    """Tray icon living in the same process as the window it opens."""

    open_requested = pyqtSignal()
    quit_requested = pyqtSignal()
    unavailable = pyqtSignal()

    WAIT_INTERVAL_MS = 2000
    WAIT_ATTEMPTS = 30  # ~1 min: at login the tray host often starts after us

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        menu = QMenu()
        menu.addAction("Open Dashboard").triggered.connect(self.open_requested.emit)
        menu.addAction("Refresh Now").triggered.connect(lambda: controller.refresh(force=True))
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(self.quit_requested.emit)
        self.setContextMenu(menu)
        self._menu = menu

        self.activated.connect(self._on_activated)
        self.setIcon(QIcon(gauge_pixmap(64, None, None)))
        self.setToolTip(f"AI-Usage-Gauge v{VERSION} — loading…")

        controller.updated.connect(self._on_usage)
        if controller.latest:
            self._on_usage(controller.latest)

        self._attempts = 0
        self._wait_timer = QTimer(self)
        self._wait_timer.timeout.connect(self._try_show)
        self._try_show()

    def _try_show(self):
        """Showing a tray icon before the tray host exists silently does
        nothing, which is how the icon used to end up missing entirely."""
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._wait_timer.stop()
            self.show()
            return
        if not self._wait_timer.isActive():
            self._wait_timer.start(self.WAIT_INTERVAL_MS)
        self._attempts += 1
        if self._attempts >= self.WAIT_ATTEMPTS:
            self._wait_timer.stop()
            self.unavailable.emit()

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_requested.emit()

    def _on_usage(self, result):
        if not result.get("usage"):
            self.setIcon(QIcon(gauge_pixmap(64, None, None)))
            self.setToolTip(f"AI-Usage-Gauge — {result.get('error', 'no data')}")
            return
        lim = limits_of(result["usage"])
        session = lim.get("session", {}).get("percent", 0)
        weekly = lim.get("weekly_all", {}).get("percent", 0)
        self.setIcon(QIcon(gauge_pixmap(64, session, weekly)))
        stale = "  (stale)" if result.get("stale") else ""
        self.setToolTip(
            f"AI-Usage-Gauge{stale}\n"
            f"Session: {session:.0f}% — {severity(session)[2]}\n"
            f"Weekly: {weekly:.0f}% — {severity(weekly)[2]}"
        )


# ── Dashboard pieces ────────────────────────────────────────────────────────

class Card(QFrame):
    """Rounded panel with an optional title row."""

    def __init__(self, title=None, subtitle=None):
        super().__init__()
        self.setObjectName("card")
        self.setStyleSheet(
            f"QFrame#card {{ background:{CARD}; border:1px solid {LINE}; border-radius:16px; }}")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(12)
        if title:
            self.head = QHBoxLayout()
            box = QVBoxLayout()
            box.setSpacing(2)
            box.addWidget(label(title, 14, TEXT, QFont.Weight.DemiBold))
            if subtitle:
                box.addWidget(label(subtitle, 11, MUTED))
            self.head.addLayout(box)
            self.head.addStretch()
            self.body.addLayout(self.head)


def gauge_angle(pct):
    """Dial angle for a percentage, in Qt's degrees (0 = 3 o'clock, counter-
    clockwise): 0 % sits at 8 o'clock, 100 % at 4 o'clock, a 240° sweep."""
    return 210 - 2.4 * max(0, min(pct, 100))


def zone_color(hex_color, alpha):
    c = QColor(hex_color)
    c.setAlphaF(alpha)
    return c


# The redline: the stretches of the scale where the status turns High / Near
# limit, painted faintly into the track so you can see them coming.
ZONES = ((70, 90, WARN, 0.55), (90, 100, CRIT, 0.6))


class Gauge(QWidget):
    """A usage meter as a speedometer: numbered ticks, the redline zones, a
    sweep and needle carrying severity, and the reading under the hub.
    Drawn on a fixed 200-unit dial, so any diameter scales cleanly."""

    def __init__(self, diameter, caption):
        super().__init__()
        self.setFixedSize(diameter, diameter)
        self.caption = caption
        self.pct = None

    def set_pct(self, pct):
        self.pct = pct
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.translate(self.width() / 2, self.height() / 2)
        p.scale(self.width() / 200, self.height() / 200)
        color = QColor(MUTED if self.pct is None else severity(self.pct)[0])

        face = QRadialGradient(QPointF(0, -40), 130)
        face.setColorAt(0, QColor(BASE))
        face.setColorAt(1, QColor(CRUST))
        rim = QLinearGradient(0, -100, 0, 100)
        rim.setColorAt(0, QColor("#585b70"))
        rim.setColorAt(1, QColor(CRUST))
        p.setPen(QPen(rim, 3))
        p.setBrush(face)
        p.drawEllipse(QPointF(0, 0), 97, 97)

        band = QRectF(-84, -84, 168, 168)

        def arc(lo, hi, col, width):
            p.setPen(QPen(col, width, cap=Qt.PenCapStyle.FlatCap))
            p.drawArc(band, int(gauge_angle(lo) * 16), int((gauge_angle(hi) - gauge_angle(lo)) * 16))

        p.setBrush(Qt.BrushStyle.NoBrush)
        arc(0, 100, QColor(LINE), 9)
        for lo, hi, col, alpha in ZONES:
            arc(lo, hi, zone_color(col, alpha), 9)
        if self.pct:
            arc(0, self.pct, zone_color(color.name(), 0.16), 15)
            arc(0, self.pct, color, 9)

        p.setFont(ui_font(12, QFont.Weight.Medium))
        for v in range(0, 101, 5):
            t = math.radians(gauge_angle(v))
            ux, uy = math.cos(t), -math.sin(t)
            inner = 65 if v % 10 == 0 else 70
            p.setPen(QPen(QColor(TEXT2 if v % 10 == 0 else MUTED), 2.2 if v % 10 == 0 else 1.3))
            p.drawLine(QPointF(75 * ux, 75 * uy), QPointF(inner * ux, inner * uy))
            if v % 20 == 0:
                p.setPen(QColor(TEXT2))
                p.drawText(QRectF(53 * ux - 16, 53 * uy - 8, 32, 16), Qt.AlignmentFlag.AlignCenter, str(v))

        p.setPen(QColor(TEXT))
        p.setFont(ui_font(34, QFont.Weight.DemiBold))
        p.drawText(QRectF(-60, 30, 120, 40), Qt.AlignmentFlag.AlignCenter,
                   "—" if self.pct is None else f"{self.pct:.0f}%")
        p.setPen(QColor(MUTED))
        caption = ui_font(10, QFont.Weight.DemiBold)
        caption.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        p.setFont(caption)
        p.drawText(QRectF(-60, 68, 120, 14), Qt.AlignmentFlag.AlignCenter, self.caption)

        p.rotate(-gauge_angle(self.pct or 0))  # the needle is drawn pointing at 3 o'clock
        needle = QPolygonF([QPointF(80, 0), QPointF(0, 3.4), QPointF(-16, 2.4),
                            QPointF(-16, -2.4), QPointF(0, -3.4)])
        p.setPen(QPen(zone_color(color.name(), 0.25), 5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(needle)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPolygon(needle)
        hub = QRadialGradient(QPointF(-3, -3), 14)
        hub.setColorAt(0, QColor("#6c7086"))
        hub.setColorAt(1, QColor(LINE))
        p.setPen(QPen(QColor(CRUST), 1.5))
        p.setBrush(hub)
        p.drawEllipse(QPointF(0, 0), 10, 10)
        p.setBrush(QColor(CRUST))
        p.drawEllipse(QPointF(0, 0), 3, 3)


class LimitBlock(QWidget):
    """Gauge + name + status (glyph and word) + reset countdown."""

    def __init__(self, diameter, caption):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.gauge = Gauge(diameter, caption)
        lay.addWidget(self.gauge, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.name = label("", 14, TEXT, QFont.Weight.DemiBold)
        self.status = label("", 12, TEXT2)
        self.reset = label("", 11, MUTED)
        for w in (self.name, self.status, self.reset):
            w.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            lay.addWidget(w)

    def set_entry(self, kind, entry):
        title, window = LIMIT_LABELS.get(kind, (kind.replace("_", " ").title(), ""))
        self.name.setText(f"{title} · {window}" if window else title)
        pct = entry.get("percent", 0) if entry else None
        self.gauge.set_pct(pct)
        if pct is None:
            self.status.setText("No data")
            self.reset.setText("")
            return
        color, glyph, word = severity(pct)
        # The glyph carries the colour; the word stays in text ink.
        self.status.setText(f"<span style='color:{color}'>{glyph}</span>&nbsp; {word}")
        self.reset.setText(fmt_reset(entry.get("resets_at")))


class MeterRow(QWidget):
    """Label, value and a thin horizontal meter — for extra limits and spend."""

    def __init__(self, name, value, pct, note=""):
        super().__init__()
        self.pct = pct
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.addWidget(label(name, 12, TEXT, QFont.Weight.Medium))
        top.addStretch()
        color, glyph, word = severity(pct)
        top.addWidget(label(f"<span style='color:{color}'>{glyph}</span>&nbsp;{word}", 11, TEXT2))
        top.addSpacing(10)
        top.addWidget(label(value, 12, TEXT, QFont.Weight.DemiBold))
        lay.addLayout(top)
        self.bar = QWidget()
        self.bar.setFixedHeight(14)
        self.bar.paintEvent = self._paint_bar
        lay.addWidget(self.bar)
        if note:
            lay.addWidget(label(note, 11, MUTED))

    def _paint_bar(self, _e):
        """A linear gauge to match the dials: the same redline zones in the
        track, the fill carrying severity, a tick every 10 % underneath."""
        p = QPainter(self.bar)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.bar.width()
        track = QPainterPath()
        track.addRoundedRect(QRectF(0, 0, w, 8), 4, 4)
        p.setClipPath(track)
        p.fillRect(QRectF(0, 0, w, 8), QColor(LINE))
        for lo, hi, col, alpha in ZONES:
            p.fillRect(QRectF(w * lo / 100, 0, w * (hi - lo) / 100, 8), zone_color(col, alpha))
        p.fillRect(QRectF(0, 0, w * min(self.pct, 100) / 100, 8), QColor(severity(self.pct)[0]))
        p.setClipping(False)
        for v in range(0, 101, 10):
            x = min(max(w * v / 100, 0.5), w - 0.5)
            p.setPen(QPen(QColor(TEXT2 if v % 50 == 0 else MUTED), 1))
            p.drawLine(QPointF(x, 10), QPointF(x, 14 if v % 50 == 0 else 12))


class StatTile(QFrame):
    def __init__(self, name):
        super().__init__()
        self.setObjectName("tile")
        self.setStyleSheet(
            f"QFrame#tile {{ background:{CARD}; border:1px solid {LINE}; border-radius:14px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(4)
        lay.addWidget(label(name, 11, MUTED))
        self.value = label("—", 22, TEXT, QFont.Weight.DemiBold)
        lay.addWidget(self.value)


class ColumnChart(QWidget):
    """Daily tokens, one series: thin columns (<=24px, 4px rounded tops,
    square at the baseline), hairline grid at round ticks, the peak labelled
    on its cap, and a hover tooltip with the day's tokens and cost."""

    PAD_L, PAD_R, PAD_T, PAD_B = 44, 8, 22, 26

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(190)
        self.setMouseTracking(True)
        self.days = []      # [(date, tokens, cost)]
        self.hover = None

    def set_days(self, days):
        self.days = days
        self.hover = None
        self.update()

    def _geometry(self):
        n = len(self.days)
        plot_w = self.width() - self.PAD_L - self.PAD_R
        slot = plot_w / max(n, 1)
        bar_w = min(24.0, slot * 0.62)
        top = max((t for _, t, _ in self.days), default=0)
        step = nice_step(top / 3) if top else 1
        ymax = step * max(1, -(-top // step))
        return slot, bar_w, step, ymax

    def _bar_rect(self, i, tokens, slot, bar_w, ymax):
        base = self.height() - self.PAD_B
        h = (base - self.PAD_T) * tokens / ymax
        x = self.PAD_L + slot * i + (slot - bar_w) / 2
        return QRectF(x, base - h, bar_w, h)

    def mouseMoveEvent(self, e):
        slot = self._geometry()[0]
        i = int((e.position().x() - self.PAD_L) // slot) if self.days else -1
        hover = i if 0 <= i < len(self.days) else None
        if hover != self.hover:
            self.hover = hover
            self.update()

    def leaveEvent(self, _e):
        self.hover = None
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not any(t for _, t, _ in self.days):
            p.setPen(QColor(MUTED))
            p.setFont(ui_font(12))
            p.drawText(QRectF(self.rect()), Qt.AlignmentFlag.AlignCenter,
                       "No usage logged in the last 14 days")
            return
        slot, bar_w, step, ymax = self._geometry()
        base = self.height() - self.PAD_B

        # Grid + y ticks: hairline, solid, one step off the card.
        p.setFont(ui_font(10))
        tick = 0
        while tick <= ymax:
            y = base - (base - self.PAD_T) * tick / ymax
            p.setPen(QPen(QColor(LINE), 1))
            p.drawLine(QPointF(self.PAD_L, y), QPointF(self.width() - self.PAD_R, y))
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(0, y - 8, self.PAD_L - 8, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, fmt_tokens(int(tick)))
            tick += step

        peak = max(range(len(self.days)), key=lambda i: self.days[i][1])
        every = 1 if slot >= 34 else 2
        for i, (day, tokens, _cost) in enumerate(self.days):
            r = self._bar_rect(i, tokens, slot, bar_w, ymax)
            color = QColor(ACCENT)
            if self.hover is not None and self.hover != i:
                color.setAlphaF(0.4)
            if tokens:
                path = QPainterPath()
                rad = min(4.0, r.height(), r.width() / 2)
                path.moveTo(r.left(), r.bottom())
                path.lineTo(r.left(), r.top() + rad)
                path.quadTo(r.left(), r.top(), r.left() + rad, r.top())
                path.lineTo(r.right() - rad, r.top())
                path.quadTo(r.right(), r.top(), r.right(), r.top() + rad)
                path.lineTo(r.right(), r.bottom())
                path.closeSubpath()
                p.fillPath(path, color)
            if (len(self.days) - 1 - i) % every == 0:  # always label the latest day
                p.setPen(QColor(MUTED))
                p.drawText(QRectF(r.center().x() - 30, base + 6, 60, 16),
                           Qt.AlignmentFlag.AlignHCenter, f"{day:%b} {day.day}")
            if i == peak and self.hover is None:
                p.setPen(QColor(TEXT2))
                p.drawText(QRectF(r.center().x() - 40, r.top() - 18, 80, 16),
                           Qt.AlignmentFlag.AlignHCenter, fmt_tokens(tokens))

        if self.hover is not None:
            self._tooltip(p, self.hover, slot, bar_w, ymax)

    def _tooltip(self, p, i, slot, bar_w, ymax):
        day, tokens, cost = self.days[i]
        lines = [f"{day:%a %b} {day.day}", f"{tokens:,} tokens", f"${cost:,.2f} est."]
        fm = QFontMetricsF(ui_font(11, QFont.Weight.DemiBold))
        w = max(fm.horizontalAdvance(s) for s in lines) + 20
        h = 16 * len(lines) + 12
        r = self._bar_rect(i, tokens, slot, bar_w, ymax)
        x = min(max(r.center().x() - w / 2, 2), self.width() - w - 2)
        y = max(r.top() - h - 8, 2)
        p.setPen(QPen(QColor(LINE), 1))
        p.setBrush(QColor(CRUST))
        p.drawRoundedRect(QRectF(x, y, w, h), 8, 8)
        for n, s in enumerate(lines):
            p.setFont(ui_font(11, QFont.Weight.DemiBold if n == 0 else QFont.Weight.Normal))
            p.setPen(QColor(TEXT if n == 0 else TEXT2))
            p.drawText(QRectF(x + 10, y + 6 + 16 * n, w - 20, 16), Qt.AlignmentFlag.AlignVCenter, s)


class BarList(QWidget):
    """Ranked rows: name + detail line, a thin bar for share, value at the
    tip in text ink. One colour for every bar — the categories are nominal."""

    def __init__(self):
        super().__init__()
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(12)

    def set_rows(self, rows):
        """rows: [(name, detail, value, tooltip)] sorted largest first."""
        while self.lay.count():
            self.lay.takeAt(0).widget().deleteLater()
        if not rows:
            self.lay.addWidget(label("Nothing logged yet", 12, MUTED))
            return
        top = max(v for _, _, v, _ in rows) or 1
        for name, detail, value, tip in rows:
            row = QWidget()
            row.setToolTip(tip)
            g = QGridLayout(row)
            g.setContentsMargins(0, 0, 0, 0)
            g.setHorizontalSpacing(10)
            g.setVerticalSpacing(4)
            title = label(name, 12, TEXT, QFont.Weight.Medium)
            title.setMinimumWidth(10)
            g.addWidget(title, 0, 0)
            g.addWidget(label(f"${value:,.2f}", 12, TEXT, QFont.Weight.DemiBold), 0, 1,
                        alignment=Qt.AlignmentFlag.AlignRight)
            bar = QWidget()
            bar.setFixedHeight(6)
            bar.paintEvent = lambda _e, b=bar, f=value / top: self._paint(b, f)
            g.addWidget(bar, 1, 0, 1, 2)
            g.addWidget(label(detail, 11, MUTED), 2, 0, 1, 2)
            g.setColumnStretch(0, 1)
            self.lay.addWidget(row)

    @staticmethod
    def _paint(bar, frac):
        p = QPainter(bar)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(LINE))
        p.drawRoundedRect(QRectF(bar.rect()), 3, 3)
        p.setBrush(QColor(ACCENT))
        p.drawRoundedRect(QRectF(0, 0, max(6.0, bar.width() * frac), bar.height()), 3, 3)


def flat_button(text, tip=""):
    b = QPushButton(text)
    b.setToolTip(tip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setStyleSheet(f"""
        QPushButton {{ background:{LINE}; color:{TEXT}; border:none; border-radius:9px;
                       padding:6px 12px; font-size:12px; }}
        QPushButton:hover {{ background:#45475a; }}
        QPushButton:disabled {{ color:{MUTED}; }}
        QPushButton:checked {{ background:{ACCENT}; color:{CRUST}; }}
    """)
    return b


class Backdrop(QWidget):
    def paintEvent(self, _e):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, QColor(CRUST))
        g.setColorAt(1, QColor(BASE))
        p.fillRect(self.rect(), g)


class Dashboard(QMainWindow):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.hide_on_close = True
        self._history_at = 0.0   # monotonic stamp of the last log scan
        self._history_thread = None
        self.setWindowTitle(f"AI-Usage-Gauge v{VERSION}")
        self.setWindowIcon(QIcon(ICON_PATH))
        self.resize(460, 560)
        self.setMinimumSize(460, 560)

        backdrop = Backdrop()
        self.setCentralWidget(backdrop)
        outer = QVBoxLayout(backdrop)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
            QScrollArea, QScrollArea > QWidget > QWidget {{ background:transparent; }}
            QScrollBar:vertical {{ background:transparent; width:10px; margin:4px 2px; }}
            QScrollBar::handle:vertical {{ background:{LINE}; border-radius:3px; min-height:30px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
        """)
        outer.addWidget(scroll)
        page = QWidget()
        scroll.setWidget(page)
        self.page = QVBoxLayout(page)
        self.page.setContentsMargins(20, 18, 20, 20)
        self.page.setSpacing(14)

        self._build_header()
        self._build_limits()
        self._build_history()
        self.page.addStretch()

        self.controller.updated.connect(self._on_limits_result)
        self.controller.busy_changed.connect(self._on_busy)
        if self.controller.latest:
            self._on_limits_result(self.controller.latest)
        else:
            self.controller.refresh()

        self._clock = QTimer(self)  # keeps "updated Xm ago" and reset countdowns current
        self._clock.timeout.connect(self._redraw_times)
        self._clock.start(30_000)
        self._center_on_screen()

    # ---- layout ----

    def _build_header(self):
        row = QHBoxLayout()
        row.setSpacing(12)
        icon = QLabel()
        icon.setPixmap(gauge_pixmap(40, 62, 31, tile=True))
        row.addWidget(icon)
        box = QVBoxLayout()
        box.setSpacing(1)
        box.addWidget(label(f"AI-Usage-Gauge v{VERSION}", 18, TEXT, QFont.Weight.Bold))
        self.updated = label("Loading…", 11, MUTED)
        # Wrap, don't widen: a long offline error here used to push the whole
        # page wider than the window, and the right side got clipped.
        self.updated.setWordWrap(True)
        box.addWidget(self.updated)
        row.addLayout(box)
        row.addStretch()
        self.refresh_btn = flat_button("↻  Refresh", "Fetch plan limits and rescan session logs")
        self.refresh_btn.clicked.connect(self._refresh_all)
        row.addWidget(self.refresh_btn)
        self.page.addLayout(row)

    def _build_limits(self):
        card = Card("Plan limits", "Live from your Claude account")
        gauges = QHBoxLayout()
        gauges.setSpacing(20)
        self.session = LimitBlock(172, "5 HOUR")
        self.weekly = LimitBlock(172, "7 DAY")
        gauges.addStretch()
        gauges.addWidget(self.session, alignment=Qt.AlignmentFlag.AlignTop)
        gauges.addWidget(self.weekly, alignment=Qt.AlignmentFlag.AlignTop)
        gauges.addStretch()
        card.body.addLayout(gauges)
        self.extra = QVBoxLayout()
        self.extra.setSpacing(12)
        card.body.addLayout(self.extra)
        self.limits_error = label("", 12, TEXT2)
        self.limits_error.setWordWrap(True)
        self.limits_error.hide()
        card.body.addWidget(self.limits_error)
        self.page.addWidget(card)

    def _build_history(self):
        tiles = QGridLayout()
        tiles.setSpacing(10)
        self.tiles = {}
        for i, (key, name) in enumerate((("cost30", "Cost · 30 days"), ("costall", "Cost · all time"),
                                         ("sessions", "Sessions"), ("tokens30", "Tokens · 30 days"))):
            self.tiles[key] = StatTile(name)
            tiles.addWidget(self.tiles[key], i // 2, i % 2)
        self.page.addLayout(tiles)
        self.history_note = label("", 11, MUTED)
        self.history_note.setWordWrap(True)
        self.page.addWidget(self.history_note)

        daily = Card("Daily tokens", "Last 14 days · hover a column for details")
        self.table_btn = flat_button("Table", "Show the numbers as a table")
        self.table_btn.setCheckable(True)
        daily.head.addWidget(self.table_btn)
        self.daily_stack = QStackedWidget()
        self.chart = ColumnChart()
        self.daily_table = QTableWidget(0, 3)
        self.daily_table.setHorizontalHeaderLabels(["Day", "Tokens", "Est. cost"])
        self.daily_table.verticalHeader().setVisible(False)
        self.daily_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.daily_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.daily_table.setShowGrid(False)
        self.daily_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.daily_table.setStyleSheet(f"""
            QTableWidget {{ background:transparent; color:{TEXT}; border:none; font-size:12px; }}
            QHeaderView::section {{ background:transparent; color:{MUTED}; border:none;
                                    border-bottom:1px solid {LINE}; padding:6px; font-size:11px; }}
        """)
        self.daily_stack.addWidget(self.chart)
        self.daily_stack.addWidget(self.daily_table)
        self.daily_stack.setMinimumHeight(200)
        self.table_btn.toggled.connect(lambda on: self.daily_stack.setCurrentIndex(int(on)))
        daily.body.addWidget(self.daily_stack)
        self.page.addWidget(daily)

        models_card = Card("Cost by model", "Last 30 days")
        self.models = BarList()
        models_card.body.addWidget(self.models)
        self.page.addWidget(models_card)

        projects_card = Card("Cost by project", "All time · top 8")
        self.projects = BarList()
        projects_card.body.addWidget(self.projects)
        rescan = flat_button("Rescan logs", "Re-read the local session logs")
        rescan.clicked.connect(self.refresh_history)
        projects_card.head.addWidget(rescan)
        self.page.addWidget(projects_card)

    def _center_on_screen(self):
        screen = QApplication.primaryScreen().availableGeometry()
        frame = self.frameGeometry()
        frame.moveCenter(screen.center())
        self.move(frame.topLeft())

    # ---- window behaviour ----

    def showEvent(self, event):
        """Scanning every session log takes a moment, so it waits until the
        window is actually looked at — starting with --tray does no scan.

        Rescans on every open, throttled: the tray keeps this process alive for
        days, so scanning only once per launch left the history frozen at
        whatever it was the first time the window was opened."""
        super().showEvent(event)
        self.refresh_history()

    def closeEvent(self, event):
        """Closing hides to the tray; quit from the tray menu to exit."""
        if self.hide_on_close:
            event.ignore()
            self.hide()
        else:
            event.accept()

    # ---- plan limits ----

    def _on_busy(self, busy):
        # Not setEnabled(False): disabling the focused button hands focus to
        # the next widget and the scroll area jumps to it. The controller
        # already ignores clicks while a fetch is running.
        self.refresh_btn.setText("Refreshing…" if busy else "↻  Refresh")

    def _refresh_all(self):
        self.controller.refresh(force=True)
        self.refresh_history(force=True)

    def _redraw_times(self):
        if self.controller.latest:
            self._on_limits_result(self.controller.latest)
        if self.isVisible():  # a window left open all day should still age forward
            self.refresh_history()

    def _on_limits_result(self, result):
        usage = result.get("usage")
        text = f"Updated {fmt_ago(result.get('fetched_at', time.time()))}"
        if result.get("stale"):
            text += f"  ·  {WARN_GLYPH} offline copy ({result.get('error', 'offline')})"
        self.updated.setText(text if usage else f"{WARN_GLYPH} {result.get('error', 'No data')}")

        while self.extra.count():
            self.extra.takeAt(0).widget().deleteLater()
        lim = limits_of(usage)
        self.session.set_entry("session", lim.pop("session", None))
        self.weekly.set_entry("weekly_all", lim.pop("weekly_all", None))
        for kind, entry in lim.items():
            name = LIMIT_LABELS.get(kind, (kind.replace("_", " ").title(),))[0]
            self.extra.addWidget(MeterRow(name, f"{entry.get('percent', 0):.0f}%",
                                          entry.get("percent", 0), fmt_reset(entry.get("resets_at"))))
        spend = (usage or {}).get("spend") or {}
        if spend.get("enabled") and spend.get("limit"):
            scale = 10 ** spend.get("used", {}).get("exponent", 2)
            used = spend.get("used", {}).get("amount_minor", 0) / scale
            cap = spend.get("limit", {}).get("amount_minor", 0) / scale
            self.extra.addWidget(MeterRow("Extra usage", f"${used:,.2f} of ${cap:,.2f}",
                                          spend.get("percent", 0), "Pay-as-you-go beyond the plan"))
        self.limits_error.setVisible(not usage)
        self.limits_error.setText("" if usage else f"Couldn't load plan limits: {result.get('error')}")

    # ---- history ----

    def refresh_history(self, force=False):
        if self._history_thread is not None and self._history_thread.isRunning():
            return
        if not force and time.monotonic() - self._history_at < HISTORY_MIN_S:
            return
        self._history_at = time.monotonic()
        self.history_note.setText("Scanning local session logs…")
        if self._history_thread is not None:
            self._history_thread.wait()  # a QThread freed mid-teardown aborts the process
        self._history_thread = Worker(lambda: core.scan_history(days=30))
        self._history_thread.done.connect(self._on_history_result)
        self._history_thread.start()

    def _on_history_result(self, result):
        self.tiles["cost30"].value.setText(f"${result['total_cost_30d']:,.2f}")
        self.tiles["costall"].value.setText(f"${result['total_cost_all_time']:,.2f}")
        self.tiles["sessions"].value.setText(f"{result['session_count']:,}")
        self.tiles["tokens30"].value.setText(fmt_tokens(result["total_tokens_30d"]))

        today = datetime.now(timezone.utc).date()  # the core buckets days in UTC
        days = []
        for back in range(13, -1, -1):
            d = today - timedelta(days=back)
            b = result["by_day"].get(d.isoformat(), {})
            days.append((d, b.get("tokens", 0), b.get("cost", 0.0)))
        self.chart.set_days(days)
        self.daily_table.setRowCount(len(days))
        for i, (d, tokens, cost) in enumerate(reversed(days)):
            for col, val in enumerate((f"{d:%a %b} {d.day}", f"{tokens:,}", f"${cost:,.2f}")):
                item = QTableWidgetItem(val)
                if col:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.daily_table.setItem(i, col, item)

        rows = []
        for model_id, m in sorted(result["by_model"].items(), key=lambda kv: -kv[1]["cost"]):
            detail = (f"in {fmt_tokens(m['input'])} · out {fmt_tokens(m['output'])} · "
                      f"cache {fmt_tokens(m['cache_read'])} read / {fmt_tokens(m['cache_write'])} write")
            name = model_id if m["known"] else f"{model_id}  (estimated price)"
            rows.append((name, detail, m["cost"], f"{model_id}: ${m['cost']:,.4f}"))
        self.models.set_rows(rows)

        home = str(Path.home())
        projects = sorted(result["by_project"].items(), key=lambda kv: -kv[1]["cost"])
        rows = [(cwd.replace(home, "~", 1), f"{p['sessions']:,} session{'s' * (p['sessions'] != 1)}",
                 p["cost"], cwd) for cwd, p in projects[:8]]
        rest = projects[8:]
        if rest:
            rows.append((f"Other ({len(rest)} projects)",
                         f"{sum(p['sessions'] for _, p in rest):,} sessions",
                         sum(p["cost"] for _, p in rest), "Everything outside the top 8"))
        self.projects.set_rows(rows)

        note = "Costs are estimates at API list prices, from local session logs."
        if result["unknown_models"]:
            note += f" Unrecognised models use {core.DEFAULT_PRICING_MODEL} prices."
        self.history_note.setText(note)


def present(win):
    win.show()
    win.setWindowState(win.windowState() & ~Qt.WindowState.WindowMinimized)
    win.raise_()
    win.activateWindow()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AI-Usage-Gauge")
    app.setApplicationDisplayName("AI-Usage-Gauge")
    app.setDesktopFileName("claude-usage")
    app.setWindowIcon(QIcon(ICON_PATH))
    app.setStyle("Fusion")  # consistent look regardless of the desktop's native Qt theme

    # One running copy: a later launch (menu entry, autostart) asks the
    # running one to show its window, then exits.
    probe = QLocalSocket()
    probe.connectToServer(SOCKET_NAME)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.flush()
        probe.waitForBytesWritten(300)
        return 0

    app.setQuitOnLastWindowClosed(False)  # closing the window leaves the tray up

    controller = UsageController(app)
    win = Dashboard(controller)
    tray = UsageTray(controller, app)
    tray.open_requested.connect(lambda: present(win))
    tray.quit_requested.connect(app.quit)

    def on_tray_unavailable():
        """No tray host turned up. Fall back to a plain window app rather than
        leaving an invisible process with no way to reach it."""
        win.hide_on_close = False
        app.setQuitOnLastWindowClosed(True)
        present(win)

    tray.unavailable.connect(on_tray_unavailable)
    # Clears the stale socket file a previously killed instance left behind.
    QLocalServer.removeServer(SOCKET_NAME)
    server = QLocalServer(app)
    server.newConnection.connect(lambda: (server.nextPendingConnection().deleteLater(), present(win)))
    server.listen(SOCKET_NAME)

    if "--tray" not in sys.argv:
        present(win)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
