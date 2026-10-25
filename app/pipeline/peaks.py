"""Deteksi lonjakan suara (teriakan, reaksi heboh) dari audio WAV 16 kHz mono."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

WINDOW = 0.5  # detik


def loudness_db(path: Path) -> np.ndarray:
    """RMS per jendela 0,5 detik dalam dB. Dibaca per potongan agar hemat RAM."""
    values = []
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        win = int(rate * WINDOW)
        block = win * 600  # 5 menit per baca
        total = w.getnframes()
        pos = 0
        while pos < total:
            frames = w.readframes(min(block, total - pos))
            pos += block
            x = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            n = len(x) // win
            if n == 0:
                continue
            x = x[: n * win].reshape(n, win)
            rms = np.sqrt(np.mean(x * x, axis=1) + 1e-10)
            values.append(20 * np.log10(rms))
    return np.concatenate(values) if values else np.zeros(0)


def find_peaks(path: Path, max_peaks: int = 60, min_rise_db: float = 8.0) -> list[dict]:
    db = loudness_db(path)
    if db.size < 10:
        return []
    # Baseline = median bergerak 60 detik, supaya cocok untuk live yang volumenya naik-turun.
    k = int(60 / WINDOW) | 1
    pad = k // 2
    padded = np.pad(db, pad, mode="edge")
    from numpy.lib.stride_tricks import sliding_window_view

    baseline = np.median(sliding_window_view(padded, k)[:: 1], axis=1)[: db.size]
    rise = db - baseline
    loud_floor = np.percentile(db, 70)
    hot = (rise >= min_rise_db) & (db >= loud_floor)
    peaks: list[dict] = []
    i = 0
    while i < db.size:
        if not hot[i]:
            i += 1
            continue
        j = i
        while j + 1 < db.size and (hot[j + 1] or (j + 2 < db.size and hot[j + 2])):
            j += 1
        seg = rise[i : j + 1]
        top = int(np.argmax(seg)) + i
        peaks.append(
            {
                "t": round(top * WINDOW, 1),
                "start": round(i * WINDOW, 1),
                "end": round((j + 1) * WINDOW, 1),
                "rise": round(float(rise[top]), 1),
            }
        )
        i = j + 1
    # Gabungkan lonjakan yang berdekatan (< 3 detik).
    merged: list[dict] = []
    for p in peaks:
        if merged and p["start"] - merged[-1]["end"] < 3:
            m = merged[-1]
            m["end"] = p["end"]
            if p["rise"] > m["rise"]:
                m["rise"], m["t"] = p["rise"], p["t"]
        else:
            merged.append(dict(p))
    merged.sort(key=lambda p: p["rise"], reverse=True)
    top = merged[:max_peaks]
    top.sort(key=lambda p: p["t"])
    return top
