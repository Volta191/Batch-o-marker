import multiprocessing as mp
import os
import random
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QSettings,
    Qt,
    QThread,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QKeySequence,
    QPainter,
    QPalette,
    QPixmap,
    QShortcut,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

import engine


def _format_duration(sec: float) -> str:
    s = max(0, int(round(sec)))
    h, rem = divmod(s, 3600)
    m, rem = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{rem:02d}"
    return f"{m:02d}:{rem:02d}"


def _pil_to_qimage(pil_img) -> QImage:
    if pil_img.mode == "RGBA":
        data = pil_img.tobytes("raw", "RGBA")
        qimg = QImage(data, pil_img.width, pil_img.height, pil_img.width * 4, QImage.Format.Format_RGBA8888)
        return qimg.copy()
    rgb = pil_img if pil_img.mode == "RGB" else pil_img.convert("RGB")
    data = rgb.tobytes("raw", "RGB")
    qimg = QImage(data, rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888)
    return qimg.copy()


# -----------------------------
# Background Workers (Qt Threads)
# -----------------------------


class _PreviewSignals(QObject):
    ready = Signal(int, QImage, str, int, int, int, int, bool)  # req_id, img, name, w, h, idx, total, show_orig
    failed = Signal(int, str)


class PreviewTask(QRunnable):
    def __init__(
        self,
        req_id: int,
        file_path: Path,
        tmpl: Dict[str, Any],
        idx: int,
        total: int,
        show_original: bool,
    ) -> None:
        super().__init__()
        self.req_id = req_id
        self.file_path = file_path
        self.tmpl = tmpl
        self.idx = idx
        self.total = total
        self.show_original = show_original
        self.signals = _PreviewSignals()

    def run(self) -> None:
        try:
            pil_img, orig_w, orig_h = engine.render_preview_pil(
                str(self.file_path),
                self.tmpl,
                max_px=2000,
                show_original=self.show_original,
            )
            qimg = _pil_to_qimage(pil_img)
            self.signals.ready.emit(
                self.req_id,
                qimg,
                self.file_path.name,
                orig_w,
                orig_h,
                self.idx,
                self.total,
                self.show_original,
            )
        except Exception as e:
            self.signals.failed.emit(self.req_id, str(e))


class ScanWorker(QThread):
    completed = Signal(list, float)
    failed = Signal(str)

    def __init__(self, input_dir: Path, exclude_dir: Optional[Path]) -> None:
        super().__init__()
        self.input_dir = input_dir
        self.exclude_dir = exclude_dir

    def run(self) -> None:
        try:
            import time

            t0 = time.perf_counter()
            files = engine.iter_images(self.input_dir, exclude_dir=self.exclude_dir)
            ms = round((time.perf_counter() - t0) * 1000.0, 1)
            self.completed.emit(files, ms)
        except Exception as e:
            self.failed.emit(str(e))


class BatchWorker(QThread):
    progress = Signal(dict)
    completed = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
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
        zip_dest_path: Optional[Path],
        cancel_ev: threading.Event,
    ) -> None:
        super().__init__()
        self.files = files
        self.base = base
        self.out_root = out_root
        self.tdata = tdata
        self.output_format = output_format
        self.quality = quality
        self.overwrite = overwrite
        self.open_when_done = open_when_done
        self.save_mode = save_mode
        self.workers = workers
        self.preserve_exif = preserve_exif
        self.zip_dest_path = zip_dest_path
        self.cancel_ev = cancel_ev

    def run(self) -> None:
        try:
            res = engine.execute_batch_job(
                files=self.files,
                base=self.base,
                out_root=self.out_root,
                tdata=self.tdata,
                output_format=self.output_format,
                quality=self.quality,
                overwrite=self.overwrite,
                open_when_done=self.open_when_done,
                save_mode=self.save_mode,
                workers=self.workers,
                preserve_exif=self.preserve_exif,
                zip_dest_path=self.zip_dest_path,
                cancel_ev=self.cancel_ev,
                on_progress=lambda snap: self.progress.emit(snap),
            )
            self.completed.emit(res)
        except Exception as e:
            self.failed.emit(str(e))


# -----------------------------
# Interactive Zoomable Preview Canvas
# -----------------------------


class PreviewCanvas(QGraphicsView):
    zoomChanged = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item = QGraphicsPixmapItem()
        self._scene.addItem(self._item)
        self._fit_mode = True
        self._has_image = False

        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setMinimumSize(400, 360)

        self._placeholder = self._scene.addText("Drop a photo folder here or click Browse…")
        self._placeholder.setDefaultTextColor(QColor("#8aa0b4"))

    def set_pixmap(self, pixmap: QPixmap, reset_fit: bool = False) -> None:
        if self._placeholder and self._placeholder.scene() is self._scene:
            self._scene.removeItem(self._placeholder)
            self._placeholder = None
        self._item.setPixmap(pixmap)
        self._scene.setSceneRect(self._item.boundingRect())
        self._has_image = True
        if reset_fit or self._fit_mode:
            self.fit_view()

    def fit_view(self) -> None:
        if not self._has_image:
            return
        self._fit_mode = True
        self.resetTransform()
        self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
        self.zoomChanged.emit("Fit")

    def zoom_actual(self) -> None:
        if not self._has_image:
            return
        self._fit_mode = False
        self.resetTransform()
        self.zoomChanged.emit("100%")

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self._has_image:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        self._fit_mode = False
        factor = 1.15 if delta > 0 else (1.0 / 1.15)
        self.scale(factor, factor)
        pct = round(self.transform().m11() * 100)
        self.zoomChanged.emit(f"{pct}%")

    def mouseDoubleClickEvent(self, event) -> None:
        if self._has_image:
            if self._fit_mode:
                self.zoom_actual()
            else:
                self.fit_view()
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._has_image and self._fit_mode:
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)


