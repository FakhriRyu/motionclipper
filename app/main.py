"""Web app localhost MotionClipper KL. Jalankan dengan ./run.sh lalu buka http://localhost:8000"""
from __future__ import annotations

import io
import platform
import re
import shutil
import subprocess
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import storage, worker
from .config import DATA, ENV_FILE, IS_MAC_ARM, reload_settings, save_env, settings, system_info
from .pipeline import llm
from .pipeline.clips import checklist, compose_caption, times_for
from .pipeline.render import asset_ready, render_preview
from .pipeline.run import layout_for, reset_from
from .storage import STAGE_LABELS, STAGES, campaign_dir, get_campaign, job_dir, read_json, write_json

APP_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app):
    storage.seed_example_campaign()
    worker.start()
    yield


app = FastAPI(title="MotionClipper KL", lifespan=lifespan)
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
storage.ensure_dirs()
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
app.mount("/files", StaticFiles(directory=str(DATA)), name="files")


def fmt_time(t: float | None) -> str:
    if t is None:
        return "–"
    t = float(t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    frac = t - int(t)
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return base + (f".{int(frac * 10)}" if frac >= 0.05 else "")


def rupiah(usd: float) -> str:
    return "Rp" + f"{usd * settings.usd_to_idr:,.0f}".replace(",", ".")


templates.env.filters["tc"] = fmt_time
templates.env.filters["rupiah"] = rupiah
templates.env.globals.update(STAGES=STAGES, STAGE_LABELS=STAGE_LABELS, IS_MAC=IS_MAC_ARM, STATUS_TEXT={
    "queued": "Menunggu antrian", "running": "Berjalan", "done": "Selesai", "error": "Gagal", "cancelled": "Dibatalkan"})


def _lines(text: str | None) -> list[str]:
    return [l.strip() for l in (text or "").splitlines() if l.strip()]


def _list_sep(text: str | None) -> list[str]:
    return [p.strip() for p in re.split(r"[,\n]", text or "") if p.strip()]


def _num(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _job_or_404(jid: str) -> dict:
    job = storage.get_job(jid)
    if not job:
        raise HTTPException(404, "Job tidak ditemukan")
    return job


def _campaign_or_404(cid: str) -> dict:
    c = get_campaign(cid)
    if not c:
        raise HTTPException(404, "Campaign tidak ditemukan")
    return c


# ---------------------------------------------------------------- halaman


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    jobs = storage.list_jobs()
    campaigns = storage.list_campaigns()
    counts = {}
    for j in jobs:
        counts[j["campaign_id"]] = counts.get(j["campaign_id"], 0) + 1
    names = {c["id"]: c["name"] for c in campaigns}
    for j in jobs:
        j["clip_count"] = len(storage.get_clips(j["id"]))
    return templates.TemplateResponse(request, "index.html", {
        "jobs": jobs, "campaigns": campaigns, "counts": counts, "names": names,
        "sys": system_info(), "assets": {c["id"]: asset_ready(c) for c in campaigns},
    })


@app.post("/campaigns")
async def create_campaign(request: Request):
    form = await request.form()
    name = (form.get("name") or "Campaign baru").strip()
    cid = storage.new_campaign_id(name)
    data = dict(storage.DEFAULT_CAMPAIGN)
    data["name"] = name
    storage.save_campaign(cid, data)
    return RedirectResponse(f"/campaigns/{cid}", status_code=303)


@app.get("/campaigns/{cid}", response_class=HTMLResponse)
def campaign_page(request: Request, cid: str, saved: int = 0):
    c = _campaign_or_404(cid)
    return templates.TemplateResponse(request, "campaign.html", {
        "c": c, "saved": saved, "assets": asset_ready(c),
        "files": {k: (c.get(k) or {}).get("file") and storage.asset_path(c, k) is not None for k in ("watermark", "logo", "asset")},
    })


async def _save_upload(upload, dest_dir: Path, stem: str, allowed: set[str]) -> str | None:
    if not upload or not getattr(upload, "filename", ""):
        return None
    ext = Path(upload.filename).suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, f"Format {ext} tidak didukung untuk {stem}. Pakai: {', '.join(sorted(allowed))}")
    for old in dest_dir.glob(stem + ".*"):
        old.unlink()
    name = stem + ext
    with open(dest_dir / name, "wb") as f:
        shutil.copyfileobj(upload.file, f, length=1024 * 1024)
    return name


@app.post("/campaigns/{cid}")
async def save_campaign(request: Request, cid: str):
    c = _campaign_or_404(cid)
    form = await request.form()
    cdir = campaign_dir(cid)
    c["name"] = (form.get("name") or c["name"]).strip()
    c["eligible_from"] = form.get("eligible_from") or ""
    c["platforms"] = form.getlist("platforms")
    c["sources"] = _lines(form.get("sources"))
    c["content_hint"] = (form.get("content_hint") or "").strip()
    c["moment_keywords"] = _list_sep(form.get("moment_keywords"))
    c["banned"] = _lines(form.get("banned"))
    c["caption_required"] = _lines(form.get("caption_required"))
    c["hashtags_ordered"] = [t if t.startswith("#") else "#" + t for t in re.split(r"[\s,]+", form.get("hashtags_ordered") or "") if t]
    c["duration_min"] = int(_num(form.get("duration_min"), 20))
    c["duration_max"] = max(c["duration_min"] + 5, int(_num(form.get("duration_max"), 60)))
    c["num_clips"] = max(1, min(30, int(_num(form.get("num_clips"), 8))))
    c["notes"] = (form.get("notes") or "").strip()
    images = {".png", ".jpg", ".jpeg", ".webp"}
    for kind in ("watermark", "logo"):
        cfg = c[kind]
        cfg["position"] = form.get(f"{kind}_position") or cfg["position"]
        cfg["width"] = int(_num(form.get(f"{kind}_width"), cfg["width"]))
        saved = await _save_upload(form.get(f"{kind}_file"), cdir, kind, images)
        if saved:
            cfg["file"] = saved
    a = c["asset"]
    a["insert_at"] = form.get("asset_insert_at") or a["insert_at"]
    a["trim_start"] = _num(form.get("asset_trim_start"), 0.0)
    a["trim_end"] = _num(form.get("asset_trim_end"), 0.0)
    saved = await _save_upload(form.get("asset_file"), cdir, "asset", {".mp4", ".mov", ".mkv", ".webm", ".m4v"})
    if saved:
        a["file"] = saved
    lay = c["layout"]
    lay["mode"] = form.get("layout_mode") or lay["mode"]
    lay["content_fit"] = form.get("layout_content_fit") or lay["content_fit"]
    lay["facecam_pos"] = form.get("layout_facecam_pos") or lay["facecam_pos"]
    lay["facecam_width"] = int(_num(form.get("layout_facecam_width"), lay["facecam_width"]))
    s = c["subtitle"]
    s["enabled"] = form.get("sub_enabled") == "on"
    s["hook_overlay"] = form.get("sub_hook_overlay") == "on"
    s["uppercase"] = form.get("sub_uppercase") == "on"
    s["words_per_line"] = max(1, min(6, int(_num(form.get("sub_words_per_line"), 3))))
    s["font"] = (form.get("sub_font") or "Arial Black").strip()
    s["size"] = max(30, min(140, int(_num(form.get("sub_size"), 76))))
    s["color"] = form.get("sub_color") or s["color"]
    s["highlight"] = form.get("sub_highlight") or s["highlight"]
    storage.save_campaign(cid, c)
    # Perbarui caption & checklist clip yang sudah ada agar ikut aturan terbaru.
    fresh = get_campaign(cid)
    ready = asset_ready(fresh)
    for job in storage.list_jobs():
        if job["campaign_id"] != cid:
            continue

        def refresh(clips):
            for cl in clips:
                if cl.get("title"):
                    cl["caption_full"] = compose_caption(cl.get("caption_body", ""), fresh)
                if cl.get("render", {}).get("status") == "done":
                    cl["checks"] = checklist(cl, fresh, ready)

        storage.update_clips(job["id"], refresh)
    return RedirectResponse(f"/campaigns/{cid}?saved=1", status_code=303)


@app.post("/campaigns/{cid}/remove/{kind}")
def remove_asset(cid: str, kind: str):
    c = _campaign_or_404(cid)
    if kind not in ("watermark", "logo", "asset"):
        raise HTTPException(400)
    p = storage.asset_path(c, kind)
    if p:
        p.unlink(missing_ok=True)
    c[kind]["file"] = ""
    storage.save_campaign(cid, c)
    return RedirectResponse(f"/campaigns/{cid}?saved=1", status_code=303)


@app.get("/jobs/new", response_class=HTMLResponse)
def new_job_page(request: Request, campaign: str = "", url: str = ""):
    return templates.TemplateResponse(request, "job_new.html", {
        "campaigns": storage.list_campaigns(), "selected": campaign, "url": url,
        "inbox": storage.list_inbox(), "inbox_dir": str(storage.INBOX), "sys": system_info(),
    })


@app.post("/jobs")
async def create_job(request: Request):
    form = await request.form()
    cid = form.get("campaign_id") or ""
    _campaign_or_404(cid)
    created = []
    for url in _lines(form.get("urls")):
        if not re.match(r"^https?://", url):
            raise HTTPException(400, f"Link tidak valid: {url}")
        job = storage.new_job(cid, {"type": "youtube", "url": url})
        created.append(job["id"])
    inbox_file = form.get("inbox_file")
    if inbox_file:
        path = storage.INBOX / Path(inbox_file).name
        job = storage.new_job(cid, {"type": "file", "path": str(path)}, title=path.stem)
        created.append(job["id"])
    upload = form.get("upload")
    if upload is not None and getattr(upload, "filename", ""):
        ext = Path(upload.filename).suffix.lower() or ".mp4"
        job = storage.new_job(cid, {"type": "file", "path": ""}, title=Path(upload.filename).stem)
        dest = job_dir(job["id"]) / f"source{ext}"
        with open(dest, "wb") as f:
            shutil.copyfileobj(upload.file, f, length=4 * 1024 * 1024)
        storage.update_job(job["id"], source={"type": "file", "path": str(dest)})
        created.append(job["id"])
    if not created:
        raise HTTPException(400, "Isi link YouTube, pilih file dari folder inbox, atau unggah file video.")
    for jid in created:
        worker.submit({"kind": "pipeline", "job": jid})
    return RedirectResponse(f"/jobs/{created[0]}" if len(created) == 1 else "/", status_code=303)


@app.get("/jobs/{jid}", response_class=HTMLResponse)
def job_page(request: Request, jid: str):
    job = _job_or_404(jid)
    campaign = get_campaign(job["campaign_id"]) or {}
    return templates.TemplateResponse(request, "job.html", {
        "job": job, "c": campaign, "layout": layout_for(job, campaign) if campaign else {},
    })


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0):
    return templates.TemplateResponse(request, "settings.html", {
        "s": settings, "sys": system_info(), "saved": saved, "env_path": str(ENV_FILE),
    })


