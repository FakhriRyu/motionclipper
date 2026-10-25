"""Urutan tahap pipeline. Setiap tahap menyimpan hasilnya ke disk, jadi bisa dilanjutkan."""
from __future__ import annotations

import shutil
import traceback
from pathlib import Path

from .. import storage
from ..storage import STAGES, get_campaign, job_dir, job_log, read_json, update_job, write_json
from . import llm
from .clips import checklist, clip_text, compose_caption, validate
from .media import Cancelled, PipelineError, download_youtube, extract_audio, probe, snapshot, youtube_info
from .peaks import find_peaks
from .render import asset_ready, render_clip
from .transcribe import finalize_transcript, run_transcription

CANCELLED: set[str] = set()


def _cancel_check(jid: str):
    return lambda: jid in CANCELLED


def _progress(jid: str):
    def fn(frac: float):
        update_job(jid, progress=round(frac, 4))

    return fn


def _stage(jid: str, stage: str, status: str, msg: str = "") -> None:
    def apply(job):
        st = job["stages"].setdefault(stage, {})
        st["status"] = status
        if msg:
            st["msg"] = msg
        stamp = storage.now_iso()
        if status == "running":
            st["started"] = stamp
            job["stage"] = stage
            job["progress"] = 0.0
        elif status in ("done", "skipped", "error"):
            st["ended"] = stamp

    update_job(jid, apply)


def layout_for(job: dict, campaign: dict) -> dict:
    base = dict(campaign.get("layout") or {})
    if job.get("layout"):
        base.update(job["layout"])
    return base


def add_usage(jid: str, usage: dict) -> None:
    def apply(job):
        u = job.setdefault("usage", {})
        for k in ("prompt_hit", "prompt_miss", "completion"):
            u[k] = u.get(k, 0) + usage.get(k, 0)
        u["cost_usd"] = round(u.get("cost_usd", 0.0) + usage.get("cost_usd", 0.0), 6)

    update_job(jid, apply)


# ---------------------------------------------------------------- tahap


def stage_download(jid: str) -> None:
    job = storage.get_job(jid)
    jdir = job_dir(jid)
    if job.get("video_file") and (jdir / job["video_file"]).exists():
        return
    src = job["source"]
    if src["type"] == "youtube":
        job_log(jid, "Membaca info video YouTube…")
        info = youtube_info(src["url"])
        update_job(jid, title=info["title"] or job["title"])
        job_log(jid, f"Mengunduh: {info['title']}")
        path = download_youtube(src["url"], jdir, on_progress=_progress(jid), should_cancel=_cancel_check(jid), log=lambda m: job_log(jid, m))
        video_file = path.name
    else:
        path = Path(src["path"])
        if not path.exists():
            raise PipelineError(f"File tidak ditemukan: {path}")
        video_file = str(path) if not str(path).startswith(str(jdir)) else path.name
    info = probe(jdir / video_file)
    if not info["has_audio"]:
        raise PipelineError("Video tidak punya audio, jadi tidak bisa ditranskripsi.")
    update_job(jid, video_file=video_file, duration=info["duration"], source_size=[info["width"], info["height"]])
    at = min(max(5.0, info["duration"] * 0.25), max(0.0, info["duration"] - 1))
    snapshot(jdir / video_file, jdir / "snapshot.jpg", at)
    update_job(jid, snapshot_at=at)


def stage_audio(jid: str) -> None:
    job = storage.get_job(jid)
    jdir = job_dir(jid)
    out = jdir / "audio.wav"
    if out.exists():
        return
    tmp = jdir / "audio.tmp.wav"
    extract_audio(jdir / job["video_file"], tmp, job.get("duration") or 0, _progress(jid), _cancel_check(jid))
    tmp.replace(out)


def stage_transcribe(jid: str) -> None:
    jdir = job_dir(jid)
    out = jdir / "transcript.json"
    if out.exists():
        return
    raw = jdir / "words_raw.json"
    run_transcription(jdir / "audio.wav", raw, _progress(jid), _cancel_check(jid), log=lambda m: job_log(jid, m))
    data = finalize_transcript(raw, out)
    job_log(jid, f"Transkrip selesai: {len(data['words'])} kata, {len(data['sentences'])} kalimat.")


