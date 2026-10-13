"""
core.py - everything that isn't UI.

* locating / downloading / updating a local FFmpeg
* probing media files with ffprobe
* output format presets and the FFmpeg command-line builder

No Qt imports here, so this module can be tested headlessly.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

APP_NAME = "FFmpeg Studio"
APP_VERSION = "1.0.0"  # keep in sync with the git tag you release (installer picks this up via build.py)
GITHUB_REPO = "Error78346/FFMPEG-studio"
APP_ID = "FFmpegStudio"
EXE = ".exe" if os.name == "nt" else ""
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
SUPPORTS_AUTO_DOWNLOAD = os.name == "nt"  # we pull the Windows builds from gyan.dev
USER_AGENT = f"{APP_ID}/1.0"


# --------------------------------------------------------------------------- #
#  Paths
# --------------------------------------------------------------------------- #
def app_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    p = base / APP_ID
    p.mkdir(parents=True, exist_ok=True)
    return p


def managed_root() -> Path:
    return app_data_dir() / "ffmpeg"


def managed_bin() -> Path:
    return managed_root() / "bin"


def staged_dir() -> Path:
    return app_data_dir() / "ffmpeg-staged"


def app_dir() -> Path:
    """Folder containing the EXE (frozen) or this script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def run_quiet(cmd, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(c) for c in cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=NO_WINDOW,
    )


# --------------------------------------------------------------------------- #
#  Versions
# --------------------------------------------------------------------------- #
def numeric_prefix(v: Optional[str]) -> Optional[str]:
    m = re.match(r"^(\d+(?:\.\d+)*)", v or "")
    return m.group(1) if m else None


def version_tuple(v: Optional[str]) -> Optional[tuple]:
    p = numeric_prefix(v)
    return tuple(int(x) for x in p.split(".")) if p else None


def is_newer(remote: Optional[str], local: Optional[str]) -> bool:
    r, l = version_tuple(remote), version_tuple(local)
    if r is None:
        return False
    if l is None:
        return True
    n = max(len(r), len(l))
    r += (0,) * (n - len(r))
    l += (0,) * (n - len(l))
    return r > l


@dataclass
class FFmpegInstall:
    ffmpeg: Path
    ffprobe: Path
    source: str  # "managed" | "portable" | "system"
    version: Optional[str]


def read_version(ffmpeg: Path) -> Optional[str]:
    vfile = ffmpeg.parent.parent / "VERSION"
    if ffmpeg.parent == managed_bin() and vfile.is_file():
        txt = vfile.read_text().strip()
        if txt:
            return txt
    try:
        out = run_quiet([ffmpeg, "-version"], 20).stdout
    except Exception:
        return None
    m = re.search(r"ffmpeg version (\S+)", out)
    return m.group(1) if m else None


def _pair(folder: Path):
    ff, fp = folder / f"ffmpeg{EXE}", folder / f"ffprobe{EXE}"
    return (ff, fp) if ff.is_file() and fp.is_file() else None


def locate_ffmpeg() -> Optional[FFmpegInstall]:
    """Search order: app-managed copy -> portable copy next to the EXE -> PATH."""
    candidates = [
        ("managed", managed_bin()),
        ("portable", app_dir() / "ffmpeg" / "bin"),
        ("portable", app_dir() / "ffmpeg"),
        ("portable", app_dir()),
    ]
    for source, folder in candidates:
        pair = _pair(folder)
        if pair:
            return FFmpegInstall(pair[0], pair[1], source, read_version(pair[0]))
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ff and fp:
        return FFmpegInstall(Path(ff), Path(fp), "system", read_version(Path(ff)))
    return None


def list_encoders(ffmpeg: Path) -> frozenset:
    try:
        out = run_quiet([ffmpeg, "-hide_banner", "-encoders"], 30).stdout
    except Exception:
        return frozenset()
    return frozenset(re.findall(r"^\s*[VAS][\w.]{5}\s+(\S+)", out, re.M))


