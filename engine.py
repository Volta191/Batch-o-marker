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
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from PIL import ExifTags, Image, ImageDraw, ImageFont, ImageOps

# -----------------------------
# Paths / Storage (dev + PyInstaller)
# -----------------------------

IS_FROZEN = getattr(sys, "frozen", False)
SOURCE_DIR = Path(__file__).parent.resolve()
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR)).resolve()
STATIC_DIR = BUNDLE_DIR / "static"


def _default_user_data_dir() -> Path:
    sysname = platform.system().lower()
    if sysname.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home() / "AppData" / "Local")
        return base / "Batch-o-mark"
    if sysname == "darwin":
        return Path.home() / "Library" / "Application Support" / "Batch-o-mark"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".local" / "share")
    return base / "Batch-o-mark"


def _resolve_store_dir() -> Path:
    if not IS_FROZEN:
        return SOURCE_DIR / "templates_store"
    # Portable mode: if templates_store sits next to the executable and is writable, prefer it
    exe_dir = Path(sys.executable).resolve().parent
    portable = exe_dir / "templates_store"
    if portable.exists() and os.access(portable, os.W_OK):
        return portable
    return _default_user_data_dir() / "templates_store"


STORE_DIR = _resolve_store_dir()
IMAGES_DIR = STORE_DIR / "images"
FONTS_DIR = STORE_DIR / "fonts"
TEMPLATES_JSON = STORE_DIR / "templates.json"
BUNDLED_STORE_DIR = BUNDLE_DIR / "templates_store"
DEFAULT_FONT = FONTS_DIR / "DejaVuSans.ttf"
BUNDLED_DEFAULT_FONT = BUNDLED_STORE_DIR / "fonts" / "DejaVuSans.ttf"

