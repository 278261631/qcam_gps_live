import os
import sys
import time
import threading

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton, QComboBox,
    QScrollArea, QSplitter, QPlainTextEdit, QListWidget, QFileDialog,
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
import numpy as np
from PIL import Image
from PIL.ImageQt import ImageQt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live.sdk_wrapper import (
    QHYCCDSDK, ControlID, QHYCCDError, error_string, platform_info,
    SINGLE_MODE, LIVE_MODE, BayerPattern, parse_gps_from_frame,
)


class ImageLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._original_pixmap = None
        self.setMinimumSize(100, 100)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background-color: #1a1a1a;")

    def set_display_pixmap(self, pixmap):
        self._original_pixmap = pixmap
        self._fit_to_label()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._original_pixmap:
            self._fit_to_label()

    def _fit_to_label(self):
        if self._original_pixmap is None:
            return
        lw = self.width()
        lh = self.height()
        if lw < 10 or lh < 10:
            return
        pw = self._original_pixmap.width()
        ph = self._original_pixmap.height()
        scale = min(lw / pw, lh / ph, 1.0)
        nw, nh = int(pw * scale), int(ph * scale)
        scaled = self._original_pixmap.scaled(
            nw, nh, Qt.KeepAspectRatio, Qt.FastTransformation,
        )
        self.setPixmap(scaled)