HW_ENCODERS = {
    "nvenc": {"h264": "h264_nvenc", "h265": "hevc_nvenc"},
    "qsv": {"h264": "h264_qsv", "h265": "hevc_qsv"},
    "amf": {"h264": "h264_amf", "h265": "hevc_amf"},
}
HW_LABELS = {"nvenc": "NVIDIA NVENC", "qsv": "Intel Quick Sync", "amf": "AMD AMF"}


def hw_choices(encoders) -> list[str]:
    return [k for k, v in HW_ENCODERS.items() if v["h264"] in encoders]


# --------------------------------------------------------------------------- #
#  Download / update  (Windows builds from gyan.dev, GitHub mirror as fallback)
# --------------------------------------------------------------------------- #
GYAN_VERSION_URL = "https://www.gyan.dev/ffmpeg/builds/release-version"
GYAN_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
GITHUB_LATEST_URL = "https://api.github.com/repos/GyanD/codexffmpeg/releases/latest"


def _github_zip(ver: str) -> str:
    return f"https://github.com/GyanD/codexffmpeg/releases/download/{ver}/ffmpeg-{ver}-essentials_build.zip"


def http_get(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


@dataclass
class Release:
    version: str
    sources: list  # [(zip_url, sha256_url | None), ...] tried in order


def latest_release() -> Release:
    try:
        ver = http_get(GYAN_VERSION_URL, 15).decode().strip()
        if re.fullmatch(r"\d+(\.\d+)+", ver):
            return Release(ver, [(GYAN_ZIP_URL, GYAN_ZIP_URL + ".sha256"), (_github_zip(ver), None)])
    except Exception:
        pass
    data = json.loads(http_get(GITHUB_LATEST_URL, 15))
    tag = str(data["tag_name"]).lstrip("v")
    return Release(tag, [(_github_zip(tag), None)])


def _download(url: str, dest: Path, progress: Optional[Callable[[int, int], None]], expected_sha: Optional[str]):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if expected_sha and h.hexdigest().lower() != expected_sha.lower():
        raise RuntimeError("Checksum mismatch - download was corrupted")


def stage_release(rel: Release, progress: Optional[Callable[[int, int], None]] = None) -> str:
    """Download + extract + sanity-check a release into the 'staged' folder.

    Nothing the running app uses is touched here, so this is safe to run in the
    background while conversions are in progress.  Returns the real version.
    """
    tmp = app_data_dir() / "tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    zpath = tmp / "ffmpeg.zip"
    last_err: Optional[Exception] = None
    for url, sha_url in rel.sources:
        try:
            expected = None
            if sha_url:
                try:
                    expected = http_get(sha_url, 15).decode().split()[0]
                except Exception:
                    expected = None
            _download(url, zpath, progress, expected)
            last_err = None
            break
        except InterruptedError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        except Exception as e:  # try next source
            last_err = e
            zpath.unlink(missing_ok=True)
    if last_err is not None:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"Download failed: {last_err}")

    ex = tmp / "x"
    with zipfile.ZipFile(zpath) as zf:
        zf.extractall(ex)
    roots = [p.parent.parent for p in ex.rglob(f"bin/ffmpeg{EXE}")]
    if not roots:
        raise RuntimeError("Downloaded archive didn't contain ffmpeg")
    root = roots[0]
    (root / "bin" / f"ffplay{EXE}").unlink(missing_ok=True)  # not needed, saves ~100 MB
    real = read_version(root / "bin" / f"ffmpeg{EXE}")
    if not real:
        raise RuntimeError("Downloaded FFmpeg failed to start")
    version = numeric_prefix(real) or rel.version
    (root / "VERSION").write_text(version)
    shutil.rmtree(staged_dir(), ignore_errors=True)
    shutil.move(str(root), str(staged_dir()))
    shutil.rmtree(tmp, ignore_errors=True)
    return version