@app.post("/settings")
async def save_settings(request: Request):
    form = await request.form()
    updates = {}
    key = (form.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        updates["DEEPSEEK_API_KEY"] = key
    for k in ("DEEPSEEK_MODEL", "WHISPER_MODEL", "WHISPER_LANGUAGE", "CHUNK_MINUTES", "SOURCE_MAX_HEIGHT", "FFMPEG_BIN", "FFPROBE_BIN"):
        v = (form.get(k) or "").strip()
        if v or k in ("FFMPEG_BIN", "FFPROBE_BIN"):
            updates[k] = v
    save_env(updates)
    return RedirectResponse("/settings?saved=1", status_code=303)


# ---------------------------------------------------------------- API


def _job_summary(job: dict) -> dict:
    return {k: job.get(k) for k in ("id", "title", "status", "stage", "progress", "error", "campaign_id", "usage", "created")} | {
        "clip_count": len(storage.get_clips(job["id"])),
        "queue_pos": (worker.queued_jobs().index(job["id"]) + 1) if job["id"] in worker.queued_jobs() else None,
    }


@app.get("/api/jobs")
def api_jobs():
    return [_job_summary(j) for j in storage.list_jobs()]


@app.get("/api/jobs/{jid}")
def api_job(jid: str):
    job = _job_or_404(jid)
    clips = storage.get_clips(jid)
    jdir = job_dir(jid)
    peaks = read_json(jdir / "peaks.json", []) or []
    return {
        "job": job | {"queue_pos": _job_summary(job)["queue_pos"]},
        "clips": clips,
        "peaks": [{"t": p["t"], "rise": p["rise"]} for p in peaks],
        "has_snapshot": (jdir / "snapshot.jpg").exists(),
        "has_transcript": (jdir / "transcript.json").exists(),
        "cost_idr": rupiah((job.get("usage") or {}).get("cost_usd", 0)),
    }


@app.post("/api/jobs/{jid}/cancel")
def api_cancel(jid: str):
    _job_or_404(jid)
    worker.cancel(jid)
    return {"ok": True}


@app.post("/api/jobs/{jid}/retry")
def api_retry(jid: str):
    _job_or_404(jid)
    worker.submit({"kind": "pipeline", "job": jid})
    return {"ok": True}


@app.post("/api/jobs/{jid}/rerun/{stage}")
def api_rerun(jid: str, stage: str):
    job = _job_or_404(jid)
    if stage not in STAGES:
        raise HTTPException(400)
    if job["status"] in ("running", "queued"):
        raise HTTPException(409, "Job masih berjalan. Batalkan dulu.")
    reset_from(jid, stage)
    worker.submit({"kind": "pipeline", "job": jid})
    return {"ok": True}


@app.post("/api/jobs/{jid}/layout")
async def api_layout(jid: str, request: Request):
    job = _job_or_404(jid)
    body = await request.json()
    layout = _clean_layout(body.get("layout") or {})
    if body.get("save_default"):
        c = _campaign_or_404(job["campaign_id"])
        c["layout"].update(layout)
        storage.save_campaign(c["id"], c)
        storage.update_job(jid, layout=None)
    else:
        storage.update_job(jid, layout=layout)
    return {"ok": True}


def _clean_layout(raw: dict) -> dict:
    def region(r):
        if not r:
            return None
        return {k: round(max(0.0, min(1.0, float(r.get(k, 0)))), 4) for k in ("x", "y", "w", "h")}

    out = {}
    if raw.get("mode") in ("center", "facecam_box", "split_facecam_bottom"):
        out["mode"] = raw["mode"]
    if raw.get("content_fit") in ("cover", "fit_blur"):
        out["content_fit"] = raw["content_fit"]
    if raw.get("facecam_pos"):
        out["facecam_pos"] = str(raw["facecam_pos"])
    if raw.get("facecam_width"):
        out["facecam_width"] = int(_num(raw["facecam_width"], 380))
    if "content" in raw:
        out["content"] = region(raw["content"])
    if "facecam" in raw:
        out["facecam"] = region(raw["facecam"])
    return out


@app.post("/api/jobs/{jid}/preview")
async def api_preview(jid: str, request: Request):
    job = _job_or_404(jid)
    body = await request.json()
    campaign = _campaign_or_404(job["campaign_id"])
    layout = layout_for(job, campaign)
    layout.update(_clean_layout(body.get("layout") or {}))
    at = float(body.get("at") or job.get("snapshot_at") or 10)
    try:
        render_preview(job, job_dir(jid), campaign, layout, at)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[-600:]}, status_code=400)
    return {"ok": True, "url": f"/files/jobs/{jid}/preview.jpg?t={int(time.time() * 1000)}"}


