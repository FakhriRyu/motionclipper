"""Tes ujung-ke-ujung tanpa internet: video sintetis, transkrip palsu, DeepSeek palsu.

Jalankan dari folder proyek:  .venv/bin/python tests/e2e_mock.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="mck-test-"))
os.environ["MCK_DATA_DIR"] = str(TMP / "data")
os.environ["MOCK_LLM"] = "1"
os.environ["WHISPER_BACKEND"] = "mock"
os.environ["MOCK_TRANSCRIPT"] = str(TMP / "words.json")
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402

FF = settings.ffmpeg_bin


def ff(*args):
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_fixtures():
    # Video sumber 150 dtk, 1280x720, kotak 'facecam' di kanan bawah, suara dengan lonjakan di detik 40, 95, 130.
    vol = "volume='if(between(t,40,42)+between(t,95,97)+between(t,130,132),1.0,0.08)':eval=frame"
    ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=150",
       "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=150",
       "-filter_complex", "[0:v]drawbox=x=960:y=480:w=300:h=220:color=orange@0.9:t=fill[v];[1:a]" + vol + "[a]",
       "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(TMP / "source.mp4"))
    ff("-f", "lavfi", "-i", "color=c=white@0.0:s=600x160,format=rgba", "-vf", "drawbox=x=0:y=0:w=600:h=160:color=red@0.8:t=12", "-frames:v", "1", str(TMP / "wm.png"))
    ff("-f", "lavfi", "-i", "color=c=0x2f43cf:s=300x300", "-frames:v", "1", str(TMP / "logo.png"))
    ff("-f", "lavfi", "-i", "smptebars=size=1280x720:rate=30:duration=4",
       "-f", "lavfi", "-i", "sine=frequency=880:duration=4", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(TMP / "asset.mp4"))
    words = []
    t = 0.5
    k = 0
    vocab = ["wah", "ini", "pack", "kartu", "baru", "anjir", "dapet", "rare", "gila", "akhirnya", "full", "art", "keren", "banget"]
    while t < 148:
        w = vocab[k % len(vocab)]
        k += 1
        if k % 9 == 0:
            w += "."
        words.append({"w": " " + w, "s": round(t, 2), "e": round(t + 0.32, 2), "p": 0.9})
        t += 0.42 if k % 9 else 1.1
    (TMP / "words.json").write_text(json.dumps({"words": words}))


def main():
    make_fixtures()
    from fastapi.testclient import TestClient

    from app import storage
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200, r.text
        assert "Oripaku" in r.text, "campaign contoh tidak dibuat"

        # Lengkapi campaign contoh dengan aset.
        c = storage.get_campaign("oripaku")
        form = {
            "name": c["name"], "eligible_from": c["eligible_from"], "platforms": c["platforms"],
            "sources": "\n".join(c["sources"]), "content_hint": c["content_hint"],
            "moment_keywords": ", ".join(c["moment_keywords"]), "banned": "\n".join(c["banned"]),
            "caption_required": "www.oripaku.com", "hashtags_ordered": "#oripaku #oripakuMK #motionklip #ezklip",
            "duration_min": "20", "duration_max": "40", "num_clips": "3",
            "watermark_position": "top_left", "watermark_width": "300",
            "logo_position": "top_right", "logo_width": "200",
            "asset_insert_at": "after_hook", "asset_trim_start": "0", "asset_trim_end": "0",
            "layout_mode": "facecam_box", "layout_content_fit": "cover", "layout_facecam_pos": "top_center", "layout_facecam_width": "380",
            "sub_enabled": "on", "sub_hook_overlay": "on", "sub_uppercase": "on", "sub_words_per_line": "3",
            "sub_font": "Arial Black", "sub_size": "76", "sub_color": "#FFFFFF", "sub_highlight": "#FFD21F", "notes": "tes",
        }
        files = {
            "watermark_file": ("wm.png", open(TMP / "wm.png", "rb"), "image/png"),
            "logo_file": ("logo.png", open(TMP / "logo.png", "rb"), "image/png"),
            "asset_file": ("asset.mp4", open(TMP / "asset.mp4", "rb"), "video/mp4"),
        }
        r = client.post("/campaigns/oripaku", data=form, files=files, follow_redirects=False)
        assert r.status_code == 303, r.text
        c = storage.get_campaign("oripaku")
        assert c["watermark"]["file"] and c["logo"]["file"] and c["asset"]["file"]
        assert c["hashtags_ordered"] == ["#oripaku", "#oripakuMK", "#motionklip", "#ezklip"]
        assert client.get("/campaigns/oripaku").status_code == 200

        # Job dari folder inbox.
        (storage.INBOX / "live.mp4").write_bytes((TMP / "source.mp4").read_bytes())
        assert "live.mp4" in client.get("/jobs/new").text
        r = client.post("/jobs", data={"campaign_id": "oripaku", "urls": "", "inbox_file": "live.mp4"}, follow_redirects=False)
        assert r.status_code == 303, r.text
        jid = r.headers["location"].rsplit("/", 1)[-1]

        t0 = time.time()
        while True:
            d = client.get(f"/api/jobs/{jid}").json()
            if d["job"]["status"] in ("done", "error"):
                break
            assert time.time() - t0 < 600, "terlalu lama"
            time.sleep(1)
        job = d["job"]
        assert job["status"] == "done", job.get("error")
        print("Tahap:", {k: v["status"] for k, v in job["stages"].items()})
        print("Lonjakan suara:", d["peaks"])
        assert any(abs(p["t"] - 41) < 3 for p in d["peaks"]), "lonjakan detik 40 tidak terdeteksi"
        clips = d["clips"]
        assert clips, "tidak ada clip"
        for cl in clips:
            assert cl["render"]["status"] == "done", cl["render"]
            out = storage.job_dir(jid) / cl["render"]["file"]
            info = json.loads(subprocess.run([settings.ffprobe_bin, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(out)], capture_output=True, text=True).stdout)
            v = next(s for s in info["streams"] if s["codec_type"] == "video")
            assert (v["width"], v["height"]) == (1080, 1920), (v["width"], v["height"])
            dur = float(info["format"]["duration"])
            expected = cl["end"] - cl["start"] + 4
            assert abs(dur - expected) < 0.6, (dur, expected)
            assert "www.oripaku.com" in cl["caption_full"] and cl["caption_full"].endswith("#oripaku #oripakuMK #motionklip #ezklip")
            assert all(ch["ok"] for ch in cl["checks"]), cl["checks"]
            print(f"{cl['id']}: {cl['start']}-{cl['end']} -> {dur:.2f} dtk, checks ok")

        # Pratinjau tata letak dengan area facecam.
        layout = {"mode": "facecam_box", "content": {"x": 0.1, "y": 0.05, "w": 0.6, "h": 0.9}, "facecam": {"x": 0.75, "y": 0.66, "w": 0.235, "h": 0.31}}
        r = client.post(f"/api/jobs/{jid}/preview", json={"layout": layout})
        assert r.status_code == 200 and r.json()["ok"], r.text
        for mode in ("split_facecam_bottom", "center"):
            r = client.post(f"/api/jobs/{jid}/preview", json={"layout": layout | {"mode": mode, "content_fit": "fit_blur"}})
            assert r.status_code == 200, r.text
        r = client.post(f"/api/jobs/{jid}/layout", json={"layout": layout | {"mode": "split_facecam_bottom"}, "save_default": False})
        assert r.status_code == 200

        # Edit clip + koreksi subtitle + render ulang.
        cid = clips[0]["id"]
        r = client.post(f"/api/jobs/{jid}/clips/{cid}", json={"start": clips[0]["start"] + 1, "end": clips[0]["end"], "caption_body": "Caption baru #spam www.lain.com", "approved": True})
        assert r.status_code == 200, r.text
        upd = r.json()
        assert upd["render"].get("stale") and "#spam" not in upd["caption_full"] and upd["approved"]
        sents = client.get(f"/api/jobs/{jid}/clips/{cid}/sentences").json()
        r = client.post(f"/api/jobs/{jid}/sentences", json={"clip_id": cid, "edits": [{"i": sents[0]["i"], "text": "Halo semua ini tes koreksi"}]})
        assert r.status_code == 200
        tr = storage.read_json(storage.job_dir(jid) / "transcript.json")
        s0 = tr["sentences"][sents[0]["i"]]
        assert " ".join(w["w"] for w in tr["words"][s0["w0"]: s0["w1"] + 1]) == "Halo semua ini tes koreksi"
        r = client.post(f"/api/jobs/{jid}/render", json={"clips": [cid]})
        assert r.status_code == 200
        # Clip manual.
        r = client.post(f"/api/jobs/{jid}/clips", json={"start": 100, "end": 125})
        assert r.status_code == 200, r.text
        manual = r.json()["id"]
        t0 = time.time()
        while True:
            d = client.get(f"/api/jobs/{jid}").json()
            sts = {c["id"]: c["render"]["status"] for c in d["clips"]}
            if all(s in ("done", "error") for s in sts.values()) and d["job"]["status"] != "running":
                break
            assert time.time() - t0 < 300
            time.sleep(1)
        assert sts[cid] == "done" and sts[manual] == "done", sts
        assert not d["clips"][0]["render"].get("stale")
        m = next(c for c in d["clips"] if c["id"] == manual)
        assert m["title"], "metadata clip manual kosong"

        # Ekspor ZIP.
        r = client.get(f"/jobs/{jid}/export.zip")
        assert r.status_code == 200
        z = zipfile.ZipFile(__import__("io").BytesIO(r.content))
        assert f"{cid}.mp4" in z.namelist() and "captions.txt" in z.namelist()

        # Halaman.
        for path in ("/", f"/jobs/{jid}", "/settings", "/jobs/new?campaign=oripaku"):
            assert client.get(path).status_code == 200, path

        # Ulangi dari tahap pilih momen.
        r = client.post(f"/api/jobs/{jid}/rerun/select")
        assert r.status_code == 200, r.text
        t0 = time.time()
        while client.get(f"/api/jobs/{jid}").json()["job"]["status"] not in ("done", "error"):
            assert time.time() - t0 < 300
            time.sleep(1)
        assert client.get(f"/api/jobs/{jid}").json()["job"]["status"] == "done"

        r = client.post(f"/api/jobs/{jid}/delete-source")
        assert r.status_code == 200
        print("Data tes di:", TMP)
        print("SEMUA TES LULUS")


if __name__ == "__main__":
    main()
