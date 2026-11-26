import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import ExifTags, Image, ImageDraw, ImageFont, ImageOps
from starlette.background import BackgroundTask

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
DEFAULT_FONT = STORE_DIR / "fonts" / "DejaVuSans.ttf"
SYSTEM_FONTS_CANDIDATES = [
    DEFAULT_FONT,
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
    Path("/Library/Fonts/Arial.ttf"),
    Path.home() / ".fonts" / "DejaVuSans.ttf",
]

# -----------------------------
# Global job state
# -----------------------------

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

ALLOWED_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}
COMPRESSED_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
FORMAT_TO_EXT = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TIFF": ".tiff",
}
EXT_TO_FORMAT = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
    ".bmp": "BMP",
    ".tiff": "TIFF",
    ".tif": "TIFF",
}
SKIP_DIR_NAMES = {"@eaDir", ".Trashes", ".Spotlight-V100", "__MACOSX"}

# Pillow resampling compatibility
try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
    RESAMPLE_BICUBIC = Image.Resampling.BICUBIC
except AttributeError:
    RESAMPLE_LANCZOS = Image.LANCZOS
    RESAMPLE_BICUBIC = Image.BICUBIC

app = FastAPI(title="Batch-o-mark Web App")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# -----------------------------
# Store helpers
# -----------------------------


def ensure_store() -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    if not TEMPLATES_JSON.exists():
        TEMPLATES_JSON.write_text("{}", encoding="utf-8")


