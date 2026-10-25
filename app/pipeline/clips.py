"""Validasi clip dari AI: tempel ke batas kalimat, atur durasi, buang tumpang tindih."""
from __future__ import annotations

import re


def _int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def snap_clip(sentences: list[dict], a: int, b: int, dmin: float, dmax: float) -> tuple[int, int]:
    n = len(sentences)
    a = max(0, min(n - 1, a))
    b = max(a, min(n - 1, b))

    def dur(x, y):
        return sentences[y]["e"] - sentences[x]["s"]

    while dur(a, b) > dmax and b > a:
        b -= 1
    while dur(a, b) < dmin and b < n - 1 and dur(a, b + 1) <= dmax:
        b += 1
    while dur(a, b) < dmin and a > 0 and dur(a - 1, b) <= dmax:
        a -= 1
    return a, b


def times_for(sentences: list[dict], a: int, b: int, media_duration: float) -> tuple[float, float]:
    start = max(0.0, sentences[a]["s"] - 0.15)
    end = sentences[b]["e"] + 0.35
    if b + 1 < len(sentences):
        end = min(end, sentences[b + 1]["s"] - 0.02)
    if media_duration:
        end = min(end, media_duration)
    return round(start, 2), round(max(end, start + 1.0), 2)


def overlap(x: dict, y: dict) -> float:
    return max(0.0, min(x["end"], y["end"]) - max(x["start"], y["start"]))


def validate(raw: list[dict], sentences: list[dict], campaign: dict, media_duration: float) -> list[dict]:
    dmin, dmax = float(campaign["duration_min"]), float(campaign["duration_max"])
    candidates = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        a, b = snap_clip(sentences, _int(r.get("start_line")), _int(r.get("end_line")), dmin, dmax)
        start, end = times_for(sentences, a, b, media_duration)
        candidates.append(
            {
                "start": start,
                "end": end,
                "line_start": a,
                "line_end": b,
                "score": max(0, min(100, _int(r.get("score"), 50))),
                "hook_line": str(r.get("hook_line") or sentences[a]["t"])[:200],
                "reason": str(r.get("reason") or "")[:400],
                "category": str(r.get("category") or "")[:30],
            }
        )
    candidates.sort(key=lambda c: c["score"], reverse=True)
    accepted: list[dict] = []
    for c in candidates:
        length = c["end"] - c["start"]
        if all(overlap(c, x) < 0.2 * length for x in accepted):
            accepted.append(c)
    for i, c in enumerate(accepted, 1):
        c["id"] = f"c{i:02d}"
    return accepted


def clip_text(sentences: list[dict], start: float, end: float) -> str:
    return " ".join(s["t"] for s in sentences if s["e"] > start and s["s"] < end)


_HASHTAG = re.compile(r"#\w+")
_URL = re.compile(r"(https?://\S+|www\.\S+)", re.I)


def compose_caption(body: str, campaign: dict) -> str:
    body = _URL.sub("", _HASHTAG.sub("", body or "")).strip()
    body = re.sub(r"[ \t]{2,}", " ", body)
    parts = [body] if body else []
    required = [r for r in campaign.get("caption_required", []) if r.strip()]
    if required:
        parts.append("\n".join(required))
    tags = [t if t.startswith("#") else "#" + t for t in campaign.get("hashtags_ordered", []) if t.strip()]
    if tags:
        parts.append(" ".join(tags))
    return "\n\n".join(parts)


def checklist(clip: dict, campaign: dict, assets: dict) -> list[dict]:
    """Cek otomatis aturan brief per clip. assets: {'watermark': bool, 'logo': bool, 'asset': bool}"""
    items = []
    dmin, dmax = campaign["duration_min"], campaign["duration_max"]
    total = clip.get("final_duration") or (clip["end"] - clip["start"])
    items.append({"label": f"Durasi {dmin}–{dmax} dtk", "ok": dmin - 0.5 <= total <= dmax + 0.5 + (clip.get("asset_duration") or 0), "detail": f"{total:.1f} dtk"})
    if campaign.get("watermark", {}).get("position") != "none":
        items.append({"label": "Watermark tampil", "ok": assets.get("watermark", False), "detail": "" if assets.get("watermark") else "File watermark belum diunggah"})
    if campaign.get("logo", {}).get("position") != "none":
        items.append({"label": "Logo tidak transparan", "ok": assets.get("logo", False), "detail": "" if assets.get("logo") else "File logo belum diunggah"})
    if campaign.get("asset", {}).get("insert_at", "none") != "none":
        items.append({"label": "Asset video wajib", "ok": assets.get("asset", False), "detail": "" if assets.get("asset") else "File asset belum diunggah"})
    cap = clip.get("caption_full") or ""
    for req in campaign.get("caption_required", []):
        if req.strip():
            items.append({"label": f"Caption memuat {req}", "ok": req in cap, "detail": ""})
    tags = [t for t in campaign.get("hashtags_ordered", []) if t.strip()]
    if tags:
        pos = [cap.find(t) for t in tags]
        ok = all(p >= 0 for p in pos) and pos == sorted(pos)
        items.append({"label": "Hashtag lengkap & berurutan", "ok": ok, "detail": " ".join(tags)})
    comp = clip.get("compliance") or {}
    items.append({"label": "Lolos cek larangan (AI)", "ok": comp.get("ok", True), "detail": "; ".join(comp.get("issues", []))})
    return items
