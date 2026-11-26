# Batch-o-mark — Batch Photo Watermarking Tool

**Batch-o-mark** is a cross-platform **desktop (`PySide6`) and local web (`FastAPI`) application** for batch watermarking large photo collections (3,000–10,000+ photos).

> Runs 100% locally on your machine. No cloud uploads.

---

## ✨ Features

- **Native Desktop UI (`PySide6` / Qt 6)** for Windows and macOS — drag & drop folders, interactive zoomable preview (mouse wheel zoom, pan, double-click Fit / 100%, Before/After toggle), Dark & Light themes (`Alt+T`).
- **High-Throughput Engine (`engine.py`)** tuned for 3,000–10,000+ photos:
  - Multi-core `ProcessPoolExecutor` with bounded sliding-window scheduling
  - Per-worker LRU caching for rendered watermark layers, logos, and fonts
  - In-place RGB compositing (low memory usage, fast JPEG/WebP output)
  - Fast `os.scandir` recursive folder scanning
- **Text & Image Watermarks**: custom `.ttf/.otf/.ttc` fonts, PNG logos with alpha, scale, opacity, rotation, margin, and tiling.
- **Macros**: `{date}`, `{date:%Y-%m-%d %H:%M}` (reads EXIF `DateTimeOriginal` SubIFD with file `mtime` fallback), `{filename}`, `{stem}`, `{dirname}`.
- **EXIF & Color Profile Preservation**: respects EXIF orientation (`exif_transpose` + resets `Orientation=1`) and preserves camera EXIF and `icc_profile`.
- **Flexible Output**: save directly to a folder (preserving directory structure, with resume support when *Overwrite* is unchecked) or export as a ZIP archive.
- **Template Storage**: saved in `templates_store/templates.json` (or in OS user data directory / portable folder when packaged).

---

## 🧭 Project Layout

```text
Batch-o-marker/
├─ desktop_app.py             # Native PySide6 desktop application
├─ engine.py                  # Shared watermarking, EXIF, and batch processing engine
├─ main.py                    # Optional FastAPI web server
├─ static/                    # Web UI assets (index.html, app.js, styles.css)
├─ templates_store/           # Saved templates, logos, and bundled DejaVuSans fonts
├─ requirements-desktop.txt   # Minimal dependencies for desktop app (PySide6, Pillow)
├─ requirements.txt           # Full dependencies (desktop + FastAPI web server)
├─ Batch-o-mark.spec          # PyInstaller spec for Windows (.exe) & macOS (.app)
└─ .github/workflows/build.yml # GitHub Actions auto-build for Windows & macOS
```

---

## 🚀 Quick Start (Desktop App)

Requires **Python 3.10–3.13** (Python 3.12 recommended).

### macOS / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-desktop.txt
python desktop_app.py
```

### Windows (PowerShell)
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-desktop.txt
python desktop_app.py
```

---

## 📦 Standalone Builds (`.exe` / `.app`)

### Option A: GitHub Actions (Recommended)
Run the **Build Desktop Apps (Windows & macOS)** workflow in `.github/workflows/build.yml` (manually via **Actions → Run workflow** or by pushing a `v*` tag). Download the artifacts from the workflow **Summary** page:
- `Batch-o-mark-windows-x64` → extract and run `Batch-o-mark.exe`
- `Batch-o-mark-macos` → extract, open `Batch-o-mark-macos.dmg`, and drag `Batch-o-mark.app` to Applications

> **macOS Gatekeeper note:** If macOS blocks the unsigned `.app` on first launch, run:
> ```bash
> xattr -cr /Applications/Batch-o-mark.app
> ```

### Option B: Build Locally with PyInstaller
```bash
pip install -r requirements-desktop.txt pyinstaller
pyinstaller --clean --noconfirm Batch-o-mark.spec
```
Output will be in `dist/Batch-o-mark/` (and `dist/Batch-o-mark.app` on macOS).

---

## 🌐 Web UI Mode (Optional)

If you prefer running Batch-o-mark in a browser or Docker container:

```bash
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8000
# Open http://127.0.0.1:8000
```

### Key Web API Endpoints
- `GET /` — Web UI
- `GET /api/templates` / `POST /api/templates` / `DELETE /api/templates/{name}` — Manage templates
- `POST /api/scan_local` — Fast recursive image count
- `POST /api/preview_local` — Live JPEG preview (supports `inline_template`, `index`, `show_original`)
- `POST /api/process_local_start` — Start batch job (`DIR` or `ZIP`)
- `GET /api/job_events` — SSE progress stream
- `GET /api/process_local_poll_status` — Polling status fallback
- `POST /api/cancel_job` — Cancel active job
- `GET /api/download_job_zip/{job_id}` — Stream completed ZIP archive

---

## 📜 License

MIT — see [LICENSE](LICENSE).