def stage_peaks(jid: str) -> None:
    jdir = job_dir(jid)
    out = jdir / "peaks.json"
    if out.exists():
        return
    peaks = find_peaks(jdir / "audio.wav")
    write_json(out, peaks)
    job_log(jid, f"Ditemukan {len(peaks)} lonjakan suara.")


def stage_select(jid: str) -> None:
    jdir = job_dir(jid)
    if storage.get_clips(jid):
        return
    job = storage.get_job(jid)
    campaign = get_campaign(job["campaign_id"])
    transcript = read_json(jdir / "transcript.json")
    peaks = read_json(jdir / "peaks.json", [])
    sentences = transcript["sentences"]
    count = int(campaign.get("num_clips") or 8)
    job_log(jid, f"Mengirim {len(sentences)} kalimat ke DeepSeek untuk memilih {count} momen…")
    raw, usage = llm.select_moments(campaign, sentences, peaks, count)
    add_usage(jid, usage)
    write_json(jdir / "selection_raw.json", raw)
    clips = validate(raw, sentences, campaign, job.get("duration") or 0)
    if not clips:
        raise PipelineError("DeepSeek tidak menemukan momen yang valid.")
    for c in clips:
        c.update({"title": "", "hook_text": "", "caption_body": "", "caption_full": "", "compliance": {"ok": True, "issues": []},
                  "render": {"status": "pending"}, "approved": False, "source": "ai"})
    storage.save_clips(jid, clips)
    job_log(jid, f"{len(clips)} clip dipilih (biaya ≈ ${usage.get('cost_usd', 0):.4f}).")


def metadata_for(jid: str, clip_ids: list[str] | None = None) -> None:
    jdir = job_dir(jid)
    job = storage.get_job(jid)
    campaign = get_campaign(job["campaign_id"])
    sentences = read_json(jdir / "transcript.json")["sentences"]
    clips = storage.get_clips(jid)
    todo = [c for c in clips if (clip_ids is None and not c.get("title")) or (clip_ids and c["id"] in clip_ids)]
    if not todo:
        return
    items = [{"id": c["id"], "text": clip_text(sentences, c["start"], c["end"])} for c in todo]
    meta, usage = llm.write_metadata(campaign, items)
    add_usage(jid, usage)

    def apply(all_clips):
        for c in all_clips:
            m = meta.get(c["id"])
            if not m:
                continue
            c["title"] = str(m.get("title") or "")[:80]
            c["hook_text"] = str(m.get("hook_text") or "")[:80]
            c["caption_body"] = str(m.get("caption") or "")
            c["caption_full"] = compose_caption(c["caption_body"], campaign)
            c["compliance"] = {"ok": bool(m.get("compliance_ok", True)), "issues": [str(x) for x in (m.get("issues") or [])][:5]}

    storage.update_clips(jid, apply)
    job_log(jid, f"Caption dibuat untuk {len(meta)} clip.")


def stage_metadata(jid: str) -> None:
    metadata_for(jid, None)


