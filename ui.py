"""ui.py - main window, file rows, drop zone and the advanced-options dialog."""
from __future__ import annotations

import dataclasses
import itertools
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QActionGroup, QColor, QDesktopServices, QFontDatabase, QPainter
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSystemTrayIcon,
                               QTabWidget, QToolButton, QVBoxLayout, QWidget)

import core
from core import FORMATS, FORMATS_BY_KEY, Options
from theme import ThemeManager, make_icon
from workers import ConvertWorker, ProbeWorker, UpdateWorker

CONVERTIBLE = ("ready", "failed", "cancelled")
SOFT_STATES = ("done", "failed", "cancelled")


def repolish(w: QWidget):
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def open_path(p: Path):
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))


def reveal(p: Path):
    if os.name == "nt" and p.exists():
        subprocess.Popen(["explorer", "/select,", str(p)])
    else:
        open_path(p.parent if p.is_file() else p)


def fill_format_combo(combo: QComboBox, kind: str, select_key: str):
    combo.blockSignals(True)
    combo.clear()
    allowed = {"video": ["video", "audio", "image"], "audio": ["audio"], "image": ["image"]}[kind]
    for cat in allowed:
        combo.addItem(cat.upper())
        combo.model().item(combo.count() - 1).setEnabled(False)  # group header
        for f in FORMATS:
            if f.category == cat:
                combo.addItem(f.label, f.key)
    idx = combo.findData(select_key)
    if idx < 0:
        idx = next(i for i in range(combo.count()) if combo.itemData(i))
    combo.setCurrentIndex(idx)
    combo.blockSignals(False)


class ElidedLabel(QLabel):
    """Single-line label that truncates with an ellipsis instead of forcing the layout wider."""

    def __init__(self, text: str = ""):
        super().__init__()
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)

    def setText(self, text: str):  # noqa: N802
        self._full = text
        self.setToolTip(text)
        self.update()

    def sizeHint(self):  # noqa: N802
        return QSize(120, self.fontMetrics().height() + 2)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(40, self.fontMetrics().height() + 2)

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setFont(self.font())
        pal = QApplication.palette()
        tone = self.property("tone")
        if tone == "ok":
            col = pal.highlight().color()
        elif tone == "err":
            col = QColor(QApplication.instance().property("danger_color") or "#B3261E")
        elif self.objectName() == "muted":
            col = pal.placeholderText().color()
        else:
            col = pal.windowText().color()
        p.setPen(col)
        text = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideMiddle, self.width())
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)


# --------------------------------------------------------------------------- #
#  Drop zone
# --------------------------------------------------------------------------- #
class DropZone(QFrame):
    browse = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("dropzone")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)

        self.full = QWidget()
        fl = QVBoxLayout(self.full)
        fl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        fl.setSpacing(14)
        title = QLabel("Convert video, audio & images")
        title.setObjectName("h2")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.btn = QPushButton("＋   Choose Files")
        self.btn.setObjectName("primary")
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.setMinimumWidth(220)
        hint = QLabel("or drop files / folders anywhere in this window")
        hint.setObjectName("muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        fl.addWidget(title)
        fl.addWidget(self.btn, 0, Qt.AlignmentFlag.AlignCenter)
        fl.addWidget(hint)

        self.compact = QWidget()
        cl = QHBoxLayout(self.compact)
        cl.setContentsMargins(0, 0, 0, 0)
        self.btn2 = QPushButton("＋  Add more files")
        self.btn2.setCursor(Qt.CursorShape.PointingHandCursor)
        hint2 = QLabel("or drop them anywhere in this window")
        hint2.setObjectName("muted")
        cl.addWidget(self.btn2)
        cl.addWidget(hint2)
        cl.addStretch(1)

        lay.addWidget(self.full)
        lay.addWidget(self.compact)
        self.btn.clicked.connect(self.browse)
        self.btn2.clicked.connect(self.browse)
        self.set_compact(False)

    def set_compact(self, compact: bool):
        self.full.setVisible(not compact)
        self.compact.setVisible(compact)
        self.setMaximumHeight(76 if compact else 16777215)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed if compact else QSizePolicy.Policy.Expanding)

    def set_drag(self, on: bool):
        self.setProperty("drag", on)
        repolish(self)


