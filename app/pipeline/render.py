"""Render clip 9:16: tata letak facecam, watermark, logo, asset wajib, dan subtitle."""
from __future__ import annotations

from pathlib import Path

from ..config import settings, system_info, video_encoder_args
from ..storage import asset_path, campaign_dir, read_json, write_json
from .media import PipelineError, probe, run_ffmpeg
from .subtitles import Timeline, build_ass

W, H = 1080, 1920
TOP_Y = 170
SPLIT_TOP = 1152


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def region_px(region: dict | None, sw: int, sh: int) -> tuple[int, int, int, int]:
    if not region:
        return sw, sh, 0, 0
    x = max(0.0, min(1.0, float(region.get("x", 0))))
    y = max(0.0, min(1.0, float(region.get("y", 0))))
    w = max(0.02, min(1.0 - x, float(region.get("w", 1))))
    h = max(0.02, min(1.0 - y, float(region.get("h", 1))))
    cw, ch = _even(w * sw), _even(h * sh)
    cw, ch = min(cw, sw - sw % 2), min(ch, sh - sh % 2)
    cx, cy = min(int(x * sw), sw - cw), min(int(y * sh), sh - ch)
    return cw, ch, max(0, cx), max(0, cy)


def _fill(label_in: str, label_out: str, tw: int, th: int, fit: str, tag: str) -> str:
    if fit == "fit_blur":
        return (
            f"[{label_in}]split=2[{tag}a][{tag}b];"
            f"[{tag}a]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},boxblur=24:2,eq=brightness=-0.08[{tag}bg];"
            f"[{tag}b]scale={tw}:{th}:force_original_aspect_ratio=decrease[{tag}fg];"
            f"[{tag}bg][{tag}fg]overlay=(W-w)/2:(H-h)/2[{label_out}]"
        )
    return f"[{label_in}]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th}[{label_out}]"


def effective_mode(layout: dict) -> str:
    mode = layout.get("mode", "center")
    if mode in ("facecam_box", "split_facecam_bottom") and not layout.get("facecam"):
        return "center"
    return mode if mode in ("center", "facecam_box", "split_facecam_bottom") else "center"


def layout_chain(src: str, out: str, layout: dict, sw: int, sh: int, tag: str) -> str:
    """Filter dari stream video sumber [src] ke kanvas 1080x1920 [out]."""
    return _layout_chain(src, f"{tag}fin", layout, sw, sh, tag) + f";[{tag}fin]setsar=1[{out}]"


