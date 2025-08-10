# Watermarker — Batch Image Watermarking Tool

Local web app for adding watermarks to large batches of photos. Supports text & image watermarks, EXIF macros, preview, multi-core processing, and preserving the original folder structure.

## Quick Start (Python 3.12 recommended)
```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate

pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
uvicorn main:app
```
Open http://127.0.0.1:8000

## Notes
- Templates are stored in `templates_store/templates.json`. Optional assets: `templates_store/images/` and `templates_store/fonts/`.
- For text watermarks, you can use EXIF macros: `{date}` or `{date:%Y-%m-%d}`.
- Use *Folder on Disk (server)* mode for huge folders (no browser upload limit).
