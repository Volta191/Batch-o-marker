# Batch-o-mark — Batch Image Watermarking Tool

Watermarker is a **desktop (`PySide6`) and local web (`FastAPI`) app** for batch watermarking large photo sets (3,000–10,000+ photos). It supports **text and image watermarks**, **EXIF date & filename macros**, **multi-core processing**, **EXIF/ICC preservation**, and **preserves your original folder structure**.

> Runs entirely on your machine. No cloud uploads.

## Desktop App (Windows & macOS)

```bash
pip install -r requirements-desktop.txt
python desktop_app.py
```

Build standalone `.exe` / `.app` locally:
```bash
pip install -r requirements-desktop.txt pyinstaller
pyinstaller --clean --noconfirm Batch-o-mark.spec
```
Or trigger the **Build Desktop Apps (Windows & macOS)** GitHub Actions workflow in `.github/workflows/build.yml` to download ready-made `Batch-o-mark-windows-x64.zip` and `Batch-o-mark-macos.zip`.

## [Recent Release](https://github.com/Volta191/Batch-o-marker/releases). Compiled for Windows 
---

## ✨ Features

- 📂 **Batch processing** of an entire folder (recursive)
- 🖼 **Text or image watermarks**
- 🔍 **Preview panel** (first image from selected folder, with template applied)
- 🧮 **Multi-core** processing (uses all CPU cores)
- 🗂 **Preserves directory structure** in output
- 🗓 **EXIF date macros** in text watermark: `{date}`, `{date:%Y-%m-%d %H:%M}`
- 🎛 Controls for **position**, **scale**, **opacity**, **rotation**, **tiling**
- 💾 **Template storage** (text/image templates with optional custom fonts)
- 📦 Output as **ZIP** or **save directly to a folder**

---

## 🧭 Project layout

```

watermarker/
├─ main.py
├─ requirements.txt
├─ static/
│  ├─ index.html
│  ├─ app.js
│  └─ styles.css
├─ templates\_store/
│  ├─ templates.json         # saved templates
│  ├─ images/                # watermark images (optional)
│  └─ fonts/                 # custom fonts (optional)
├─ README.md
└─ LICENSE

````

---

## 🛠 Requirements

- **Python 3.12** (recommended)  
  - Windows + Python 3.13 may try to compile some packages from source; 3.12 has prebuilt wheels and it's smoother.
- Modern browser (Chrome/Edge/Safari/Firefox)

---

## 📦 Installation

### Windows / Linux

```bash
# 1) Clone
git clone https://github.com/<your-username>/watermarker.git
cd watermarker

# 2) Create venv
python -m venv .venv

# 3) Activate
# Windows:
.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate

# 4) Install deps
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
````

---

## ▶️ Run

```bash
uvicorn main:app
# App → http://127.0.0.1:8000
```

> If you want auto-reload while developing (hot reload), use `uvicorn main:app --reload`.
> On Windows, `--reload` may pull extra dev deps; if pip complains, run without `--reload`.

---
Окей, тогда сделаем на английском и в твоём стиле README, чтобы подходило для GitHub.

⸻


## 🚀 Running on macOS