# --------------------------------------------------------------------------- #
#  One file in the list
# --------------------------------------------------------------------------- #
class FileRow(QFrame):
    removeRequested = Signal(object)
    optionsRequested = Signal(object)
    formatChanged = Signal(object)

    def __init__(self, path: Path):
        super().__init__()
        self.setObjectName("card")
        self.path = path
        self.token = 0
        self.kind = core.kind_of(path) or "video"
        self.info: core.MediaInfo | None = None
        self.opts = Options()
        self.state = "probing"
        self.out_path: Path | None = None
        self.fraction = 0.0
        self.log = ""
        self._base_meta = "Reading file…"

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(14)

        self.badge = QLabel((path.suffix.lstrip(".").upper() or "FILE")[:4])
        self.badge.setObjectName("badge")
        self.badge.setFixedSize(54, 54)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

        mid = QVBoxLayout()
        mid.setSpacing(4)
        self.name = ElidedLabel(path.name)
        f = self.name.font()
        f.setBold(True)
        self.name.setFont(f)
        self.meta = ElidedLabel("Reading file…")
        self.meta.setObjectName("muted")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        self.bar.setVisible(False)
        mid.addWidget(self.name)
        mid.addWidget(self.meta)
        mid.addWidget(self.bar)

        out_lbl = QLabel("Output:")
        out_lbl.setObjectName("muted")
        self.out_label = out_lbl
        self.combo = QComboBox()
        self.combo.setMinimumWidth(170)
        self.gear = QToolButton()
        self.gear.setText("⚙")
        self.gear.setToolTip("Advanced options")
        self.gear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open = QToolButton()
        self.btn_open.setText("Open")
        self.btn_open.setObjectName("link")
        self.btn_folder = QToolButton()
        self.btn_folder.setText("Folder")
        self.btn_folder.setObjectName("link")
        self.btn_open.setVisible(False)
        self.btn_folder.setVisible(False)
        self.btn_remove = QToolButton()
        self.btn_remove.setText("✕")
        self.btn_remove.setToolTip("Remove")
        self.btn_remove.setCursor(Qt.CursorShape.PointingHandCursor)

        lay.addWidget(self.badge)
        lay.addLayout(mid, 1)
        lay.addWidget(self.btn_open)
        lay.addWidget(self.btn_folder)
        lay.addWidget(out_lbl)
        lay.addWidget(self.combo)
        lay.addWidget(self.gear)
        lay.addWidget(self.btn_remove)

        self.combo.currentIndexChanged.connect(self._fmt_changed)
        self.gear.clicked.connect(lambda: self.optionsRequested.emit(self))
        self.btn_remove.clicked.connect(lambda: self.removeRequested.emit(self))
        self.btn_open.clicked.connect(lambda: self.out_path and open_path(self.out_path))
        self.btn_folder.clicked.connect(lambda: self.out_path and reveal(self.out_path))
        self.combo.setEnabled(False)
        self.gear.setEnabled(False)

    # -- helpers ---------------------------------------------------------- #
    @property
    def fmt(self) -> core.OutFmt:
        return FORMATS_BY_KEY[self.combo.currentData()]

    def _status(self, text: str, tone: str = ""):
        self.meta.setText(text)
        self.meta.setProperty("tone", tone)
        repolish(self.meta)

    def _fmt_changed(self):
        if self.state in SOFT_STATES:
            self.mark_ready()
        self.formatChanged.emit(self)

    def _lock(self, locked: bool):
        self.combo.setEnabled(not locked)
        self.gear.setEnabled(not locked)

    # -- state transitions ------------------------------------------------ #
    def set_info(self, info: core.MediaInfo, defaults: dict):
        self.info = info
        if self.kind == "video" and not info.has_video and info.has_audio:
            self.kind = "audio"
        elif self.kind != "image" and info.has_video and info.duration == 0 and not info.has_audio:
            self.kind = "image"
        bits = []
        if info.has_video and info.width:
            bits.append(f"{info.width}×{info.height}")
        if info.duration:
            bits.append(core.fmt_duration(info.duration))
        bits.append(core.human_size(info.size))
        self._base_meta = "  ·  ".join(bits)
        fill_format_combo(self.combo, self.kind, defaults.get(self.kind, ""))
        self.mark_ready()

    def set_unreadable(self, msg: str):
        self.state = "unreadable"
        self._status(f"Can't read this file — {msg}", "err")
        self._lock(True)
        self.combo.setVisible(False)
        self.gear.setVisible(False)
        self.out_label.setVisible(False)

    def mark_ready(self):
        self.state = "ready"
        self.fraction = 0.0
        self.bar.setVisible(False)
        self.btn_open.setVisible(False)
        self.btn_folder.setVisible(False)
        self._lock(False)
        self.btn_remove.setToolTip("Remove")
        self._status(self._base_meta)

    def set_queued(self):
        self.state = "queued"
        self._lock(True)
        self._status("Waiting in queue…")

    def set_running(self):
        self.state = "running"
        self.fraction = 0.0
        self._lock(True)
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.btn_remove.setToolTip("Cancel this conversion")
        self._status("Starting…")

    def set_progress(self, frac: float, text: str):
        if frac < 0:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))
            self.fraction = frac
        self._status(text)

    def set_done(self, out: Path):
        self.state, self.out_path, self.fraction = "done", out, 1.0
        self.bar.setVisible(False)
        self._lock(False)
        self.btn_remove.setToolTip("Remove")
        self.btn_open.setVisible(True)
        self.btn_folder.setVisible(True)
        txt = f"✓ Done  ·  {out.name}"
        try:
            new = out.stat().st_size
            old = self.info.size if self.info else 0
            txt += f"  ·  {core.human_size(new)}"
            if old and self.fmt.category != "image":
                txt += f"  ({(new - old) / old * 100:+.0f}%)"
        except OSError:
            pass
        self._status(txt, "ok")

    def set_failed(self, short: str, log: str):
        self.state, self.log, self.fraction = "failed", log, 1.0
        self.bar.setVisible(False)
        self._lock(False)
        self.btn_remove.setToolTip("Remove")
        self._status(f"✗ Failed — {short}", "err")
        self.meta.setToolTip(log or short)

    def set_cancelled(self):
        self.state, self.fraction = "cancelled", 1.0
        self.bar.setVisible(False)
        self._lock(False)
        self.btn_remove.setToolTip("Remove")
        self._status("Cancelled")


# --------------------------------------------------------------------------- #
#  Advanced options dialog
# --------------------------------------------------------------------------- #
RES_CHOICES = [("original", "Original size"), ("2160", "2160p (4K)"), ("1440", "1440p (2K)"),
               ("1080", "1080p (Full HD)"), ("720", "720p (HD)"), ("480", "480p"), ("360", "360p"),
               ("240", "240p"), ("custom", "Custom…")]
RATE_CHOICES = [(0, "Original"), (22050, "22.05 kHz"), (32000, "32 kHz"), (44100, "44.1 kHz"),
                (48000, "48 kHz"), (96000, "96 kHz")]
