"""Panggilan DeepSeek API (cloud). Hanya teks transkrip yang dikirim, bukan video."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from ..config import settings
from .media import PipelineError


def is_peak(now: datetime | None = None) -> bool:
    """Jam sibuk DeepSeek: Senin–Jumat 01:00–04:00 dan 06:00–10:00 UTC."""
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    return 1 <= now.hour < 4 or 6 <= now.hour < 10


def cost_usd(usage: dict) -> float:
    factor = 1.0 if is_peak() else 0.5
    return factor * (
        usage.get("prompt_hit", 0) * settings.price_in_hit
        + usage.get("prompt_miss", 0) * settings.price_in_miss
        + usage.get("completion", 0) * settings.price_out
    ) / 1_000_000


def _client():
    if not settings.deepseek_api_key:
        raise PipelineError("API key DeepSeek belum diisi. Buka halaman Pengaturan.")
    from openai import OpenAI

    return OpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url, timeout=900, max_retries=2)


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise PipelineError("DeepSeek tidak mengembalikan JSON yang valid:\n" + text[:500])


def chat_json(system: str, user: str, thinking: bool) -> tuple[dict, dict]:
    """Kirim prompt, minta JSON. Kembalikan (data, usage)."""
    client = _client()
    kwargs = dict(
        model=settings.deepseek_model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}},
        max_tokens=32000 if thinking else 8000,
    )
    from openai import BadRequestError

    try:
        resp = client.chat.completions.create(**kwargs)
    except BadRequestError:
        # Sebagian kombinasi mode mungkin tidak menerima response_format; coba tanpa itu.
        kwargs.pop("response_format")
        resp = client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or ""
    u = resp.usage
    hit = int(getattr(u, "prompt_cache_hit_tokens", 0) or 0)
    miss = int(getattr(u, "prompt_cache_miss_tokens", 0) or 0)
    if not hit and not miss:
        miss = int(getattr(u, "prompt_tokens", 0) or 0)
    usage = {"prompt_hit": hit, "prompt_miss": miss, "completion": int(getattr(u, "completion_tokens", 0) or 0)}
    usage["cost_usd"] = cost_usd(usage)
    return _parse_json(content), usage


# ---------------------------------------------------------------- prompt

SELECT_SYSTEM = """Kamu editor video pendek profesional untuk TikTok, Instagram Reels, dan YouTube Shorts berbahasa Indonesia.
Tugasmu memilih momen terbaik dari transkrip video panjang untuk dijadikan clip vertikal campaign berbayar per views.

Kriteria momen yang bagus:
- Hook kuat di 3 detik pertama (kalimat pembuka yang bikin penasaran atau reaksi heboh).
- Cerita utuh: ada awal, puncak, dan penutup/punchline. Penonton paham tanpa menonton video penuh.
- Emosi kuat: kaget, senang, tegang, lucu. Baris bertanda {LONJAKAN} berarti suara tiba-tiba keras (teriak/reaksi), sering jadi puncak momen.
- Sesuai arahan campaign dan TIDAK melanggar larangan campaign.