SYSTEM_FONTS_CANDIDATES = [
    DEFAULT_FONT,
    BUNDLED_DEFAULT_FONT,
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("C:/Windows/Fonts/segoeui.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
    Path("/Library/Fonts/Arial.ttf"),
    Path.home() / ".fonts" / "DejaVuSans.ttf",
]

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

try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
    RESAMPLE_BICUBIC = Image.Resampling.BICUBIC
except AttributeError:
    RESAMPLE_LANCZOS = Image.LANCZOS
    RESAMPLE_BICUBIC = Image.BICUBIC


# -----------------------------
# Store helpers
# -----------------------------


def ensure_store() -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    FONTS_DIR.mkdir(parents=True, exist_ok=True)

    # Seed bundled fonts & initial templates.json if running frozen with a fresh user data dir
    if BUNDLED_STORE_DIR.exists() and BUNDLED_STORE_DIR.resolve() != STORE_DIR.resolve():
        bundled_fonts = BUNDLED_STORE_DIR / "fonts"
        if bundled_fonts.exists():
            for f in bundled_fonts.iterdir():
                if f.is_file():
                    dst = FONTS_DIR / f.name
                    if not dst.exists():
                        try:
                            shutil.copy2(f, dst)
                        except OSError:
                            pass
        bundled_tpls = BUNDLED_STORE_DIR / "templates.json"
        if not TEMPLATES_JSON.exists() and bundled_tpls.exists():
            try:
                shutil.copy2(bundled_tpls, TEMPLATES_JSON)
            except OSError:
                pass

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


def safe_filename_stem(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", name.strip()).strip("._")
    return cleaned or f"tpl_{uuid.uuid4().hex[:8]}"


def save_template_entry(
    name: str,
    data: Dict[str, Any],
    watermark_src_path: Optional[str] = None,
    watermark_bytes: Optional[bytes] = None,
    watermark_ext: Optional[str] = None,
    font_src_path: Optional[str] = None,
    font_bytes: Optional[bytes] = None,
    font_ext: Optional[str] = None,
) -> Dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Template name is required")

    ensure_store()
    t = load_templates()
    old = t.get(clean_name, {}) if isinstance(t.get(clean_name), dict) else {}
    safe_stem = safe_filename_stem(clean_name)

    entry: Dict[str, Any] = {
        "type": data.get("type", "text"),
        "scale": float(data.get("scale", 0.2)),
        "opacity": float(data.get("opacity", 0.25)),
        "position": data.get("position", "bottom-right"),
        "rotation": float(data.get("rotation", 0.0)),
        "margin": int(data.get("margin", 16)),
        "tile_gap": int(data.get("tile_gap", 80)),
    }

    if entry["type"] == "image":
        if watermark_bytes is not None:
            ext = (watermark_ext or ".png").lower()
            if ext not in ALLOWED_IMG_EXT:
                raise ValueError("Unsupported watermark image type")
            dest = (IMAGES_DIR / f"{safe_stem}{ext}").resolve()
            dest.write_bytes(watermark_bytes)
            entry["image_path"] = str(dest)
        elif watermark_src_path:
            src = Path(watermark_src_path)
            ext = src.suffix.lower()
            if ext not in ALLOWED_IMG_EXT:
                raise ValueError("Unsupported watermark image type")
            dest = (IMAGES_DIR / f"{safe_stem}{ext}").resolve()
            if src.resolve() != dest:
                shutil.copy2(src, dest)
            entry["image_path"] = str(dest)
        elif data.get("image_path"):
            entry["image_path"] = data["image_path"]
        elif old.get("image_path"):
            entry["image_path"] = old["image_path"]
        else:
            raise ValueError("Select a watermark image for image template")
    else:
        entry["text"] = data.get("text") if data.get("text") is not None else "WATERMARK"
        entry["text_color"] = data.get("text_color") or "#FFFFFF"
        if font_bytes is not None:
            fext = (font_ext or ".ttf").lower()
            if fext not in (".ttf", ".otf", ".ttc"):
                raise ValueError("Unsupported font type")
            dest = (FONTS_DIR / f"{safe_stem}{fext}").resolve()
            dest.write_bytes(font_bytes)
            entry["font_path"] = str(dest)
        elif font_src_path:
            src = Path(font_src_path)
            fext = src.suffix.lower()
            if fext not in (".ttf", ".otf", ".ttc"):
                raise ValueError("Unsupported font type")
            dest = (FONTS_DIR / f"{safe_stem}{fext}").resolve()
            if src.resolve() != dest:
                shutil.copy2(src, dest)
            entry["font_path"] = str(dest)
        elif "font_path" in data:
            if data["font_path"]:
                entry["font_path"] = data["font_path"]
        elif old.get("font_path"):
            entry["font_path"] = old["font_path"]

    t[clean_name] = entry
    save_templates(t)
    return entry


def delete_template_entry(name: str) -> bool:
    t = load_templates()
    if name not in t:
        return False
    del t[name]
    save_templates(t)
    return True


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
            for root in (STORE_DIR.parent, BUNDLE_DIR, SOURCE_DIR):
                cand = (root / up).resolve()
                if cand.exists():
                    return str(cand)
        elif up.exists():
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


def render_preview_pil(
    in_path: str,
    tmpl: Mapping[str, Any],
    max_px: int = 1800,
    show_original: bool = False,
) -> Tuple[Image.Image, int, int]:
    if show_original:
        with Image.open(in_path) as raw_img:
            img = ImageOps.exif_transpose(raw_img)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            else:
                img.load()
    else:
        img, _, _ = _apply_watermark_pil(in_path, tmpl, "PNG", preserve_exif=False)

    orig_w, orig_h = img.size
    if max_px > 0 and (orig_w > max_px or orig_h > max_px):
        img.thumbnail((max_px, max_px), RESAMPLE_LANCZOS)
    return img, orig_w, orig_h


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


def open_folder_native(path: Path) -> None:
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


# -----------------------------
# Unified sliding-window batch runner
# -----------------------------


def execute_batch_job(
    files: List[Path],
    base: Path,
    out_root: Optional[Path],
    tdata: Dict[str, Any],
    output_format: str = "KEEP",
    quality: int = 90,
    overwrite: bool = True,
    open_when_done: bool = False,
    save_mode: str = "DIR",
    workers: int = 0,
    preserve_exif: bool = True,
    zip_dest_path: Optional[Path] = None,
    cancel_ev: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    executor_cls: Any = ProcessPoolExecutor,
) -> Dict[str, Any]:
    if cancel_ev is None:
        cancel_ev = threading.Event()

    started_at = time.monotonic()
    total = len(files)
    done = 0
    processed = 0
    skipped = 0
    errors = 0
    error_samples: List[Dict[str, str]] = []

    mode = (save_mode or "DIR").strip().upper()
    created_dirs: set[Path] = set()
    workdir: Optional[Path] = None
    zip_path: Optional[Path] = zip_dest_path
    zf: Optional[zipfile.ZipFile] = None

    last_emit = 0.0

    def _emit(force: bool = False, state: str = "running") -> Dict[str, Any]:
        nonlocal last_emit
        now = time.monotonic()
        elapsed = max(0.0, now - started_at)
        rate = (done / elapsed) if elapsed > 0.2 and done > 0 else 0.0
        remaining = max(0, total - done)
        eta_sec = (remaining / rate) if rate > 0 and state == "running" else 0.0
        snap = {
            "state": state,
            "save_mode": mode,
            "total": total,
            "done": done,
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "error_samples": list(error_samples),
            "out_dir": str(zip_path or out_root or ""),
            "zip_path": str(zip_path) if zip_path else None,
            "elapsed_sec": round(elapsed, 2),
            "rate": round(rate, 2),
            "eta_sec": round(eta_sec, 1),
        }
        if on_progress and (force or (now - last_emit) >= 0.12):
            last_emit = now
            on_progress(snap)
        return snap

    _emit(force=True, state="running")

    try:
        if mode == "ZIP":
            workdir = Path(tempfile.mkdtemp(prefix="wmk_zip_"))
            zip_out_root = workdir / "out"
            zip_out_root.mkdir(parents=True, exist_ok=True)
            if zip_path is None:
                zip_path = Path(tempfile.gettempdir()) / f"watermarked_{uuid.uuid4().hex}.zip"
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            zf = zipfile.ZipFile(zip_path, mode="w", allowZip64=True)
            target_root = zip_out_root
        else:
            if out_root is None:
                raise ValueError("out_root is required for DIR mode")
            out_root.mkdir(parents=True, exist_ok=True)
            target_root = out_root

        cpu_cnt = os.cpu_count() or 4
        max_workers = max(1, min(64, workers if workers and workers > 0 else cpu_cnt))
        max_in_flight = max(8, max_workers * 4)

        file_iter = iter(files)
        exhausted = False
        pending: Dict[Any, Tuple[Path, str]] = {}

        with executor_cls(max_workers=max_workers) as pool:
            def _record(src_p: Path, dst_str: str, err: Optional[str]) -> None:
                nonlocal done, processed, errors
                if zf is not None and not err and os.path.exists(dst_str):
                    arcname = os.path.relpath(dst_str, target_root)
                    ext = Path(dst_str).suffix.lower()
                    comp = zipfile.ZIP_STORED if ext in COMPRESSED_IMG_EXT else zipfile.ZIP_DEFLATED
                    zf.write(dst_str, arcname, compress_type=comp)
                    try:
                        os.unlink(dst_str)
                    except OSError:
                        pass

                done += 1
                if err:
                    errors += 1
                    if len(error_samples) < 100:
                        error_samples.append({"file": str(src_p), "error": err})
                else:
                    processed += 1

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

                    if mode == "DIR" and (not overwrite) and dst.exists():
                        done += 1
                        skipped += 1
                        _emit(force=False, state="running")
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
                    _record(src_p, out_p, err)
                _emit(force=False, state="running")

            if cancel_ev.is_set() and pending:
                for fu in list(pending.keys()):
                    fu.cancel()
                for fu, (src_p, dst_str) in list(pending.items()):
                    if not fu.cancelled():
                        try:
                            out_p, err = fu.result()
                            _record(src_p, out_p, err)
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
        if cancelled and zip_path is not None and zip_dest_path is None:
            try:
                zip_path.unlink(missing_ok=True)
            except Exception:
                pass
            zip_path = None

    final_state = "cancelled" if cancel_ev.is_set() else "done"
    if final_state == "done" and mode == "DIR" and open_when_done and out_root is not None:
        open_folder_native(out_root)
    return _emit(force=True, state=final_state)
