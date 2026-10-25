"""Membuat file subtitle ASS: kata per kata dengan sorotan kata aktif, plus teks hook."""
from __future__ import annotations

import re

W, H = 1080, 1920


def ass_color(hex_color: str, alpha: int = 0) -> str:
    h = (hex_color or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


class Timeline:
    """Memetakan waktu sumber ke waktu clip akhir (memperhitungkan asset yang disisipkan)."""

    def __init__(self, start: float, end: float, insert_at: str, hook_len: float, asset_dur: float):
        self.start, self.end = start, end
        self.insert_at = insert_at if asset_dur > 0 else "none"
        self.hook_len = hook_len
        self.asset_dur = asset_dur

    def interval(self, s: float, e: float) -> tuple[float, float] | None:
        rs, re_ = s - self.start, e - self.start
        length = self.end - self.start
        if re_ <= 0 or rs >= length:
            return None
        rs, re_ = max(0.0, rs), min(length, re_)
        if self.insert_at == "start":
            return rs + self.asset_dur, re_ + self.asset_dur
        if self.insert_at == "after_hook":
            if rs < self.hook_len:
                return rs, min(re_, self.hook_len)
            return rs + self.asset_dur, re_ + self.asset_dur
        return rs, re_

    @property
    def total(self) -> float:
        return (self.end - self.start) + (self.asset_dur if self.insert_at != "none" else 0)


def group_words(words: list[dict], per_line: int, max_chars: int = 20) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for i, w in enumerate(words):
        cur.append(w)
        text_len = sum(len(x["w"]) + 1 for x in cur)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["s"] - w["e"]) if nxt else 99
        if len(cur) >= per_line or text_len >= max_chars or gap > 0.6 or re.search(r"[.!?,…]$", w["w"]):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def build_ass(words: list[dict], timeline: Timeline, style: dict, layout_mode: str, hook_text: str = "") -> str:
    size = int(style.get("size", 76))
    font = style.get("font") or "Arial Black"
    base = ass_color(style.get("color", "#FFFFFF"))
    hi = ass_color(style.get("highlight", "#FFD21F"))
    upper = bool(style.get("uppercase", True))
    per_line = max(1, int(style.get("words_per_line", 3)))

    if layout_mode == "split_facecam_bottom":
        pos = r"\an5\pos(540,1152)"
    else:
        pos = r"\an2\pos(540,1390)"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},{size},{base},{base},&H00000000,&H78000000,1,0,0,0,100,100,0,0,1,{max(3, size // 12)},2,2,70,70,0,1
Style: Hook,{font},{int(size * 0.8)},&H00FFFFFF,&H00FFFFFF,&H00141414,&H00141414,1,0,0,0,100,100,0,0,3,16,0,8,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    if hook_text and style.get("hook_overlay", True):
        txt = _esc(hook_text.upper() if upper else hook_text)
        hook_end = min(3.0, timeline.total)
        if timeline.insert_at == "after_hook":
            hook_end = min(hook_end, timeline.hook_len)
        events.append(f"Dialogue: 1,{ass_time(0)},{ass_time(hook_end)},Hook,,0,0,0,,{{\\an8\\pos(540,430)\\fad(120,200)}}{txt}")

    if style.get("enabled", True):
        inside = [w for w in words if timeline.interval(w["s"], w["e"])]
        groups = group_words(inside, per_line)
        for gi, g in enumerate(groups):
            next_start = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else None
            g_end = g[-1]["e"] + 0.25
            if next_start is not None:
                g_end = min(g_end, next_start) if next_start - g[-1]["e"] > 0.4 else next_start
            tokens = [_esc(w["w"].upper() if upper else w["w"]) for w in g]
            for k, w in enumerate(g):
                s = w["s"]
                e = g[k + 1]["s"] if k + 1 < len(g) else g_end
                iv = timeline.interval(s, max(e, s + 0.05))
                if not iv or iv[1] - iv[0] < 0.02:
                    continue
                parts = []
                for j, tok in enumerate(tokens):
                    parts.append(f"{{\\c{hi}}}{tok}{{\\c{base}}}" if j == k else tok)
                events.append(f"Dialogue: 0,{ass_time(iv[0])},{ass_time(iv[1])},Sub,,0,0,0,,{{{pos}}}{' '.join(parts)}")
    return header + "\n".join(events) + "\n"
