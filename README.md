# MotionClipper KL

Web app localhost untuk membuat clip vertikal 9:16 dari video campaign clipping (Discord / Whop).
Semua proses video berjalan di Mac; hanya teks transkrip yang dikirim ke DeepSeek untuk memilih momen dan menulis caption.

```
Link YouTube dari brief
  → unduh (yt-dlp) → audio → transkripsi (mlx-whisper, GPU M1) → lonjakan suara
  → DeepSeek pilih momen → DeepSeek tulis caption + cek larangan
  → render 9:16: facecam, watermark, logo, asset wajib, subtitle kata per kata
  → review di browser → setujui → unduh ZIP / buka folder → upload ke Shorts, Reels, TikTok
```

## 1. Pasang (sekali saja)

Butuh: Mac Apple Silicon (M1 ke atas), [Homebrew](https://brew.sh), ruang disk kosong minimal 15 GB.

```bash
cd ~/Documents/PersonalProject/MotionClipperKL
chmod +x setup.sh run.sh
./setup.sh
```

`setup.sh` memasang `ffmpeg-full` (ffmpeg dengan dukungan subtitle), Python 3.12, Deno (dibutuhkan yt-dlp untuk YouTube),
lalu paket Python di folder `.venv`. Model Whisper (±1,5 GB) diunduh otomatis saat transkripsi pertama.

## 2. Jalankan

```bash
./run.sh
```

Browser terbuka ke **http://localhost:8000**. Biarkan jendela Terminal tetap terbuka; tekan `Ctrl+C` untuk berhenti.
Selama aplikasi jalan, Mac dicegah tidur (pakai `caffeinate`), jadi antrian bisa jalan semalaman. Jangan tutup lid laptop
kecuali tersambung charger dan monitor luar.

## 3. Alur kerja harian

1. **Pengaturan** → isi API key DeepSeek (dari platform.deepseek.com → API keys) → klik *Tes koneksi DeepSeek*.
2. **Campaign Oripaku** sudah terisi dari brief. Unduh watermark, logo, dan asset video dari Google Drive campaign,
   lalu unggah di halaman campaign. Titik hijau di beranda berarti asetnya sudah lengkap.
3. **Job baru** → klik link sumber dari brief (atau tempel link lain) → *Mulai proses*. Beberapa link = beberapa job,
   diproses satu per satu.
4. Saat transkripsi berjalan, buka halaman job. Di panel **Tata letak 9:16**, gambar kotak *Area konten* (meja/kartu)
   dan *Area facecam* di atas cuplikan video, klik *Lihat pratinjau*, lalu *Jadikan default campaign*.
   Untuk video berikutnya dari streamer yang sama, tata letak ini dipakai otomatis.
5. Setelah clip jadi: tonton, koreksi waktu atau teks subtitle bila perlu, klik **Render ulang**, lalu **Setujui**.
6. **Unduh ZIP disetujui** (berisi MP4 + `captions.txt`) atau **Buka folder di Finder**.
   Satu file MP4 dipakai untuk ketiga platform; caption sudah memuat website dan hashtag sesuai urutan brief.

Cek otomatis di tiap clip: durasi, watermark, logo, asset wajib, teks wajib di caption, urutan hashtag, dan cek larangan
oleh AI. Clip yang ditandai merah sebaiknya ditinjau sebelum diunggah.

Di luar aplikasi: pasang Server Tag Discord **MKLP**, unggah hanya setelah tanggal eligible, dan jangan hapus atau sembunyikan
clip yang sudah disubmit.

## Biaya

Hanya DeepSeek yang berbayar. Perkiraan dengan `deepseek-flash`: ±Rp350 per jam video di jam sibuk, setengahnya di luar jam
sibuk (jam sibuk DeepSeek: Senin–Jumat 08:00–11:00 dan 13:00–17:00 WIB). Biaya nyata tiap job tampil di halaman job.

## Tips untuk M1 RAM 8 GB

- Tutup Chrome/aplikasi berat saat transkripsi live berjam-jam.
- Model default `large-v3-turbo`. Kalau sering kehabisan RAM atau terlalu lambat, ganti ke `medium` atau `small` di Pengaturan.
- Video diunduh maksimal 1080p dengan codec H.264 agar ringan didekode. Live 5 jam bisa 5–9 GB;
  pakai **Hapus video sumber** setelah semua clip selesai untuk membebaskan disk.
- File video besar yang sudah ada di Mac: taruh di `data/inbox/`, lalu pilih dari halaman Job baru (tidak perlu diunggah ulang).

## Struktur folder

```
app/                 kode aplikasi (FastAPI + pipeline)
  pipeline/          unduh, transkripsi, lonjakan suara, DeepSeek, validasi, render
  templates/ static/ halaman web
data/                dibuat otomatis, tidak perlu di-backup ke Git
  campaigns/<id>/    campaign.json + watermark, logo, asset
  jobs/<id>/         video sumber, transkrip, clips/*.mp4, job.json
  inbox/             taruh file video lokal di sini
.env                 API key dan pengaturan (jangan dibagikan)
```

Setiap tahap menyimpan hasilnya ke disk. Kalau aplikasi ditutup di tengah jalan, job dilanjutkan otomatis saat dibuka lagi.
Menu **Ulangi dari tahap…** di halaman job menjalankan ulang tahap tertentu (misalnya pilih momen ulang dengan brief baru).

## Masalah umum

| Gejala | Solusi |
| --- | --- |
| Banner "FFmpeg tanpa libass" / clip tanpa subtitle | `brew install ffmpeg-full`, lalu jalankan ulang `./run.sh` |
| Unduhan YouTube gagal atau kualitas rendah | `brew install deno` dan `.venv/bin/pip install -U "yt-dlp[default]"` (yt-dlp perlu sering diperbarui) |
| Transkripsi gagal, pesan RAM habis | Pilih model lebih kecil di Pengaturan, tutup aplikasi lain, klik *Lanjutkan* |
| "API key DeepSeek belum diisi" | Isi di Pengaturan, lalu klik *Lanjutkan* di halaman job |
| Port 8000 dipakai aplikasi lain | `PORT=8010 ./run.sh` |

## Tes tanpa internet

```bash
PATH="/opt/homebrew/opt/ffmpeg-full/bin:$PATH" .venv/bin/python tests/e2e_mock.py
```

Membuat video sintetis, memakai transkrip dan respons DeepSeek palsu, lalu menguji seluruh pipeline sampai ekspor ZIP.
