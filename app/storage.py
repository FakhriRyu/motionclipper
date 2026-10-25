"""Penyimpanan berbasis folder + JSON. Satu folder per campaign dan per job."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import DATA

CAMPAIGNS = DATA / "campaigns"
JOBS = DATA / "jobs"
INBOX = DATA / "inbox"

_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
_global = threading.Lock()


def _lock(key: str) -> threading.RLock:
    with _global:
        return _locks[key]


def ensure_dirs() -> None:
    for d in (CAMPAIGNS, JOBS, INBOX):
        d.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "campaign"


# ---------------------------------------------------------------- campaign

DEFAULT_CAMPAIGN: dict = {
    "name": "",
    "eligible_from": "",
    "platforms": ["youtube_shorts", "instagram_reels", "tiktok"],
    "sources": [],
    "content_hint": "",
    "moment_keywords": [],
    "banned": [],
    "caption_required": [],
    "hashtags_ordered": [],
    "duration_min": 20,
    "duration_max": 60,
    "num_clips": 8,
    "watermark": {"file": "", "position": "top_left", "width": 300},
    "logo": {"file": "", "position": "top_right", "width": 220},
    "asset": {"file": "", "insert_at": "after_hook", "trim_start": 0.0, "trim_end": 0.0},
    "layout": {
        "mode": "facecam_box",
        "content": None,
        "facecam": None,
        "facecam_pos": "top_center",
        "facecam_width": 380,
        "content_fit": "cover",
    },
    "subtitle": {
        "enabled": True,
        "words_per_line": 3,
        "uppercase": True,
        "font": "Arial Black",
        "size": 76,
        "color": "#FFFFFF",
        "highlight": "#FFD21F",
        "hook_overlay": True,
    },
    "notes": "",
}


def campaign_dir(cid: str) -> Path:
    return CAMPAIGNS / cid


def _merge_defaults(data: dict, defaults: dict) -> dict:
    out = dict(defaults)
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(defaults.get(k), dict):
            out[k] = _merge_defaults(v, defaults[k])
        else:
            out[k] = v
    return out


def list_campaigns() -> list[dict]:
    ensure_dirs()
    out = []
    for d in sorted(CAMPAIGNS.iterdir()):
        c = get_campaign(d.name)
        if c:
            out.append(c)
    return out


def get_campaign(cid: str) -> dict | None:
    data = read_json(campaign_dir(cid) / "campaign.json")
    if data is None:
        return None
    data = _merge_defaults(data, DEFAULT_CAMPAIGN)
    data["id"] = cid
    return data


def save_campaign(cid: str, data: dict) -> dict:
    with _lock("c:" + cid):
        data = dict(data)
        data.pop("id", None)
        write_json(campaign_dir(cid) / "campaign.json", data)
    return get_campaign(cid)


def new_campaign_id(name: str) -> str:
    base = slugify(name)
    cid = base
    i = 2
    while campaign_dir(cid).exists():
        cid = f"{base}-{i}"
        i += 1
    return cid


def asset_path(campaign: dict, kind: str) -> Path | None:
    f = (campaign.get(kind) or {}).get("file")
    if not f:
        return None
    p = campaign_dir(campaign["id"]) / f
    return p if p.exists() else None


def seed_example_campaign() -> None:
    """Isi contoh campaign Oripaku saat aplikasi pertama kali dibuka."""
    ensure_dirs()
    if any(CAMPAIGNS.iterdir()):
        return
    example = dict(DEFAULT_CAMPAIGN)
    example.update(
        {
            "name": "Oripaku x Motion Klip",
            "eligible_from": "2026-09-02",
            "sources": [
                "https://www.youtube.com/watch?v=fdcPgmqWwIw",
                "https://www.youtube.com/watch?v=EvWJQAC4jl0",
                "https://www.youtube.com/watch?v=Iv1c02lGKmk",
                "https://www.youtube.com/watch?v=yJ0_4cYOHCU",
            ],
            "content_hint": (
                "Momen buka pack kartu (Pokemon, Manga One Piece, dll.) saat dapat kartu langka "
                "atau 'pull hit'. Reaksi heboh, kaget, senang. Konten positif untuk brand Oripaku."
            ),
            "moment_keywords": ["hit", "rare", "full art", "secret", "anjir", "gila", "akhirnya", "dapet"],
            "banned": [
                "narasi negatif terhadap pihak lain atau brand lain",
                "memutar konteks video secara menyesatkan",
                "isu sensitif",
                "kata tidak pantas, menghina, SARA",
                "merendahkan atau menyerang orang lain",
                "konten yang merugikan brand",
            ],
            "caption_required": ["www.oripaku.com"],
            "hashtags_ordered": ["#oripaku", "#oripakuMK", "#motionklip", "#ezklip"],
            "notes": (
                "Wajib: watermark Motion Klip, logo Oripaku tidak transparan sepanjang video, "
                "asset video 'Oripaku ini punya Windah dan mas Dean' / 'Oripaku tempat brewek online'. "
                "Pasang Server Tag Discord MKLP. Clip yang sudah disubmit jangan dihapus atau disembunyikan."
            ),
        }
    )
    save_campaign("oripaku", example)


# ---------------------------------------------------------------- jobs

STAGES = ["download", "audio", "transcribe", "peaks", "select", "metadata", "render"]
STAGE_LABELS = {
    "download": "Unduh video",
    "audio": "Ekstrak audio",
    "transcribe": "Transkripsi",
    "peaks": "Lonjakan suara",
    "select": "Pilih momen",
    "metadata": "Caption & cek",
    "render": "Render clip",
}


def job_dir(jid: str) -> Path:
    return JOBS / jid


def new_job(campaign_id: str, source: dict, title: str = "") -> dict:
    ensure_dirs()
    jid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    job = {
        "id": jid,
        "campaign_id": campaign_id,
        "source": source,
        "title": title or source.get("url") or Path(source.get("path", "video")).name,
        "status": "queued",
        "stage": None,
        "stages": {s: {"status": "pending"} for s in STAGES},
        "progress": 0.0,
        "log": [],
        "error": None,
        "duration": None,
        "video_file": None,
        "layout": None,
        "usage": {"prompt_hit": 0, "prompt_miss": 0, "completion": 0, "cost_usd": 0.0},
        "cancel": False,
        "created": now_iso(),
        "updated": now_iso(),
    }
    write_json(job_dir(jid) / "job.json", job)
    return job


def get_job(jid: str) -> dict | None:
    return read_json(job_dir(jid) / "job.json")


def list_jobs() -> list[dict]:
    ensure_dirs()
    jobs = []
    for d in JOBS.iterdir():
        j = get_job(d.name)
        if j:
            jobs.append(j)
    jobs.sort(key=lambda j: j.get("created", ""), reverse=True)
    return jobs


def update_job(jid: str, fn: Callable[[dict], Any] | None = None, **fields) -> dict:
    with _lock("j:" + jid):
        job = get_job(jid)
        if job is None:
            raise KeyError(jid)
        if fn:
            fn(job)
        job.update(fields)
        job["updated"] = now_iso()
        write_json(job_dir(jid) / "job.json", job)
        return job


def job_log(jid: str, message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")

    def add(job):
        job["log"] = (job.get("log") or [])[-199:] + [f"{stamp}  {message}"]

    update_job(jid, add)


def get_clips(jid: str) -> list[dict]:
    return read_json(job_dir(jid) / "clips.json", []) or []


def get_clips_by_id(jid: str, clip_id: str) -> dict | None:
    return next((c for c in get_clips(jid) if c["id"] == clip_id), None)


def save_clips(jid: str, clips: list[dict]) -> None:
    with _lock("k:" + jid):
        write_json(job_dir(jid) / "clips.json", clips)


def update_clips(jid: str, fn: Callable[[list[dict]], Any]) -> list[dict]:
    with _lock("k:" + jid):
        clips = get_clips(jid)
        fn(clips)
        write_json(job_dir(jid) / "clips.json", clips)
        return clips


def update_clip(jid: str, clip_id: str, **fields) -> dict | None:
    found: dict | None = None

    def apply(clips):
        nonlocal found
        for c in clips:
            if c["id"] == clip_id:
                c.update(fields)
                found = c

    update_clips(jid, apply)
    return found


def delete_job(jid: str) -> None:
    shutil.rmtree(job_dir(jid), ignore_errors=True)


def list_inbox() -> list[str]:
    ensure_dirs()
    exts = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
    return sorted(p.name for p in INBOX.iterdir() if p.suffix.lower() in exts)