@app.post("/api/jobs/{jid}/render")
async def api_render(jid: str, request: Request):
    _job_or_404(jid)
    body = await request.json()
    clip_ids = body.get("clips")

    def mark(clips):
        for c in clips:
            if clip_ids is None or c["id"] in clip_ids:
                c["render"] = {"status": "queued"}

    storage.update_clips(jid, mark)
    worker.submit({"kind": "render", "job": jid, "clips": clip_ids})
    return {"ok": True}


@app.post("/api/jobs/{jid}/clips/{clip_id}")
async def api_update_clip(jid: str, clip_id: str, request: Request):
    job = _job_or_404(jid)
    campaign = _campaign_or_404(job["campaign_id"])
    body = await request.json()
    clip = storage.get_clips_by_id(jid, clip_id)
    if not clip:
        raise HTTPException(404)
    fields: dict = {}
    if "start" in body or "end" in body:
        start = max(0.0, _num(body.get("start"), clip["start"]))
        end = _num(body.get("end"), clip["end"])
        if job.get("duration"):
            end = min(end, job["duration"])
        if end - start < 1:
            raise HTTPException(400, "Durasi clip minimal 1 detik.")
        if abs(start - clip["start"]) > 0.05 or abs(end - clip["end"]) > 0.05:
            fields.update(start=round(start, 2), end=round(end, 2), render={**clip.get("render", {}), "stale": True})
    for k in ("title", "hook_text", "caption_body"):
        if k in body:
            fields[k] = str(body[k])
    if "hook_text" in body and body["hook_text"] != clip.get("hook_text"):
        fields["render"] = {**(fields.get("render") or clip.get("render", {})), "stale": True}
    if "caption_body" in body:
        fields["caption_full"] = compose_caption(fields["caption_body"], campaign)
    if "approved" in body:
        fields["approved"] = bool(body["approved"])
    updated = storage.update_clip(jid, clip_id, **fields)
    if updated and updated.get("render", {}).get("status") == "done":
        storage.update_clip(jid, clip_id, checks=checklist(updated, campaign, asset_ready(campaign)))
    return storage.get_clips_by_id(jid, clip_id)