def has_staged() -> bool:
    s = staged_dir()
    return (s / "bin" / f"ffmpeg{EXE}").is_file() and (s / "VERSION").is_file()


def apply_staged() -> Optional[str]:
    """Swap the staged build in.  Raises PermissionError if the old one is in use."""
    if not has_staged():
        return None
    final, old = managed_root(), app_data_dir() / "ffmpeg.old"
    shutil.rmtree(old, ignore_errors=True)
    if final.exists():
        final.rename(old)  # fails on Windows while ffmpeg.exe is running
    try:
        staged_dir().rename(final)
    except Exception:
        if old.exists() and not final.exists():
            old.rename(final)
        raise
    shutil.rmtree(old, ignore_errors=True)
    return (final / "VERSION").read_text().strip()


# --------------------------------------------------------------------------- #
#  App self-update (GitHub Releases)
# --------------------------------------------------------------------------- #
@dataclass
class AppRelease:
    version: str
    tag: str
    page_url: str
    notes: str = ""
    asset_name: str = ""
    asset_url: str = ""
    sha256: str = ""


def trusted_download_url(url: str) -> bool:
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host == "github.com" or host.endswith(".github.com")
                                    or host.endswith(".githubusercontent.com"))


_VER_RE = re.compile(r"(?<![\d.])v?(\d+(?:\.\d+)+)")


def release_version(d: dict) -> Optional[str]:
    """Version of a GitHub release: from the tag if it has one (v1.2.0), else from the title ("Version 1.2.0: ...")."""
    for field in ("tag_name", "name"):
        m = _VER_RE.search(str(d.get(field) or ""))
        if m:
            return m.group(1)
    return None