### 1. Install Python 3.10+ (if not already installed)
It’s recommended to use [Homebrew](https://brew.sh/):
```bash
brew install python

Check the version:

python3 --version

```
⸻

2. Clone the repository
```bash
git clone https://github.com/username/batch-o-mark.git
cd batch-o-mark
```

⸻

3. Create and activate a virtual environment
```
python3 -m venv .venv
source .venv/bin/activate
```
Use deactivate to exit the virtual environment.

⸻

4. Upgrade pip and install dependencies
```bash
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

⸻

5. Run the application
```bash
uvicorn main:app --reload
```
Once started, open your browser and go to:

http://127.0.0.1:8000


⸻

6. Build a binary for macOS (optional)

If you want to bundle the app into a standalone binary or .app:
```
pip install pyinstaller
pyinstaller --name "batch-o-mark" --add-data "static:static" --add-data "templates_store:templates_store" main.py
```
The build output will be located in:

dist/batch-o-mark


⸻

Notes
	•	If you get compilation errors for Pillow or other dependencies, make sure Xcode Command Line Tools are installed:
```
xcode-select --install
```
  • On macOS, the app may run faster than on Windows due to optimizations in system libraries.

---

## 🖥 Using the app

### 1) Create or select a template

**Text watermark:**

* Type your text
* (Optional) Upload a `.ttf/.otf` font for best Cyrillic/Unicode support
* Adjust **position**, **scale (fraction of image width)**, **opacity**, **rotation**
* **Tiling** mode repeats the watermark in a grid

**Image watermark:**

* Upload a PNG (preferably with alpha)
* Same controls for position/scale/opacity/rotation/tiling

**EXIF date macros in text:**

* `{date}` → DateTimeOriginal if available (else DateTime/DateTimeDigitized, else file mtime)
* `{date:%Y-%m-%d %H:%M}` → custom `strftime` format

> Example: `© My Studio {date:%Y-%m-%d}`

---

### 2) Preview (right panel)

* Choose the **input folder** on the left (folder-on-disk mode).
* Click **Update Preview** (button on the right).
* The app finds the **first image** in the folder (alphabetically, recursively), applies the **currently selected template**, and shows the result.
* The preview scales to fit the panel for comfortable viewing.

> Tip: Preview uses current form values even if you haven’t saved the template yet.

---

### 3) Batch processing

You have two ways to feed images:

#### A) Browser upload (for smaller batches)

* Click “Choose folder” and pick a folder (uses `webkitdirectory` to preserve relative paths).
* Select a template.
* Choose output format (**KEEP/JPEG/PNG/WEBP/BMP/TIFF**) and quality (for JPEG/WEBP).
* Click **Start**. You’ll get a **ZIP** that mirrors the original directory structure.

#### B) Folder on disk (for very large batches)

* Switch to **Folder on Disk (server)** mode.
* Enter absolute path to **Input folder** (e.g., `C:\Users\me\Pictures\RAW` or `/Users/me/Pictures/RAW`).
* Choose output mode:

  * **ZIP** → download a single archive
  * **Save to folder** → write files directly to a target directory (preserving structure)
* Click **Start**.
* On completion:

  * ZIP mode returns a download,
  * Folder mode returns a JSON summary and writes files to disk.

---

## ⚙️ Configuration notes

* **Templates storage**

  * `templates_store/templates.json`: text + image watermark presets
  * `templates_store/images/`: your watermark logos
  * `templates_store/fonts/`: your custom fonts
* **Output format**:

  * `KEEP` keeps original format by extension where possible
  * JPEG/WEBP use the `quality` setting (1–100)
* **EXIF rotation** is respected (`ImageOps.exif_transpose`)

---

## 🐳 Docker (optional)

### Build & run

```bash
# Build image
docker build -t watermarker .

# Run and expose port 8000
docker run --rm -p 8000:8000 watermarker
# → open http://127.0.0.1:8000
```

### With mounted volumes (recommended for folder mode)

```bash
docker run --rm -p 8000:8000 \
  -v /absolute/path/to/images:/images \
  -v /absolute/path/to/output:/output \
  watermarker
```

> In the UI, use `/images` as input and `/output` as output directories.

### docker-compose

```yaml
version: "3"
services:
  app:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./data/images:/images
      - ./data/output:/output
```

```bash
docker compose up --build
```

---

## 📦 Packaging a standalone binary (optional)

If you want a single-file executable for non-technical users:

### Windows (PyInstaller)

```powershell
pip install pyinstaller
echo "import uvicorn; from main import app; uvicorn.run(app, host='127.0.0.1', port=8000)" > run_app.py
pyinstaller --onefile --noconsole ^
  --name Watermarker ^
  --add-data "static;static" ^
  --add-data "templates_store;templates_store" ^
  run_app.py
# ➜ dist/Watermarker.exe
```

### macOS/Linux (PyInstaller)

```bash
pip install pyinstaller
printf "import uvicorn\nfrom main import app\nuvicorn.run(app, host='127.0.0.1', port=8000)\n" > run_app.py
pyinstaller --onefile --noconsole \
  --name watermarker \
  --add-data "static:static" \
  --add-data "templates_store:templates_store" \
  run_app.py
# ➜ dist/watermarker (binary)
```

> Notes:
> * On macOS first run may be blocked by Gatekeeper (unidentified developer). Use **System Settings → Privacy & Security → Open Anyway** or sign the binary.
> * Binaries are large (Python + libs embedded) — that’s normal.

---

## 🔌 API (for power users)

* `GET /` — UI
* `GET /api/templates` — list templates (JSON)
* `POST /api/templates` — create/update template
  form fields vary by type: `type=text|image`, `text`, `watermark_image`, `font_file`, `position`, `scale`, `opacity`, `rotation`, `tile_gap`, `margin`
* `DELETE /api/templates/{name}` — delete template
* `POST /api/preview` — preview for one uploaded image (returns PNG)
* `POST /api/process` — browser-upload batch → ZIP
* `POST /api/preview_local` — preview first image from a local folder (returns PNG)
* `POST /api/process_local` — local folder batch → ZIP or save to folder

---

## 🧪 Troubleshooting

* **Pillow build error on Windows + Python 3.13**
  Use **Python 3.12**. 3.13 may lack prebuilt wheels and try to compile from source.
* **Port already in use**
  Change port: `uvicorn main:app --port 8001`
* **“dubious ownership” on Git**
  On external/network drives:


* **Fonts**
  Upload a `.ttf/.otf` that supports your script in the template panel.

---

## 🤝 Contributing

PRs welcome! For larger changes, please open an issue first to discuss.
Typical areas to contribute:

* More macros (e.g. `{filename}`, `{dirname}`)
* Better preview (zoom/pan/next/previous)
* SVG watermarks
* CLI batch runner

---

## 📜 License

MIT — see [LICENSE](LICENSE).
