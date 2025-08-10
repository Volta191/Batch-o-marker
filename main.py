import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, Dict, Mapping, cast

from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageEnhance, ExifTags
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Body, Query
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# -----------------------------
# Environment / Executor
# -----------------------------

IS_FROZEN = getattr(sys, "frozen", False)  # True in PyInstaller exe
EXECUTOR = ThreadPoolExecutor if IS_FROZEN else ProcessPoolExecutor

# -----------------------------
# Paths / Storage
# -----------------------------
APP_DIR = Path(__file__).parent.resolve()
STATIC_DIR = APP_DIR / "static"
STORE_DIR = APP_DIR / "templates_store"
IMAGES_DIR = STORE_DIR / "images"
FONTS_DIR = STORE_DIR / "fonts"
TEMPLATES_JSON = STORE_DIR / "templates.json"
DEFAULT_FONT = APP_DIR / "templates_store" / "fonts" / "DejaVuSans.ttf"
SYSTEM_FONTS_CANDIDATES = [
    DEFAULT_FONT,  # наш вшитый, абсолютный путь
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
    Path("/Library/Fonts/Arial.ttf"),
    Path.home() / ".fonts" / "DejaVuSans.ttf",
]
print("DEFAULT_FONT:", DEFAULT_FONT)
print("DEFAULT_FONT exists:", DEFAULT_FONT.exists())


# -----------------------------
# Global job state
# -----------------------------

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

ALLOWED_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}

# Pillow LANCZOS compatibility
try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS  # Pillow >= 9.1
except Exception:
    RESAMPLE_LANCZOS = Image.LANCZOS  # older alias

app = FastAPI(title="Watermarker Web App")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# -----------------------------
# Store helpers
# -----------------------------

def ensure_store():
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    if not TEMPLATES_JSON.exists():
        TEMPLATES_JSON.write_text("{}", encoding="utf-8")

