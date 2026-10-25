"""Pengaturan aplikasi. Nilai dibaca dari file .env di folder proyek."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MCK_DATA_DIR", ROOT / "data"))
ENV_FILE = ROOT / ".env"

IS_MAC_ARM = platform.system() == "Darwin" and platform.machine() == "arm64"

# Lokasi umum ffmpeg-full dari Homebrew (keg-only, jadi tidak otomatis ada di PATH).
FFMPEG_CANDIDATES = [
    "/opt/homebrew/opt/ffmpeg-full/bin",
    "/usr/local/opt/ffmpeg-full/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
]


def _find_bin(name: str, configured: str | None) -> str:
    if configured:
        return configured
    for folder in FFMPEG_CANDIDATES:
        p = Path(folder) / name
        if p.exists():
            return str(p)
    return shutil.which(name) or name


@dataclass
class Settings:
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    whisper_backend: str = "auto"  # auto | mlx | faster | mock
    whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    whisper_language: str = "id"
    chunk_minutes: int = 20
    source_max_height: int = 1080
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    usd_to_idr: float = 16500.0
    # Harga deepseek-flash per 1 juta token (USD), jam sibuk. Off-peak = setengahnya.
    price_in_hit: float = 0.006
    price_in_miss: float = 0.30
    price_out: float = 1.20
    mock_transcript: str = ""  # hanya untuk tes
    mock_llm: bool = False  # hanya untuk tes
    extra: dict = field(default_factory=dict)


ENV_KEYS = {
    "DEEPSEEK_API_KEY": "deepseek_api_key",
    "DEEPSEEK_BASE_URL": "deepseek_base_url",
    "DEEPSEEK_MODEL": "deepseek_model",
    "WHISPER_BACKEND": "whisper_backend",
    "WHISPER_MODEL": "whisper_model",
    "WHISPER_LANGUAGE": "whisper_language",
    "CHUNK_MINUTES": "chunk_minutes",
    "SOURCE_MAX_HEIGHT": "source_max_height",
    "FFMPEG_BIN": "ffmpeg_bin",
    "FFPROBE_BIN": "ffprobe_bin",
    "USD_TO_IDR": "usd_to_idr",
    "MOCK_TRANSCRIPT": "mock_transcript",
    "MOCK_LLM": "mock_llm",
}


def load_settings() -> Settings:
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        values.update({k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None})
    for key in ENV_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]
    s = Settings()
    for key, attr in ENV_KEYS.items():
        if key not in values or values[key] == "":
            continue
        raw = values[key]
        current = getattr(s, attr)
        if isinstance(current, bool):
            setattr(s, attr, raw.strip().lower() in ("1", "true", "yes", "ya"))
        elif isinstance(current, int):
            setattr(s, attr, int(float(raw)))
        elif isinstance(current, float):
            setattr(s, attr, float(raw))
        else:
            setattr(s, attr, raw.strip())
    s.ffmpeg_bin = _find_bin("ffmpeg", values.get("FFMPEG_BIN"))
    s.ffprobe_bin = _find_bin("ffprobe", values.get("FFPROBE_BIN"))
    return s


settings = load_settings()


def reload_settings() -> Settings:
    global settings
    new = load_settings()
    settings.__dict__.update(new.__dict__)
    _system_cache.clear()
    return settings


def save_env(updates: dict[str, str]) -> None:
    """Tulis/ubah kunci di .env tanpa menghapus baris lain."""
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    ENV_FILE.write_text("\n".join(out) + "\n")
    reload_settings()


_system_cache: dict = {}


def system_info() -> dict:
    """Cek kelengkapan alat. Hasil di-cache sampai pengaturan berubah."""
    if _system_cache:
        return _system_cache
    info: dict = {"platform": f"{platform.system()} {platform.machine()}", "is_mac_arm": IS_MAC_ARM}
    try:
        ver = subprocess.run([settings.ffmpeg_bin, "-hide_banner", "-version"], capture_output=True, text=True, timeout=10)
        info["ffmpeg"] = ver.stdout.splitlines()[0].split(" Copyright")[0] if ver.stdout else ""
        filters = subprocess.run([settings.ffmpeg_bin, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=10).stdout
        encoders = subprocess.run([settings.ffmpeg_bin, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10).stdout
        info["has_ass"] = any(l.split()[1:2] == ["ass"] for l in filters.splitlines() if len(l.split()) > 1)
        info["has_videotoolbox"] = "h264_videotoolbox" in encoders
        info["has_x264"] = "libx264" in encoders
    except (OSError, subprocess.SubprocessError):
        info.update(ffmpeg="", has_ass=False, has_videotoolbox=False, has_x264=False)
    try:
        import yt_dlp  # noqa: F401
        info["yt_dlp"] = yt_dlp.version.__version__
    except Exception:  # noqa: BLE001
        info["yt_dlp"] = ""
    info["deno"] = bool(shutil.which("deno") or Path("/opt/homebrew/bin/deno").exists())
    info["mlx_whisper"] = _importable("mlx_whisper")
    info["faster_whisper"] = _importable("faster_whisper")
    info["api_key"] = bool(settings.deepseek_api_key) or settings.mock_llm
    _system_cache.update(info)
    return info


def _importable(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def video_encoder_args() -> list[str]:
    info = system_info()
    if info.get("has_videotoolbox"):
        return ["-c:v", "h264_videotoolbox", "-b:v", "8M", "-maxrate", "10M", "-profile:v", "high", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high"]