class QHYCamWindow(QMainWindow):
    live_frame_signal = Signal(int, int, int, int, bytes)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("QHYCCD Camera Live")
        self.resize(1100, 750)
        self.setMinimumSize(900, 600)

        self.sdk = QHYCCDSDK()
        self._live_running = False
        self._live_thread = None
        self._captured_image = None
        self._camera_list = []
        self._current_read_mode = 1
        self._full_w = 0
        self._full_h = 0
        self._mem_len = 0
        self._params = {}

        self.live_frame_signal.connect(self._on_live_frame)

        self._build_ui()
        self._refresh_sdk_status()
        QTimer.singleShot(200, self._auto_init)

    # ==============================================================
    # UI Construction
    # ==============================================================
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter)

        left = QWidget()
        left.setMaximumWidth(420)
        splitter.addWidget(left)

        right = QWidget()
        splitter.addWidget(right)

        self._build_left_panel(left)
        self._build_right_panel(right)

    def _build_left_panel(self, parent):
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(scroll)

        sw = QWidget()
        scroll.setWidget(sw)
        sl = QVBoxLayout(sw)
        sl.setContentsMargins(6, 2, 6, 2)
        sl.setSpacing(4)

        # --- SDK Status ---
        grp = QGroupBox("SDK Status")
        gl = QVBoxLayout(grp)
        self._sdk_status_label = QLabel("SDK: Not loaded")
        self._sdk_status_label.setStyleSheet("color: gray;")
        gl.addWidget(self._sdk_status_label)
        self._sdk_ver_label = QLabel("")
        self._sdk_ver_label.setStyleSheet("color: #555;")
        gl.addWidget(self._sdk_ver_label)
        self._platform_label = QLabel("")
        self._platform_label.setStyleSheet("color: #555;")
        gl.addWidget(self._platform_label)
        sl.addWidget(grp)

        # --- Camera Scan ---
        grp = QGroupBox("Camera")
        gl = QVBoxLayout(grp)
        self._cam_listwidget = QListWidget()
        self._cam_listwidget.setMaximumHeight(80)
        gl.addWidget(self._cam_listwidget)
        sl.addWidget(grp)

        # --- Camera Operations ---
        grp = QGroupBox("Camera Control")
        gl = QVBoxLayout(grp)
        hr = QHBoxLayout()
        self._btn_close = QPushButton("Close Camera")
        self._btn_close.clicked.connect(self._close_camera)
        self._btn_close.setEnabled(False)
        hr.addWidget(self._btn_close)
        hr.addStretch()
        gl.addLayout(hr)
        self._cam_model_label = QLabel("Model: --")
        gl.addWidget(self._cam_model_label)
        self._cam_color_label = QLabel("")
        gl.addWidget(self._cam_color_label)
        self._gps_label = QLabel("GPS: --")
        self._gps_label.setStyleSheet("color: #555;")
        gl.addWidget(self._gps_label)
        sl.addWidget(grp)

        # --- Parameters ---
        grp = QGroupBox("Parameters")
        gl = QVBoxLayout(grp)
        param_specs = [
            ("Exposure (us)", ControlID.CONTROL_EXPOSURE, "100000"),
            ("Gain", ControlID.CONTROL_GAIN, "10"),
            ("Offset", ControlID.CONTROL_OFFSET, "140"),
            ("USB Traffic", ControlID.CONTROL_USBTRAFFIC, "30"),
            ("Speed", ControlID.CONTROL_SPEED, "0"),
            ("DDR Buffer", ControlID.CONTROL_DDR, "1"),
        ]

        for label_text, ctrl_id, default in param_specs:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 1, 0, 1)
            lbl = QLabel(label_text)
            lbl.setFixedWidth(130)
            rl.addWidget(lbl)
            entry = QLineEdit(default)
            entry.setFixedWidth(90)
            rl.addWidget(entry)
            b1 = QPushButton("Set")
            b1.setFixedWidth(40)
            b1.clicked.connect(lambda checked, cid=ctrl_id, e=entry: self._set_param(cid, e))
            rl.addWidget(b1)
            b2 = QPushButton("Get")
            b2.setFixedWidth(40)
            b2.clicked.connect(lambda checked, cid=ctrl_id, e=entry: self._get_param(cid, e))
            rl.addWidget(b2)
            rl.addStretch()
            gl.addWidget(row)
            self._params[ctrl_id] = entry

        # --- Temperature ---
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 1, 0, 1)
        rl.addWidget(QLabel("Target Temp (C)"))
        self._temp_entry = QLineEdit("0")
        self._temp_entry.setFixedWidth(90)
        rl.addWidget(self._temp_entry)
        b1 = QPushButton("Set")
        b1.setFixedWidth(40)
        b1.clicked.connect(self._set_temp)
        rl.addWidget(b1)
        b2 = QPushButton("Get")
        b2.setFixedWidth(40)
        b2.clicked.connect(self._get_temp)
        rl.addWidget(b2)
        rl.addStretch()
        gl.addWidget(row)
        sl.addWidget(grp)

        # --- ROI ---
        grp = QGroupBox("Resolution / ROI")
        gl = QVBoxLayout(grp)

        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("X"))
        self._res_x_entry = QLineEdit("0")
        self._res_x_entry.setFixedWidth(60)
        rl.addWidget(self._res_x_entry)
        rl.addSpacing(6)
        rl.addWidget(QLabel("Y"))
        self._res_y_entry = QLineEdit("0")
        self._res_y_entry.setFixedWidth(60)
        rl.addWidget(self._res_y_entry)
        rl.addStretch()
        gl.addWidget(row)

        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("W"))
        self._res_w_entry = QLineEdit("0")
        self._res_w_entry.setFixedWidth(60)
        rl.addWidget(self._res_w_entry)
        rl.addSpacing(6)
        rl.addWidget(QLabel("H"))
        self._res_h_entry = QLineEdit("0")
        self._res_h_entry.setFixedWidth(60)
        rl.addWidget(self._res_h_entry)
        rl.addStretch()
        gl.addWidget(row)

        gl.addWidget(QPushButton("Set Resolution", clicked=self._set_resolution))
        gl.addWidget(QPushButton("Fill Effective Area", clicked=self._fill_effective_area))
        sl.addWidget(grp)

        # --- Bin / Bits ---
        grp = QGroupBox("Binning / Bits")
        gl = QVBoxLayout(grp)

        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("Bin X"))
        self._binx_entry = QLineEdit("1")
        self._binx_entry.setFixedWidth(60)
        rl.addWidget(self._binx_entry)
        rl.addWidget(QLabel("Bin Y"))
        self._biny_entry = QLineEdit("1")
        self._biny_entry.setFixedWidth(60)
        rl.addWidget(self._biny_entry)
        rl.addWidget(QPushButton("Set Bin", clicked=self._set_binning))
        rl.addStretch()
        gl.addWidget(row)

        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 4, 0, 0)
        rl.addWidget(QLabel("Bits"))
        self._bits_combo = QComboBox()
        self._bits_combo.addItems(["8", "16"])
        self._bits_combo.setFixedWidth(80)
        rl.addWidget(self._bits_combo)
        rl.addWidget(QPushButton("Set Bits", clicked=self._set_bits))
        rl.addStretch()
        gl.addWidget(row)
        sl.addWidget(grp)

        # --- Capture ---
        grp = QGroupBox("Capture")
        gl = QVBoxLayout(grp)

        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        self._btn_single = QPushButton("Single Frame", clicked=self._capture_single)
        self._btn_single.setEnabled(False)
        rl.addWidget(self._btn_single)
        self._btn_live = QPushButton("Start Live", clicked=self._toggle_live)
        self._btn_live.setEnabled(False)
        rl.addWidget(self._btn_live)
        rl.addStretch()
        gl.addWidget(row)

        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 4, 0, 0)
        self._btn_save = QPushButton("Save Image", clicked=self._save_image)
        self._btn_save.setEnabled(False)
        rl.addWidget(self._btn_save)
        rl.addStretch()
        gl.addWidget(row)

        self._capture_info_label = QLabel("")
        self._capture_info_label.setStyleSheet("color: #555;")
        gl.addWidget(self._capture_info_label)
        sl.addWidget(grp)

        sl.addStretch()

    def _build_right_panel(self, parent):
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)

        rs = QSplitter(Qt.Vertical)
        layout.addWidget(rs)

        # Image View
        vf = QGroupBox("Image View")
        vl = QVBoxLayout(vf)
        vl.setContentsMargins(4, 4, 4, 4)
        self._image_label = ImageLabel()
        vl.addWidget(self._image_label)
        rs.addWidget(vf)

        # Log
        lf = QGroupBox("Log")
        ll = QVBoxLayout(lf)
        ll.setContentsMargins(4, 4, 4, 4)
        lh = QHBoxLayout()
        lh.addStretch()
        lh.addWidget(QPushButton("Clear", clicked=self._clear_log))
        ll.addLayout(lh)
        self._log_text = QPlainTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setMaximumBlockCount(5000)
        self._log_text.setStyleSheet(
            "font-family: Consolas; font-size: 9pt; background-color: #1e1e1e; color: #d4d4d4;"
        )
        ll.addWidget(self._log_text)
        rs.addWidget(lf)

    # ==============================================================
    # Logging
    # ==============================================================
    def _log(self, msg):
        self._log_text.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def _clear_log(self):
        self._log_text.clear()

    # ==============================================================
    # SDK Management
    # ==============================================================
    def _refresh_sdk_status(self):
        pinfo = platform_info()
        self._platform_label.setText(
            f"Platform: {pinfo['system']}  |  Library: {pinfo['lib_name']}"
        )
        self._sdk_status_label.setText("SDK: Ready")
        ver = self.sdk.get_sdk_version_string()
        self._sdk_ver_label.setText(f"SDK Version: {ver}")

    def _check_sdk(self):
        if not self.sdk.lib:
            try:
                self.sdk.init_resource()
            except RuntimeError as e:
                self._log(f"SDK init failed: {e}")
                return False
        return True

    def _auto_init(self):
        if not self._check_sdk():
            self._log("SDK not available, connect a QHYCCD camera.")
            return

        self._log("=== Auto Init ===")

        ret = self.sdk.init_resource()
        if ret != QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"ERROR: InitQHYCCDResource: {error_string(ret)}")
            return
        self._log("InitQHYCCDResource: OK")
        self.sdk.enable_message(True)

        count = self.sdk.scan()
        self._log(f"ScanQHYCCD: {count} camera(s)")
        if count == 0:
            self._log("No camera found.")
            self.sdk.release_resource()
            return

        self._camera_list.clear()
        self._cam_listwidget.clear()
        for i in range(count):
            cid = self.sdk.get_camera_id(i)
            model = self.sdk.get_camera_model(cid) if cid else "Unknown"
            self._cam_listwidget.addItem(f"[{i}] {model} ({cid})")
            self._camera_list.append(cid)

        cid = self._camera_list[0]
        self._log(f"OpenQHYCCD({cid})...")
        handle = self.sdk.open(cid)
        if not handle:
            self._log("ERROR: OpenQHYCCD failed.")
            self.sdk.release_resource()
            return

        model = self.sdk.get_camera_model(cid)
        fw = self.sdk.get_fw_version()
        self._cam_model_label.setText(f"Model: {model}  FW: {fw}")
        self._log(f"Camera opened: {model}, FW: {fw}")

        bayer = self.sdk.get_color_bayer()
        color_str = f"Color ({bayer.name})" if bayer else "Mono"
        extra = []
        if self.sdk.has_cooler():
            extra.append("Cooler")
        if self.sdk.has_gps():
            extra.append("GPS")
        self._cam_color_label.setText(f"{color_str}  {' | '.join(extra)}")

        self.sdk.set_read_mode(self._current_read_mode)
        self._log(f"SetQHYCCDReadMode({self._current_read_mode})")
        self.sdk.set_stream_mode(LIVE_MODE)
        self._log("SetQHYCCDStreamMode(LIVE_MODE)")

        ret = self.sdk.init()
        if ret != QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"ERROR: InitQHYCCD: {error_string(ret)}")
            return
        self._log("InitQHYCCD: OK")

        self.sdk.set_bits_mode(int(self._bits_combo.currentText()))
        self._log(f"SetQHYCCDBitsMode({self._bits_combo.currentText()})")

        chip = self.sdk.get_chip_info()
        if chip:
            cw, ch, iw, ih, pw, ph, bpp = chip
            self._log(
                f"Chip: {cw:.1f}x{ch:.1f}mm, Max: {iw}x{ih}, "
                f"Pixel: {pw:.1f}x{ph:.1f}um, {bpp}bit"
            )

        area = self.sdk.get_effective_area()
        if area and area[2] > 0:
            self._full_w, self._full_h = area[2], area[3]
            self._res_x_entry.setText(str(area[0]))
            self._res_y_entry.setText(str(area[1]))
            self._res_w_entry.setText(str(area[2]))
            self._res_h_entry.setText(str(area[3]))
            self.sdk.set_resolution(area[0], area[1], area[2], area[3])
            self._log(f"SetQHYCCDResolution: ({area[0]},{area[1]}) {area[2]}x{area[3]}")
        else:
            self._full_w = self._full_h = 0

        self._mem_len = self.sdk.get_mem_length()
        self._log(f"GetQHYCCDMemLength: {self._mem_len} bytes")

        self.sdk.set_param(ControlID.CONTROL_EXPOSURE, 100000.0)

        if self.sdk.has_gps():
            self.sdk.set_param(ControlID.CAM_GPS, 1.0)
            self._log("GPS: Enabled")

        self._btn_single.setEnabled(True)
        self._btn_live.setEnabled(True)
        self._btn_close.setEnabled(True)
        self._enable_params(True)
        self._log("=== Ready ===")

    def _close_camera(self):
        if self._live_running:
            self._stop_live()
        if self.sdk.handle:
            self._log("Closing camera...")
            self.sdk.close()
            self._log("Camera closed.")

        self._cam_model_label.setText("Model: --")
        self._cam_color_label.setText("")
        self._gps_label.setText("GPS: --")
        self._btn_close.setEnabled(False)
        self._btn_single.setEnabled(False)
        self._btn_live.setEnabled(False)
        self._btn_save.setEnabled(False)
        self._enable_params(False)

    def _enable_params(self, enabled):
        for entry in self._params.values():
            entry.setEnabled(enabled)

    # ==============================================================
    # Parameters
    # ==============================================================
    def _set_param(self, control_id, entry):
        if not self._check_sdk() or not self.sdk.handle:
            return
        try:
            val = float(entry.text())
        except ValueError:
            self._log("ERROR: Invalid numeric value.")
            return
        ctrl_name = ControlID(control_id).name
        ret = self.sdk.set_param(control_id, val)
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"Set {ctrl_name} = {val}")
        else:
            self._log(f"ERROR set {ctrl_name}: {error_string(ret)}")

    def _get_param(self, control_id, entry):
        if not self._check_sdk() or not self.sdk.handle:
            return
        val = self.sdk.get_param(control_id)
        ctrl_name = ControlID(control_id).name
        if val is not None:
            entry.setText(str(int(val)) if val == int(val) else f"{val:.2f}")
            self._log(f"Get {ctrl_name} = {val}")
        else:
            self._log(f"ERROR get {ctrl_name}: no value")

    def _set_temp(self):
        if not self._check_sdk() or not self.sdk.handle:
            return
        try:
            val = float(self._temp_entry.text())
        except ValueError:
            self._log("ERROR: Invalid temperature value.")
            return
        ret = self.sdk.control_temp(val)
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"Set target temp = {val} C")
        else:
            self._log(f"ERROR set temp: {error_string(ret)}")

    def _get_temp(self):
        if not self._check_sdk() or not self.sdk.handle:
            return
        t = self.sdk.get_temp()
        p = self.sdk.get_cooler_pwm()
        if t is not None:
            self._temp_entry.setText(f"{t:.1f}")
            self._log(f"Current temp: {t:.1f} C, PWM: {p}")

    # ==============================================================
    # Resolution / ROI
    # ==============================================================
    def _set_resolution(self):
        if not self._check_sdk() or not self.sdk.handle:
            return
        try:
            x = int(self._res_x_entry.text() or 0)
            y = int(self._res_y_entry.text() or 0)
            w = int(self._res_w_entry.text() or 0)
            h = int(self._res_h_entry.text() or 0)
        except ValueError:
            self._log("ERROR: Invalid resolution values.")
            return
        ret = self.sdk.set_resolution(x, y, w, h)
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"Resolution set: ({x},{y}) {w}x{h}")
        else:
            self._log(f"ERROR set resolution: {error_string(ret)}")

    def _fill_effective_area(self):
        if not self._check_sdk() or not self.sdk.handle:
            return
        area = self.sdk.get_effective_area()
        if area:
            self._res_x_entry.setText(str(area[0]))
            self._res_y_entry.setText(str(area[1]))
            self._res_w_entry.setText(str(area[2]))
            self._res_h_entry.setText(str(area[3]))
            self._log(f"Effective area: ({area[0]},{area[1]}) {area[2]}x{area[3]}")

    # ==============================================================
    # Binning / Bits
    # ==============================================================
    def _set_binning(self):
        if not self._check_sdk() or not self.sdk.handle:
            return
        try:
            bx = int(self._binx_entry.text())
            by = int(self._biny_entry.text())
        except ValueError:
            self._log("ERROR: Invalid bin values.")
            return
        ret = self.sdk.set_bin_mode(bx, by)
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"Binning set: {bx}x{by}")
        else:
            self._log(f"ERROR set binning: {error_string(ret)}")

    def _set_bits(self):
        if not self._check_sdk() or not self.sdk.handle:
            return
        bits = int(self._bits_combo.currentText())
        ret = self.sdk.set_bits_mode(bits)
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"Bits mode set: {bits}bit")
        else:
            self._log(f"ERROR set bits: {error_string(ret)}")

    # ==============================================================
    # Single frame capture
    # ==============================================================
    def _capture_single(self):
        if not self.sdk.handle:
            return

        self._log("Single frame: switching to SINGLE_MODE...")

        self.sdk.set_stream_mode(SINGLE_MODE)
        ret = self.sdk.init()
        if ret != QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"ERROR re-init: {error_string(ret)}")
            return

        ret = self.sdk.exp_single_frame()
        if ret == QHYCCDError.QHYCCD_READ_DIRECTLY:
            self._log("READ_DIRECTLY mode, reading...")
            self._read_single_frame(after_switch_back=True)
        elif ret == QHYCCDError.QHYCCD_SUCCESS:
            self._log("Exposure started, waiting...")
            QTimer.singleShot(100, lambda: self._read_single_frame(after_switch_back=True))
        else:
            self._log(f"ERROR exposure: {error_string(ret)}")
            self._restore_live_mode()

    def _read_single_frame(self, after_switch_back=False):
        result = self.sdk.get_single_frame()
        if result is None:
            self._log("Waiting for frame...")
            QTimer.singleShot(300, lambda: self._read_single_frame(after_switch_back))
            return
        w, h, bpp, ch, imgdata = result
        self._log(f"Frame: {w}x{h}, {bpp}bit, {ch}ch, {len(imgdata)} bytes")
        self._display_image(w, h, bpp, ch, imgdata)
        self._btn_save.setEnabled(True)

        if after_switch_back:
            self._restore_live_mode()

    def _restore_live_mode(self):
        self._log("Restoring LIVE_MODE...")
        self.sdk.set_stream_mode(LIVE_MODE)
        self.sdk.init()
        self._log("LIVE_MODE restored.")

    # ==============================================================
    # Live capture
    # ==============================================================
    def _toggle_live(self):
        if self._live_running:
            self._stop_live()
        else:
            self._start_live()

    def _start_live(self):
        if not self.sdk.handle:
            return
        self._log("Starting live video...")
        self.sdk.set_param(ControlID.CONTROL_EXPOSURE, 100000.0)
        ret = self.sdk.begin_live()
        if ret != QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"ERROR begin live: {error_string(ret)}")
            return
        self._live_running = True
        self._btn_live.setText("Stop Live")
        self._btn_single.setEnabled(False)
        self._btn_save.setEnabled(True)
        self._live_thread = threading.Thread(target=self._live_loop, daemon=True)
        self._live_thread.start()

    def _live_loop(self):
        while self._live_running:
            result = self.sdk.get_live_frame()
            if result is None:
                time.sleep(0.05)
                continue
            w, h, bpp, ch, imgdata = result
            self.live_frame_signal.emit(w, h, bpp, ch, imgdata)
            time.sleep(0.03)

    def _stop_live(self):
        self._log("Stopping live video...")
        self._live_running = False
        if self._live_thread:
            self._live_thread.join(timeout=2)
            self._live_thread = None
        self.sdk.stop_live()
        self._btn_live.setText("Start Live")
        self._btn_single.setEnabled(True)
        self._log("Live video stopped.")

    def _on_live_frame(self, w, h, bpp, ch, imgdata):
        if not self._live_running:
            return
        self._display_image(w, h, bpp, ch, imgdata)

    # ==============================================================
    # Image display & save
    # ==============================================================
    def _parse_frame_gps(self, imgdata, w, bpp, ch):
        gps = parse_gps_from_frame(imgdata, w, bpp, ch)
        if gps and gps.get("locked"):
            self._gps_label.setText(
                f"GPS: {gps['year']}-{gps['month']:02d}-{gps['day']:02d} "
                f"{gps['hour']:02d}:{gps['minute']:02d}:{gps['second']:02d} UTC  "
                f"Seq: {gps['seq']}"
            )
        elif gps:
            self._gps_label.setText(f"GPS: Unlocked (seq={gps['seq']})")

    def _display_image(self, w, h, bpp, ch, imgdata):
        self._parse_frame_gps(imgdata, w, bpp, ch)
        try:
            dtype = np.uint16 if bpp == 16 else np.uint8
            shape = (h, w) if ch == 1 else (h, w, ch)
            arr = np.frombuffer(imgdata, dtype=dtype).reshape(shape)

            if bpp == 16:
                arr = (arr >> 8).astype(np.uint8)
            if ch == 1:
                arr = np.ascontiguousarray(arr)

            img = Image.fromarray(arr)
            self._captured_image = img

            w_img, h_img = img.size
            raw_bytes = img.tobytes("raw", img.mode)
            if img.mode == "L":
                fmt = QImage.Format_Grayscale8
                bpl = w_img
            elif img.mode == "RGB":
                fmt = QImage.Format_RGB888
                bpl = w_img * 3
            else:
                img_rgb = img.convert("RGB")
                raw_bytes = img_rgb.tobytes("raw", "RGB")
                fmt = QImage.Format_RGB888
                bpl = img_rgb.width

            qimg = QImage(raw_bytes, w_img, h_img, bpl, fmt)
            pixmap = QPixmap.fromImage(qimg)
            self._image_label.set_display_pixmap(pixmap)
            self._capture_info_label.setText(f"Image: {w}x{h}")
        except Exception as e:
            self._log(f"ERROR rendering image: {e}")

    def _save_image(self):
        if self._captured_image is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Image",
            filter="PNG Image (*.png);;FITS (*.fits);;TIFF (*.tiff);;All (*.*)",
        )
        if path:
            self._captured_image.save(path)
            self._log(f"Image saved: {path}")

    # ==============================================================
    # Cleanup
    # ==============================================================
    def closeEvent(self, event):
        if self._live_running:
            self._stop_live()
        if self.sdk.handle:
            self.sdk.close()
        self.sdk.release_resource()
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = QHYCamWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