@app.post("/api/jobs/{jid}/clips")
async def api_add_clip(jid: str, request: Request):
    job = _job_or_404(jid)
    body = await request.json()
    transcript = read_json(job_dir(jid) / "transcript.json")
    if not transcript:
        raise HTTPException(400, "Transkrip belum selesai.")
    start, end = _num(body.get("start"), -1), _num(body.get("end"), -1)
    if start < 0 or end - start < 3:
        raise HTTPException(400, "Isi waktu mulai dan selesai (minimal 3 detik).")
    clips = storage.get_clips(jid)
    n = 1 + max([int(c["id"][1:]) for c in clips if c["id"][1:].isdigit()] or [0])
    cid = f"c{n:02d}"
    clip = {
        "id": cid, "start": round(start, 2), "end": round(min(end, job.get("duration") or end), 2), "score": 0,
        "hook_line": "", "reason": "Ditambahkan manual", "category": "manual", "title": "", "hook_text": "",
        "caption_body": "", "caption_full": compose_caption("", _campaign_or_404(job["campaign_id"])),
        "compliance": {"ok": True, "issues": []}, "render": {"status": "queued"}, "approved": False, "source": "manual",
    }
    storage.update_clips(jid, lambda cl: cl.append(clip))
    kind = "meta_render" if (settings.deepseek_api_key or settings.mock_llm) else "render"
    worker.submit({"kind": kind, "job": jid, "clips": [cid]})
    return clip