class FolderDropLineEdit(QLineEdit):
    folderDropped = Signal(str)

    def __init__(self, placeholder: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for u in event.mimeData().urls():
                p = Path(u.toLocalFile())
                if p.is_dir():
                    event.acceptProposedAction()
                    return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            for u in event.mimeData().urls():
                p = Path(u.toLocalFile())
                if p.is_dir():
                    self.setText(str(p))
                    self.folderDropped.emit(str(p))
                    event.acceptProposedAction()
                    return
        super().dropEvent(event)


# -----------------------------
# Main Window
# -----------------------------


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Batch-o-mark — Batch Photo Watermarker")
        self.setAcceptDrops(True)

        self.settings = QSettings("BatchOMark", "BatchOMark")
        self.templates: Dict[str, Any] = {}
        self.scanned_files: List[Path] = []
        self.preview_index: int = 0
        self.show_original: bool = False
        self.preview_req_id: int = 0
        self.zoom_label_text: str = "Fit"
        self.pending_wm_image_src: Optional[str] = None
        self.pending_font_src: Optional[str] = None

        self.thread_pool = QThreadPool.globalInstance()
        self.scan_worker: Optional[ScanWorker] = None
        self.batch_worker: Optional[BatchWorker] = None
        self.cancel_ev: Optional[threading.Event] = None

        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(260)
        self.preview_timer.timeout.connect(self.refresh_preview)

        self._loading_form = False
        self._build_ui()
        self._apply_theme(self.settings.value("theme", "dark"))
        self._load_templates()
        self._restore_settings()
        self._setup_shortcuts()

    # ---------- UI Construction ----------

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        # Header
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        h1 = QLabel("Batch-o-mark")
        h1.setObjectName("appTitle")
        subtitle = QLabel("Multi-core desktop photo watermarker (3,000–10,000+ photos) • Drag & drop folders anywhere")
        subtitle.setObjectName("appSubtitle")
        title_box.addWidget(h1)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)

        self.theme_btn = QPushButton("Theme: Dark")
        self.theme_btn.setToolTip("Switch Dark / Light theme (Alt+T)")
        self.theme_btn.clicked.connect(self.toggle_theme)
        header.addWidget(self.theme_btn)
        outer.addLayout(header)

        # Main Splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter, 1)

        # Left Panel (Scrollable)
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setMinimumWidth(420)
        left_container = QWidget()
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(2, 2, 8, 2)
        left_layout.setSpacing(12)

        left_layout.addWidget(self._build_templates_group())
        left_layout.addWidget(self._build_batch_group())
        left_layout.addStretch(1)
        left_scroll.setWidget(left_container)

        # Right Panel (Preview)
        right_panel = self._build_preview_group()

        splitter.addWidget(left_scroll)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([470, 760])

    def _build_templates_group(self) -> QGroupBox:
        box = QGroupBox("Templates")
        layout = QVBoxLayout(box)
        layout.setSpacing(8)

        # Selector bar
        sel_row = QHBoxLayout()
        self.tpl_combo = QComboBox()
        self.tpl_combo.currentTextChanged.connect(self._on_template_selected)
        self.new_tpl_btn = QPushButton("New")
        self.new_tpl_btn.clicked.connect(self._reset_editor)
        self.save_tpl_btn = QPushButton("Save")
        self.save_tpl_btn.setObjectName("primaryBtn")
        self.save_tpl_btn.clicked.connect(self._save_template)
        self.del_tpl_btn = QPushButton("Delete")
        self.del_tpl_btn.setObjectName("dangerBtn")
        self.del_tpl_btn.clicked.connect(self._delete_template)
        sel_row.addWidget(self.tpl_combo, 1)
        sel_row.addWidget(self.new_tpl_btn)
        sel_row.addWidget(self.save_tpl_btn)
        sel_row.addWidget(self.del_tpl_btn)
        layout.addLayout(sel_row)

        form = QFormLayout()
        form.setSpacing(7)

        self.tpl_name_edit = QLineEdit()
        self.tpl_name_edit.setPlaceholderText("e.g., studio_bottom_right")
        form.addRow("Name:", self.tpl_name_edit)

        type_pos_row = QHBoxLayout()
        self.tpl_type_combo = QComboBox()
        self.tpl_type_combo.addItem("Text", "text")
        self.tpl_type_combo.addItem("Image (PNG)", "image")
        self.tpl_type_combo.currentIndexChanged.connect(self._on_editor_changed)

        self.tpl_pos_combo = QComboBox()
        for label, val in [
            ("Bottom right", "bottom-right"),
            ("Bottom left", "bottom-left"),
            ("Top right", "top-right"),
            ("Top left", "top-left"),
            ("Center", "center"),
            ("Tile", "tile"),
        ]:
            self.tpl_pos_combo.addItem(label, val)
        self.tpl_pos_combo.currentIndexChanged.connect(self._on_editor_changed)
        type_pos_row.addWidget(self.tpl_type_combo, 1)
        type_pos_row.addWidget(self.tpl_pos_combo, 1)
        form.addRow("Type / Position:", type_pos_row)

        # Text fields
        self.text_container = QWidget()
        text_form = QFormLayout(self.text_container)
        text_form.setContentsMargins(0, 0, 0, 0)
        text_form.setSpacing(6)

        self.tpl_text_edit = QLineEdit("© My Brand")
        self.tpl_text_edit.setToolTip("Macros: {date}, {date:%Y-%m-%d %H:%M}, {filename}, {stem}, {dirname}")
        self.tpl_text_edit.textChanged.connect(self._schedule_preview)
        text_form.addRow("Text:", self.tpl_text_edit)

        color_row = QHBoxLayout()
        self.color_preview_btn = QPushButton()
        self.color_preview_btn.setFixedSize(34, 28)
        self.color_preview_btn.clicked.connect(self._pick_color)
        self.color_hex_edit = QLineEdit("#ffffff")
        self.color_hex_edit.setMaximumWidth(100)
        self.color_hex_edit.textChanged.connect(self._on_color_hex_changed)
        color_row.addWidget(self.color_preview_btn)
        color_row.addWidget(self.color_hex_edit)
        color_row.addStretch(1)
        text_form.addRow("Color:", color_row)

        font_row = QHBoxLayout()
        self.font_label = QLabel("Default (DejaVuSans)")
        self.font_label.setObjectName("mutedLabel")
        self.pick_font_btn = QPushButton("Font…")
        self.pick_font_btn.clicked.connect(self._pick_font_file)
        self.clear_font_btn = QPushButton("Reset")
        self.clear_font_btn.clicked.connect(self._clear_font_file)
        font_row.addWidget(self.font_label, 1)
        font_row.addWidget(self.pick_font_btn)
        font_row.addWidget(self.clear_font_btn)
        text_form.addRow("Font:", font_row)

        macro_hint = QLabel("Macros: {date}, {date:%Y-%m-%d}, {filename}, {stem}, {dirname}")
        macro_hint.setObjectName("mutedLabel")
        text_form.addRow("", macro_hint)
        form.addRow(self.text_container)

        # Image fields
        self.image_container = QWidget()
        img_row = QHBoxLayout(self.image_container)
        img_row.setContentsMargins(0, 0, 0, 0)
        self.wm_img_label = QLabel("No watermark image selected")
        self.wm_img_label.setObjectName("mutedLabel")
        self.pick_wm_img_btn = QPushButton("Choose PNG…")
        self.pick_wm_img_btn.clicked.connect(self._pick_wm_image)
        img_row.addWidget(QLabel("Logo:"))
        img_row.addWidget(self.wm_img_label, 1)
        img_row.addWidget(self.pick_wm_img_btn)
        form.addRow(self.image_container)

        # Sliders
        self.scale_slider, self.scale_spin = self._make_double_slider(0.02, 1.0, 0.01, 0.2, 100.0)
        form.addRow("Scale:", self._pair_widget(self.scale_slider, self.scale_spin))

        self.opacity_slider, self.opacity_spin = self._make_double_slider(0.0, 1.0, 0.01, 0.25, 100.0)
        form.addRow("Opacity:", self._pair_widget(self.opacity_slider, self.opacity_spin))

        self.margin_slider, self.margin_spin = self._make_int_slider(0, 400, 16)
        self.margin_pair = self._pair_widget(self.margin_slider, self.margin_spin)
        self.margin_label = QLabel("Margin (px):")
        form.addRow(self.margin_label, self.margin_pair)

        self.rot_slider, self.rot_spin = self._make_int_slider(-180, 180, 0)
        form.addRow("Rotation (°):", self._pair_widget(self.rot_slider, self.rot_spin))

        self.gap_slider, self.gap_spin = self._make_int_slider(10, 600, 80)
        self.gap_pair = self._pair_widget(self.gap_slider, self.gap_spin)
        self.gap_label = QLabel("Tile gap (px):")
        form.addRow(self.gap_label, self.gap_pair)

        layout.addLayout(form)
        return box

    def _build_batch_group(self) -> QGroupBox:
        box = QGroupBox("Batch Processing")
        layout = QVBoxLayout(box)
        layout.setSpacing(8)

        form = QFormLayout()
        form.setSpacing(7)

        # Source folder
        src_row = QHBoxLayout()
        self.input_dir_edit = FolderDropLineEdit("Drop folder or click Browse…")
        self.input_dir_edit.editingFinished.connect(self._on_input_dir_committed)
        self.input_dir_edit.folderDropped.connect(self._on_input_folder_set)
        browse_in_btn = QPushButton("Browse…")
        browse_in_btn.clicked.connect(self._browse_input_dir)
        scan_btn = QPushButton("Scan")
        scan_btn.clicked.connect(self.scan_folder)
        src_row.addWidget(self.input_dir_edit, 1)
        src_row.addWidget(browse_in_btn)
        src_row.addWidget(scan_btn)
        form.addRow("Source folder:", src_row)

        self.scan_summary_label = QLabel("Select a folder to scan images")
        self.scan_summary_label.setObjectName("mutedLabel")
        form.addRow("", self.scan_summary_label)

        # Template to apply
        self.run_tpl_combo = QComboBox()
        form.addRow("Template:", self.run_tpl_combo)

        # Format + Quality
        fmt_row = QHBoxLayout()
        self.out_format_combo = QComboBox()
        for label, val in [
            ("Same as source", "KEEP"),
            ("JPEG", "JPEG"),
            ("PNG", "PNG"),
            ("WEBP", "WEBP"),
            ("BMP", "BMP"),
            ("TIFF", "TIFF"),
        ]:
            self.out_format_combo.addItem(label, val)
        self.out_format_combo.currentIndexChanged.connect(self._update_conditional_fields)
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(1, 100)
        self.quality_spin.setValue(90)
        self.quality_label = QLabel("Quality:")
        fmt_row.addWidget(self.out_format_combo, 1)
        fmt_row.addWidget(self.quality_label)
        fmt_row.addWidget(self.quality_spin)
        form.addRow("Output format:", fmt_row)

        # Workers + Preserve EXIF
        perf_row = QHBoxLayout()
        self.workers_combo = QComboBox()
        cpu_cnt = os.cpu_count() or 4
        self.workers_combo.addItem(f"Auto ({cpu_cnt} cores)", 0)
        for w in (2, 4, 8, 12, 16):
            self.workers_combo.addItem(f"{w} workers", w)
        self.preserve_exif_cb = QCheckBox("Preserve EXIF & color profile")
        self.preserve_exif_cb.setChecked(True)
        perf_row.addWidget(self.workers_combo)
        perf_row.addWidget(self.preserve_exif_cb)
        form.addRow("Performance:", perf_row)

        # Output mode
        self.save_mode_combo = QComboBox()
        self.save_mode_combo.addItem("Local folder (best for 3,000–10,000+ photos)", "DIR")
        self.save_mode_combo.addItem("ZIP archive", "ZIP")
        self.save_mode_combo.currentIndexChanged.connect(self._update_conditional_fields)
        form.addRow("Save mode:", self.save_mode_combo)

        # Destination folder
        self.dest_container = QWidget()
        dest_v = QVBoxLayout(self.dest_container)
        dest_v.setContentsMargins(0, 0, 0, 0)
        dest_v.setSpacing(6)
        dst_row = QHBoxLayout()
        self.output_dir_edit = FolderDropLineEdit("Destination folder path")
        self.output_dir_edit.folderDropped.connect(lambda p: self.settings.setValue("output_dir", p))
        browse_out_btn = QPushButton("Browse…")
        browse_out_btn.clicked.connect(self._browse_output_dir)
        dst_row.addWidget(self.output_dir_edit, 1)
        dst_row.addWidget(browse_out_btn)
        dest_v.addLayout(dst_row)

        self.overwrite_cb = QCheckBox("Overwrite existing files (uncheck to resume)")
        self.overwrite_cb.setChecked(True)
        self.open_done_cb = QCheckBox("Open destination folder when finished")
        dest_v.addWidget(self.overwrite_cb)
        dest_v.addWidget(self.open_done_cb)
        self.dest_label = QLabel("Destination:")
        form.addRow(self.dest_label, self.dest_container)

        layout.addLayout(form)

        # Action buttons
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start Watermarking")
        self.start_btn.setObjectName("primaryBtn")
        self.start_btn.setMinimumHeight(36)
        self.start_btn.clicked.connect(self.start_batch)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("dangerBtn")
        self.stop_btn.setMinimumHeight(36)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_batch)

        btn_row.addWidget(self.start_btn, 2)
        btn_row.addWidget(self.stop_btn, 1)
        layout.addLayout(btn_row)

        # Progress
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.progress_stats_label = QLabel("")
        self.progress_stats_label.setObjectName("mutedLabel")
        self.progress_stats_label.setVisible(False)
        layout.addWidget(self.progress_stats_label)

        self.error_log = QPlainTextEdit()
        self.error_log.setReadOnly(True)
        self.error_log.setMaximumHeight(110)
        self.error_log.setVisible(False)
        layout.addWidget(self.error_log)

        return box

    def _build_preview_group(self) -> QGroupBox:
        box = QGroupBox("Live Preview (Mouse wheel = Zoom • Drag = Pan • Double-click = Fit / 100%)")
        layout = QVBoxLayout(box)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        self.preview_btn = QPushButton("Update Preview")
        self.preview_btn.setObjectName("primaryBtn")
        self.preview_btn.clicked.connect(self.refresh_preview)

        self.prev_img_btn = QPushButton("◀ Prev")
        self.prev_img_btn.clicked.connect(self._prev_image)
        self.next_img_btn = QPushButton("Next ▶")
        self.next_img_btn.clicked.connect(self._next_image)
        self.rand_img_btn = QPushButton("Random")
        self.rand_img_btn.clicked.connect(self._random_image)

        self.orig_btn = QPushButton("Original")
        self.orig_btn.setCheckable(True)
        self.orig_btn.toggled.connect(self._toggle_original)

        self.fit_btn = QPushButton("Fit")
        self.fit_btn.clicked.connect(lambda: self.canvas.fit_view())
        self.zoom100_btn = QPushButton("100%")
        self.zoom100_btn.clicked.connect(lambda: self.canvas.zoom_actual())

        self.auto_preview_cb = QCheckBox("Auto-preview")
        self.auto_preview_cb.setChecked(True)

        toolbar.addWidget(self.preview_btn)
        toolbar.addWidget(self.prev_img_btn)
        toolbar.addWidget(self.next_img_btn)
        toolbar.addWidget(self.rand_img_btn)
        toolbar.addWidget(self.orig_btn)
        toolbar.addWidget(self.fit_btn)
        toolbar.addWidget(self.zoom100_btn)
        toolbar.addStretch(1)
        toolbar.addWidget(self.auto_preview_cb)
        layout.addLayout(toolbar)

        self.preview_meta_label = QLabel("Applies current editor settings (even unsaved)")
        self.preview_meta_label.setObjectName("mutedLabel")
        layout.addWidget(self.preview_meta_label)

        self.canvas = PreviewCanvas()
        self.canvas.zoomChanged.connect(self._on_zoom_changed)
        layout.addWidget(self.canvas, 1)

        return box

    # ---------- Slider Helpers ----------

    def _make_double_slider(self, mn: float, mx: float, step: float, val: float, mult: float):
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(int(round(mn * mult)), int(round(mx * mult)))
        slider.setValue(int(round(val * mult)))

        spin = QDoubleSpinBox()
        spin.setRange(mn, mx)
        spin.setSingleStep(step)
        spin.setDecimals(2)
        spin.setValue(val)
        spin.setFixedWidth(84)

        def on_slider(v: int) -> None:
            spin.blockSignals(True)
            spin.setValue(v / mult)
            spin.blockSignals(False)
            self._schedule_preview()

        def on_spin(v: float) -> None:
            slider.blockSignals(True)
            slider.setValue(int(round(v * mult)))
            slider.blockSignals(False)
            self._schedule_preview()

        slider.valueChanged.connect(on_slider)
        spin.valueChanged.connect(on_spin)
        return slider, spin

    def _make_int_slider(self, mn: int, mx: int, val: int):
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(mn, mx)
        slider.setValue(val)

        spin = QSpinBox()
        spin.setRange(mn, mx)
        spin.setValue(val)
        spin.setFixedWidth(84)

        def on_slider(v: int) -> None:
            spin.blockSignals(True)
            spin.setValue(v)
            spin.blockSignals(False)
            self._schedule_preview()

        def on_spin(v: int) -> None:
            slider.blockSignals(True)
            slider.setValue(v)
            slider.blockSignals(False)
            self._schedule_preview()

        slider.valueChanged.connect(on_slider)
        spin.valueChanged.connect(on_spin)
        return slider, spin

    def _pair_widget(self, slider: QSlider, spin: QWidget) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(slider, 1)
        lay.addWidget(spin)
        return w

    # ---------- Theme & Shortcuts ----------

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Alt+T"), self, activated=self.toggle_theme)
        QShortcut(QKeySequence("Left"), self, activated=self._prev_image)
        QShortcut(QKeySequence("Right"), self, activated=self._next_image)

    def toggle_theme(self) -> None:
        cur = self.settings.value("theme", "dark")
        nxt = "light" if cur == "dark" else "dark"
        self.settings.setValue("theme", nxt)
        self._apply_theme(nxt)

    def _apply_theme(self, theme: str) -> None:
        app = QApplication.instance()
        if app is None:
            return
        app.setStyle("Fusion")
        is_dark = theme != "light"
        self.theme_btn.setText("Theme: Dark" if is_dark else "Theme: Light")

        pal = QPalette()
        if is_dark:
            pal.setColor(QPalette.ColorRole.Window, QColor("#0b0f14"))
            pal.setColor(QPalette.ColorRole.WindowText, QColor("#e7edf5"))
            pal.setColor(QPalette.ColorRole.Base, QColor("#0e141d"))
            pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#121821"))
            pal.setColor(QPalette.ColorRole.Text, QColor("#e7edf5"))
            pal.setColor(QPalette.ColorRole.Button, QColor("#161f2b"))
            pal.setColor(QPalette.ColorRole.ButtonText, QColor("#e7edf5"))
            pal.setColor(QPalette.ColorRole.Highlight, QColor("#5aa9ff"))
            pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#001018"))
            qss = """
                QGroupBox { border: 1px solid #1e2632; border-radius: 10px; margin-top: 10px; padding: 12px 10px 10px 10px; background: #121821; font-weight: 600; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #9db0c2; }
                QLabel#appTitle { font-size: 20px; font-weight: 700; }
                QLabel#appSubtitle, QLabel#mutedLabel { color: #9db0c2; font-size: 12px; }
                QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit { border: 1px solid #263140; border-radius: 6px; padding: 5px 8px; background: #0e141d; }
                QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #5aa9ff; }
                QPushButton { border: 1px solid #263140; border-radius: 7px; padding: 6px 12px; background: #161f2b; }
                QPushButton:hover { border-color: #3a4b61; }
                QPushButton#primaryBtn { background: #5aa9ff; color: #001018; border-color: #5aa9ff; font-weight: 600; }
                QPushButton#primaryBtn:hover { background: #73b7ff; }
                QPushButton#dangerBtn { background: #ff6b6b; color: #2b0000; border-color: #ff6b6b; font-weight: 600; }
                QPushButton:disabled { opacity: 0.45; }
                QPushButton:checked { border-color: #5aa9ff; color: #5aa9ff; }
                QProgressBar { border: 1px solid #263140; border-radius: 6px; text-align: center; background: #0e141d; height: 18px; }
                QProgressBar::chunk { background: #5aa9ff; border-radius: 5px; }
            """
        else:
            pal.setColor(QPalette.ColorRole.Window, QColor("#f4f6fa"))
            pal.setColor(QPalette.ColorRole.WindowText, QColor("#0b0f14"))
            pal.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
            pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#f2f5f9"))
            pal.setColor(QPalette.ColorRole.Text, QColor("#0b0f14"))
            pal.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
            pal.setColor(QPalette.ColorRole.ButtonText, QColor("#0b0f14"))
            pal.setColor(QPalette.ColorRole.Highlight, QColor("#2e7fda"))
            pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
            qss = """
                QGroupBox { border: 1px solid #d8e1ec; border-radius: 10px; margin-top: 10px; padding: 12px 10px 10px 10px; background: #ffffff; font-weight: 600; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #586b7d; }
                QLabel#appTitle { font-size: 20px; font-weight: 700; }
                QLabel#appSubtitle, QLabel#mutedLabel { color: #586b7d; font-size: 12px; }
                QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit { border: 1px solid #cfd9e6; border-radius: 6px; padding: 5px 8px; background: #f8fafc; }
                QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #2e7fda; }
                QPushButton { border: 1px solid #cfd9e6; border-radius: 7px; padding: 6px 12px; background: #ffffff; }
                QPushButton:hover { border-color: #9ab2cc; }
                QPushButton#primaryBtn { background: #2e7fda; color: #ffffff; border-color: #2e7fda; font-weight: 600; }
                QPushButton#dangerBtn { background: #e04747; color: #ffffff; border-color: #e04747; font-weight: 600; }
                QPushButton:checked { border-color: #2e7fda; color: #2e7fda; }
                QProgressBar { border: 1px solid #cfd9e6; border-radius: 6px; text-align: center; background: #eef3f8; height: 18px; }
                QProgressBar::chunk { background: #2e7fda; border-radius: 5px; }
            """
        app.setPalette(pal)
        app.setStyleSheet(qss)

    # ---------- Templates ----------

    def _load_templates(self, prefer_name: Optional[str] = None) -> None:
        self.templates = engine.load_templates()
        prev_edit = prefer_name or self.tpl_combo.currentText()
        prev_run = prefer_name or self.run_tpl_combo.currentData()

        self.tpl_combo.blockSignals(True)
        self.run_tpl_combo.blockSignals(True)
        self.tpl_combo.clear()
        self.run_tpl_combo.clear()

        self.run_tpl_combo.addItem("★ Current editor settings", "__editor__")
        keys = sorted(self.templates.keys())
        for k in keys:
            self.tpl_combo.addItem(k)
            self.run_tpl_combo.addItem(k, k)

        if prev_edit and prev_edit in self.templates:
            self.tpl_combo.setCurrentText(prev_edit)
        if prev_run == "__editor__":
            self.run_tpl_combo.setCurrentIndex(0)
        elif prev_run and prev_run in self.templates:
            idx = self.run_tpl_combo.findData(prev_run)
            if idx >= 0:
                self.run_tpl_combo.setCurrentIndex(idx)
        elif keys:
            self.run_tpl_combo.setCurrentIndex(1)

        self.tpl_combo.blockSignals(False)
        self.run_tpl_combo.blockSignals(False)

        if self.tpl_combo.currentText() in self.templates:
            self._populate_editor(self.tpl_combo.currentText())
        else:
            self._update_conditional_fields()

    def _on_template_selected(self, name: str) -> None:
        if name in self.templates:
            self._populate_editor(name)
            idx = self.run_tpl_combo.findData(name)
            if idx >= 0:
                self.run_tpl_combo.setCurrentIndex(idx)
            self._schedule_preview()

    def _populate_editor(self, name: str) -> None:
        tpl = self.templates.get(name)
        if not tpl:
            return
        self._loading_form = True
        try:
            self.tpl_name_edit.setText(name)
            self.tpl_type_combo.setCurrentIndex(max(0, self.tpl_type_combo.findData(tpl.get("type", "text"))))
            self.tpl_pos_combo.setCurrentIndex(max(0, self.tpl_pos_combo.findData(tpl.get("position", "bottom-right"))))
            self.tpl_text_edit.setText(tpl.get("text", "WATERMARK"))

            c_hex = tpl.get("text_color") or "#FFFFFF"
            self.color_hex_edit.setText(c_hex)
            self._set_color_btn(c_hex)

            self.pending_font_src = None
            self.pending_wm_image_src = None
            fp = tpl.get("font_path")
            self.font_label.setText(Path(fp).name if fp else "Default (DejaVuSans)")
            ip = tpl.get("image_path")
            self.wm_img_label.setText(Path(ip).name if ip else "No watermark image selected")

            self.scale_spin.setValue(float(tpl.get("scale", 0.2)))
            self.opacity_spin.setValue(float(tpl.get("opacity", 0.25)))
            self.margin_spin.setValue(int(tpl.get("margin", 16)))
            self.rot_spin.setValue(int(round(float(tpl.get("rotation", 0.0)))))
            self.gap_spin.setValue(int(tpl.get("tile_gap", 80)))
        finally:
            self._loading_form = False
        self._update_conditional_fields()

    def _reset_editor(self) -> None:
        self._loading_form = True
        try:
            self.tpl_name_edit.clear()
            self.tpl_type_combo.setCurrentIndex(0)
            self.tpl_pos_combo.setCurrentIndex(0)
            self.tpl_text_edit.setText("© My Brand")
            self.color_hex_edit.setText("#ffffff")
            self._set_color_btn("#ffffff")
            self.pending_font_src = None
            self.pending_wm_image_src = None
            self.font_label.setText("Default (DejaVuSans)")
            self.wm_img_label.setText("No watermark image selected")
            self.scale_spin.setValue(0.2)
            self.opacity_spin.setValue(0.25)
            self.margin_spin.setValue(16)
            self.rot_spin.setValue(0)
            self.gap_spin.setValue(80)
        finally:
            self._loading_form = False
        self._update_conditional_fields()
        self.tpl_name_edit.setFocus()

    def _collect_editor_template(self) -> Dict[str, Any]:
        ttype = self.tpl_type_combo.currentData()
        cur_name = self.tpl_combo.currentText()
        saved = self.templates.get(cur_name, {})
        data: Dict[str, Any] = {
            "type": ttype,
            "position": self.tpl_pos_combo.currentData(),
            "scale": float(self.scale_spin.value()),
            "opacity": float(self.opacity_spin.value()),
            "margin": int(self.margin_spin.value()),
            "rotation": float(self.rot_spin.value()),
            "tile_gap": int(self.gap_spin.value()),
        }
        if ttype == "image":
            if self.pending_wm_image_src:
                data["image_path"] = self.pending_wm_image_src
            elif saved.get("image_path"):
                data["image_path"] = saved["image_path"]
        else:
            data["text"] = self.tpl_text_edit.text() or "WATERMARK"
            data["text_color"] = self.color_hex_edit.text().strip() or "#FFFFFF"
            if self.pending_font_src:
                data["font_path"] = self.pending_font_src
            elif self.font_label.text() != "Default (DejaVuSans)" and saved.get("font_path"):
                data["font_path"] = saved["font_path"]
        return data

    def _save_template(self) -> None:
        name = self.tpl_name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing Name", "Please enter a template name.")
            return
        data = self._collect_editor_template()
        try:
            engine.save_template_entry(
                name=name,
                data=data,
                watermark_src_path=self.pending_wm_image_src,
                font_src_path=self.pending_font_src,
            )
            self.pending_wm_image_src = None
            self.pending_font_src = None
            self._load_templates(prefer_name=name)
            QMessageBox.information(self, "Saved", f'Template "{name}" saved.')
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _delete_template(self) -> None:
        name = self.tpl_combo.currentText()
        if not name or name not in self.templates:
            return
        ans = QMessageBox.question(self, "Delete Template", f'Delete template "{name}"?')
        if ans != QMessageBox.StandardButton.Yes:
            return
        engine.delete_template_entry(name)
        self._load_templates()

    # ---------- Editor Controls ----------

    def _set_color_btn(self, color_hex: str) -> None:
        c = QColor(color_hex)
        if c.isValid():
            self.color_preview_btn.setStyleSheet(f"background-color: {c.name()}; border-radius: 6px;")

    def _pick_color(self) -> None:
        cur = QColor(self.color_hex_edit.text().strip() or "#ffffff")
        chosen = QColorDialog.getColor(cur, self, "Select Watermark Color")
        if chosen.isValid():
            self.color_hex_edit.setText(chosen.name())

    def _on_color_hex_changed(self, txt: str) -> None:
        c = QColor(txt.strip())
        if c.isValid():
            self._set_color_btn(c.name())
            self._schedule_preview()

    def _pick_font_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose Font", "", "Fonts (*.ttf *.otf *.ttc)")
        if path:
            self.pending_font_src = path
            self.font_label.setText(Path(path).name)
            self._schedule_preview()

    def _clear_font_file(self) -> None:
        self.pending_font_src = None
        self.font_label.setText("Default (DejaVuSans)")
        self._schedule_preview()

    def _pick_wm_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose Watermark Image", "", "Images (*.png *.webp *.jpg *.jpeg *.bmp *.tiff)"
        )
        if path:
            self.pending_wm_image_src = path
            self.wm_img_label.setText(Path(path).name)
            self._schedule_preview()

    def _on_editor_changed(self) -> None:
        self._update_conditional_fields()
        self._schedule_preview()

    def _update_conditional_fields(self) -> None:
        is_img = self.tpl_type_combo.currentData() == "image"
        self.text_container.setVisible(not is_img)
        self.image_container.setVisible(is_img)

        pos = self.tpl_pos_combo.currentData()
        is_tile = pos == "tile"
        is_center = pos == "center"
        self.gap_label.setVisible(is_tile)
        self.gap_pair.setVisible(is_tile)
        self.margin_label.setVisible(not is_center)
        self.margin_pair.setVisible(not is_center)

        fmt = self.out_format_combo.currentData()
        show_q = fmt in ("KEEP", "JPEG", "WEBP")
        self.quality_label.setVisible(show_q)
        self.quality_spin.setVisible(show_q)

        is_dir = self.save_mode_combo.currentData() == "DIR"
        self.dest_label.setVisible(is_dir)
        self.dest_container.setVisible(is_dir)

    # ---------- Folder Selection, Drag & Drop, Scanning ----------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for u in event.mimeData().urls():
                if Path(u.toLocalFile()).is_dir():
                    event.acceptProposedAction()
                    return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            for u in event.mimeData().urls():
                p = Path(u.toLocalFile())
                if p.is_dir():
                    self.input_dir_edit.setText(str(p))
                    self._on_input_folder_set(str(p))
                    event.acceptProposedAction()
                    return
        super().dropEvent(event)

    def _browse_input_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Select Source Photo Folder", self.input_dir_edit.text().strip())
        if chosen:
            self.input_dir_edit.setText(chosen)
            self._on_input_folder_set(chosen)

    def _browse_output_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Select Destination Folder", self.output_dir_edit.text().strip())
        if chosen:
            self.output_dir_edit.setText(chosen)
            self.settings.setValue("output_dir", chosen)

    def _on_input_dir_committed(self) -> None:
        path = self.input_dir_edit.text().strip()
        if path and path != self.settings.value("input_dir", ""):
            self._on_input_folder_set(path)

    def _on_input_folder_set(self, path: str) -> None:
        self.settings.setValue("input_dir", path)
        self.preview_index = 0
        self.scan_folder()

    def scan_folder(self) -> None:
        raw = self.input_dir_edit.text().strip()
        if not raw:
            self.scan_summary_label.setText("Select a folder to scan images")
            return
        base = Path(raw)
        if not base.exists() or not base.is_dir():
            self.scan_summary_label.setText("Source folder does not exist")
            return

        out_txt = self.output_dir_edit.text().strip()
        excl = Path(out_txt) if (out_txt and self.save_mode_combo.currentData() == "DIR") else None

        self.scan_summary_label.setText("Scanning folder…")
        self.scan_worker = ScanWorker(base, excl)
        self.scan_worker.completed.connect(self._on_scan_done)
        self.scan_worker.failed.connect(lambda err: self.scan_summary_label.setText(f"Scan error: {err}"))
        self.scan_worker.start()

    def _on_scan_done(self, files: List[Path], ms: float) -> None:
        self.scanned_files = files
        self.scan_summary_label.setText(f"{len(files):,} image(s) found ({ms} ms)")
        if files:
            self.preview_index = max(0, min(self.preview_index, len(files) - 1))
            self.refresh_preview()
        else:
            self.preview_meta_label.setText("No supported images found in this folder")

    # ---------- Live Preview ----------

    def _schedule_preview(self) -> None:
        if self._loading_form or not self.auto_preview_cb.isChecked():
            return
        if not self.scanned_files:
            return
        self.preview_timer.start()

    def _prev_image(self) -> None:
        if not self.scanned_files:
            return
        self.preview_index = max(0, self.preview_index - 1)
        self.refresh_preview()

    def _next_image(self) -> None:
        if not self.scanned_files:
            return
        self.preview_index = min(len(self.scanned_files) - 1, self.preview_index + 1)
        self.refresh_preview()

    def _random_image(self) -> None:
        if len(self.scanned_files) > 1:
            self.preview_index = random.randrange(len(self.scanned_files))
            self.refresh_preview()

    def _toggle_original(self, checked: bool) -> None:
        self.show_original = checked
        self.refresh_preview()

    def _on_zoom_changed(self, zoom_txt: str) -> None:
        self.zoom_label_text = zoom_txt

    def refresh_preview(self) -> None:
        if not self.scanned_files:
            return
        idx = max(0, min(self.preview_index, len(self.scanned_files) - 1))
        target = self.scanned_files[idx]
        tmpl = self._collect_editor_template()

        self.preview_req_id += 1
        req_id = self.preview_req_id
        self.preview_meta_label.setText(f"Rendering {target.name}…")

        task = PreviewTask(req_id, target, tmpl, idx, len(self.scanned_files), self.show_original)
        task.signals.ready.connect(self._on_preview_ready)
        task.signals.failed.connect(self._on_preview_failed)
        self.thread_pool.start(task)

    def _on_preview_ready(
        self, req_id: int, qimg: QImage, name: str, w: int, h: int, idx: int, total: int, show_orig: bool
    ) -> None:
        if req_id != self.preview_req_id:
            return
        pix = QPixmap.fromImage(qimg)
        self.canvas.set_pixmap(pix)
        tag = "ORIGINAL" if show_orig else "WATERMARKED"
        self.preview_meta_label.setText(f"{name} • {w}×{h} • {idx + 1:,} / {total:,} ({tag}) • Zoom: {self.zoom_label_text}")

    def _on_preview_failed(self, req_id: int, err: str) -> None:
        if req_id != self.preview_req_id:
            return
        self.preview_meta_label.setText(f"Preview error: {err}")

    # ---------- Batch Run ----------

    def start_batch(self) -> None:
        if self.batch_worker and self.batch_worker.isRunning():
            return

        in_txt = self.input_dir_edit.text().strip()
        if not in_txt:
            QMessageBox.warning(self, "Missing Source", "Please select a source folder.")
            return
        base = Path(in_txt)
        if not base.exists() or not base.is_dir():
            QMessageBox.warning(self, "Invalid Source", "Source folder does not exist.")
            return

        mode = self.save_mode_combo.currentData()
        out_root: Optional[Path] = None
        zip_dest: Optional[Path] = None

        if mode == "DIR":
            out_txt = self.output_dir_edit.text().strip()
            if not out_txt:
                QMessageBox.warning(self, "Missing Destination", "Please select a destination folder.")
                return
            out_root = Path(out_txt)
            if out_root.resolve() == base.resolve() and self.overwrite_cb.isChecked():
                QMessageBox.warning(
                    self,
                    "Unsafe Destination",
                    "Destination folder cannot be the same as Source folder when Overwrite is enabled.",
                )
                return
            self.settings.setValue("output_dir", out_txt)
        else:
            zip_file, _ = QFileDialog.getSaveFileName(self, "Save Output ZIP", str(base.parent / "watermarked.zip"), "ZIP Archive (*.zip)")
            if not zip_file:
                return
            zip_dest = Path(zip_file)

        run_choice = self.run_tpl_combo.currentData()
        if run_choice == "__editor__" or not run_choice:
            tdata = self._collect_editor_template()
        else:
            tdata = dict(self.templates[run_choice])

        if tdata.get("type") == "image":
            ip = tdata.get("image_path")
            if not ip or not Path(ip).exists():
                QMessageBox.warning(self, "Missing Watermark Image", "Please select a watermark PNG file first.")
                return

        files = engine.iter_images(base, exclude_dir=out_root)
        if not files:
            QMessageBox.warning(self, "No Images", "No supported images found in the source folder.")
            return

        self._save_settings()
        self.scanned_files = files

        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.progress_stats_label.setVisible(True)
        self.progress_stats_label.setText(f"0 / {len(files):,} • Starting…")
        self.error_log.setVisible(False)
        self.error_log.clear()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.cancel_ev = threading.Event()

        self.batch_worker = BatchWorker(
            files=files,
            base=base,
            out_root=out_root,
            tdata=tdata,
            output_format=self.out_format_combo.currentData(),
            quality=int(self.quality_spin.value()),
            overwrite=self.overwrite_cb.isChecked(),
            open_when_done=self.open_done_cb.isChecked(),
            save_mode=mode,
            workers=int(self.workers_combo.currentData() or 0),
            preserve_exif=self.preserve_exif_cb.isChecked(),
            zip_dest_path=zip_dest,
            cancel_ev=self.cancel_ev,
        )
        self.batch_worker.progress.connect(self._on_batch_progress)
        self.batch_worker.completed.connect(self._on_batch_done)
        self.batch_worker.failed.connect(self._on_batch_failed)
        self.batch_worker.start()

    def stop_batch(self) -> None:
        if self.cancel_ev:
            self.cancel_ev.set()
            self.stop_btn.setEnabled(False)
            self.progress_stats_label.setText("Stopping… waiting for active workers")

    def _on_batch_progress(self, d: Dict[str, Any]) -> None:
        tot = max(1, d.get("total", 1))
        done = d.get("done", 0)
        pct = 100 if d.get("state") == "done" else min(99, int((done * 100) / tot))
        self.progress_bar.setValue(pct)

        parts = [f"{done:,} / {tot:,}"]
        if d.get("skipped", 0) > 0:
            parts.append(f"Skipped {d['skipped']:,}")
        if d.get("rate", 0) > 0:
            parts.append(f"{d['rate']} img/s")
        parts.append(f"Elapsed {_format_duration(d.get('elapsed_sec', 0))}")
        if d.get("eta_sec", 0) > 0 and d.get("state") == "running":
            parts.append(f"ETA {_format_duration(d['eta_sec'])}")
        if d.get("errors", 0) > 0:
            parts.append(f"Errors: {d['errors']}")
        self.progress_stats_label.setText(" • ".join(parts))

    def _on_batch_done(self, d: Dict[str, Any]) -> None:
        self._on_batch_progress(d)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

        samples = d.get("error_samples") or []
        if samples:
            self.error_log.setVisible(True)
            self.error_log.setPlainText("\n".join(f"{s['file']}: {s['error']}" for s in samples))

        cancelled = d.get("state") == "cancelled"
        msg = (
            f"Stopped at {d['done']:,}/{d['total']:,}. Processed: {d['processed']:,}."
            if cancelled
            else f"Finished {d['processed']:,} image(s) in {_format_duration(d.get('elapsed_sec', 0))}."
        )
        if d.get("skipped", 0):
            msg += f" Skipped: {d['skipped']:,}."
        if d.get("errors", 0):
            msg += f" Errors: {d['errors']}."
        QMessageBox.information(self, "Stopped" if cancelled else "Completed", msg)

    def _on_batch_failed(self, err: str) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        QMessageBox.critical(self, "Batch Error", err)

    # ---------- Settings & Close ----------

    def _save_settings(self) -> None:
        self.settings.setValue("input_dir", self.input_dir_edit.text().strip())
        self.settings.setValue("output_dir", self.output_dir_edit.text().strip())
        self.settings.setValue("save_mode", self.save_mode_combo.currentData())
        self.settings.setValue("out_format", self.out_format_combo.currentData())
        self.settings.setValue("quality", self.quality_spin.value())
        self.settings.setValue("workers", self.workers_combo.currentData())
        self.settings.setValue("preserve_exif", self.preserve_exif_cb.isChecked())
        self.settings.setValue("geometry", self.saveGeometry())

    def _restore_settings(self) -> None:
        geom = self.settings.value("geometry")
        if geom:
            self.restoreGeometry(geom)
        else:
            self.resize(1280, 820)

        in_dir = self.settings.value("input_dir", "")
        out_dir = self.settings.value("output_dir", "")
        if in_dir:
            self.input_dir_edit.setText(str(in_dir))
        if out_dir:
            self.output_dir_edit.setText(str(out_dir))

        sm = self.settings.value("save_mode", "DIR")
        idx = self.save_mode_combo.findData(sm)
        if idx >= 0:
            self.save_mode_combo.setCurrentIndex(idx)

        fmt = self.settings.value("out_format", "KEEP")
        fidx = self.out_format_combo.findData(fmt)
        if fidx >= 0:
            self.out_format_combo.setCurrentIndex(fidx)

        self.quality_spin.setValue(int(self.settings.value("quality", 90)))
        wk = int(self.settings.value("workers", 0))
        widx = self.workers_combo.findData(wk)
        if widx >= 0:
            self.workers_combo.setCurrentIndex(widx)

        ex = self.settings.value("preserve_exif", True)
        self.preserve_exif_cb.setChecked(str(ex).lower() != "false")
        self._update_conditional_fields()

        if in_dir and Path(str(in_dir)).is_dir():
            self.scan_folder()

    def closeEvent(self, event) -> None:
        if self.batch_worker and self.batch_worker.isRunning():
            ans = QMessageBox.question(
                self,
                "Batch Running",
                "A batch watermarking job is currently running. Stop and exit?",
            )
            if ans != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if self.cancel_ev:
                self.cancel_ev.set()
            self.batch_worker.wait(4000)
        self._save_settings()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Batch-o-mark")
    app.setOrganizationName("Batch-o-mark")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    mp.freeze_support()
    main()