def parse_app_release(d: dict) -> Optional[AppRelease]:
    ver = release_version(d)
    if not ver:
        return None
    exes = [a for a in d.get("assets", []) if str(a.get("name", "")).lower().endswith(".exe")]
    pick = next((a for a in exes if "setup" in a["name"].lower()), exes[0] if exes else None)
    rel = AppRelease(ver, str(d.get("tag_name", "")),
                     str(d.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases"),
                     str(d.get("body") or ""))
    if pick and trusted_download_url(str(pick.get("browser_download_url", ""))):
        rel.asset_name = Path(str(pick["name"])).name
        rel.asset_url = pick["browser_download_url"]
        digest = str(pick.get("digest") or "")
        if digest.lower().startswith("sha256:"):
            rel.sha256 = digest.split(":", 1)[1]
    return rel


def pick_newest(releases: list) -> Optional[AppRelease]:
    """Highest version among published, non-prerelease releases (doesn't trust GitHub's 'Latest' badge)."""
    best = None
    for d in releases:
        if d.get("draft") or d.get("prerelease"):
            continue
        rel = parse_app_release(d)
        if rel and (best is None or is_newer(rel.version, best.version)):
            best = rel
    return best


def latest_app_release() -> Optional[AppRelease]:
    """Newest published release, or None if there isn't one yet."""
    try:
        raw = http_get(f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=30", 15)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return pick_newest(json.loads(raw))


def download_installer(rel: AppRelease, progress: Optional[Callable[[int, int], None]] = None) -> Path:
    if not rel.asset_url:
        raise RuntimeError("This release has no installer attached.")
    d = app_data_dir() / "updates"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    dest = d / rel.asset_name
    _download(rel.asset_url, dest, progress, rel.sha256 or None)
    return dest


# --------------------------------------------------------------------------- #
#  Media files
# --------------------------------------------------------------------------- #
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg",
             ".ts", ".mts", ".m2ts", ".3gp", ".ogv", ".vob", ".gif"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".oga", ".opus", ".wma", ".aiff", ".aif", ".amr", ".ac3"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif", ".ico"}
ALL_EXT = VIDEO_EXT | AUDIO_EXT | IMAGE_EXT


def kind_of(path: Path) -> Optional[str]:
    e = path.suffix.lower()
    if e in VIDEO_EXT:
        return "video"
    if e in AUDIO_EXT:
        return "audio"
    if e in IMAGE_EXT:
        return "image"
    return None


@dataclass
class MediaInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    size: int = 0
    tile_grid: bool = False   # HEIC/AVIF stored as tiles: must map the stream *group*


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _parse_probe(data: dict, size: int) -> MediaInfo:
    info = MediaInfo(size=size, duration=_f(data.get("format", {}).get("duration")))
    for s in data.get("streams", []):
        t = s.get("codec_type")
        if t == "video" and not s.get("disposition", {}).get("attached_pic") and not info.has_video:
            info.has_video = True
            info.width, info.height = int(s.get("width") or 0), int(s.get("height") or 0)
            num, _, den = str(s.get("avg_frame_rate", "0/1")).partition("/")
            info.fps = _f(num) / (_f(den) or 1)
            if not info.duration:
                info.duration = _f(s.get("duration"))
        elif t == "audio":
            info.has_audio = True
            if not info.duration:
                info.duration = _f(s.get("duration"))
    # HEIC/HEIF/AVIF images made of tiles (every iPhone photo): FFmpeg 7+ exposes each tile as its own
    # stream plus a "Tile Grid" stream group.  Mapping 0:v:0 would give just ONE tile (a corner).
    for g in data.get("stream_groups", []) or []:
        if "tile" in str(g.get("type", "")).lower() or "grid" in str(g.get("type", "")).lower():
            info.tile_grid = True
            comps = g.get("components") or []
            for comp in comps[:1]:
                for src in (comp, comp.get("tile_grid") or {}, comp.get("tile_grid_options") or {}):
                    w, h = int(src.get("width") or 0), int(src.get("height") or 0)
                    if w and h:
                        info.width, info.height = w, h
            break
    return info


def probe(ffprobe: Path, path: Path) -> MediaInfo:
    base = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams"]
    r = run_quiet(base + ["-show_stream_groups", path], 60)  # FFmpeg 7+
    if r.returncode != 0 and "show_stream_groups" in r.stderr:
        r = run_quiet(base + [path], 60)  # older ffprobe doesn't know the option
    if r.returncode != 0:
        lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
        msg = lines[-1] if lines else "Unreadable file"
        prefix = f"{path}: "
        raise RuntimeError(msg[len(prefix):] if msg.startswith(prefix) else msg)
    return _parse_probe(json.loads(r.stdout or "{}"), path.stat().st_size)


def human_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_duration(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(s: str) -> Optional[float]:
    """'90', '1:30', '00:01:30.5' -> seconds.  Empty -> None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        raise ValueError(f"'{s}' is not a valid time (use SS, MM:SS or HH:MM:SS)")
    if len(parts) > 3 or any(p < 0 for p in parts):
        raise ValueError(f"'{s}' is not a valid time (use SS, MM:SS or HH:MM:SS)")
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def unique_path(folder: Path, stem: str, ext: str) -> Path:
    p = folder / f"{stem}.{ext}"
    n = 1
    while p.exists():
        p = folder / f"{stem} ({n}).{ext}"
        n += 1
    return p


# --------------------------------------------------------------------------- #
#  Formats & options
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OutFmt:
    key: str
    label: str
    ext: str
    category: str  # video | audio | image
    vcodecs: tuple = ()
    acodecs: tuple = ()


FORMATS = [
    OutFmt("mp4", "MP4", "mp4", "video", ("h264", "h265", "av1", "copy"), ("aac", "mp3", "copy")),
    OutFmt("mkv", "MKV", "mkv", "video", ("h264", "h265", "vp9", "av1", "copy"), ("aac", "opus", "mp3", "flac", "copy")),
    OutFmt("webm", "WebM", "webm", "video", ("vp9", "av1"), ("opus", "vorbis")),
    OutFmt("mov", "MOV", "mov", "video", ("h264", "h265", "prores", "copy"), ("aac", "pcm", "copy")),
    OutFmt("avi", "AVI", "avi", "video", ("mpeg4", "h264"), ("mp3", "pcm", "ac3")),
    OutFmt("gif", "GIF (animated)", "gif", "video"),
    OutFmt("mp3", "MP3", "mp3", "audio", acodecs=("mp3",)),
    OutFmt("m4a", "M4A (AAC)", "m4a", "audio", acodecs=("aac",)),
    OutFmt("wav", "WAV", "wav", "audio", acodecs=("pcm",)),
    OutFmt("flac", "FLAC", "flac", "audio", acodecs=("flac",)),
    OutFmt("ogg", "OGG (Vorbis)", "ogg", "audio", acodecs=("vorbis",)),
    OutFmt("opus", "Opus", "opus", "audio", acodecs=("opus",)),
    OutFmt("wma", "WMA", "wma", "audio", acodecs=("wma",)),
    OutFmt("jpg", "JPG", "jpg", "image"),
    OutFmt("png", "PNG", "png", "image"),
    OutFmt("webp", "WebP", "webp", "image"),
    OutFmt("bmp", "BMP", "bmp", "image"),
    OutFmt("tiff", "TIFF", "tiff", "image"),
]
FORMATS_BY_KEY = {f.key: f for f in FORMATS}

VCODEC = {"h264": "libx264", "h265": "libx265", "vp9": "libvpx-vp9", "av1": "libsvtav1",
          "mpeg4": "mpeg4", "prores": "prores_ks", "copy": "copy"}
ACODEC = {"aac": "aac", "mp3": "libmp3lame", "opus": "libopus", "vorbis": "libvorbis", "flac": "flac",
          "pcm": "pcm_s16le", "ac3": "ac3", "wma": "wmav2", "copy": "copy"}
VCODEC_LABEL = {"h264": "H.264 (AVC) - most compatible", "h265": "H.265 (HEVC) - smaller files",
                "vp9": "VP9", "av1": "AV1 - smallest, slow", "mpeg4": "MPEG-4", "prores": "Apple ProRes",
                "copy": "Copy (no re-encode)"}
ACODEC_LABEL = {"aac": "AAC", "mp3": "MP3", "opus": "Opus", "vorbis": "Vorbis", "flac": "FLAC (lossless)",
                "pcm": "PCM (uncompressed)", "ac3": "AC-3", "wma": "WMA", "copy": "Copy (no re-encode)"}
ROTATE = {"none": None, "cw90": "transpose=1", "ccw90": "transpose=2", "r180": "hflip,vflip",
          "hflip": "hflip", "vflip": "vflip"}
ROTATE_LABEL = {"none": "None", "cw90": "Rotate 90° clockwise", "ccw90": "Rotate 90° counter-clockwise",
                "r180": "Rotate 180°", "hflip": "Flip horizontally", "vflip": "Flip vertically"}

QUALITY_LABELS = ["High quality (bigger file)", "Balanced", "Small file", "Smallest file"]
SPEED_LABELS = ["Fast (bigger file)", "Balanced", "Slow (smaller file)"]
CRF = {"h264": (17, 21, 25, 29), "h265": (20, 24, 28, 32), "vp9": (24, 31, 37, 44), "av1": (22, 30, 38, 46)}
DEFAULT_ABR = {"aac": 192, "mp3": 192, "opus": 128, "vorbis": 160, "ac3": 384, "wma": 192}
LOSSY_AUDIO = set(DEFAULT_ABR)


@dataclass
class Options:
    # video
    vcodec: str = "auto"
    quality: int = 1
    speed: int = 1
    vbitrate: int = 0          # kbps, 0 = use quality
    resolution: str = "original"  # original | 2160 | 1080 | ... | custom
    width: int = 0
    height: int = 0
    fps: int = 0               # 0 = original
    rotate: str = "none"
    hw: str = "none"
    # audio
    acodec: str = "auto"
    abitrate: int = 0          # kbps, 0 = auto
    samplerate: int = 0
    channels: int = 0
    volume: int = 100
    normalize: bool = False
    remove_audio: bool = False
    # misc
    trim_start: str = ""
    trim_end: str = ""
    strip_metadata: bool = False
    custom_args: str = ""


def split_args(s: str) -> list[str]:
    s = (s or "").strip()
    if not s:
        return []
    if os.name == "nt":
        return [a.strip('"') for a in shlex.split(s, posix=False)]
    return shlex.split(s)


def command_to_string(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


# --------------------------------------------------------------------------- #
#  Command builder
# --------------------------------------------------------------------------- #
def _video_filters(o: Options, with_fps: bool = True) -> list[str]:
    f: list[str] = []
    if with_fps and o.fps:
        f.append(f"fps={o.fps:g}")
    if ROTATE.get(o.rotate):
        f.append(ROTATE[o.rotate])
    if o.resolution == "custom" and (o.width or o.height):
        w = o.width - o.width % 2 if o.width > 0 else -2
        h = o.height - o.height % 2 if o.height > 0 else -2
        f.append(f"scale={w}:{h}")
    elif o.resolution.isdigit():
        f.append(f"scale=-2:'min({o.resolution},ih)'")  # keeps aspect, never upscales
    return f


def _video_encoder_args(vkey: str, o: Options, fmt: OutFmt, encoders) -> list[str]:
    q = max(0, min(3, o.quality))
    sp = max(0, min(2, o.speed))
    enc = VCODEC[vkey]
    hw = None
    if o.hw in HW_ENCODERS and vkey in ("h264", "h265") and HW_ENCODERS[o.hw][vkey] in encoders:
        hw = o.hw
        enc = HW_ENCODERS[hw][vkey]
    kb = o.vbitrate
    a = ["-c:v", enc]
    crf = CRF.get(vkey, (0, 0, 0, 0))[q]

    if hw == "nvenc":
        a += ["-preset", ("p3", "p5", "p7")[sp]]
        a += ["-b:v", f"{kb}k"] if kb else ["-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    elif hw == "qsv":
        a += ["-preset", ("veryfast", "medium", "slow")[sp]]
        a += ["-b:v", f"{kb}k"] if kb else ["-global_quality", str(crf)]
    elif hw == "amf":
        a += ["-b:v", f"{kb}k"] if kb else ["-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf), "-qp_b", str(crf)]
    elif vkey in ("h264", "h265"):
        a += ["-preset", ("veryfast", "medium", "slow")[sp]]
        a += ["-b:v", f"{kb}k"] if kb else ["-crf", str(crf)]
    elif vkey == "vp9":
        a += ["-row-mt", "1", "-cpu-used", str((5, 3, 1)[sp])]
        a += ["-b:v", f"{kb}k"] if kb else ["-crf", str(crf), "-b:v", "0"]
    elif vkey == "av1":
        a += ["-preset", str((10, 8, 5)[sp])]
        a += ["-b:v", f"{kb}k"] if kb else ["-crf", str(crf)]
    elif vkey == "mpeg4":
        a += ["-b:v", f"{kb}k"] if kb else ["-q:v", str((2, 4, 7, 12)[q])]
    elif vkey == "prores":
        a += ["-profile:v", "3"]

    a += ["-pix_fmt", "yuv422p10le" if vkey == "prores" else "yuv420p"]
    if vkey == "h265" and fmt.key in ("mp4", "mov"):
        a += ["-tag:v", "hvc1"]  # lets Apple devices play it
    return a


def _audio_args(fmt: OutFmt, o: Options) -> list[str]:
    akey = o.acodec if o.acodec in fmt.acodecs else (fmt.acodecs[0] if fmt.acodecs else "aac")
    if akey == "copy":
        return ["-c:a", "copy"]
    a = ["-c:a", ACODEC[akey]]
    if akey in LOSSY_AUDIO:
        br = o.abitrate or DEFAULT_ABR[akey]
        a += ["-b:a", f"{br}k"]
    if o.samplerate and not (akey == "opus" and o.samplerate not in (8000, 12000, 16000, 24000, 48000)):
        a += ["-ar", str(o.samplerate)]
    if o.channels:
        a += ["-ac", str(o.channels)]
    af = []
    if o.volume != 100:
        af.append(f"volume={o.volume / 100:g}")
    if o.normalize:
        af.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    if af:
        a += ["-af", ",".join(af)]
    return a


def build_command(ffmpeg: Path, src: Path, dst: Path, fmt: OutFmt, o: Options,
                  info: MediaInfo, encoders=frozenset()) -> tuple[list[str], float]:
    """Returns (argv, expected_output_seconds).  expected = 0 when unknown (images)."""
    start = parse_time(o.trim_start) or 0.0
    end = parse_time(o.trim_end)
    dur = (end - start) if (end and end > start) else None

    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if dur and fmt.category != "image":
        cmd += ["-t", f"{dur:.3f}"]

    if fmt.category == "image":
        if not info.has_video:
            raise ValueError("This file has no picture to export.")
        # tiled HEIC/AVIF: map the Tile Grid group so FFmpeg stitches the tiles into the full picture
        cmd += ["-map", "0:g:0" if info.tile_grid else "0:v:0", "-frames:v", "1", "-update", "1"]
        vf = _video_filters(o, with_fps=False)
        if vf:
            cmd += ["-vf", ",".join(vf)]
        q = max(0, min(3, o.quality))
        if fmt.key == "jpg":
            cmd += ["-q:v", str((2, 5, 9, 15)[q])]
        elif fmt.key == "webp":
            cmd += ["-quality", str((95, 80, 60, 40)[q])]
        total = 0.0

    elif fmt.category == "audio":
        if not info.has_audio:
            raise ValueError("This file has no audio track.")
        cmd += ["-map", "0:a:0", "-vn"] + _audio_args(fmt, o)
        total = dur or max(info.duration - start, 0.0)

    else:  # video
        if not info.has_video:
            raise ValueError("This file has no video track.")
        if fmt.key == "gif":
            fps = o.fps or 15
            rot = ROTATE.get(o.rotate)
            if o.resolution == "custom" and (o.width or o.height):
                scale = f"scale={o.width or -1}:{o.height or -1}:flags=lanczos"
            elif o.resolution.isdigit():
                scale = f"scale=-1:'min({o.resolution},ih)':flags=lanczos"
            else:
                scale = "scale='min(480,iw)':-1:flags=lanczos"
            chain = ",".join([f"fps={fps:g}"] + ([rot] if rot else []) + [scale])
            vf = (chain + ",split[s0][s1];[s0]palettegen=stats_mode=diff[p];"
                  "[s1][p]paletteuse=dither=bayer:bayer_scale=5")
            cmd += ["-map", "0:v:0", "-an", "-vf", vf, "-loop", "0"]
        else:
            vkey = o.vcodec if o.vcodec in fmt.vcodecs else fmt.vcodecs[0]
            cmd += ["-map", "0:v:0"]
            if o.remove_audio or not info.has_audio:
                cmd += ["-an"]
            else:
                cmd += ["-map", "0:a:0"] + _audio_args(fmt, o)
            if vkey == "copy":
                cmd += ["-c:v", "copy"]
            else:
                cmd += _video_encoder_args(vkey, o, fmt, encoders)
                vf = _video_filters(o)
                if vf:
                    cmd += ["-vf", ",".join(vf)]
            if fmt.key in ("mp4", "mov"):
                cmd += ["-movflags", "+faststart"]
        total = dur or max(info.duration - start, 0.0)

    if o.strip_metadata:
        cmd += ["-map_metadata", "-1", "-map_chapters", "-1"]
    cmd += split_args(o.custom_args)
    cmd.append(str(dst))
    return cmd, total