@app.delete("/api/jobs/{jid}/clips/{clip_id}")
def api_delete_clip(jid: str, clip_id: str):
    _job_or_404(jid)
    storage.update_clips(jid, lambda cl: cl.__setitem__(slice(None), [c for c in cl if c["id"] != clip_id]))
    for p in (job_dir(jid) / "clips").glob(f"{clip_id}.*"):
        p.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/jobs/{jid}/clips/{clip_id}/sentences")
def api_clip_sentences(jid: str, clip_id: str):
    clip = storage.get_clips_by_id(jid, clip_id)
    transcript = read_json(job_dir(jid) / "transcript.json")
    if not clip or not transcript:
        raise HTTPException(404)
    out = []
    for i, s in enumerate(transcript["sentences"]):
        if s["e"] > clip["start"] and s["s"] < clip["end"]:
            out.append({"i": i, "s": s["s"], "t": s["t"]})
    return out


@app.post("/api/jobs/{jid}/sentences")
async def api_edit_sentences(jid: str, request: Request):
    """Koreksi teks subtitle. Waktu kata dipertahankan; jika jumlah kata berubah, waktu dibagi rata."""
    body = await request.json()
    path = job_dir(jid) / "transcript.json"
    data = read_json(path)
    if not data:
        raise HTTPException(404)
    words, sentences = data["words"], data["sentences"]
    edits = {int(e["i"]): str(e["text"]).strip() for e in body.get("edits", []) if str(e.get("text", "")).strip()}
    for i in sorted(edits, reverse=True):
        if i >= len(sentences):
            continue
        s = sentences[i]
        old = words[s["w0"] : s["w1"] + 1]
        tokens = edits[i].split()
        if [w["w"] for w in old] == tokens:
            continue
        if len(tokens) == len(old):
            new = [dict(w, w=t) for w, t in zip(old, tokens)]
        else:
            span = (s["e"] - s["s"]) / len(tokens)
            new = [{"w": t, "s": round(s["s"] + k * span, 3), "e": round(s["s"] + (k + 1) * span, 3), "p": 1.0} for k, t in enumerate(tokens)]
        delta = len(new) - len(old)
        words[s["w0"] : s["w1"] + 1] = new
        s["w1"] += delta
        s["t"] = " ".join(tokens)
        for later in sentences[i + 1 :]:
            later["w0"] += delta
            later["w1"] += delta
    write_json(path, data)
    clip_id = body.get("clip_id")
    if clip_id:
        clip = storage.get_clips_by_id(jid, clip_id)
        if clip:
            storage.update_clip(jid, clip_id, render={**clip.get("render", {}), "stale": True})
    return {"ok": True}