CHANNEL_CHOICES = [(0, "Original"), (1, "Mono"), (2, "Stereo")]


def _combo(choices, current) -> QComboBox:
    c = QComboBox()
    for key, label in choices:
        c.addItem(label, key)
    i = c.findData(current)
    c.setCurrentIndex(max(i, 0))
    return c


def _spin(lo, hi, val, special="", suffix="", step=1) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    if special:
        s.setSpecialValueText(special)
    if suffix:
        s.setSuffix(suffix)
    s.setValue(val)
    return s


class OptionsDialog(QDialog):
    def __init__(self, parent, row: FileRow, encoders, make_preview):
        super().__init__(parent)
        self.row, self.fmt, self.base = row, row.fmt, dataclasses.replace(row.opts)
        self.make_preview = make_preview
        cat = self.fmt.category
        o = self.base
        self.setWindowTitle(f"Advanced options — {row.path.name}")
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        head = QLabel(f"Convert to {self.fmt.label}")
        head.setObjectName("h2")
        root.addWidget(head)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        def page(title):
            w = QWidget()
            f = QFormLayout(w)
            f.setContentsMargins(18, 16, 18, 16)
            f.setHorizontalSpacing(20)
            f.setVerticalSpacing(12)
            f.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
            self.tabs.addTab(w, title)
            return w, f

        def add(form, label, widget):
            form.addRow(label, widget)
            return widget

        # ---- Video / picture tab
        vpage, vf = page("Picture" if cat == "image" else "Video")
        self.vcodec = add(vf, "Video codec", _combo([(k, core.VCODEC_LABEL[k]) for k in self.fmt.vcodecs], o.vcodec)) \
            if self.fmt.vcodecs else QComboBox()
        hw = core.hw_choices(encoders)
        self.hw = add(vf, "Encoder", _combo([("none", "CPU (software)")] + [(k, core.HW_LABELS[k]) for k in hw], o.hw))
        self.quality = add(vf, "Quality", _combo(list(enumerate(core.QUALITY_LABELS)), o.quality))
        self.speed = add(vf, "Encoding speed", _combo(list(enumerate(core.SPEED_LABELS)), o.speed))
        self.vbitrate = add(vf, "Target bitrate", _spin(0, 200000, o.vbitrate, "Auto (use quality)", " kbps", 100))
        self.res = add(vf, "Size", _combo(RES_CHOICES, o.resolution))
        size_w = QWidget()
        sl = QHBoxLayout(size_w)
        sl.setContentsMargins(0, 0, 0, 0)
        self.width_sp = _spin(0, 16384, o.width, "auto", " px")
        self.height_sp = _spin(0, 16384, o.height, "auto", " px")
        sl.addWidget(self.width_sp)
        sl.addWidget(QLabel("×"))
        sl.addWidget(self.height_sp)
        self.custom_size = add(vf, "Width × Height", size_w)
        self.fps = add(vf, "Frame rate", _spin(0, 240, o.fps, "Original", " fps"))
        self.rotate = add(vf, "Rotate / flip", _combo(list(core.ROTATE_LABEL.items()), o.rotate))

        # ---- Audio tab
        apage, af = page("Audio")
        self.remove_audio = add(af, "", QCheckBox("Remove audio"))
        self.remove_audio.setChecked(o.remove_audio)
        self.acodec = add(af, "Audio codec", _combo([(k, core.ACODEC_LABEL[k]) for k in self.fmt.acodecs], o.acodec)) \
            if self.fmt.acodecs else QComboBox()
        self.abitrate = add(af, "Bitrate", _spin(0, 640, o.abitrate, "Auto", " kbps", 32))
        self.rate = add(af, "Sample rate", _combo(RATE_CHOICES, o.samplerate))
        self.channels = add(af, "Channels", _combo(CHANNEL_CHOICES, o.channels))
        self.volume = add(af, "Volume", _spin(0, 400, o.volume, "", " %", 10))
        self.normalize = add(af, "", QCheckBox("Normalize loudness (EBU R128)"))
        self.normalize.setChecked(o.normalize)

        # ---- Trim tab
        tpage, tf = page("Frame" if cat == "image" else "Trim")
        self.t_start = add(tf, "Frame at" if cat == "image" else "Start", QLineEdit(o.trim_start))
        self.t_start.setPlaceholderText("00:00:00")
        self.t_end = add(tf, "End", QLineEdit(o.trim_end))
        self.t_end.setPlaceholderText("end of file")
        note = QLabel("Use seconds (90), MM:SS (1:30) or HH:MM:SS (0:01:30.5).")
        note.setObjectName("muted")
        tf.addRow("", note)

        # ---- Advanced tab
        xpage, xf = page("Advanced")
        self.strip = add(xf, "", QCheckBox("Remove metadata (title, tags, location…)"))
        self.strip.setChecked(o.strip_metadata)
        self.custom = add(xf, "Extra FFmpeg args", QLineEdit(o.custom_args))
        self.custom.setPlaceholderText('e.g. -metadata title="My clip"')
        pv = QPushButton("Show FFmpeg command")
        pv.clicked.connect(self._preview)
        xf.addRow("", pv)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.preview.setFixedHeight(110)
        xf.addRow(self.preview)

        # ---- visibility per output type
        def show_row(form, widget, visible):
            widget.setVisible(visible)
            lbl = form.labelForField(widget)
            if lbl:
                lbl.setVisible(visible)

        is_gif = self.fmt.key == "gif"
        video_like = cat == "video"
        show_row(vf, self.vcodec, bool(self.fmt.vcodecs))
        show_row(vf, self.hw, video_like and not is_gif and len(hw) > 0)
        show_row(vf, self.quality, not is_gif)
        show_row(vf, self.speed, video_like and not is_gif)
        show_row(vf, self.vbitrate, video_like and not is_gif)
        show_row(vf, self.fps, video_like)
        show_row(af, self.remove_audio, video_like)
        show_row(af, self.acodec, len(self.fmt.acodecs) > 1)
        lossy_only = cat == "audio" and self.fmt.acodecs[:1] in (("wav",), ("flac",))
        show_row(af, self.abitrate, not lossy_only)
        self.tabs.setTabVisible(self.tabs.indexOf(vpage), cat in ("video", "image"))
        self.tabs.setTabVisible(self.tabs.indexOf(apage), cat in ("video", "audio") and not is_gif)
        self.tabs.setCurrentIndex(self.tabs.indexOf(vpage if cat != "audio" else apage))

        for w in (self.vcodec, self.res, self.acodec):
            if isinstance(w, QComboBox):
                w.currentIndexChanged.connect(self._sync)
        self.remove_audio.toggled.connect(self._sync)
        self._vf, self._af = vf, af

        # ---- footer
        self.apply_all = QCheckBox(f"Apply to all {cat} outputs in the list")
        root.addWidget(self.apply_all)
        foot = QHBoxLayout()
        reset = QPushButton("Reset")
        reset.clicked.connect(self._reset)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self._save)
        foot.addWidget(reset)
        foot.addStretch(1)
        foot.addWidget(cancel)
        foot.addWidget(save)
        root.addLayout(foot)
        self._sync()

    def _sync(self):
        vcopy = self.vcodec.isVisibleTo(self) and self.vcodec.currentData() == "copy"
        for w in (self.quality, self.speed, self.vbitrate, self.hw, self.res, self.fps, self.rotate, self.custom_size):
            w.setEnabled(not vcopy)
        self.custom_size.setVisible(self.res.currentData() == "custom")
        lbl = self._vf.labelForField(self.custom_size)
        if lbl:
            lbl.setVisible(self.res.currentData() == "custom")
        no_audio = self.remove_audio.isVisibleTo(self) and self.remove_audio.isChecked()
        acopy = self.acodec.isVisibleTo(self) and self.acodec.currentData() == "copy"
        for w in (self.acodec, self.abitrate, self.rate, self.channels, self.volume, self.normalize):
            w.setEnabled(not no_audio)
        for w in (self.abitrate, self.rate, self.channels, self.volume, self.normalize):
            w.setEnabled(not no_audio and not acopy)

    def collect(self) -> Options:
        o = dataclasses.replace(self.base)
        if self.vcodec.count():
            o.vcodec = self.vcodec.currentData()
        o.hw = self.hw.currentData()
        o.quality, o.speed = self.quality.currentData(), self.speed.currentData()
        o.vbitrate = self.vbitrate.value()
        o.resolution = self.res.currentData()
        o.width, o.height, o.fps = self.width_sp.value(), self.height_sp.value(), self.fps.value()
        o.rotate = self.rotate.currentData()
        o.remove_audio = self.remove_audio.isChecked()
        if self.acodec.count():
            o.acodec = self.acodec.currentData()
        o.abitrate, o.samplerate = self.abitrate.value(), self.rate.currentData()
        o.channels, o.volume, o.normalize = self.channels.currentData(), self.volume.value(), self.normalize.isChecked()
        o.trim_start, o.trim_end = self.t_start.text().strip(), self.t_end.text().strip()
        o.strip_metadata, o.custom_args = self.strip.isChecked(), self.custom.text().strip()
        return o

    def _preview(self):
        try:
            self.preview.setPlainText(self.make_preview(self.row, self.collect()))
        except Exception as e:  # noqa: BLE001
            self.preview.setPlainText(f"Can't build command: {e}")

    def _reset(self):
        d = Options()
        self.vcodec.setCurrentIndex(0)
        self.hw.setCurrentIndex(0)
        self.quality.setCurrentIndex(d.quality)
        self.speed.setCurrentIndex(d.speed)
        self.vbitrate.setValue(0)
        self.res.setCurrentIndex(0)
        self.width_sp.setValue(0)
        self.height_sp.setValue(0)
        self.fps.setValue(0)
        self.rotate.setCurrentIndex(0)
        self.remove_audio.setChecked(False)
        if self.acodec.count():
            self.acodec.setCurrentIndex(0)
        self.abitrate.setValue(0)
        self.rate.setCurrentIndex(0)
        self.channels.setCurrentIndex(0)
        self.volume.setValue(100)
        self.normalize.setChecked(False)
        self.t_start.clear()
        self.t_end.clear()
        self.strip.setChecked(False)
        self.custom.clear()

    def _save(self):
        try:
            s, e = core.parse_time(self.t_start.text()), core.parse_time(self.t_end.text())
            if s is not None and e is not None and e <= s:
                raise ValueError("The end time must be after the start time.")
        except ValueError as err:
            QMessageBox.warning(self, "Trim", str(err))
            return
        try:
            core.split_args(self.custom.text())
        except ValueError as err:
            QMessageBox.warning(self, "Extra arguments", f"Couldn't parse the extra arguments: {err}")
            return
        self.accept()