Aturan output:
- Gunakan HANYA nomor baris (#) yang ada di transkrip. start_line = baris pertama clip, end_line = baris terakhir.
- Durasi clip (waktu end_line sampai selesai dikurangi waktu start_line) harus di dalam rentang durasi yang diminta.
- Clip tidak boleh tumpang tindih.
- Urutkan dari skor tertinggi. Skor 0-100 = peluang clip ini viral.
- Balas HANYA dengan JSON, tanpa teks lain, dengan format:
{"clips": [{"start_line": 120, "end_line": 134, "score": 88, "hook_line": "kalimat pembuka clip", "reason": "alasan singkat", "category": "reaksi|cerita|lucu|info|lainnya"}]}"""


METADATA_SYSTEM = """Kamu copywriter media sosial untuk clip pendek berbahasa Indonesia (TikTok, Reels, Shorts).
Untuk setiap clip, tulis:
- title: judul maksimal 60 karakter, memancing rasa penasaran, tidak clickbait palsu.
- hook_text: teks layar untuk 3 detik pertama, maksimal 8 kata.
- caption: 1-2 kalimat caption santai. JANGAN tulis hashtag, link, atau website (itu ditambahkan otomatis).
- compliance_ok: false jika isi clip melanggar salah satu larangan campaign, true jika aman.
- issues: daftar singkat pelanggaran yang ditemukan (kosong jika aman).
Balas HANYA dengan JSON dengan format:
{"clips": [{"id": "c01", "title": "...", "hook_text": "...", "caption": "...", "compliance_ok": true, "issues": []}]}"""


def campaign_brief(c: dict) -> str:
    parts = [f"Campaign: {c.get('name', '')}"]
    if c.get("content_hint"):
        parts.append(f"Arahan konten: {c['content_hint']}")
    if c.get("moment_keywords"):
        parts.append("Kata kunci momen: " + ", ".join(c["moment_keywords"]))
    if c.get("banned"):
        parts.append("Larangan:\n" + "\n".join(f"- {b}" for b in c["banned"]))
    return "\n".join(parts)


def fmt_time(t: float) -> str:
    t = int(t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def transcript_lines(sentences: list[dict], peaks: list[dict]) -> str:
    lines = []
    pi = 0
    for i, s in enumerate(sentences):
        tag = ""
        while pi < len(peaks) and peaks[pi]["end"] < s["s"]:
            pi += 1
        for p in peaks[pi : pi + 3]:
            if p["start"] <= s["e"] + 0.5 and p["end"] >= s["s"] - 0.5:
                tag = f" {{LONJAKAN +{p['rise']:.0f}dB}}"
                break
        lines.append(f"#{i} [{s['s']:.1f}s] {s['t']}{tag}")
    return "\n".join(lines)


def select_moments(campaign: dict, sentences: list[dict], peaks: list[dict], count: int) -> tuple[list[dict], dict]:
    if settings.mock_llm:
        return _mock_select(sentences, campaign, count), {"prompt_hit": 0, "prompt_miss": 0, "completion": 0, "cost_usd": 0.0}
    user = (
        f"{campaign_brief(campaign)}\n\n"
        f"Pilih {count} clip terbaik. Durasi tiap clip {campaign['duration_min']}-{campaign['duration_max']} detik.\n"
        f"Total durasi video: {fmt_time(sentences[-1]['e'])}.\n\n"
        f"TRANSKRIP (format: #nomor [waktu mulai] teks):\n{transcript_lines(sentences, peaks)}\n\n"
        "Balas dalam JSON."
    )
    data, usage = chat_json(SELECT_SYSTEM, user, thinking=True)
    clips = data.get("clips") if isinstance(data, dict) else None
    if not isinstance(clips, list):
        raise PipelineError("Respons DeepSeek tidak berisi daftar clips.")
    return clips, usage


def write_metadata(campaign: dict, items: list[dict]) -> tuple[dict, dict]:
    """items: [{id, text}] -> {id: {...}}"""
    if settings.mock_llm:
        return {
            it["id"]: {
                "title": "Momen pull hit paling heboh",
                "hook_text": "Ga nyangka dapet ini!",
                "caption": "Reaksinya pas dapet kartu langka bikin ikut deg-degan.",
                "compliance_ok": True,
                "issues": [],
            }
            for it in items
        }, {"prompt_hit": 0, "prompt_miss": 0, "completion": 0, "cost_usd": 0.0}
    body = "\n\n".join(f"[{it['id']}]\n{it['text'][:2000]}" for it in items)
    user = f"{campaign_brief(campaign)}\n\nCLIP:\n{body}\n\nBalas dalam JSON."
    data, usage = chat_json(METADATA_SYSTEM, user, thinking=False)
    out = {}
    for c in data.get("clips", []) if isinstance(data, dict) else []:
        if isinstance(c, dict) and c.get("id"):
            out[str(c["id"])] = c
    return out, usage


def _mock_select(sentences: list[dict], campaign: dict, count: int) -> list[dict]:
    """Pilihan palsu untuk tes tanpa API: bagi video jadi beberapa bagian rata."""
    out = []
    n = len(sentences)
    step = max(1, n // max(1, count))
    for k in range(count):
        a = k * step
        if a >= n:
            break
        b = a
        while b + 1 < n and sentences[b]["e"] - sentences[a]["s"] < campaign["duration_min"]:
            b += 1
        out.append({"start_line": a, "end_line": b, "score": 90 - k * 5, "hook_line": sentences[a]["t"], "reason": "tes", "category": "reaksi"})
    return out