@app.post("/api/jobs/{jid}/open-folder")
def api_open_folder(jid: str):
    folder = job_dir(jid) / "clips"
    folder.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        subprocess.Popen(["open", str(folder)])
        return {"ok": True}
    return {"ok": False, "path": str(folder)}


@app.get("/jobs/{jid}/export.zip")
def export_zip(jid: str, all: int = 0):
    job = _job_or_404(jid)
    clips = [c for c in storage.get_clips(jid) if c.get("render", {}).get("status") == "done" and (all or c.get("approved"))]
    if not clips:
        raise HTTPException(400, "Belum ada clip yang disetujui dan selesai dirender.")
    buf = io.BytesIO()
    captions = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for c in clips:
            f = job_dir(jid) / c["render"]["file"]
            if f.exists():
                z.write(f, f"{c['id']}.mp4")
            captions.append(f"=== {c['id']} · {c.get('title', '')}\n{c.get('caption_full', '')}\n")
        z.writestr("captions.txt", "\n".join(captions))
    buf.seek(0)
    safe = re.sub(r"[^A-Za-z0-9]+", "-", job.get("title", jid))[:40].strip("-") or jid
    return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'})


@app.post("/api/jobs/{jid}/delete-source")
def api_delete_source(jid: str):
    job = _job_or_404(jid)
    if job["status"] in ("running", "queued"):
        raise HTTPException(409, "Job masih berjalan.")
    jdir = job_dir(jid)
    freed = 0
    vf = job.get("video_file")
    if vf and not Path(vf).is_absolute() and (jdir / vf).exists():
        freed += (jdir / vf).stat().st_size
        (jdir / vf).unlink()
        storage.update_job(jid, video_file=None, source_deleted=True)
    if (jdir / "audio.wav").exists():
        freed += (jdir / "audio.wav").stat().st_size
        (jdir / "audio.wav").unlink()

    def apply(j):
        j["stages"]["download"] = {"status": "pending"}
        j["stages"]["audio"] = {"status": "pending"}

    storage.update_job(jid, apply)
    return {"ok": True, "freed_mb": round(freed / 1e6)}


@app.post("/api/jobs/{jid}/delete")
def api_delete_job(jid: str):
    job = _job_or_404(jid)
    if job["status"] == "running":
        raise HTTPException(409, "Batalkan job dulu.")
    worker.cancel(jid)
    storage.delete_job(jid)
    return {"ok": True}


@app.post("/api/settings/test-deepseek")
def api_test_deepseek():
    try:
        data, usage = llm.chat_json("Balas hanya dengan JSON.", 'Kirim JSON {"ok": true}.', thinking=False)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[:500]}, status_code=400)
    return {"ok": bool(data.get("ok", True)), "usage": usage}


@app.post("/api/settings/recheck")
def api_recheck():
    reload_settings()
    return system_info()
