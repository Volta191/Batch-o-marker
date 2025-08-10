# Watermarker — Batch Image Watermarking Tool

**Watermarker** is a local web-based application for adding watermarks to large batches of photos.

## Features
- Batch processing
- Text or image watermarks
- EXIF macros
- Multi-threaded processing
- Preserves folder structure

## Installation
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

## License
MIT
