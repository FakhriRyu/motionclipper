"""Helper FFmpeg/ffprobe dan pengunduh yt-dlp."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from ..config import settings


class Cancelled(Exception):
    pass


class PipelineError(Exception):
    pass


def probe(path: Path) -> dict:
    out = subprocess.run(
        [settings.ffprobe_bin, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise PipelineError(f"ffprobe gagal membaca {path.name}: {out.stderr.strip()[:300]}")
    data = json.loads(out.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {
        "duration": float(data.get("format", {}).get("duration") or 0),
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "has_audio": a is not None,
        "has_video": v is not None,
    }


def run_ffmpeg(
    args: list[str],
    duration: float | None = None,
    on_progress: Callable[[float], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    cwd: Path | None = None,
) -> None:
    """Jalankan ffmpeg, laporkan progres 0..1, bisa dibatalkan."""
    cmd = [settings.ffmpeg_bin, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd)
    stderr_tail: list[str] = []

    import threading

    def drain():
        for line in proc.stderr:  # type: ignore[union-attr]
            stderr_tail.append(line.rstrip())
            del stderr_tail[:-40]

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    last = 0.0
    for line in proc.stdout:  # type: ignore[union-attr]
        if should_cancel and should_cancel():
            proc.kill()
            raise Cancelled()
        if line.startswith("out_time_us=") and duration and on_progress:
            try:
                us = int(line.split("=", 1)[1])
            except ValueError:
                continue
            frac = max(0.0, min(1.0, us / 1e6 / duration))
            if frac - last >= 0.01:
                last = frac
                on_progress(frac)
    proc.wait()
    t.join(timeout=2)
    if proc.returncode != 0:
        msg = "\n".join(l for l in stderr_tail if l.strip())[-1200:]
        raise PipelineError(f"ffmpeg gagal (kode {proc.returncode}):\n{msg}")


def extract_audio(video: Path, out_wav: Path, duration: float, on_progress=None, should_cancel=None) -> None:
    run_ffmpeg(
        ["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav)],
        duration=duration,
        on_progress=on_progress,
        should_cancel=should_cancel,
    )


def snapshot(video: Path, out_jpg: Path, at: float) -> None:
    run_ffmpeg(["-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out_jpg)])


def youtube_info(url: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--no-playlist", "--skip-download", "--print", "%(title)s\t%(duration)s", url],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise PipelineError("yt-dlp gagal membaca link: " + proc.stderr.strip()[-500:])
    title, _, dur = proc.stdout.strip().splitlines()[-1].partition("\t")
    try:
        duration = float(dur)
    except ValueError:
        duration = 0.0
    return {"title": title, "duration": duration}


_PCT = re.compile(r"\[download\]\s+([\d.]+)%")


def download_youtube(url: str, out_dir: Path, on_progress=None, should_cancel=None, log=None) -> Path:
    """Unduh video (utamakan H.264 agar ringan didekode di M1). Kembalikan path file mp4."""
    h = settings.source_max_height
    fmt = (
        f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/"
        f"bv*[height<={h}][vcodec^=avc1]+ba/"
        f"bv*[height<={h}]+ba/b[height<={h}]/b"
    )
    ffmpeg_dir = str(Path(settings.ffmpeg_bin).parent) if "/" in settings.ffmpeg_bin else None
    cmd = [
        sys.executable, "-m", "yt_dlp", "--no-playlist", "--newline", "-f", fmt,
        "--merge-output-format", "mp4", "-o", str(out_dir / "source.%(ext)s"),
    ]
    if ffmpeg_dir:
        cmd += ["--ffmpeg-location", ffmpeg_dir]
    cmd.append(url)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail: list[str] = []
    part = 0  # video lalu audio: dua kali 0..100%
    last_pct = 0.0
    last_report = 0.0
    for line in proc.stdout:  # type: ignore[union-attr]
        if should_cancel and should_cancel():
            proc.kill()
            raise Cancelled()
        tail.append(line.rstrip())
        del tail[:-30]
        m = _PCT.search(line)
        if m and on_progress:
            pct = float(m.group(1))
            if pct < last_pct - 50:
                part += 1
            last_pct = pct
            frac = min(1.0, (part * 100 + pct) / 200 if part < 2 else 1.0)
            if time.time() - last_report > 1:
                last_report = time.time()
                on_progress(frac)
        elif log and line.startswith("[Merger]"):
            log("Menggabungkan video dan audio…")
    proc.wait()
    if proc.returncode != 0:
        raise PipelineError("yt-dlp gagal mengunduh:\n" + "\n".join(tail[-12:]))
    files = sorted(out_dir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    files = [f for f in files if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
    if not files:
        raise PipelineError("File hasil unduhan tidak ditemukan.")
    return files[0]
