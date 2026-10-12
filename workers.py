"""workers.py - QThread workers so the UI never blocks."""
from __future__ import annotations

import queue
import subprocess
from pathlib import Path

from PySide6.QtCore import QThread, Signal

import core

_PROGRESS_KEYS = {"frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms", "out_time",
                  "dup_frames", "drop_frames", "speed", "progress"} | {f"stream_{i}_{j}_q" for i in range(4) for j in range(4)}


def _eta(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class ConvertWorker(QThread):
    progress = Signal(float, str)   # fraction (-1 = unknown), status text
    succeeded = Signal()
    failed = Signal(str, str)       # short message, full log
    cancelled = Signal()

    def __init__(self, cmd: list[str], dst: Path, total: float, parent=None):
        super().__init__(parent)
        self.cmd, self.dst, self.total = cmd, Path(dst), total
        self._proc: subprocess.Popen | None = None
        self._cancel = False
        self._log: list[str] = []

    def cancel(self):
        self._cancel = True
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def _cleanup(self):
        try:
            self.dst.unlink(missing_ok=True)
        except OSError:
            pass

    def run(self):
        if self._cancel:
            self.cancelled.emit()
            return
        try:
            self._proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=core.NO_WINDOW,
            )
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"Couldn't start FFmpeg: {e}", str(e))
            return

        cur, speed = 0.0, 0.0
        for raw in self._proc.stdout:  # type: ignore[union-attr]
            line = raw.strip()
            if not line:
                continue
            key, sep, val = line.partition("=")
            if sep and key in _PROGRESS_KEYS:
                if key in ("out_time_us", "out_time_ms"):
                    try:
                        cur = int(val) / 1_000_000
                    except ValueError:
                        pass
                elif key == "speed":
                    try:
                        speed = float(val.strip().rstrip("x"))
                    except ValueError:
                        speed = 0.0
                elif key == "progress":
                    if self.total > 0:
                        frac = min(cur / self.total, 0.999)
                        text = f"{frac * 100:.0f}%"
                        if speed > 0:
                            text += f"  ·  {speed:.1f}x  ·  ETA {_eta((self.total - cur) / speed)}"
                        self.progress.emit(frac, text)
                    else:
                        self.progress.emit(-1.0, "Working…")
            else:
                self._log.append(line)
                del self._log[:-60]

        rc = self._proc.wait()
        if self._cancel:
            self._cleanup()
            self.cancelled.emit()
        elif rc == 0 and self.dst.exists():
            self.progress.emit(1.0, "100%")
            self.succeeded.emit()
        else:
            self._cleanup()
            short = next((l for l in reversed(self._log) if l), f"FFmpeg exited with code {rc}")
            self.failed.emit(short, "\n".join(self._log) or short)


class ProbeWorker(QThread):
    probed = Signal(int, object, str)  # token, MediaInfo | None, error

    def __init__(self, parent=None):
        super().__init__(parent)
        self._q: queue.Queue = queue.Queue()

    def add(self, ffprobe: Path, path: Path, token: int):
        self._q.put((ffprobe, path, token))

    def stop(self):
        self._q.put(None)

    def run(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            ffprobe, path, token = item
            try:
                self.probed.emit(token, core.probe(ffprobe, path), "")
            except Exception as e:  # noqa: BLE001
                self.probed.emit(token, None, str(e))


class UpdateWorker(QThread):
    """Checks for a newer FFmpeg and (optionally) downloads it to a staging folder."""
    status = Signal(str)
    progress = Signal(int)                 # 0-100, -1 = hide
    up_to_date = Signal(str)               # current version
    update_available = Signal(str)         # remote version (not downloaded)
    staged = Signal(str, bool)             # version, was_first_install
    failed = Signal(str, bool)             # message, was_first_install

    def __init__(self, auto_install: bool, force: bool = False, parent=None):
        super().__init__(parent)
        self.auto_install, self.force = auto_install, force
        self._abort = False

    def abort(self):
        self._abort = True

    def _on_dl(self, done: int, total: int):
        if self._abort:
            raise InterruptedError
        if total:
            self.progress.emit(int(done * 100 / total))

    def run(self):
        first = False
        try:
            install = core.locate_ffmpeg()
            first = install is None
            if first and not core.SUPPORTS_AUTO_DOWNLOAD:
                self.failed.emit("FFmpeg wasn't found. Install it and make sure ffmpeg/ffprobe are on your PATH.", True)
                return
            if not first and not core.SUPPORTS_AUTO_DOWNLOAD:
                self.up_to_date.emit(install.version or "")
                return

            self.status.emit("Setting up FFmpeg…" if first else "Checking for FFmpeg updates…")
            rel = core.latest_release()
            local = install.version if install else None
            newer = core.is_newer(rel.version, local)
            managed = bool(install and install.source == "managed")

            if not first and not self.force:
                if not newer:
                    self.up_to_date.emit(local or "")
                    return
                if not managed:
                    # user's own FFmpeg: tell them, never replace it behind their back
                    if core.version_tuple(local):
                        self.update_available.emit(rel.version)
                    else:
                        self.up_to_date.emit(local or "")
                    return
                if not self.auto_install:
                    self.update_available.emit(rel.version)
                    return

            self.status.emit(f"Downloading FFmpeg {rel.version}…")
            self.progress.emit(0)
            version = core.stage_release(rel, self._on_dl)
            self.progress.emit(-1)
            self.staged.emit(version, first)
        except InterruptedError:
            self.progress.emit(-1)
        except Exception as e:  # noqa: BLE001
            self.progress.emit(-1)
            self.failed.emit(str(e), first)
