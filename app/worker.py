"""Antrian kerja tunggal: satu job diproses dalam satu waktu (aman untuk RAM 8 GB)."""
from __future__ import annotations

import queue
import threading
import traceback

from . import storage
from .pipeline import run as pipeline

_q: "queue.Queue[dict]" = queue.Queue()
_thread: threading.Thread | None = None
current: dict | None = None


def submit(task: dict) -> None:
    if task["kind"] == "pipeline":
        storage.update_job(task["job"], status="queued", error=None)
    _q.put(task)


def queued_jobs() -> list[str]:
    return [t["job"] for t in list(_q.queue)]


def cancel(jid: str) -> None:
    pipeline.CANCELLED.add(jid)
    # Buang tugas yang belum mulai untuk job ini.
    with _q.mutex:
        remaining = [t for t in _q.queue if t["job"] != jid]
        _q.queue.clear()
        _q.queue.extend(remaining)
    job = storage.get_job(jid)
    if job and job["status"] == "queued":
        storage.update_job(jid, status="cancelled")
    if not (current and current["job"] == jid):
        pipeline.CANCELLED.discard(jid)


def _handle(task: dict) -> None:
    jid = task["job"]
    if storage.get_job(jid) is None:
        return
    kind = task["kind"]
    if kind == "pipeline":
        pipeline.run_pipeline(jid)
        return
    storage.update_job(jid, status="running", stage="render", progress=0.0)
    try:
        if kind == "meta_render":
            pipeline.metadata_for(jid, task["clips"])
        pipeline.render_clips(jid, task.get("clips"))
        storage.update_job(jid, status="done", stage=None, progress=1.0)
    except pipeline.Cancelled:
        storage.update_job(jid, status="cancelled", stage=None)
    except Exception as e:  # noqa: BLE001
        storage.update_job(jid, status="error", error=str(e)[-2000:], stage=None)
        storage.job_log(jid, "Gagal: " + str(e).splitlines()[0][:300])
    finally:
        pipeline.CANCELLED.discard(jid)


def _loop() -> None:
    global current
    while True:
        task = _q.get()
        current = task
        try:
            _handle(task)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            current = None
            _q.task_done()


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="clip-worker", daemon=True)
    _thread.start()
    # Lanjutkan job yang terputus saat aplikasi ditutup.
    for job in reversed(storage.list_jobs()):
        if job["status"] in ("queued", "running"):
            storage.job_log(job["id"], "Dilanjutkan setelah aplikasi dibuka ulang.")
            submit({"kind": "pipeline", "job": job["id"]})
