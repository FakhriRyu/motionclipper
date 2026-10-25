#!/usr/bin/env bash
# Menjalankan web app di http://localhost:8000 (hanya bisa diakses dari Mac ini).
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Belum di-setup. Jalankan ./setup.sh dulu."
  exit 1
fi
for d in /opt/homebrew/bin /opt/homebrew/opt/ffmpeg-full/bin /usr/local/opt/ffmpeg-full/bin; do
  [ -d "$d" ] && export PATH="$d:$PATH"
done
PORT="${PORT:-8000}"
(sleep 2; command -v open >/dev/null && open "http://localhost:$PORT") >/dev/null 2>&1 &
echo "MotionClipper KL berjalan di http://localhost:$PORT  (tekan Ctrl+C untuk berhenti)"
if command -v caffeinate >/dev/null 2>&1; then
  # Cegah Mac tidur selama aplikasi jalan, supaya antrian semalaman tidak terhenti.
  exec caffeinate -i .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
fi
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