# --------------------------------------------------------------------------- #
#  Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, app: QApplication, theme: ThemeManager, settings: QSettings):
        super().__init__()
        self.app, self.theme, self.s = app, theme, settings
        self.setWindowTitle(core.APP_NAME)
        self.setWindowIcon(make_icon())
        self.setAcceptDrops(True)
        self.setMinimumSize(900, 600)
        self.resize(1040, 780)

        self.rows: list[FileRow] = []
        self.by_token: dict[int, FileRow] = {}
        self._tokens = itertools.count(1)
        self.install: core.FFmpegInstall | None = None
        self.encoders: frozenset = frozenset()
        self.active: dict[FileRow, ConvertWorker] = {}
        self.queue: list[FileRow] = []
        self.batch: list[FileRow] = []
        self.cancelled_batch = False
        self.updater: UpdateWorker | None = None
        self.staged_version: str | None = None
        self.backlog: list[FileRow] = []
        self.last_dir: Path | None = None

        v = lambda k, d: self.s.value(k, d, type=str)  # noqa: E731
        self.out_dir: Path | None = Path(v("out_dir", "")) if v("out_dir", "") else None
        self.notify_enabled = self.s.value("notify", True, type=bool)
        self.auto_update = self.s.value("auto_update", True, type=bool)
        self.parallel = max(1, min(4, self.s.value("parallel", 1, type=int)))
        self.defaults = {"video": v("fmt_video", "mp4"), "audio": v("fmt_audio", "mp3"), "image": v("fmt_image", "jpg")}

        self.probe = ProbeWorker(self)
        self.probe.probed.connect(self._on_probed)
        self.probe.start()

        self._build_ui()
        self._build_tray()
        try:
            geo = self.s.value("geometry")
            if geo:
                self.restoreGeometry(geo)
        except Exception:  # noqa: BLE001
            pass

        # Finish any update that was downloaded last session, then find FFmpeg.
        try:
            core.apply_staged()
        except Exception:  # noqa: BLE001
            pass
        self._reload_ffmpeg()
        QTimer.singleShot(400, lambda: self._start_update())
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self._start_update())
        self.timer.start(6 * 60 * 60 * 1000)  # re-check every 6 hours while open

    # ------------------------------------------------------------------ UI #
    def _build_ui(self):
        central = QWidget()
        central.setObjectName("root")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(16)

        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(make_icon().pixmap(34, 34))
        title = QLabel(core.APP_NAME)
        title.setObjectName("h1")
        head.addWidget(logo)
        head.addWidget(title)
        head.addStretch(1)
        self.dl_bar = QProgressBar()
        self.dl_bar.setFixedWidth(110)
        self.dl_bar.setTextVisible(False)
        self.dl_bar.setRange(0, 100)
        self.dl_bar.setVisible(False)
        self.pill = QPushButton("Starting…")
        self.pill.setObjectName("pill")
        self.pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pill.setToolTip("Click to check for FFmpeg updates")
        self.pill.clicked.connect(self._pill_clicked)
        self.theme_combo = QComboBox()
        for key, label in (("system", "System theme"), ("light", "Light"), ("dark", "Dark")):
            self.theme_combo.addItem(label, key)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(self.theme.mode)))
        self.theme_combo.currentIndexChanged.connect(self._theme_changed)
        self.menu_btn = QToolButton()
        self.menu_btn.setText("⚙  Settings")
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_btn.setMenu(self._build_menu())
        head.addWidget(self.dl_bar)
        head.addWidget(self.pill)
        head.addWidget(self.theme_combo)
        head.addWidget(self.menu_btn)
        root.addLayout(head)

        self.drop = DropZone()
        self.drop.browse.connect(self.choose_files)
        root.addWidget(self.drop, 1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        self.list_lay = QVBoxLayout(holder)
        self.list_lay.setContentsMargins(0, 0, 6, 0)
        self.list_lay.setSpacing(10)
        self.list_lay.addStretch(1)
        self.scroll.setWidget(holder)
        self.scroll.viewport().setAutoFillBackground(False)
        holder.setAutoFillBackground(False)
        self.scroll.setVisible(False)
        root.addWidget(self.scroll, 1)

        # bottom bar
        self.bottom = QFrame()
        self.bottom.setObjectName("card")
        bl = QVBoxLayout(self.bottom)
        bl.setContentsMargins(16, 12, 16, 12)
        bl.setSpacing(10)
        ov = QHBoxLayout()
        self.overall_lbl = QLabel("")
        self.overall_lbl.setObjectName("muted")
        self.overall = QProgressBar()
        self.overall.setRange(0, 1000)
        self.overall.setTextVisible(False)
        ov.addWidget(self.overall_lbl)
        ov.addWidget(self.overall, 1)
        self.overall_box = QWidget()
        self.overall_box.setLayout(ov)
        ov.setContentsMargins(0, 0, 0, 0)
        self.overall_box.setVisible(False)
        bl.addWidget(self.overall_box)

        bar = QHBoxLayout()
        self.out_btn = QToolButton()
        self.out_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        om = QMenu(self)
        om.addAction("Same folder as the original file", lambda: self._set_out_dir(None))
        om.addAction("Choose a folder…", self._pick_out_dir)
        om.addSeparator()
        om.addAction("Open output folder", self._open_out_dir)
        self.out_btn.setMenu(om)
        self.set_all = QComboBox()
        self.set_all.setPlaceholderText("Set all to…")
        fill_format_combo(self.set_all, "video", "mp4")
        self.set_all.setCurrentIndex(-1)
        self.set_all.activated.connect(self._set_all_formats)
        self.clear_btn = QPushButton("Clear all")
        self.clear_btn.clicked.connect(self.clear_all)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_all)
        self.cancel_btn.setVisible(False)
        self.convert_btn = QPushButton("Convert")
        self.convert_btn.setObjectName("primary")
        self.convert_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.convert_btn.clicked.connect(self.convert_all)
        bar.addWidget(self.out_btn)
        bar.addWidget(self.set_all)
        bar.addStretch(1)
        bar.addWidget(self.clear_btn)
        bar.addWidget(self.cancel_btn)
        bar.addWidget(self.convert_btn)
        bl.addLayout(bar)
        self.bottom.setVisible(False)
        root.addWidget(self.bottom)
        self._update_out_btn()

    def _build_menu(self) -> QMenu:
        m = QMenu(self)
        self.act_notify = m.addAction("Notify when conversions finish")
        self.act_notify.setCheckable(True)
        self.act_notify.setChecked(self.notify_enabled)
        self.act_notify.toggled.connect(self._set_notify)
        self.act_auto = m.addAction("Update FFmpeg automatically")
        self.act_auto.setCheckable(True)
        self.act_auto.setChecked(self.auto_update)
        self.act_auto.toggled.connect(self._set_auto_update)
        par = m.addMenu("Parallel conversions")
        grp = QActionGroup(self)
        for n in (1, 2, 3, 4):
            a = par.addAction(str(n))
            a.setCheckable(True)
            a.setChecked(n == self.parallel)
            grp.addAction(a)
            a.triggered.connect(lambda _=False, n=n: self._set_parallel(n))
        m.addSeparator()
        m.addAction("Check for FFmpeg updates now", lambda: self._start_update(manual=True))
        m.addAction("Download / reinstall latest FFmpeg", lambda: self._start_update(manual=True, force=True))
        m.addAction("Show FFmpeg location", self._show_ffmpeg_location)
        m.addSeparator()
        m.addAction("About", self._about)
        return m

    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip(core.APP_NAME)
        self._tray_menu = QMenu()
        self._tray_menu.addAction("Show window", self._show_window)
        self._tray_menu.addAction("Quit", self.close)
        self.tray.setContextMenu(self._tray_menu)
        self.tray.activated.connect(lambda r: self._show_window() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.messageClicked.connect(self._on_notification_clicked)
        self.tray.show()

    # -------------------------------------------------------------- settings #
    def _theme_changed(self):
        mode = self.theme_combo.currentData()
        self.s.setValue("theme", mode)
        self.theme.set_mode(mode)

    def _set_notify(self, on):
        self.notify_enabled = on
        self.s.setValue("notify", on)

    def _set_auto_update(self, on):
        self.auto_update = on
        self.s.setValue("auto_update", on)

    def _set_parallel(self, n):
        self.parallel = n
        self.s.setValue("parallel", n)
        self._pump()

    def _set_out_dir(self, d: Path | None):
        self.out_dir = d
        self.s.setValue("out_dir", str(d) if d else "")
        self._update_out_btn()

    def _pick_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Choose output folder", str(self.out_dir or Path.home()))
        if d:
            self._set_out_dir(Path(d))

    def _open_out_dir(self):
        if self.out_dir:
            open_path(self.out_dir)
        elif self.last_dir:
            open_path(self.last_dir)
        elif self.rows:
            open_path(self.rows[0].path.parent)

    def _update_out_btn(self):
        txt = f"Save to: {self.out_dir}" if self.out_dir else "Save to: same folder as original"
        self.out_btn.setText(txt + "  ▾")
        self.out_btn.setToolTip("Where converted files are saved")

    def _about(self):
        v = self.install.version if self.install else "not installed"
        src = {"managed": "managed by this app", "portable": "portable copy", "system": "system PATH"}.get(
            self.install.source if self.install else "", "")
        QMessageBox.about(self, f"About {core.APP_NAME}",
                          f"<b>{core.APP_NAME}</b><br>A friendly front-end for FFmpeg.<br><br>"
                          f"FFmpeg: {v} {f'({src})' if src else ''}<br>"
                          f"Data folder: {core.app_data_dir()}")

    # ----------------------------------------------------------------- FFmpeg #
    def _reload_ffmpeg(self):
        self.install = core.locate_ffmpeg()
        if self.install:
            self.encoders = core.list_encoders(self.install.ffmpeg)
            ver = core.numeric_prefix(self.install.version) or (self.install.version or "")[:14]
            self.pill.setText(f"●  FFmpeg {ver}")
            self.pill.setToolTip(f"{self.install.ffmpeg}\nClick to check for updates")
            for r in self.backlog:
                self._queue_probe(r)
            self.backlog.clear()
        else:
            self.encoders = frozenset()
            self.pill.setText("FFmpeg missing — click to set up")
        self._refresh()

    def _show_ffmpeg_location(self):
        if self.install:
            reveal(self.install.ffmpeg)

    def _pill_clicked(self):
        self._start_update(manual=True)

    def _start_update(self, manual: bool = False, force: bool = False):
        if self.updater and self.updater.isRunning():
            return
        if not manual and not self.auto_update and self.install:
            return
        self.updater = UpdateWorker(auto_install=self.auto_update or manual, force=force, parent=self)
        u = self.updater
        u.status.connect(self._set_pill_status)
        u.progress.connect(self._on_dl_progress)
        u.staged.connect(self._on_staged)
        u.up_to_date.connect(lambda v, m=manual: self._on_up_to_date(v, m))
        u.update_available.connect(self._on_update_available)
        u.failed.connect(lambda msg, first, m=manual: self._on_update_failed(msg, first, m))
        u.start()

    def _set_pill_status(self, text: str):
        self.pill.setText(text)

    def _on_dl_progress(self, pct: int):
        if pct < 0:
            self.dl_bar.setVisible(False)
            return
        self.dl_bar.setVisible(True)
        self.dl_bar.setValue(pct)

    def _on_up_to_date(self, version: str, manual: bool):
        self._reload_ffmpeg()
        if manual:
            QMessageBox.information(self, "FFmpeg", "FFmpeg is up to date.")

    def _on_update_available(self, version: str):
        self._reload_ffmpeg()
        self.pill.setText(self.pill.text() + f"   ↑ {version} available")
        self.pill.setToolTip("A newer FFmpeg is available. Use Settings → Download / reinstall latest FFmpeg "
                             "to install a copy managed by this app.")

    def _on_update_failed(self, msg: str, first: bool, manual: bool):
        self._reload_ffmpeg()
        if first:
            self.pill.setText("FFmpeg missing — click to retry")
            QMessageBox.warning(self, "FFmpeg setup", f"Couldn't download FFmpeg:\n{msg}")
        elif manual:
            QMessageBox.warning(self, "FFmpeg update", f"Couldn't check for updates:\n{msg}")

    def _on_staged(self, version: str, first: bool):
        self.staged_version = version
        if self.active:
            self.pill.setText(self.pill.text() + "   (update ready)")
            return  # applied automatically once conversions finish
        self._apply_staged()

    def _apply_staged(self):
        try:
            core.apply_staged()
        except Exception:  # noqa: BLE001
            return  # still in use (e.g. antivirus scan) - retried on next idle / next launch
        self.staged_version = None
        self._reload_ffmpeg()
        self.pill.setText(self.pill.text() + "   ✓ updated")

    # ------------------------------------------------------------------ files #
    def choose_files(self):
        exts = " ".join(f"*{e}" for e in sorted(core.ALL_EXT))
        files, _ = QFileDialog.getOpenFileNames(self, "Choose files", str(Path.home()),
                                                f"Media files ({exts});;All files (*.*)")
        self.add_paths([Path(f) for f in files])

    def add_paths(self, paths):
        expanded: list[Path] = []
        for p in paths:
            if p.is_dir():
                expanded += sorted(x for x in p.rglob("*") if x.is_file() and x.suffix.lower() in core.ALL_EXT)
            elif p.is_file():
                expanded.append(p)
        have = {r.path for r in self.rows}
        for p in expanded:
            if p in have:
                continue
            have.add(p)
            row = FileRow(p)
            row.removeRequested.connect(self._remove_row)
            row.optionsRequested.connect(self.open_options)
            row.formatChanged.connect(self._format_changed)
            self.rows.append(row)
            row.token = next(self._tokens)
            self.by_token[row.token] = row
            self.list_lay.insertWidget(self.list_lay.count() - 1, row)
            if self.install:
                self._queue_probe(row)
            else:
                self.backlog.append(row)
        self._refresh()

    def _queue_probe(self, row: FileRow):
        self.probe.add(self.install.ffprobe, row.path, row.token)

    def _on_probed(self, token: int, info, err: str):
        row = self.by_token.get(token)
        if row is None:
            return
        if info is None:
            row.set_unreadable(err or "unsupported file")
        else:
            row.set_info(info, self.defaults)
        self._refresh()

    def _format_changed(self, row: FileRow):
        key = row.combo.currentData()
        if key:
            self.defaults[row.fmt.category] = key
            self.s.setValue(f"fmt_{row.fmt.category}", key)
        self._refresh()

    def _set_all_formats(self, idx: int):
        key = self.set_all.itemData(idx)
        self.set_all.setCurrentIndex(-1)
        if not key:
            return
        cat = FORMATS_BY_KEY[key].category
        for r in self.rows:
            if r.state in ("probing", "queued", "running", "unreadable"):
                continue
            allowed = {"video": ("video", "audio", "image"), "audio": ("audio",), "image": ("image",)}[r.kind]
            if cat in allowed:
                r.combo.setCurrentIndex(r.combo.findData(key))

    def _remove_row(self, row: FileRow):
        if row.state == "running":
            w = self.active.get(row)
            if w:
                w.cancel()
            return
        if row in self.queue:
            self.queue.remove(row)
        if row in self.batch:
            self.batch.remove(row)
        if row in self.backlog:
            self.backlog.remove(row)
        self.rows.remove(row)
        self.by_token.pop(row.token, None)
        row.setParent(None)
        row.deleteLater()
        self._refresh()
        self._update_overall()

    def clear_all(self):
        for r in list(self.rows):
            if r.state != "running":
                self._remove_row(r)

    def open_options(self, row: FileRow):
        if row.info is None or row.state in ("queued", "running", "probing", "unreadable"):
            return
        dlg = OptionsDialog(self, row, self.encoders, self._preview_command)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        opts = dlg.collect()
        targets = [row]
        if dlg.apply_all.isChecked():
            targets = [r for r in self.rows if r.fmt.category == row.fmt.category
                       and r.state in ("ready", "done", "failed", "cancelled")]
        for r in targets:
            r.opts = dataclasses.replace(opts)
            if r.state in SOFT_STATES:
                r.mark_ready()
        self._refresh()

    def _preview_command(self, row: FileRow, opts: Options) -> str:
        if not self.install or row.info is None:
            raise RuntimeError("FFmpeg isn't ready yet")
        cmd, _ = core.build_command(self.install.ffmpeg, row.path, Path(f"output.{row.fmt.ext}"),
                                    row.fmt, opts, row.info, self.encoders)
        return core.command_to_string(cmd)

    def _refresh(self):
        has = bool(self.rows)
        self.drop.set_compact(has)
        self.scroll.setVisible(has)
        self.bottom.setVisible(has)
        todo = [r for r in self.rows if r.state in CONVERTIBLE]
        busy = bool(self.active or self.queue)
        self.convert_btn.setEnabled(bool(todo) and self.install is not None)
        self.convert_btn.setText("Convert" if len(todo) <= 1 else f"Convert {len(todo)} files")
        self.cancel_btn.setVisible(busy)
        self.clear_btn.setEnabled(any(r.state != "running" for r in self.rows))
        self.set_all.setEnabled(any(r.state in ("ready", "done", "failed", "cancelled") for r in self.rows))

    # -------------------------------------------------------------- converting #
    def convert_all(self):
        if not self.install:
            self._start_update(manual=True)
            return
        todo = [r for r in self.rows if r.state in CONVERTIBLE]
        if not todo:
            return
        self.cancelled_batch = False
        for r in todo:
            r.set_queued()
            self.queue.append(r)
            if r not in self.batch:
                self.batch.append(r)
        self._refresh()
        self._pump()

    def cancel_all(self):
        self.cancelled_batch = True
        for r in self.queue:
            r.set_cancelled()
        self.queue.clear()
        for w in self.active.values():
            w.cancel()
        self._refresh()

    def _pump(self):
        while self.queue and len(self.active) < self.parallel:
            row = self.queue.pop(0)
            if row.state == "queued":
                self._start_row(row)
        self._refresh()
        self._update_overall()
        if not self.active and not self.queue and self.batch:
            self._on_idle()

    def _start_row(self, row: FileRow):
        try:
            fmt = row.fmt
            dest_dir = self.out_dir or row.path.parent
            dest_dir.mkdir(parents=True, exist_ok=True)
            dst = core.unique_path(dest_dir, row.path.stem, fmt.ext)
            cmd, total = core.build_command(self.install.ffmpeg, row.path, dst, fmt, row.opts, row.info, self.encoders)
        except Exception as e:  # noqa: BLE001
            row.set_failed(str(e), str(e))
            return
        w = ConvertWorker(cmd, dst, total, self)
        self.active[row] = w
        row.set_running()
        w.progress.connect(lambda f, t, r=row: self._on_progress(r, f, t))
        w.succeeded.connect(lambda r=row, d=dst: self._on_success(r, d))
        w.failed.connect(lambda s, l, r=row: self._on_failed(r, s, l))
        w.cancelled.connect(lambda r=row: self._on_cancelled(r))
        w.finished.connect(lambda r=row: self._worker_finished(r))
        w.start()

    def _on_progress(self, row: FileRow, frac: float, text: str):
        row.set_progress(frac, text)
        self._update_overall()

    def _on_success(self, row: FileRow, dst: Path):
        row.set_done(dst)
        self.last_dir = dst.parent

    def _on_failed(self, row: FileRow, short: str, log: str):
        row.set_failed(short, log)

    def _on_cancelled(self, row: FileRow):
        row.set_cancelled()

    def _worker_finished(self, row: FileRow):
        w = self.active.pop(row, None)
        if w:
            w.deleteLater()
        self._pump()

    def _update_overall(self):
        if not self.batch or not (self.active or self.queue):
            self.overall_box.setVisible(False)
            return
        total = len(self.batch)
        finished = sum(1 for r in self.batch if r.state in SOFT_STATES)
        frac = sum(1.0 if r.state in SOFT_STATES else (r.fraction if r.state == "running" else 0.0)
                   for r in self.batch) / total
        self.overall_box.setVisible(True)
        self.overall_lbl.setText(f"Converting…  {finished} of {total} finished")
        self.overall.setValue(int(frac * 1000))

    def _on_idle(self):
        batch, self.batch = self.batch, []
        self.overall_box.setVisible(False)
        self._refresh()
        if self.staged_version:
            self._apply_staged()
        ok = [r for r in batch if r.state == "done"]
        bad = [r for r in batch if r.state == "failed"]
        if self.cancelled_batch or not (ok or bad):
            return
        if ok and not bad:
            body = f"{ok[0].out_path.name}" if len(ok) == 1 and ok[0].out_path else f"{len(ok)} files converted"
            self._notify("Conversion complete", body)
        elif ok:
            self._notify("Conversion finished", f"{len(ok)} converted, {len(bad)} failed")
        else:
            self._notify("Conversion failed", f"{len(bad)} file(s) couldn't be converted")

    # ----------------------------------------------------------- notifications #
    def _window_focused(self) -> bool:
        return (QApplication.applicationState() == Qt.ApplicationState.ApplicationActive
                and not self.isMinimized())

    def _notify(self, title: str, body: str):
        """Only notify when the user is looking at a different window."""
        if not self.notify_enabled or self._window_focused():
            return
        QApplication.alert(self, 0)  # flash the taskbar button
        if self.tray:
            self.tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 8000)

    def _on_notification_clicked(self):
        if self.last_dir:
            open_path(self.last_dir)

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ---------------------------------------------------------- drag and drop #
    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.drop.set_drag(True)

    def dragLeaveEvent(self, e):  # noqa: N802
        self.drop.set_drag(False)

    def dropEvent(self, e):  # noqa: N802
        self.drop.set_drag(False)
        self.add_paths([Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()])

    # ----------------------------------------------------------------- closing #
    def closeEvent(self, e):  # noqa: N802
        if self.active or self.queue:
            r = QMessageBox.question(self, "Quit", "Conversions are still running. Cancel them and quit?")
            if r != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.cancel_all()
            for w in list(self.active.values()):
                w.wait(4000)
        self.s.setValue("geometry", self.saveGeometry())
        if self.updater and self.updater.isRunning():
            self.updater.abort()
            self.updater.wait(4000)
        self.probe.stop()
        self.probe.wait(3000)
        if core.has_staged():
            try:
                core.apply_staged()
            except Exception:  # noqa: BLE001
                pass
        if self.tray:
            self.tray.hide()
        e.accept()