def load_templates() -> Dict[str, Any]:
    ensure_store()
    try:
        data = json.loads(TEMPLATES_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_templates(d: Dict[str, Any]) -> None:
    ensure_store()
    TEMPLATES_JSON.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_filename_stem(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", name.strip()).strip("._")
    return cleaned or f"tpl_{uuid.uuid4().hex[:8]}"


# -----------------------------
# EXIF + Macros
# -----------------------------

EXIF_TAGS = {v: k for k, v in ExifTags.TAGS.items()}
EXIF_IFD_TAG = 0x8769
EXIF_ORIENTATION_TAG = 0x0112
EXIF_DATE_TAG_IDS = (
    0x9003,  # DateTimeOriginal
    0x9004,  # DateTimeDigitized
    0x0132,  # DateTime
)
DATE_MACRO_RE = re.compile(r"\{date(?::([^}]+))?\}")


def _parse_exif_datetime(s: str) -> Optional[datetime]:
    cleaned = s.replace("\x00", "").strip()
    if not cleaned:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            return datetime.strptime(cleaned, fmt)
        except Exception:
            continue
    return None


def get_exif_datetime(img: Image.Image) -> Optional[datetime]:
    try:
        exif = img.getexif()
        if not exif:
            return None
        # DateTimeOriginal/DateTimeDigitized live in the Exif SubIFD (0x8769) in Pillow >= 9
        sub_ifd = None
        try:
            sub_ifd = exif.get_ifd(EXIF_IFD_TAG)
        except Exception:
            sub_ifd = None

        for tag_id in EXIF_DATE_TAG_IDS:
            val = None
            if sub_ifd and tag_id in sub_ifd:
                val = sub_ifd.get(tag_id)
            elif tag_id in exif:
                val = exif.get(tag_id)
            if val:
                dt = _parse_exif_datetime(str(val))
                if dt:
                    return dt
    except Exception:
        pass
    return None


def expand_text_macros(
    text: str,
    img_path: str,
    img: Optional[Image.Image] = None,
    exif_dt: Optional[datetime] = None,
) -> str:
    if not text:
        return text

    if "{filename}" in text or "{stem}" in text or "{dirname}" in text:
        p = Path(img_path)
        text = text.replace("{filename}", p.name)
        text = text.replace("{stem}", p.stem)
        text = text.replace("{dirname}", p.parent.name)

    if "{date" not in text:
        return text

    if exif_dt is None and img is not None:
        exif_dt = get_exif_datetime(img)
    if exif_dt is None:
        try:
            exif_dt = datetime.fromtimestamp(os.path.getmtime(img_path))
        except Exception:
            exif_dt = None

    def repl(m: re.Match) -> str:
        fmt = m.group(1) or "%Y-%m-%d %H:%M:%S"
        if exif_dt:
            try:
                return exif_dt.strftime(fmt)
            except Exception:
                return exif_dt.strftime("%Y-%m-%d %H:%M:%S")
        return ""

    return DATE_MACRO_RE.sub(repl, text)


# -----------------------------
# Watermark helpers (with per-worker LRU caches)
# -----------------------------


def _clean_user_font_path(p: Any) -> Optional[str]:
    if not p:
        return None
    if isinstance(p, str) and p.strip().lower() in {"", "null", "none"}:
        return None
    return str(p)


@lru_cache(maxsize=16)
def pick_font_path(user_font_path: Optional[str]) -> Optional[str]:
    p = _clean_user_font_path(user_font_path)
    if p:
        up = Path(p)
        if not up.is_absolute():
            up = (APP_DIR / up).resolve()
        if up.exists():
            return str(up)
    for cand in SYSTEM_FONTS_CANDIDATES:
        try:
            if cand and cand.exists():
                return str(cand.resolve())
        except Exception:
            continue
    return None


@lru_cache(maxsize=64)
def _load_font(font_path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path, size=size)


@lru_cache(maxsize=128)
def parse_hex_color(s: Optional[str], default: Tuple[int, int, int] = (255, 255, 255)) -> Tuple[int, int, int]:
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
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return default


def _choose_font_for_width(candidate: str, text: str, target_w: int) -> ImageFont.FreeTypeFont:
    # Estimate size linearly from reference size=100 (cuts font loads from ~11 to 1-3)
    ref_font = _load_font(candidate, 100)
    rx0, _ry0, rx1, _ry1 = ref_font.getbbox(text)
    ref_w = max(1, rx1 - rx0)
    est = max(8, min(2000, int(round(100.0 * target_w / ref_w))))

    lo = max(8, int(est * 0.8))
    hi = min(2000, max(lo, int(est * 1.25) + 2))
    chosen = lo
    while lo <= hi:
        mid = (lo + hi) // 2
        f = _load_font(candidate, mid)
        x0, _y0, x1, _y1 = f.getbbox(text)
        w = x1 - x0
        if w <= target_w:
            chosen = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return _load_font(candidate, chosen)


@lru_cache(maxsize=64)
def _build_text_layer_cached(
    base_w: int,
    base_h: int,
    text: str,
    font_path: Optional[str],
    scale: float,
    opacity: float,
    rotation: float,
    color_hex: str,
) -> Optional[Image.Image]:
    if not text or not text.strip():
        return None

    scale_clamped = max(0.02, min(scale, 1.0))
    opacity_clamped = max(0.0, min(opacity, 1.0))
    alpha_val = int(round(255 * opacity_clamped))
    if alpha_val <= 0:
        return None

    try:
        candidate = pick_font_path(font_path)
        if candidate:
            target_w = max(10, int(base_w * scale_clamped))
            font = _choose_font_for_width(candidate, text, target_w)
        else:
            font = ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    x0, y0, x1, y1 = font.getbbox(text)
    text_w = max(1, x1 - x0)
    text_h = max(1, y1 - y0)

    base_pad = max(2, int(min(base_w, base_h) * 0.01))
    shadow_offset = max(1, int(base_pad * 0.6))
    pad = base_pad + shadow_offset + 4

    layer = Image.new("RGBA", (text_w + pad * 2, text_h + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    ox, oy = (-x0 + pad, -y0 + pad)
    draw.text((ox + shadow_offset, oy + shadow_offset), text, font=font, fill=(0, 0, 0, alpha_val))
    r, g, b = parse_hex_color(color_hex, (255, 255, 255))
    draw.text((ox, oy), text, font=font, fill=(r, g, b, alpha_val))

    if rotation:
        layer = layer.rotate(rotation, resample=RESAMPLE_BICUBIC, expand=True)

    return layer


def build_text_watermark_layer(
    base_w: int,
    base_h: int,
    text: str,
    font_path: Optional[str],
    scale: float,
    opacity: float,
    rotation: float,
    color_hex: str,
) -> Optional[Image.Image]:
    return _build_text_layer_cached(
        int(base_w),
        int(base_h),
        text,
        font_path,
        round(float(scale), 4),
        round(float(opacity), 4),
        round(float(rotation), 2),
        (color_hex or "#FFFFFF").strip().upper(),
    )


@lru_cache(maxsize=16)
def _load_wm_source(wm_path: str, mtime_ns: int) -> Image.Image:
    with Image.open(wm_path) as m:
        return m.convert("RGBA")


@lru_cache(maxsize=64)
def _build_image_layer_cached(
    base_w: int,
    base_h: int,
    wm_path: str,
    mtime_ns: int,
    scale: float,
    opacity: float,
    rotation: float,
) -> Optional[Image.Image]:
    opacity_clamped = max(0.0, min(opacity, 1.0))
    if opacity_clamped <= 0:
        return None

    mark = _load_wm_source(wm_path, mtime_ns)
    target_w = max(1, int(base_w * max(0.02, min(scale, 1.0))))
    w, h = mark.size
    if w <= 0 or h <= 0:
        return None
    factor = target_w / float(w)
    new_size = (max(1, int(w * factor)), max(1, int(h * factor)))
    mark = mark.resize(new_size, RESAMPLE_LANCZOS)
    if rotation:
        mark = mark.rotate(rotation, resample=RESAMPLE_BICUBIC, expand=True)
    if opacity_clamped < 0.999:
        alpha = mark.getchannel("A")
        lut = [int(i * opacity_clamped) for i in range(256)]
        alpha = alpha.point(lut)
        mark.putalpha(alpha)
    return mark


def build_image_watermark_layer(
    base_w: int,
    base_h: int,
    wm_path: str,
    scale: float,
    opacity: float,
    rotation: float,
) -> Optional[Image.Image]:
    mtime_ns = os.stat(wm_path).st_mtime_ns
    return _build_image_layer_cached(
        int(base_w),
        int(base_h),
        wm_path,
        mtime_ns,
        round(float(scale), 4),
        round(float(opacity), 4),
        round(float(rotation), 2),
    )


def _composite_clipped(base: Image.Image, layer: Image.Image, x: int, y: int) -> None:
    """Composites RGBA layer onto base in-place, safely clipping out-of-bounds edges."""
    bw, bh = base.size
    lw, lh = layer.size

    ix0 = max(0, x)
    iy0 = max(0, y)
    ix1 = min(bw, x + lw)
    iy1 = min(bh, y + lh)
    if ix0 >= ix1 or iy0 >= iy1:
        return

    if ix0 == x and iy0 == y and ix1 == x + lw and iy1 == y + lh:
        patch = layer
    else:
        patch = layer.crop((ix0 - x, iy0 - y, ix1 - x, iy1 - y))

    if base.mode == "RGBA":
        base.alpha_composite(patch, dest=(ix0, iy0))
    else:
        base.paste(patch, (ix0, iy0), patch)


def paste_watermark_inplace(
    base: Image.Image,
    layer: Optional[Image.Image],
    position: str,
    margin: int,
    tile_gap: int,
) -> Image.Image:
    if layer is None:
        return base

    bw, bh = base.size
    lw, lh = layer.size
    if lw <= 0 or lh <= 0:
        return base

    if position == "tile":
        step_x = max(1, lw + max(1, tile_gap))
        step_y = max(1, lh + max(1, tile_gap))
        start_y = max(0, margin)
        for row_idx, y in enumerate(range(start_y, bh, step_y)):
            x_offset = margin if row_idx % 2 == 0 else margin - (step_x // 2)
            for x in range(x_offset, bw, step_x):
                _composite_clipped(base, layer, x, y)
        return base

    positions = {
        "top-left": (margin, margin),
        "top-right": (bw - lw - margin, margin),
        "bottom-left": (margin, bh - lh - margin),
        "bottom-right": (bw - lw - margin, bh - lh - margin),
        "center": ((bw - lw) // 2, (bh - lh) // 2),
    }
    x, y = positions.get(position, positions["bottom-right"])
    if lw <= bw:
        x = max(0, min(bw - lw, x))
    else:
        x = (bw - lw) // 2
    if lh <= bh:
        y = max(0, min(bh - lh, y))
    else:
        y = (bh - lh) // 2

    _composite_clipped(base, layer, x, y)
    return base


def resolve_output_format(src_ext: str, out_format: Optional[str]) -> Tuple[str, str]:
    desired = (out_format or "").strip().upper()
    if desired in ("", "KEEP"):
        ext = src_ext.lower()
        fmt = EXT_TO_FORMAT.get(ext, "PNG")
        return fmt, (ext if ext in EXT_TO_FORMAT else ".png")
    ext = FORMAT_TO_EXT.get(desired, ".png")
    return desired, ext


def _num(val: Any, default: float) -> float:
    return float(default if val is None else val)


def _int_num(val: Any, default: int) -> int:
    return int(default if val is None else val)


def _has_transparency(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)


def _apply_watermark_pil(
    in_path: str,
    tmpl: Mapping[str, Any],
    fmt: str,
    preserve_exif: bool = True,
) -> Tuple[Image.Image, Optional[bytes], Optional[bytes]]:
    ttype = tmpl.get("type", "text")
    raw_text = str(tmpl.get("text") or "WATERMARK")

    with Image.open(in_path) as raw_img:
        icc_profile = raw_img.info.get("icc_profile") if preserve_exif else None
        exif_dt = get_exif_datetime(raw_img) if (ttype != "image" and "{date" in raw_text) else None
        img = ImageOps.exif_transpose(raw_img)

        exif_bytes: Optional[bytes] = None
        if preserve_exif and fmt in ("JPEG", "WEBP", "TIFF"):
            try:
                exif_obj = raw_img.getexif()
                if exif_obj:
                    if EXIF_ORIENTATION_TAG in exif_obj:
                        exif_obj[EXIF_ORIENTATION_TAG] = 1
                    exif_bytes = exif_obj.tobytes()
            except Exception:
                exif_bytes = None

        keep_alpha = _has_transparency(img) and fmt in ("PNG", "WEBP", "TIFF")
        target_mode = "RGBA" if keep_alpha else "RGB"
        if img.mode != target_mode:
            img = img.convert(target_mode)
        else:
            img.load()

    scale = _num(tmpl.get("scale"), 0.2)
    opacity = _num(tmpl.get("opacity"), 0.25)
    position = tmpl.get("position") or "bottom-right"
    rotation = _num(tmpl.get("rotation"), 0.0)
    margin = _int_num(tmpl.get("margin"), max(8, int(min(img.size) * 0.02)))
    tile_gap = _int_num(tmpl.get("tile_gap"), int(min(img.size) * 0.1))

    if ttype == "image":
        wm_path = tmpl.get("image_path")
        if not wm_path or not Path(wm_path).exists():
            raise FileNotFoundError("Watermark image not found")
        layer = build_image_watermark_layer(img.width, img.height, wm_path, scale, opacity, rotation)
    else:
        text = expand_text_macros(raw_text, in_path, exif_dt=exif_dt)
        font_path = tmpl.get("font_path")
        color_hex = tmpl.get("text_color") or "#FFFFFF"
        layer = build_text_watermark_layer(img.width, img.height, text, font_path, scale, opacity, rotation, color_hex)

    paste_watermark_inplace(img, layer, position, margin, tile_gap)
    return img, exif_bytes, icc_profile


def apply_watermark_to_file(
    in_path: str,
    out_path: str,
    tmpl: Mapping[str, Any],
    out_format: str,
    quality: int,
    preserve_exif: bool = True,
) -> Tuple[str, Optional[str]]:
    try:
        fmt, _ext = resolve_output_format(Path(in_path).suffix, out_format)
        composited, exif_bytes, icc_profile = _apply_watermark_pil(in_path, tmpl, fmt, preserve_exif=preserve_exif)

        save_kwargs: Dict[str, Any] = {}
        if fmt in ("JPEG", "WEBP"):
            save_kwargs["quality"] = max(1, min(100, int(quality if quality is not None else 90)))
        if fmt == "JPEG" and composited.mode != "RGB":
            composited = composited.convert("RGB")
        if fmt == "BMP" and composited.mode not in ("RGB", "L"):
            composited = composited.convert("RGB")
        if exif_bytes and fmt in ("JPEG", "WEBP", "TIFF"):
            save_kwargs["exif"] = exif_bytes
        if icc_profile and fmt in ("JPEG", "WEBP", "PNG", "TIFF"):
            save_kwargs["icc_profile"] = icc_profile

        composited.save(out_path, fmt, **save_kwargs)
        return out_path, None
    except Exception as e:
        return out_path, str(e)


# -----------------------------
# Fast directory scanning
# -----------------------------


def iter_images(input_dir: Path, exclude_dir: Optional[Path] = None) -> List[Path]:
    exclude_resolved = exclude_dir.resolve() if exclude_dir is not None else None
    results: List[str] = []

    def _walk(cur: str) -> None:
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    name = entry.name
                    if name.startswith(".") or name in SKIP_DIR_NAMES:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if exclude_resolved is not None:
                            try:
                                if Path(entry.path).resolve() == exclude_resolved:
                                    continue
                            except Exception:
                                pass
                        _walk(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        dot = name.rfind(".")
                        if dot != -1 and name[dot:].lower() in ALLOWED_IMG_EXT:
                            results.append(entry.path)
        except OSError:
            pass

    _walk(str(input_dir))
    results.sort()
    return [Path(p) for p in results]


def _resolve_template_payload(
    template_name: Optional[str],
    inline_template: Optional[str],
    templates_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    tmap = templates_map if templates_map is not None else load_templates()
    tname = (template_name or "").strip()
    saved = tmap.get(tname) if tname else None

    if inline_template:
        try:
            inline_data = json.loads(inline_template)
        except Exception:
            raise HTTPException(status_code=400, detail="inline_template must be valid JSON")
        if not isinstance(inline_data, dict):
            raise HTTPException(status_code=400, detail="inline_template must be a JSON object")
        merged = dict(inline_data)
        if saved and isinstance(saved, dict):
            if merged.get("type") == "image" and not merged.get("image_path") and saved.get("image_path"):
                merged["image_path"] = saved["image_path"]
            if merged.get("type", "text") == "text" and not merged.get("font_path") and saved.get("font_path"):
                merged["font_path"] = saved["font_path"]
        return merged

    if saved:
        return dict(saved)
    if tname:
        raise HTTPException(status_code=400, detail=f"Template '{tname}' not found")
    raise HTTPException(status_code=400, detail="Provide template_name or inline_template")


# -----------------------------
# Job helpers & unified batch runner
# -----------------------------


def _job_snapshot(job_id: str) -> Optional[Dict[str, Any]]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        now = time.monotonic()
        started_at = job["started_at"]
        finished_at = job.get("finished_at")
        elapsed = max(0.0, (finished_at or now) - started_at)
        done = job["done"]
        total = job["total"]
        rate = (done / elapsed) if elapsed > 0.2 and done > 0 else 0.0
        remaining = max(0, total - done)
        eta_sec = (remaining / rate) if rate > 0 and job["state"] in ("queued", "running") else 0.0

        return {
            "job_id": job_id,
            "state": job["state"],
            "save_mode": job["save_mode"],
            "total": total,
            "done": done,
            "processed": job["processed"],
            "skipped": job["skipped"],
            "errors": job["errors"],
            "error_samples": list(job["error_samples"]),
            "out_dir": job["out_dir"],
            "zip_ready": bool(job.get("zip_path") and Path(job["zip_path"]).exists()),
            "elapsed_sec": round(elapsed, 2),
            "rate": round(rate, 2),
            "eta_sec": round(eta_sec, 1),
        }


def _open_folder_native(path: Path) -> None:
    try:
        sysname = platform.system().lower()
        if sysname.startswith("win"):
            subprocess.Popen(["explorer", str(path)])
        elif sysname == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def _cleanup_old_zips() -> None:
    """Remove temp zips from finished jobs older than 1 hour if not active."""
    cutoff = time.monotonic() - 3600
    with JOBS_LOCK:
        for j in JOBS.values():
            zp = j.get("zip_path")
            fin = j.get("finished_at")
            if zp and fin and fin < cutoff:
                try:
                    Path(zp).unlink(missing_ok=True)
                except Exception:
                    pass
                j["zip_path"] = None


def _run_batch_job(
    job_id: str,
    files: List[Path],
    base: Path,
    out_root: Optional[Path],
    tdata: Dict[str, Any],
    output_format: str,
    quality: int,
    overwrite: bool,
    open_when_done: bool,
    save_mode: str,
    workers: int,
    preserve_exif: bool,
) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        cancel_ev: threading.Event = job["cancel"]
        job["state"] = "running"
        job["started_at"] = time.monotonic()

    created_dirs: set[Path] = set()
    workdir: Optional[Path] = None
    zip_path: Optional[Path] = None
    zf: Optional[zipfile.ZipFile] = None

    try:
        if save_mode == "ZIP":
            workdir = Path(tempfile.mkdtemp(prefix="wmk_zip_"))
            zip_out_root = workdir / "out"
            zip_out_root.mkdir(parents=True, exist_ok=True)
            zip_path = Path(tempfile.gettempdir()) / f"watermarked_{job_id}.zip"
            zf = zipfile.ZipFile(zip_path, mode="w", allowZip64=True)
            target_root = zip_out_root
        else:
            assert out_root is not None
            out_root.mkdir(parents=True, exist_ok=True)
            target_root = out_root

        max_workers = max(1, min(64, workers))
        max_in_flight = max(8, max_workers * 4)

        file_iter = iter(files)
        exhausted = False
        pending: Dict[Any, Tuple[Path, str]] = {}

        with EXECUTOR(max_workers=max_workers) as pool:
            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["pool"] = pool

            def _record_result(src_path: Path, dst_path_str: str, err: Optional[str]) -> None:
                if zf is not None and not err and os.path.exists(dst_path_str):
                    arcname = os.path.relpath(dst_path_str, target_root)
                    ext = Path(dst_path_str).suffix.lower()
                    comp = zipfile.ZIP_STORED if ext in COMPRESSED_IMG_EXT else zipfile.ZIP_DEFLATED
                    zf.write(dst_path_str, arcname, compress_type=comp)
                    try:
                        os.unlink(dst_path_str)
                    except OSError:
                        pass

                with JOBS_LOCK:
                    j = JOBS.get(job_id)
                    if not j:
                        return
                    j["done"] += 1
                    if err:
                        j["errors"] += 1
                        if len(j["error_samples"]) < 100:
                            j["error_samples"].append({"file": str(src_path), "error": err})
                    else:
                        j["processed"] += 1

            while not cancel_ev.is_set():
                while not exhausted and len(pending) < max_in_flight and not cancel_ev.is_set():
                    try:
                        src = next(file_iter)
                    except StopIteration:
                        exhausted = True
                        break

                    rel = src.relative_to(base)
                    _fmt, target_ext = resolve_output_format(src.suffix, output_format)
                    dst = (target_root / rel).with_suffix(target_ext)

                    if dst.parent not in created_dirs:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        created_dirs.add(dst.parent)

                    if save_mode == "DIR" and (not overwrite) and dst.exists():
                        with JOBS_LOCK:
                            j = JOBS.get(job_id)
                            if j:
                                j["done"] += 1
                                j["skipped"] += 1
                        continue

                    fu = pool.submit(
                        apply_watermark_to_file,
                        str(src),
                        str(dst),
                        tdata,
                        output_format,
                        int(quality),
                        preserve_exif,
                    )
                    pending[fu] = (src, str(dst))

                if not pending:
                    if exhausted:
                        break
                    continue

                done_set, _ = wait(pending.keys(), timeout=0.2, return_when=FIRST_COMPLETED)
                for fu in done_set:
                    src_p, dst_str = pending.pop(fu)
                    try:
                        out_p, err = fu.result()
                    except Exception as exc:
                        out_p, err = dst_str, str(exc)
                    _record_result(src_p, out_p, err)

            # Cancel remaining futures if stopped early
            if cancel_ev.is_set() and pending:
                for fu in list(pending.keys()):
                    fu.cancel()
                for fu, (src_p, dst_str) in list(pending.items()):
                    if not fu.cancelled():
                        try:
                            out_p, err = fu.result()
                            _record_result(src_p, out_p, err)
                        except Exception:
                            pass
                pending.clear()
    finally:
        if zf is not None:
            try:
                zf.close()
            except Exception:
                pass
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)

        cancelled = cancel_ev.is_set()
        if cancelled and zip_path is not None:
            try:
                zip_path.unlink(missing_ok=True)
            except Exception:
                pass
            zip_path = None

        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if j:
                j["pool"] = None
                j["zip_path"] = str(zip_path) if zip_path else None
                j["finished_at"] = time.monotonic()
                j["state"] = "cancelled" if cancelled else "done"

        if (not cancelled) and save_mode == "DIR" and open_when_done and out_root is not None:
            _open_folder_native(out_root)


def _create_and_start_job(
    input_dir: str,
    template_name: Optional[str],
    inline_template: Optional[str],
    output_format: str,
    quality: int,
    output_dir: Optional[str],
    overwrite: bool,
    open_when_done: bool,
    save_mode: str = "DIR",
    workers: int = 0,
    preserve_exif: bool = True,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    _cleanup_old_zips()

    base = Path(input_dir.strip())
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=400, detail="input_dir does not exist or is not a directory")

    mode = (save_mode or "DIR").strip().upper()
    if mode not in ("DIR", "ZIP"):
        raise HTTPException(status_code=400, detail="save_mode must be DIR or ZIP")

    out_root: Optional[Path] = None
    if mode == "DIR":
        if not output_dir or not output_dir.strip():
            raise HTTPException(status_code=400, detail="output_dir is required for DIR mode")
        out_root = Path(output_dir.strip())
        try:
            if out_root.resolve() == base.resolve() and overwrite:
                raise HTTPException(
                    status_code=400,
                    detail="Destination folder cannot be the same as source folder when Overwrite is enabled.",
                )
        except HTTPException:
            raise
        except Exception:
            pass

    tdata = _resolve_template_payload(template_name, inline_template)
    files = iter_images(base, exclude_dir=out_root)
    if not files:
        raise HTTPException(status_code=400, detail="No images found in input_dir")

    worker_count = workers if workers and workers > 0 else (os.cpu_count() or 4)
    jid = (job_id or uuid.uuid4().hex).strip()
    now = time.monotonic()

    with JOBS_LOCK:
        JOBS[jid] = {
            "state": "queued",
            "save_mode": mode,
            "total": len(files),
            "done": 0,
            "processed": 0,
            "skipped": 0,
            "errors": 0,
            "error_samples": [],
            "out_dir": str(out_root) if out_root else "ZIP Archive",
            "zip_path": None,
            "started_at": now,
            "finished_at": None,
            "cancel": threading.Event(),
            "pool": None,
        }

    threading.Thread(
        target=_run_batch_job,
        args=(
            jid,
            files,
            base,
            out_root,
            tdata,
            output_format,
            int(quality),
            bool(overwrite),
            bool(open_when_done),
            mode,
            int(worker_count),
            bool(preserve_exif),
        ),
        daemon=True,
    ).start()

    return {"job_id": jid, "total": len(files)}


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
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Template name is required")

    t = load_templates()
    safe_stem = _safe_filename_stem(clean_name)

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
            old = t.get(clean_name, {})
            if "image_path" not in old:
                raise HTTPException(status_code=400, detail="Upload watermark_image for image template")
            data["image_path"] = old["image_path"]
        else:
            ext = os.path.splitext(watermark_image.filename or "wm.png")[1].lower()
            if ext not in ALLOWED_IMG_EXT:
                raise HTTPException(status_code=400, detail="Unsupported watermark image type")
            dest = (IMAGES_DIR / f"{safe_stem}{ext}").resolve()
            dest.write_bytes(await watermark_image.read())
            data["image_path"] = str(dest)
    else:
        data["text"] = text if text is not None else "WATERMARK"
        if text_color:
            data["text_color"] = text_color
        if font_file is not None:
            fext = os.path.splitext(font_file.filename or "font.ttf")[1].lower()
            if fext not in (".ttf", ".otf", ".ttc"):
                raise HTTPException(status_code=400, detail="Unsupported font type")
            dest = (FONTS_DIR / f"{safe_stem}{fext}").resolve()
            dest.write_bytes(await font_file.read())
            data["font_path"] = str(dest)
        else:
            old = t.get(clean_name, {})
            if "font_path" in old:
                data["font_path"] = old["font_path"]

    t[clean_name] = {**t.get(clean_name, {}), **data}
    save_templates(t)
    return {"ok": True, "saved": clean_name, "template": t[clean_name]}


@app.delete("/api/templates/{name}")
async def delete_template(name: str):
    t = load_templates()
    if name not in t:
        raise HTTPException(status_code=404, detail="Template not found")
    del t[name]
    save_templates(t)
    return {"ok": True}


@app.post("/api/pick_folder")
async def pick_folder():
    """Open native OS folder picker when running locally."""
    sysname = platform.system().lower()
    try:
        if sysname == "darwin":
            proc = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                'POSIX path of (choose folder with prompt "Select folder")',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            if proc.returncode != 0:
                msg = err.decode(errors="ignore").strip()
                if "User canceled" in msg or "-128" in msg:
                    return {"cancelled": True, "path": None}
                raise HTTPException(status_code=500, detail=msg or "Folder picker failed")
            return {"cancelled": False, "path": out.decode().strip().rstrip("/")}

        if sysname.startswith("win"):
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
                "if($d.ShowDialog() -eq 'OK'){Write-Output $d.SelectedPath}"
            )
            proc = await asyncio.create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-Command",
                ps_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            chosen = out.decode(errors="ignore").strip()
            return {"cancelled": not bool(chosen), "path": chosen or None}

        for cmd in (
            ["zenity", "--file-selection", "--directory"],
            ["kdialog", "--getexistingdirectory"],
        ):
            if shutil.which(cmd[0]):
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, _ = await proc.communicate()
                chosen = out.decode(errors="ignore").strip()
                return {"cancelled": proc.returncode != 0 or not chosen, "path": chosen or None}

        raise HTTPException(status_code=501, detail="Native folder picker is not available in this environment")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/scan_local")
async def scan_local(
    input_dir: str = Form(...),
    output_dir: Optional[str] = Form(None),
):
    base = Path(input_dir.strip())
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=400, detail="input_dir does not exist or is not a directory")

    exclude_dir = Path(output_dir.strip()) if output_dir and output_dir.strip() else None
    t0 = time.perf_counter()
    files = iter_images(base, exclude_dir=exclude_dir)
    scan_ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "count": len(files),
        "scan_ms": scan_ms,
        "sample": [str(p.relative_to(base)) for p in files[:5]],
    }


@app.post("/api/preview_local")
async def preview_local(
    input_dir: str = Form(...),
    template_name: Optional[str] = Form(None),
    inline_template: Optional[str] = Form(None),
    index: int = Form(0),
    preview_max_px: int = Form(1400),
    show_original: bool = Form(False),
    watermark_image: Optional[UploadFile] = File(None),
    font_file: Optional[UploadFile] = File(None),
):
    base = Path(input_dir.strip())
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=400, detail="input_dir does not exist or is not a directory")

    tdata = _resolve_template_payload(template_name, inline_template)

    files = iter_images(base)
    if not files:
        raise HTTPException(status_code=400, detail="No images found in input_dir")

    total = len(files)
    idx = max(0, min(index, total - 1))
    target = files[idx]

    workdir = Path(tempfile.mkdtemp(prefix="wmk_prev_local_"))
    try:
        if watermark_image is not None:
            ext = os.path.splitext(watermark_image.filename or "wm.png")[1].lower()
            if ext in ALLOWED_IMG_EXT:
                tmp_wm = workdir / f"wm{ext}"
                tmp_wm.write_bytes(await watermark_image.read())
                tdata["image_path"] = str(tmp_wm)

        if font_file is not None:
            fext = os.path.splitext(font_file.filename or "font.ttf")[1].lower()
            if fext in (".ttf", ".otf", ".ttc"):
                tmp_font = workdir / f"font{fext}"
                tmp_font.write_bytes(await font_file.read())
                tdata["font_path"] = str(tmp_font)

        if show_original:
            with Image.open(target) as raw_img:
                img = ImageOps.exif_transpose(raw_img)
                if img.mode != "RGB":
                    img = img.convert("RGB")
        else:
            img, _, _ = _apply_watermark_pil(str(target), tdata, "JPEG", preserve_exif=False)

        orig_w, orig_h = img.size
        max_dim = max(320, min(2400, int(preview_max_px)))
        if orig_w > max_dim or orig_h > max_dim:
            img.thumbnail((max_dim, max_dim), RESAMPLE_LANCZOS)

        buf = io.BytesIO()
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=88)
        buf.seek(0)

        headers = {
            "X-Total-Images": str(total),
            "X-Image-Index": str(idx),
            "X-Image-Name": target.name,
            "X-Image-Width": str(orig_w),
            "X-Image-Height": str(orig_h),
            "Cache-Control": "no-store",
        }
        return StreamingResponse(buf, media_type="image/jpeg", headers=headers)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/api/process_local")
async def process_local(
    input_dir: str = Form(...),
    template_name: Optional[str] = Form(None),
    inline_template: Optional[str] = Form(None),
    output_format: str = Form("KEEP"),
    quality: int = Form(90),
):
    """Legacy synchronous ZIP endpoint kept for API compatibility."""
    base = Path(input_dir.strip())
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=400, detail="input_dir does not exist or is not a directory")

    tdata = _resolve_template_payload(template_name, inline_template)
    files = iter_images(base)
    if not files:
        raise HTTPException(status_code=400, detail="No images found in input_dir")

    temp_zip = Path(tempfile.gettempdir()) / f"watermarked_{uuid.uuid4().hex}.zip"
    workdir = Path(tempfile.mkdtemp(prefix="wmk_zip_"))
    out_root = workdir / "out"
    out_root.mkdir(parents=True, exist_ok=True)

    max_workers = max(1, os.cpu_count() or 4)
    try:
        with zipfile.ZipFile(temp_zip, mode="w", allowZip64=True) as zf:
            with EXECUTOR(max_workers=max_workers) as pool:
                pending = set()
                created_dirs: set[Path] = set()
                for src in files:
                    rel = src.relative_to(base)
                    _fmt, target_ext = resolve_output_format(src.suffix, output_format)
                    dst = (out_root / rel).with_suffix(target_ext)
                    if dst.parent not in created_dirs:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        created_dirs.add(dst.parent)
                    pending.add(
                        pool.submit(
                            apply_watermark_to_file,
                            str(src),
                            str(dst),
                            tdata,
                            output_format,
                            int(quality),
                            True,
                        )
                    )
                    if len(pending) >= max_workers * 4:
                        done_set, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for fu in done_set:
                            outp, err = fu.result()
                            if not err and os.path.exists(outp):
                                arcname = os.path.relpath(outp, out_root)
                                comp = zipfile.ZIP_STORED if Path(outp).suffix.lower() in COMPRESSED_IMG_EXT else zipfile.ZIP_DEFLATED
                                zf.write(outp, arcname, compress_type=comp)
                                os.unlink(outp)

                while pending:
                    done_set, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for fu in done_set:
                        outp, err = fu.result()
                        if not err and os.path.exists(outp):
                            arcname = os.path.relpath(outp, out_root)
                            comp = zipfile.ZIP_STORED if Path(outp).suffix.lower() in COMPRESSED_IMG_EXT else zipfile.ZIP_DEFLATED
                            zf.write(outp, arcname, compress_type=comp)
                            os.unlink(outp)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return FileResponse(
        path=str(temp_zip),
        media_type="application/zip",
        filename=temp_zip.name,
        background=BackgroundTask(lambda p=temp_zip: p.unlink(missing_ok=True)),
    )


@app.post("/api/process_local_start")
async def process_local_start(payload: Dict[str, Any] = Body(...)):
    return _create_and_start_job(
        input_dir=str(payload.get("input_dir") or ""),
        template_name=payload.get("template_name"),
        inline_template=payload.get("inline_template"),
        output_format=str(payload.get("output_format") or "KEEP"),
        quality=int(payload.get("quality", 90)),
        output_dir=payload.get("output_dir"),
        overwrite=bool(payload.get("overwrite", True)),
        open_when_done=bool(payload.get("open_when_done", False)),
        save_mode=str(payload.get("save_mode") or "DIR"),
        workers=int(payload.get("workers") or 0),
        preserve_exif=bool(payload.get("preserve_exif", True)),
    )


async def _sse_event_stream(job_id: str):
    snap = _job_snapshot(job_id)
    if not snap:
        yield f"event: error\ndata: {json.dumps({'detail': 'Job not found'})}\n\n"
        return

    yield f"event: start\ndata: {json.dumps(snap)}\n\n"
    last_done = -1
    last_state = None

    while True:
        snap = _job_snapshot(job_id)
        if not snap:
            break
        if snap["done"] != last_done or snap["state"] != last_state:
            last_done = snap["done"]
            last_state = snap["state"]
            if snap["state"] in ("done", "cancelled"):
                payload = {
                    **snap,
                    "cancelled": snap["state"] == "cancelled",
                }
                yield f"event: done\ndata: {json.dumps(payload)}\n\n"
                break
            yield f"event: progress\ndata: {json.dumps(snap)}\n\n"
        await asyncio.sleep(0.15)


@app.get("/api/job_events")
async def job_events(job_id: str = Query(...)):
    if not _job_snapshot(job_id):
        raise HTTPException(status_code=404, detail="job_id not found")
    return StreamingResponse(
        _sse_event_stream(job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/download_job_zip/{job_id}")
async def download_job_zip(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        zp = job.get("zip_path")
    if not zp or not Path(zp).exists():
        raise HTTPException(status_code=404, detail="ZIP file is not available")

    p = Path(zp)

    def _cleanup() -> None:
        p.unlink(missing_ok=True)
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["zip_path"] = None

    return FileResponse(
        path=str(p),
        media_type="application/zip",
        filename=f"watermarked_{job_id[:8]}.zip",
        background=BackgroundTask(_cleanup),
    )


@app.post("/api/cancel_job")
async def cancel_job(payload: dict = Body(...)):
    job_id = str(payload.get("job_id", "")).strip()
    if not job_id:
        raise HTTPException(status_code=404, detail="Job not found")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        ev = job.get("cancel")
        if ev:
            ev.set()
        if job["state"] in ("queued", "running"):
            job["state"] = "cancelling"
    return {"ok": True}


@app.get("/api/process_local_sse")
async def process_local_sse(
    input_dir: str,
    template_name: Optional[str] = None,
    output_format: str = "KEEP",
    quality: int = 90,
    output_dir: Optional[str] = None,
    overwrite: bool = True,
    open_when_done: bool = False,
    job_id: Optional[str] = None,
    workers: int = 0,
    preserve_exif: bool = True,
):
    res = _create_and_start_job(
        input_dir=input_dir,
        template_name=template_name,
        inline_template=None,
        output_format=output_format,
        quality=quality,
        output_dir=output_dir,
        overwrite=overwrite,
        open_when_done=open_when_done,
        save_mode="DIR",
        workers=workers,
        preserve_exif=preserve_exif,
        job_id=job_id,
    )
    return StreamingResponse(
        _sse_event_stream(res["job_id"]),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/process_local_poll_start")
async def process_local_poll_start(payload: Dict[str, Any] = Body(...)):
    return await process_local_start(payload)


@app.get("/api/process_local_poll_status")
async def process_local_poll_status(job_id: str = Query(...)):
    snap = _job_snapshot(job_id)
    if not snap:
        raise HTTPException(status_code=404, detail="job_id not found")
    return snap


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
