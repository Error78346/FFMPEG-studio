# FFmpeg Studio

A FreeConvert-style desktop front-end for FFmpeg (Python + PySide6), packaged to a single EXE with PyInstaller.

## Run from source
    pip install -r requirements.txt
    python main.py

## Build the EXE (Windows)
    python build.py            # -> dist/FFmpeg Studio.exe  (single file)
    python build.py --onedir   # -> dist/FFmpeg Studio/     (starts faster)

## What it does
- **Drop files** (or whole folders) anywhere in the window, pick an output format per file, hit **Convert**.
- **Formats**: MP4, MKV, WebM, MOV, AVI, animated GIF - MP3, M4A, WAV, FLAC, OGG, Opus, WMA - JPG, PNG, WebP, BMP, TIFF (frame grab from video).
- **⚙ per file**: codec (H.264 / H.265 / VP9 / AV1 / ProRes / copy), quality + speed, target bitrate, resolution, FPS,
  rotate/flip, GPU encoders (NVENC / Quick Sync / AMF - shown only if your FFmpeg has them), audio codec/bitrate/sample rate/channels/volume,
  loudness normalize, remove audio, trim, strip metadata, extra FFmpeg args, and a "show command" preview.
- **Local FFmpeg**: uses a copy it manages in `%LOCALAPPDATA%\FFmpegStudio\ffmpeg`. Search order is managed copy -> `ffmpeg\` next to the EXE -> PATH.
  If none is found it downloads one on first run (gyan.dev "release essentials", SHA-256 verified, GitHub mirror as fallback).
- **Updates**: checks at launch and every 6 h. A newer release is downloaded to a staging folder in the background and swapped in
  when no conversion is running (or on next launch). Your own PATH/portable FFmpeg is never replaced - you just see "update available".
- **Notification** when a batch finishes - only if this app isn't the focused window. Click it to open the output folder.
- **Themes**: follows the system light/dark setting by default (live), or force Light / Dark. Accent: dark green (light), lighter green (dark).
- Parallel conversions (1-4), cancel per file or all, auto-rename instead of overwriting.

## Notes
- Unsigned EXEs can trigger Windows SmartScreen; that's normal for self-built apps.
- Settings live in the registry (`HKCU\Software\FFmpegStudio`).
- The downloaded builds are GPL-licensed FFmpeg binaries from gyan.dev; they're downloaded, not bundled in your EXE.
