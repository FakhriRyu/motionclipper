#!/usr/bin/env bash
# Pasang semua kebutuhan MotionClipper KL di Mac (Apple Silicon).
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname)" == "Darwin" ]]; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew belum terpasang. Pasang dulu dari https://brew.sh lalu jalankan ./setup.sh lagi."
    exit 1
  fi
  echo "==> Memasang ffmpeg-full (ffmpeg dengan subtitle/libass) dan Python 3.12…"
  brew list ffmpeg-full >/dev/null 2>&1 || brew install ffmpeg-full
  brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
  echo "==> Memasang Deno (dibutuhkan yt-dlp untuk mengunduh dari YouTube)…"
  command -v deno >/dev/null 2>&1 || brew install deno
  PY="$(brew --prefix python@3.12)/bin/python3.12"
else
  PY="$(command -v python3)"
fi

echo "==> Membuat virtual environment .venv"
"$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
echo "==> Memasang paket Python (FastAPI, yt-dlp, mlx-whisper, …)"
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install --upgrade "yt-dlp[default]" >/dev/null

[ -f .env ] || cp .env.example .env
mkdir -p data/inbox
chmod +x run.sh

echo ""
echo "Selesai. Jalankan:  ./run.sh"
echo "Lalu buka http://localhost:8000 dan isi API key DeepSeek di halaman Pengaturan."