def _layout_chain(src: str, out: str, layout: dict, sw: int, sh: int, tag: str) -> str:
    mode = effective_mode(layout)
    fit = layout.get("content_fit", "cover")
    cw, ch, cx, cy = region_px(layout.get("content"), sw, sh)
    pre = f"[{src}]fps=30,setsar=1"
    if mode == "center":
        return f"{pre},crop={cw}:{ch}:{cx}:{cy}[{tag}c];" + _fill(f"{tag}c", out, W, H, fit, tag + "f")
    fw, fh, fx, fy = region_px(layout.get("facecam"), sw, sh)
    chain = f"{pre},split=2[{tag}s1][{tag}s2];[{tag}s1]crop={cw}:{ch}:{cx}:{cy}[{tag}c];[{tag}s2]crop={fw}:{fh}:{fx}:{fy}[{tag}fc0];"
    if mode == "split_facecam_bottom":
        chain += _fill(f"{tag}c", f"{tag}top", W, SPLIT_TOP, fit, tag + "t") + ";"
        chain += f"[{tag}fc0]scale={W}:{H - SPLIT_TOP}:force_original_aspect_ratio=increase,crop={W}:{H - SPLIT_TOP}[{tag}bot];"
        chain += f"[{tag}top][{tag}bot]vstack=inputs=2[{out}]"
        return chain
    # facecam_box
    box_w = _even(int(layout.get("facecam_width", 380)))
    box_h = _even(box_w * fh / max(1, fw))
    border = 6
    pos = layout.get("facecam_pos", "top_center")
    total_w = box_w + 2 * border
    xs = {"top_center": (W - total_w) // 2, "top_left": 40, "top_right": W - total_w - 40, "middle_left": 40, "middle_right": W - total_w - 40}
    ys = {"top_center": TOP_Y, "top_left": TOP_Y, "top_right": TOP_Y, "middle_left": 820, "middle_right": 820}
    x, y = xs.get(pos, xs["top_center"]), ys.get(pos, TOP_Y)
    chain += _fill(f"{tag}c", f"{tag}bg", W, H, fit, tag + "f") + ";"
    chain += f"[{tag}fc0]scale={box_w}:{box_h},pad=iw+{2 * border}:ih+{2 * border}:{border}:{border}:white[{tag}fc];"
    chain += f"[{tag}bg][{tag}fc]overlay={x}:{y}[{out}]"
    return chain


OVERLAY_POS = {
    "top_left": ("40", str(TOP_Y)),
    "top_right": ("W-w-40", str(TOP_Y)),
    "top_center": ("(W-w)/2", str(TOP_Y)),
    "middle_left": ("40", "(H-h)/2"),
    "middle_right": ("W-w-40", "(H-h)/2"),
}


def overlay_inputs(campaign: dict) -> list[tuple[Path, dict]]:
    out = []
    for kind in ("watermark", "logo"):
        cfg = campaign.get(kind) or {}
        p = asset_path(campaign, kind)
        if p and cfg.get("position", "none") != "none":
            out.append((p, cfg))
    return out


def asset_ready(campaign: dict) -> dict:
    return {
        "watermark": bool(asset_path(campaign, "watermark")) and campaign["watermark"].get("position") != "none",
        "logo": bool(asset_path(campaign, "logo")) and campaign["logo"].get("position") != "none",
        "asset": bool(asset_path(campaign, "asset")) and campaign["asset"].get("insert_at", "none") != "none",
    }


def normalized_asset(campaign: dict) -> tuple[Path | None, float]:
    """Asset video wajib diubah sekali ke 1080x1920 30fps, lalu disimpan sebagai cache."""
    src = asset_path(campaign, "asset")
    cfg = campaign.get("asset") or {}
    if not src or cfg.get("insert_at", "none") == "none":
        return None, 0.0
    cdir = campaign_dir(campaign["id"])
    out = cdir / "asset_norm.mp4"
    meta_file = cdir / "asset_norm.json"
    key = {"src": src.name, "mtime": src.stat().st_mtime, "ts": float(cfg.get("trim_start") or 0), "te": float(cfg.get("trim_end") or 0)}
    meta = read_json(meta_file, {}) or {}
    if out.exists() and meta.get("key") == key:
        return out, float(meta["duration"])
    info = probe(src)
    args: list[str] = []
    if key["ts"] > 0:
        args += ["-ss", f"{key['ts']:.2f}"]
    if key["te"] > key["ts"]:
        args += ["-t", f"{key['te'] - key['ts']:.2f}"]
    args += ["-i", str(src)]
    graph = "[0:v]fps=30,setsar=1[v0];" + _fill("v0", "vfit", W, H, "fit_blur", "as") + ";[vfit]setsar=1[vout]"
    if info["has_audio"]:
        graph += ";[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[aout]"
        amap = ["-map", "[aout]"]
    else:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        amap = ["-map", "1:a", "-shortest"]
    args += ["-filter_complex", graph, "-map", "[vout]", *amap, *video_encoder_args(), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(out)]
    run_ffmpeg(args)
    duration = probe(out)["duration"]
    write_json(meta_file, {"key": key, "duration": duration})
    return out, duration


def hook_length(sentences: list[dict], start: float, end: float) -> float:
    for s in sentences:
        if s["e"] > start + 0.3 and s["s"] < end:
            length = s["e"] - start
            return length if 1.5 <= length <= 6.0 else 3.0
    return 3.0


def build_graph(inputs_video: list[str], campaign: dict, layout: dict, sw: int, sh: int,
                asset_idx: int | None, overlay_start_idx: int, overlays: list, ass_file: str | None,
                with_audio: bool) -> tuple[str, str, str | None]:
    """Kembalikan (filter_complex, label video akhir, label audio akhir)."""
    parts = []
    seq_v, seq_a = [], []
    for i, inp in enumerate(inputs_video):
        parts.append(layout_chain(f"{inp}:v", f"L{i}", layout, sw, sh, f"p{i}"))
        if with_audio:
            parts.append(f"[{inp}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[A{i}]")
    insert_at = (campaign.get("asset") or {}).get("insert_at", "none") if asset_idx is not None else "none"
    labels = [(f"L{i}", f"A{i}") for i in range(len(inputs_video))]
    if asset_idx is not None:
        asset_pair = (f"{asset_idx}:v", f"{asset_idx}:a")
        if insert_at == "start":
            order = [asset_pair] + labels
        elif insert_at == "end":
            order = labels + [asset_pair]
        else:  # after_hook: dua potongan sumber, asset di tengah
            order = [labels[0], asset_pair] + labels[1:]
        seq_v = [f"[{v}]" for v, _ in order]
        seq_a = [f"[{a}]" for _, a in order]
        concat_in = "".join(v + a for v, a in zip(seq_v, seq_a))
        parts.append(f"{concat_in}concat=n={len(order)}:v=1:a=1[cv][ca]")
        vlabel, alabel = "cv", "ca"
    else:
        vlabel = "L0"
        alabel = "A0" if with_audio else None
    for k, (_path, cfg) in enumerate(overlays):
        idx = overlay_start_idx + k
        x, y = OVERLAY_POS.get(cfg.get("position", "top_left"), OVERLAY_POS["top_left"])
        width = _even(int(cfg.get("width", 260)))
        parts.append(f"[{idx}:v]scale={width}:-2,format=rgba[ov{k}]")
        parts.append(f"[{vlabel}][ov{k}]overlay=x={x}:y={y}:format=auto[o{k}]")
        vlabel = f"o{k}"
    if ass_file:
        parts.append(f"[{vlabel}]ass={ass_file}[vs]")
        vlabel = "vs"
    parts.append(f"[{vlabel}]format=yuv420p[vout]")
    return ";".join(parts), "vout", alabel


def render_clip(job: dict, jdir: Path, clip: dict, campaign: dict, transcript: dict, layout: dict,
                on_progress=None, should_cancel=None) -> dict:
    src = jdir / (job.get("video_file") or "")
    if not job.get("video_file") or not src.exists():
        raise PipelineError("File video sumber tidak ada (mungkin sudah dihapus). Jalankan ulang tahap unduh.")
    info = probe(src)
    sw, sh = info["width"], info["height"]
    start, end = float(clip["start"]), float(clip["end"])
    sentences = transcript.get("sentences", [])
    asset_file, asset_dur = normalized_asset(campaign)
    insert_at = (campaign.get("asset") or {}).get("insert_at", "none") if asset_file else "none"
    hook_len = hook_length(sentences, start, end)
    if insert_at == "after_hook" and end - start <= hook_len + 0.5:
        insert_at = "end"

    args: list[str] = []
    video_inputs: list[str] = []
    if insert_at == "after_hook":
        args += ["-ss", f"{start:.3f}", "-t", f"{hook_len:.3f}", "-i", str(src)]
        args += ["-ss", f"{start + hook_len:.3f}", "-t", f"{end - start - hook_len:.3f}", "-i", str(src)]
        video_inputs = ["0", "1"]
    else:
        args += ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src)]
        video_inputs = ["0"]
    asset_idx = None
    if asset_file:
        asset_idx = len(video_inputs)
        args += ["-i", str(asset_file)]
    overlays = overlay_inputs(campaign)
    ov_start = len(video_inputs) + (1 if asset_file else 0)
    for p, _cfg in overlays:
        args += ["-i", str(p)]

    timeline = Timeline(start, end, insert_at, hook_len, asset_dur)
    clips_dir = jdir / "clips"
    clips_dir.mkdir(exist_ok=True)
    ass_name = None
    warnings = []
    sub_cfg = campaign.get("subtitle") or {}
    if sub_cfg.get("enabled", True) or (sub_cfg.get("hook_overlay", True) and clip.get("hook_text")):
        if system_info().get("has_ass"):
            words = transcript.get("words", [])
            ass_text = build_ass(words, timeline, sub_cfg, effective_mode(layout), clip.get("hook_text", ""))
            ass_name = f"{clip['id']}.ass"
            (clips_dir / ass_name).write_text(ass_text, encoding="utf-8")
        else:
            warnings.append("Subtitle tidak dibuat: ffmpeg tanpa libass. Pasang ffmpeg-full (lihat README).")

    graph, vlabel, alabel = build_graph(video_inputs, campaign, layout, sw, sh, asset_idx if asset_file else None,
                                        ov_start, overlays, ass_name, with_audio=info["has_audio"])
    if asset_file and not info["has_audio"]:
        raise PipelineError("Video sumber tidak punya audio; tidak bisa digabung dengan asset.")
    out_name = f"{clip['id']}.mp4"
    out = clips_dir / out_name
    tmp = clips_dir / f"{clip['id']}.tmp.mp4"
    args += ["-filter_complex", graph, "-map", f"[{vlabel}]"]
    if alabel:
        args += ["-map", f"[{alabel}]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    args += [*video_encoder_args(), "-r", "30", "-movflags", "+faststart", tmp.name]
    run_ffmpeg(args, duration=timeline.total, on_progress=on_progress, should_cancel=should_cancel, cwd=clips_dir)
    tmp.replace(out)
    thumb = clips_dir / f"{clip['id']}.jpg"
    run_ffmpeg(["-ss", f"{min(1.5, timeline.total / 2):.2f}", "-i", str(out), "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", str(thumb)])
    return {
        "file": f"clips/{out_name}",
        "thumb": f"clips/{thumb.name}",
        "final_duration": round(timeline.total, 2),
        "asset_duration": round(asset_dur if insert_at != "none" else 0, 2),
        "asset_inserted": insert_at,
        "warnings": warnings,
    }


def render_preview(job: dict, jdir: Path, campaign: dict, layout: dict, at: float) -> Path:
    src = jdir / (job.get("video_file") or "")
    if not job.get("video_file") or not src.exists():
        raise PipelineError("Video sumber belum tersedia.")
    info = probe(src)
    overlays = overlay_inputs(campaign)
    args = ["-ss", f"{at:.2f}", "-i", str(src)]
    for p, _ in overlays:
        args += ["-i", str(p)]
    graph, vlabel, _ = build_graph(["0"], campaign, layout, info["width"], info["height"], None, 1, overlays, None, with_audio=False)
    out = jdir / "preview.jpg"
    args += ["-filter_complex", graph, "-map", f"[{vlabel}]", "-frames:v", "1", "-q:v", "3", str(out)]
    run_ffmpeg(args)
    return out
