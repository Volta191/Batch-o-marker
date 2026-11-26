import asyncio
import json
import os
import platform
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

import engine

# Keep ThreadPoolExecutor only if frozen inside FastAPI web mode (desktop_app uses ProcessPoolExecutor via engine)
WEB_EXECUTOR = ThreadPoolExecutor if engine.IS_FROZEN else ProcessPoolExecutor

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

app = FastAPI(title="Batch-o-mark Web App")
if engine.STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(engine.STATIC_DIR)), name="static")


def _resolve_template_payload(
    template_name: Optional[str],
    inline_template: Optional[str],
    templates_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    tmap = templates_map if templates_map is not None else engine.load_templates()
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


def _cleanup_old_zips() -> None:
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


def _run_web_job(
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

    def _on_prog(snap: Dict[str, Any]) -> None:
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if not j:
                return
            j["done"] = snap["done"]
            j["processed"] = snap["processed"]
            j["skipped"] = snap["skipped"]
            j["errors"] = snap["errors"]
            j["error_samples"] = snap["error_samples"]

    res = engine.execute_batch_job(
        files=files,
        base=base,
        out_root=out_root,
        tdata=tdata,
        output_format=output_format,
        quality=quality,
        overwrite=overwrite,
        open_when_done=open_when_done,
        save_mode=save_mode,
        workers=workers,
        preserve_exif=preserve_exif,
        cancel_ev=cancel_ev,
        on_progress=_on_prog,
        executor_cls=WEB_EXECUTOR,
    )

    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if j:
            j["done"] = res["done"]
            j["processed"] = res["processed"]
            j["skipped"] = res["skipped"]
            j["errors"] = res["errors"]
            j["error_samples"] = res["error_samples"]
            j["zip_path"] = res.get("zip_path")
            j["finished_at"] = time.monotonic()
            j["state"] = res["state"]


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
    files = engine.iter_images(base, exclude_dir=out_root)
    if not files:
        raise HTTPException(status_code=400, detail="No images found in input_dir")

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
        }

    threading.Thread(
        target=_run_web_job,
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
            int(workers or 0),
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
    return (engine.STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/templates")
async def list_templates():
    return engine.load_templates()


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
    wm_bytes = await watermark_image.read() if watermark_image is not None else None
    wm_ext = os.path.splitext(watermark_image.filename or "wm.png")[1].lower() if watermark_image else None
    f_bytes = await font_file.read() if font_file is not None else None
    f_ext = os.path.splitext(font_file.filename or "font.ttf")[1].lower() if font_file else None

    try:
        entry = engine.save_template_entry(
            name=name,
            data={
                "type": type,
                "text": text,
                "scale": scale,
                "opacity": opacity,
                "position": position,
                "rotation": rotation,
                "margin": margin,
                "tile_gap": tile_gap,
                "text_color": text_color,
            },
            watermark_bytes=wm_bytes,
            watermark_ext=wm_ext,
            font_bytes=f_bytes,
            font_ext=f_ext,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "saved": name.strip(), "template": entry}


@app.delete("/api/templates/{name}")
async def delete_template(name: str):
    if not engine.delete_template_entry(name):
        raise HTTPException(status_code=404, detail="Template not found")
    return {"ok": True}


@app.post("/api/pick_folder")
async def pick_folder():
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
    files = engine.iter_images(base, exclude_dir=exclude_dir)
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
    import io

    base = Path(input_dir.strip())
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=400, detail="input_dir does not exist or is not a directory")

    tdata = _resolve_template_payload(template_name, inline_template)
    files = engine.iter_images(base)
    if not files:
        raise HTTPException(status_code=400, detail="No images found in input_dir")

    total = len(files)
    idx = max(0, min(index, total - 1))
    target = files[idx]

    workdir = Path(tempfile.mkdtemp(prefix="wmk_prev_local_"))
    try:
        if watermark_image is not None:
            ext = os.path.splitext(watermark_image.filename or "wm.png")[1].lower()
            if ext in engine.ALLOWED_IMG_EXT:
                tmp_wm = workdir / f"wm{ext}"
                tmp_wm.write_bytes(await watermark_image.read())
                tdata["image_path"] = str(tmp_wm)

        if font_file is not None:
            fext = os.path.splitext(font_file.filename or "font.ttf")[1].lower()
            if fext in (".ttf", ".otf", ".ttc"):
                tmp_font = workdir / f"font{fext}"
                tmp_font.write_bytes(await font_file.read())
                tdata["font_path"] = str(tmp_font)

        max_dim = max(320, min(2400, int(preview_max_px)))
        img, orig_w, orig_h = engine.render_preview_pil(
            str(target), tdata, max_px=max_dim, show_original=show_original
        )
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
    base = Path(input_dir.strip())
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=400, detail="input_dir does not exist or is not a directory")

    tdata = _resolve_template_payload(template_name, inline_template)
    files = engine.iter_images(base)
    if not files:
        raise HTTPException(status_code=400, detail="No images found in input_dir")

    res = engine.execute_batch_job(
        files=files,
        base=base,
        out_root=None,
        tdata=tdata,
        output_format=output_format,
        quality=int(quality),
        save_mode="ZIP",
        executor_cls=WEB_EXECUTOR,
    )
    temp_zip = Path(res["zip_path"])
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
                payload = {**snap, "cancelled": snap["state"] == "cancelled"}
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


if __name__ == "__main__":
    import multiprocessing as mp
    import webbrowser
    import uvicorn

    mp.freeze_support()
    port = int(os.environ.get("PORT", 8000))
    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
