"""Menjalankan transkripsi di subprocess lalu menyusun kalimat dari kata."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from ..config import IS_MAC_ARM, ROOT, settings
from .media import Cancelled, PipelineError


def pick_backend() -> str:
    b = settings.whisper_backend
    if b != "auto":
        return b
    if settings.mock_transcript:
        return "mock"
    if IS_MAC_ARM:
        return "mlx"
    return "faster"


def run_transcription(audio: Path, out_json: Path, on_progress=None, should_cancel=None, log=None) -> None:
    backend = pick_backend()
    cmd = [
        sys.executable, "-m", "app.pipeline.transcribe_worker",
        "--audio", str(audio), "--out", str(out_json),
        "--backend", backend, "--model", settings.whisper_model,
        "--language", settings.whisper_language, "--chunk", str(max(60, settings.chunk_minutes * 60)),
    ]
    if backend == "mock":
        cmd += ["--mock", settings.mock_transcript]
    if log:
        log(f"Transkripsi dengan {backend} · model {settings.whisper_model}")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    err_tail: list[str] = []

    import threading

    def drain():
        for line in proc.stderr:  # type: ignore[union-attr]
            if line.strip():
                err_tail.append(line.rstrip())
                del err_tail[:-25]

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    for line in proc.stdout:  # type: ignore[union-attr]
        if should_cancel and should_cancel():
            proc.kill()
            raise Cancelled()
        if line.startswith("PROGRESS") and on_progress:
            try:
                on_progress(float(line.split()[1]))
            except (IndexError, ValueError):
                pass
    proc.wait()
    t.join(timeout=2)
    if proc.returncode != 0:
        hint = ""
        joined = "\n".join(err_tail)
        if "No module named 'mlx_whisper'" in joined:
            hint = "\nmlx-whisper belum terpasang. Jalankan ./setup.sh lagi."
        elif "Killed" in joined or proc.returncode in (-9, 137):
            hint = "\nKemungkinan RAM habis. Tutup aplikasi lain atau pakai model lebih kecil di Pengaturan."
        raise PipelineError("Transkripsi gagal:\n" + joined[-1200:] + hint)


_END = re.compile(r"[.!?…]$")


def build_sentences(words: list[dict], max_gap: float = 0.8, max_len: float = 10.0, max_words: int = 28) -> list[dict]:
    """Kelompokkan kata jadi kalimat pendek: putus di tanda baca, jeda, atau panjang maksimum."""
    sentences: list[dict] = []
    start_i = 0
    for i, w in enumerate(words):
        text = w["w"].strip()
        is_last = i == len(words) - 1
        gap = (words[i + 1]["s"] - w["e"]) if not is_last else 0
        length = w["e"] - words[start_i]["s"]
        count = i - start_i + 1
        if is_last or _END.search(text) or gap > max_gap or length >= max_len or count >= max_words:
            seg = words[start_i : i + 1]
            sentences.append(
                {
                    "s": round(seg[0]["s"], 2),
                    "e": round(seg[-1]["e"], 2),
                    "t": " ".join(x["w"].strip() for x in seg).strip(),
                    "w0": start_i,
                    "w1": i,
                }
            )
            start_i = i + 1
    return [s for s in sentences if s["t"]]


def clean_words(words: list[dict]) -> list[dict]:
    out = []
    last_end = 0.0
    for w in words:
        text = (w.get("w") or "").strip()
        if not text:
            continue
        s, e = float(w["s"]), float(w["e"])
        s = max(s, last_end - 0.05)
        if e <= s:
            e = s + 0.12
        out.append({"w": text, "s": round(s, 3), "e": round(e, 3), "p": round(float(w.get("p", 1)), 3)})
        last_end = e
    return out


def load_transcript(path: Path) -> dict:
    from ..storage import read_json

    data = read_json(path)
    if not data:
        raise PipelineError("Transkrip belum ada.")
    return data


def finalize_transcript(raw_json: Path, out_json: Path) -> dict:
    from ..storage import read_json, write_json

    raw = read_json(raw_json, {}) or {}
    words = clean_words(raw.get("words", []))
    if not words:
        raise PipelineError("Transkripsi tidak menghasilkan kata. Apakah videonya ada suara?")
    sentences = build_sentences(words)
    data = {"words": words, "sentences": sentences}
    write_json(out_json, data)
    return data