def load_templates():
    ensure_store()
    try:
        return json.loads(TEMPLATES_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_templates(d):
    ensure_store()
    TEMPLATES_JSON.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

# -----------------------------
# EXIF + Macros
# -----------------------------

EXIF_TAGS = {v: k for k, v in ExifTags.TAGS.items()}

def _parse_exif_datetime(s: str):
    try:
        return datetime.strptime(s, "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None

def get_exif_datetime(img: Image.Image):
    try:
        exif = img.getexif()
        if not exif:
            return None
        for tag_name in ("DateTimeOriginal", "DateTime", "DateTimeDigitized"):
            tag_id = EXIF_TAGS.get(tag_name)
            if tag_id and tag_id in exif:
                dt = _parse_exif_datetime(str(exif.get(tag_id)))
                if dt:
                    return dt
    except Exception:
        pass
    return None

DATE_MACRO_RE = re.compile(r"\{date(?::([^}]+))?}")

def expand_text_macros(text: str, img_path: str, img: Image.Image) -> str:
    if not text:
        return text
    exif_dt = get_exif_datetime(img)
    if exif_dt is None:
        try:
            ts = os.path.getmtime(img_path)
            exif_dt = datetime.fromtimestamp(ts)
        except Exception:
            exif_dt = None

    def repl(m):
        fmt = m.group(1) or "%Y-%m-%d %H:%M:%S"
        if exif_dt:
            try:
                return exif_dt.strftime(fmt)
            except Exception:
                return exif_dt.strftime("%Y-%m-%d %H:%M:%S")
        return ""

    return DATE_MACRO_RE.sub(repl, text)

# -----------------------------
# Watermark helpers
# -----------------------------

def _clean_user_font_path(p):
    """Нормализуем то, что приходит из JSON/форм: '', 'null', 'None' -> None."""
    if not p:
        return None
    if isinstance(p, str) and p.strip().lower() in {"", "null", "none"}:
        return None
    return p

def pick_font_path(user_font_path: str | None) -> str | None:
    p = _clean_user_font_path(user_font_path)
    if p:
        up = Path(p)
        # делаем абсолютным относительно директории приложения
        if not up.is_absolute():
            up = (APP_DIR / up).resolve()
        if up.exists():
            return str(up)
    # дальше candidates (включая абсолютный DEFAULT_FONT)
    for cand in SYSTEM_FONTS_CANDIDATES:
        try:
            if cand and Path(cand).exists():
                return str(Path(cand).resolve())
        except Exception:
            continue
    return None

def parse_hex_color(s: Optional[str], default=(255, 255, 255)):
    if not s:
        return default
    s = s.strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return default
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        return (r, g, b)
    except Exception:
        return default

def build_text_watermark_layer(base_w, base_h, text, font_path, scale, opacity, rotation, color_hex):
    # --- Choose font ---
    try:
        candidate = pick_font_path(font_path)  # функция из предыдущего сообщения
        if candidate:
            lo, hi = 8, 2000
            target_w = max(10, int(base_w * max(0.02, min(scale, 1.0))))
            chosen = 64
            while lo <= hi:
                mid = (lo + hi) // 2
                f = ImageFont.truetype(candidate, size=mid)
                x0, y0, x1, y1 = f.getbbox(text)
                w = x1 - x0
                if w < target_w:
                    chosen = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            font = ImageFont.truetype(candidate, size=chosen)
        else:
            font = ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    # --- exact bbox + safety padding ---
    x0, y0, x1, y1 = font.getbbox(text)
    text_w, text_h = (x1 - x0), (y1 - y0)

    # увеличиваем паддинг, чтобы не обрезало тени / выступающие части букв
    base_pad = max(2, int(min(base_w, base_h) * 0.01))
    shadow_offset = max(1, int(base_pad * 0.6))
    pad = base_pad + shadow_offset + 4  # добавил +4 px запаса

    layer = Image.new("RGBA", (text_w + pad * 2, text_h + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # компенсируем смещение bbox (иначе срезает верх/низ)
    ox, oy = (-x0 + pad, -y0 + pad)

    # shadow (black)
    draw.text((ox + shadow_offset, oy + shadow_offset), text, font=font, fill=(0, 0, 0, int(255 * opacity)))
    # main text color
    r, g, b = parse_hex_color(color_hex, (255, 255, 255))
    draw.text((ox, oy), text, font=font, fill=(r, g, b, int(255 * opacity)))

    if rotation:
        layer = layer.rotate(rotation, expand=True)

    return layer

def build_image_watermark_layer(base_w, base_h, wm_path, scale, opacity, rotation):
    mark = Image.open(wm_path).convert("RGBA")
    target_w = max(1, int(base_w * max(0.02, min(scale, 1.0))))
    w, h = mark.size
    factor = target_w / float(w)
    new_size = (max(1, int(w * factor)), max(1, int(h * factor)))
    mark = mark.resize(new_size, RESAMPLE_LANCZOS)
    if rotation:
        mark = mark.rotate(rotation, expand=True)
    alpha = mark.split()[3]
    alpha = ImageEnhance.Brightness(alpha).enhance(max(0.0, min(1.0, opacity)))
    mark.putalpha(alpha)
    return mark

def paste_watermark(base, layer, position, margin, tile_gap):
    bw, bh = base.size
    lw, lh = layer.size
    out = base.copy()
    if position == "tile":
        step_x = lw + tile_gap
        step_y = lh + tile_gap
        for y in range(margin, bh + step_y, step_y):
            x_offset = margin if (y // step_y) % 2 == 0 else margin + step_x // 3
            for x in range(x_offset, bw + step_x, step_x):
                out.alpha_composite(layer, dest=(min(x, bw - lw), min(y, bh - lh)))
        return out

    positions = {
        "top-left": (margin, margin),
        "top-right": (bw - lw - margin, margin),
        "bottom-left": (margin, bh - lh - margin),
        "bottom-right": (bw - lw - margin, bh - lh - margin),
        "center": ((bw - lw) // 2, (bh - lh) // 2),
    }
    x, y = positions.get(position, positions["bottom-right"])
    x = max(0, min(bw - lw, x))
    y = max(0, min(bh - lh, y))
    out.alpha_composite(layer, dest=(x, y))
    return out

def apply_watermark_to_file(in_path, out_path, tmpl, out_format, quality):
    try:
        img = Image.open(in_path)
        img = ImageOps.exif_transpose(img).convert("RGBA")

        ttype = tmpl.get("type", "text")
        scale = float(tmpl.get("scale", 0.2))
        opacity = float(tmpl.get("opacity", 0.25))
        position = tmpl.get("position", "bottom-right")
        rotation = float(tmpl.get("rotation", 0.0))
        margin = int(tmpl.get("margin", max(8, int(min(img.size) * 0.02))))
        tile_gap = int(tmpl.get("tile_gap", int(min(img.size) * 0.1)))

        if ttype == "image":
            wm_path = tmpl.get("image_path")
            if not wm_path or not Path(wm_path).exists():
                return out_path, "Watermark image not found"
            layer = build_image_watermark_layer(img.width, img.height, wm_path, scale, opacity, rotation)
        else:
            text = tmpl.get("text", "WATERMARK")
            text = expand_text_macros(text, in_path, img)
            font_path = tmpl.get("font_path")
            color_hex = tmpl.get("text_color", "#FFFFFF")
            layer = build_text_watermark_layer(img.width, img.height, text, font_path, scale, opacity, rotation, color_hex)

        composited = paste_watermark(img, layer, position, margin, tile_gap)

        out_ext = Path(out_path).suffix.lower()
        desired_format = out_format.upper() if out_format else None
        if desired_format in (None, "", "KEEP"):
            fmt = {
                ".jpg": "JPEG",
                ".jpeg": "JPEG",
                ".png": "PNG",
                ".webp": "WEBP",
                ".bmp": "BMP",
                ".tiff": "TIFF",
            }.get(out_ext, "PNG")
        else:
            fmt = desired_format

        save_kwargs = {}
        if fmt in ("JPEG", "WEBP"):
            save_kwargs["quality"] = max(1, min(100, int(quality if quality is not None else 90)))
            if fmt == "JPEG":
                composited = composited.convert("RGB")

        composited.save(out_path, fmt, **save_kwargs)
        return out_path, None
    except Exception as e:
        return out_path, str(e)

# -----------------------------
# SSE generator
# -----------------------------
def _sse_gen_for_job(files, base, out_root, tdata, output_format, quality, overwrite, open_when_done, job_id: str):
    cancel_ev: threading.Event = JOBS[job_id]["cancel"]
    total = JOBS[job_id]["total"]

    # start
    yield f"event: start\ndata: {json.dumps({'total': total})}\n\n"

    done = 0
    errors = 0
    max_workers = max(1, os.cpu_count() or 4)

    with EXECUTOR(max_workers=max_workers) as pool:
        JOBS[job_id]["pool"] = pool
        futs = []

        # submit tasks
        for src in files:
            if cancel_ev.is_set():
                break
            rel = src.relative_to(base)
            dst = out_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if (not overwrite) and dst.exists():
                done += 1
                JOBS[job_id]["done"] = done
                yield f"event: progress\ndata: {json.dumps({'done': done, 'total': total})}\n\n"
                continue
            futs.append(pool.submit(
                apply_watermark_to_file, str(src), str(dst), tdata, output_format, int(quality)
            ))

        # cancel non-started if requested
        if cancel_ev.is_set():
            for f in futs:
                f.cancel()

        # collect results
        for fu in as_completed(futs):
            if cancel_ev.is_set():
                for f in futs:
                    f.cancel()
                break
            _p, err = fu.result()
            done += 1
            JOBS[job_id]["done"] = done
            if err:
                errors += 1
            yield f"event: progress\ndata: {json.dumps({'done': done, 'total': total})}\n\n"

    # finish
    JOBS[job_id]["pool"] = None
    JOBS[job_id]["errors"] = errors
    cancelled = cancel_ev.is_set()
    JOBS[job_id]["state"] = "cancelled" if cancelled else "done"

    if (not cancelled) and open_when_done and platform.system().lower().startswith("win"):
        try:
            subprocess.Popen(["explorer", str(out_root)])
        except Exception:
            pass

    payload = {'processed': done, 'out_dir': str(out_root), 'errors': errors, 'cancelled': cancelled}
    yield f"event: done\ndata: {json.dumps(payload)}\n\n"

# -----------------------------
# Routes
# -----------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

@app.get("/api/templates")
async def list_templates():
    return load_templates()

@app.post("/api/templates")
async def create_or_update_template(
    name: str = Form(...),
    type: str = Form("text"),
    text: Optional[str] = Form(None),
    scale: float = Form(0.2),
    opacity: float = Form(0.25),
    position: str = Form("bottom-right"),
    rotation: float = Form(0.0),
    margin: int = Form(16),
    tile_gap: int = Form(80),
    text_color: Optional[str] = Form("#FFFFFF"),
    watermark_image: Optional[UploadFile] = File(None),
    font_file: Optional[UploadFile] = File(None),
):
    t = load_templates()

    data: Dict[str, Any] = {
        "type": type,
        "scale": scale,
        "opacity": opacity,
        "position": position,
        "rotation": rotation,
        "margin": margin,
        "tile_gap": tile_gap,
    }

    if type == "image":
        if watermark_image is None:
            old = t.get(name, {})
            if "image_path" not in old:
                raise HTTPException(status_code=400, detail="Upload watermark_image for image template")
            data["image_path"] = old["image_path"]
        else:
            ext = os.path.splitext(watermark_image.filename or "wm.png")[1].lower()
            if ext not in ALLOWED_IMG_EXT:
                raise HTTPException(status_code=400, detail="Unsupported watermark image type")
            dest = IMAGES_DIR / f"{name}{ext}"
            with dest.open("wb") as f:
                f.write(await watermark_image.read())
            data["image_path"] = str(dest)
    else:
        data["text"] = text or "WATERMARK"
        if text_color:
            data["text_color"] = text_color
        if font_file is not None:
            fext = os.path.splitext(font_file.filename or "font.ttf")[1].lower()
            if fext not in (".ttf", ".otf", ".ttc"):
                raise HTTPException(status_code=400, detail="Unsupported font type")
            dest = FONTS_DIR / f"{name}{fext}"
            with dest.open("wb") as f:
                f.write(await font_file.read())
            data["font_path"] = str(dest)
        else:
            old = t.get(name, {})
            if "font_path" in old:
                data["font_path"] = old["font_path"]

    t[name] = {**t.get(name, {}), **data}
    save_templates(t)
    return {"ok": True, "saved": name, "template": t[name]}

@app.delete("/api/templates/{name}")
async def delete_template(name: str):
    t = load_templates()
    if name not in t:
        raise HTTPException(status_code=404, detail="Template not found")
    del t[name]
    save_templates(t)
    return {"ok": True}

def iter_images(input_dir: Path):
    for p in sorted(input_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in ALLOWED_IMG_EXT:
            yield p

@app.post("/api/preview_local")
async def preview_local(
    input_dir: str = Form(...),
    template_name: Optional[str] = Form(None),
    inline_template: Optional[str] = Form(None),
):
    base = Path(input_dir)
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=400, detail="input_dir does not exist or is not a directory")

    if template_name:
        tdata = load_templates().get(template_name)
        if not tdata:
            raise HTTPException(status_code=400, detail=f"Template '{template_name}' not found")
    elif inline_template:
        try:
            tdata = json.loads(inline_template)
        except Exception:
            raise HTTPException(status_code=400, detail="inline_template must be valid JSON")
    else:
        raise HTTPException(status_code=400, detail="Provide template_name or inline_template")

    first = next(iter_images(base), None)
    if not first:
        raise HTTPException(status_code=400, detail="No images found in input_dir")

    workdir = Path(tempfile.mkdtemp(prefix="wmk_prev_local_"))
    out_path = workdir / "preview.png"
    _out, err = apply_watermark_to_file(str(first), str(out_path), tdata, "PNG", 90)
    if err:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=err)

    data = out_path.read_bytes()
    shutil.rmtree(workdir, ignore_errors=True)
    return StreamingResponse(io.BytesIO(data), media_type="image/png")

@app.post("/api/process_local")
async def process_local(
    input_dir: str = Form(...),
    template_name: Optional[str] = Form(None),
    inline_template: Optional[str] = Form(None),
    output_format: str = Form("KEEP"),
    quality: int = Form(90),
):
    base = Path(input_dir)
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=400, detail="input_dir does not exist or is not a directory")

    if template_name:
        tdata = load_templates().get(template_name)
        if not tdata:
            raise HTTPException(status_code=400, detail=f"Template '{template_name}' not found")
    elif inline_template:
        try:
            tdata = json.loads(inline_template)
        except Exception:
            raise HTTPException(status_code=400, detail="inline_template must be valid JSON")
    else:
        raise HTTPException(status_code=400, detail="Provide template_name or inline_template")

    files = list(iter_images(base))
    if not files:
        raise HTTPException(status_code=400, detail="No images found in input_dir")

    temp_zip = Path(tempfile.gettempdir()) / f"watermarked_{uuid.uuid4().hex}.zip"
    workdir = Path(tempfile.mkdtemp(prefix="wmk_zip_"))
    out_root = workdir / "out"
    out_root.mkdir(parents=True, exist_ok=True)

    results: list[str] = []
    errors: list[dict] = []

    max_workers = max(1, os.cpu_count() or 4)
    # IMPORTANT: Use EXECUTOR (in exe it's threads, regular run — processes)
    with EXECUTOR(max_workers=max_workers) as pool:
        futs = []
        for src in files:
            rel = src.relative_to(base)
            dst = out_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            futs.append(
                pool.submit(
                    apply_watermark_to_file,
                    str(src), str(dst), tdata, output_format, int(quality)
                )
            )
        for fu in as_completed(futs):
            out_path, err = fu.result()
            results.append(out_path)
            if err:
                errors.append({"file": out_path, "error": err})

    with zipfile.ZipFile(temp_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for outp in results:
            if os.path.exists(outp):
                arcname = os.path.relpath(outp, out_root)
                zf.write(outp, arcname)

    shutil.rmtree(workdir, ignore_errors=True)
    return FileResponse(path=str(temp_zip), media_type="application/zip", filename=temp_zip.name)

@app.post("/api/cancel_job")
async def cancel_job(payload: dict = Body(...)):
    job_id = str(payload.get("job_id", "")).strip()
    if not job_id or job_id not in JOBS:
        raise HTTPException(404, "Job not found")
    job = JOBS[job_id]
    ev = job.get("cancel")
    if ev:
        ev.set()
    job["state"] = "cancelling"
    pool = job.get("pool")
    if pool:
        try:
            pool.shutdown(cancel_futures=True)
        except Exception:
            pass
    return {"ok": True}

# -----------------------------
# SSE endpoint (progress)
# -----------------------------
@app.get("/api/process_local_sse")
async def process_local_sse(
    input_dir: str,
    template_name: Optional[str] = None,
    output_format: str = "KEEP",
    quality: int = 90,
    output_dir: str = None,
    overwrite: bool = True,
    open_when_done: bool = False,
    job_id: Optional[str] = None,   # client-supplied id (for cancel)
):
    base = Path(input_dir)
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=400, detail="input_dir does not exist or is not a directory")
    if not output_dir:
        raise HTTPException(status_code=400, detail="output_dir is required")

    tdata_raw = load_templates().get((template_name or "").strip())
    if not tdata_raw:
        raise HTTPException(status_code=400, detail="Template not found")
    tdata: Mapping[str, Any] = cast(Mapping[str, Any], tdata_raw)

    files = list(iter_images(base))
    if not files:
        raise HTTPException(status_code=400, detail="No images found in input_dir")

    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    jid = (job_id or uuid.uuid4().hex).strip()
    JOBS[jid] = {
        "state": "running",
        "total": len(files),
        "done": 0,
        "errors": 0,
        "out_dir": str(out_root),
        "cancel": threading.Event(),
        "pool": None,
    }

    return StreamingResponse(
        _sse_gen_for_job(files, base, out_root, tdata, output_format, quality, overwrite, open_when_done, jid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )

# -----------------------------
# Polling fallback
# -----------------------------
def _run_poll_job(job_id: str, files, base: Path, out_root: Path, tdata: dict,
                  output_format: str, quality: int, overwrite: bool, open_when_done: bool):
    job = JOBS.get(job_id)
    if not job:
        return
    cancel_ev: threading.Event = job["cancel"]

    JOBS[job_id]["state"] = "running"
    done = 0
    errors = 0
    total = len(files)

    max_workers = max(1, os.cpu_count() or 4)
    with EXECUTOR(max_workers=max_workers) as pool:
        JOBS[job_id]["pool"] = pool
        futs = []
        for src in files:
            if cancel_ev.is_set():
                break
            rel = src.relative_to(base)
            dst = out_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if (not overwrite) and dst.exists():
                done += 1
                JOBS[job_id]["done"] = done
                continue
            futs.append(pool.submit(
                apply_watermark_to_file, str(src), str(dst), tdata, output_format, int(quality)
            ))

        if cancel_ev.is_set():
            for f in futs:
                f.cancel()

        for fu in as_completed(futs):
            if cancel_ev.is_set():
                for f in futs:
                    f.cancel()
                break
            _p, err = fu.result()
            done += 1
            JOBS[job_id]["done"] = done
            if err:
                errors += 1

    JOBS[job_id]["pool"] = None
    JOBS[job_id]["errors"] = errors

    cancelled = cancel_ev.is_set()
    JOBS[job_id]["state"] = "cancelled" if cancelled else "done"

    if (not cancelled) and open_when_done and platform.system().lower().startswith("win"):
        try:
            subprocess.Popen(["explorer", str(out_root)])
        except Exception:
            pass

@app.post("/api/process_local_poll_start")
async def process_local_poll_start(payload: Dict[str, Any] = Body(...)):
    input_dir = payload.get("input_dir")
    template_name = payload.get("template_name")
    output_format = payload.get("output_format", "KEEP")
    quality = int(payload.get("quality", 90))
    output_dir = payload.get("output_dir")
    overwrite = bool(payload.get("overwrite", True))
    open_when_done = bool(payload.get("open_when_done", False))

    base = Path(input_dir or "")
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=400, detail="input_dir does not exist or is not a directory")
    if not output_dir:
        raise HTTPException(status_code=400, detail="output_dir is required")

    tdata = load_templates().get((template_name or "").strip(), None)
    if not tdata:
        raise HTTPException(status_code=400, detail="Template not found")

    files = list(iter_images(base))
    if not files:
        raise HTTPException(status_code=400, detail="No images found in input_dir")

    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "state": "queued",
        "total": len(files),
        "done": 0,
        "errors": 0,
        "out_dir": str(out_root),
        "cancel": threading.Event(),
        "pool": None,
    }
    threading.Thread(
        target=_run_poll_job,
        args=(job_id, files, base, out_root, tdata, output_format, quality, overwrite, open_when_done),
        daemon=True
    ).start()
    return {"job_id": job_id, "total": len(files)}

@app.get("/api/process_local_poll_status")
async def process_local_poll_status(job_id: str = Query(...)):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    return job

# -----------------------------
# Entrypoint (dev)
# -----------------------------
if __name__ == "__main__":
    import multiprocessing as mp
    import webbrowser
    import uvicorn

    mp.freeze_support()  # important for PyInstaller on Windows
    port = int(os.environ.get("PORT", 8000))
    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
