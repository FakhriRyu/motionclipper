"""Proses terpisah untuk transkripsi.

Dijalankan sebagai subprocess supaya memori model Whisper langsung dilepas setelah selesai,
penting untuk Mac dengan RAM 8 GB. Audio dibaca per potongan (default 20 menit).

Pemakaian:
  python -m app.pipeline.transcribe_worker --audio audio.wav --out words.json \
      --backend mlx --model mlx-community/whisper-large-v3-turbo --language id --chunk 1200
Menulis baris "PROGRESS <0..1>" ke stdout.
"""
from __future__ import annotations

import argparse
import inspect
import json
import sys
import wave

import numpy as np


def read_chunks(path: str, chunk_sec: int):
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        total = w.getnframes()
        step = chunk_sec * rate
        pos = 0
        while pos < total:
            w.setpos(pos)
            frames = w.readframes(min(step, total - pos))
            audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            yield pos / rate, audio, total / rate
            pos += step


def transcribe_mlx(audio: np.ndarray, model: str, language: str) -> list[dict]:
    import mlx_whisper

    kwargs = dict(
        path_or_hf_repo=model,
        word_timestamps=True,
        language=language or None,
        condition_on_previous_text=False,
        verbose=None,
    )
    params = inspect.signature(mlx_whisper.transcribe).parameters
    if "hallucination_silence_threshold" in params:
        kwargs["hallucination_silence_threshold"] = 2.0
    result = mlx_whisper.transcribe(audio, **kwargs)
    words = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []) or []:
            words.append({"w": w["word"], "s": float(w["start"]), "e": float(w["end"]), "p": float(w.get("probability", 1))})
    return words


_faster_model = None


def transcribe_faster(audio: np.ndarray, model: str, language: str) -> list[dict]:
    global _faster_model
    from faster_whisper import WhisperModel

    name = model.split("/")[-1].replace("whisper-", "") if "mlx-community" in model else model
    if _faster_model is None:
        _faster_model = WhisperModel(name, device="auto", compute_type="int8")
    segments, _info = _faster_model.transcribe(
        audio, language=language or None, word_timestamps=True, vad_filter=True, condition_on_previous_text=False
    )
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append({"w": w.word, "s": float(w.start), "e": float(w.end), "p": float(w.probability)})
    return words


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--backend", default="mlx")
    ap.add_argument("--model", required=True)
    ap.add_argument("--language", default="id")
    ap.add_argument("--chunk", type=int, default=1200)
    ap.add_argument("--mock", default="")
    args = ap.parse_args()

    if args.backend == "mock":
        with open(args.mock, encoding="utf-8") as f:
            data = json.load(f)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"words": data["words"]}, f, ensure_ascii=False)
        print("PROGRESS 1.0", flush=True)
        return 0

    fn = transcribe_mlx if args.backend == "mlx" else transcribe_faster
    all_words: list[dict] = []
    for offset, audio, total in read_chunks(args.audio, args.chunk):
        if audio.size < 1600:  # < 0.1 detik
            continue
        words = fn(audio, args.model, args.language)
        for w in words:
            w["s"] = round(w["s"] + offset, 3)
            w["e"] = round(w["e"] + offset, 3)
        all_words.extend(words)
        done = min(1.0, (offset + audio.size / 16000) / total) if total else 1.0
        print(f"PROGRESS {done:.4f}", flush=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"words": all_words}, f, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