def render_clips(jid: str, clip_ids: list[str] | None = None, only_pending: bool = False) -> None:
    jdir = job_dir(jid)
    job = storage.get_job(jid)
    campaign = get_campaign(job["campaign_id"])
    transcript = read_json(jdir / "transcript.json")
    layout = layout_for(job, campaign)
    assets = asset_ready(campaign)
    clips = storage.get_clips(jid)
    targets = [c for c in clips if (clip_ids is None or c["id"] in clip_ids)]
    if only_pending:
        targets = [c for c in targets if c.get("render", {}).get("status") != "done" or not (jdir / c["render"].get("file", "x")).exists()]
    for n, clip in enumerate(targets, 1):
        if jid in CANCELLED:
            raise Cancelled()
        storage.update_clip(jid, clip["id"], render={"status": "rendering", "progress": 0})
        job_log(jid, f"Render {clip['id']} ({n}/{len(targets)})…")

        def prog(frac, cid=clip["id"], n=n):
            storage.update_clip(jid, cid, render={"status": "rendering", "progress": round(frac, 3)})
            update_job(jid, progress=round(((n - 1) + frac) / len(targets), 4))

        try:
            result = render_clip(job, jdir, storage.get_clips_by_id(jid, clip["id"]), campaign, transcript, layout, prog, _cancel_check(jid))
            result["status"] = "done"
            result["rendered_at"] = storage.now_iso()
            fresh = storage.get_clips_by_id(jid, clip["id"]) or clip
            fresh.update({"final_duration": result["final_duration"], "asset_duration": result["asset_duration"]})
            storage.update_clip(jid, clip["id"], render=result, checks=checklist(fresh, campaign, assets),
                                final_duration=result["final_duration"], asset_duration=result["asset_duration"])
        except Cancelled:
            storage.update_clip(jid, clip["id"], render={"status": "pending"})
            raise
        except Exception as e:  # noqa: BLE001
            storage.update_clip(jid, clip["id"], render={"status": "error", "error": str(e)[-1500:]})
            job_log(jid, f"Render {clip['id']} gagal: {str(e).splitlines()[0][:200]}")


def stage_render(jid: str) -> None:
    render_clips(jid, None, only_pending=True)


STAGE_FUNCS = {
    "download": stage_download,
    "audio": stage_audio,
    "transcribe": stage_transcribe,
    "peaks": stage_peaks,
    "select": stage_select,
    "metadata": stage_metadata,
    "render": stage_render,
}


def reset_from(jid: str, stage: str) -> None:
    """Hapus hasil tahap `stage` dan setelahnya, supaya dijalankan ulang."""
    jdir = job_dir(jid)
    job = storage.get_job(jid)
    idx = STAGES.index(stage)
    later = STAGES[idx:]
    if "download" in later and job.get("video_file") and not Path(job["video_file"]).is_absolute():
        (jdir / job["video_file"]).unlink(missing_ok=True)
        update_job(jid, video_file=None)
    if "audio" in later:
        (jdir / "audio.wav").unlink(missing_ok=True)
    if "transcribe" in later:
        (jdir / "transcript.json").unlink(missing_ok=True)
        (jdir / "words_raw.json").unlink(missing_ok=True)
    if "peaks" in later:
        (jdir / "peaks.json").unlink(missing_ok=True)
    if "select" in later:
        (jdir / "clips.json").unlink(missing_ok=True)
        shutil.rmtree(jdir / "clips", ignore_errors=True)
    elif "metadata" in later:
        def clear(clips):
            for c in clips:
                c["title"] = ""
        storage.update_clips(jid, clear)

    def apply(j):
        for s in later:
            j["stages"][s] = {"status": "pending"}

    update_job(jid, apply)


def run_pipeline(jid: str) -> None:
    CANCELLED.discard(jid)
    update_job(jid, status="running", error=None)
    try:
        for stage in STAGES:
            job = storage.get_job(jid)
            if job["stages"].get(stage, {}).get("status") == "done":
                continue
            _stage(jid, stage, "running")
            STAGE_FUNCS[stage](jid)
            _stage(jid, stage, "done")
        update_job(jid, status="done", stage=None, progress=1.0)
        job_log(jid, "Selesai.")
    except Cancelled:
        job = storage.get_job(jid)
        if job.get("stage"):
            _stage(jid, job["stage"], "pending")
        update_job(jid, status="cancelled")
        job_log(jid, "Dibatalkan.")
    except Exception as e:  # noqa: BLE001
        job = storage.get_job(jid)
        if job.get("stage"):
            _stage(jid, job["stage"], "error", str(e)[:300])
        update_job(jid, status="error", error=str(e)[-2000:])
        job_log(jid, "Gagal: " + str(e).splitlines()[0][:300] if str(e) else "Gagal")
        traceback.print_exc()
    finally:
        CANCELLED.discard(jid)
