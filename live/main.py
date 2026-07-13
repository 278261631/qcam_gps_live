import os
import sys
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from PIL import Image, ImageTk
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live.sdk_wrapper import (
    QHYCCDSDK, ControlID, QHYCCDError, error_string, platform_info,
    SINGLE_MODE, LIVE_MODE, BayerPattern,
)


class QHYCamApp:

    def __init__(self, root):
        self.root = root
        self.root.title("QHYCCD Camera Live")
        self.root.geometry("1100x750")
        self.root.minsize(900, 600)

        self.sdk = QHYCCDSDK()
        self._live_running = False
        self._live_thread = None
        self._captured_image = None
        self._photo = None
        self._camera_list = []
        self._current_read_mode = 0

        self._build_ui()
        self._refresh_sdk_status()

    # ==============================================================
    # UI Construction
    # ==============================================================
    def _build_ui(self):
        main_pw = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_pw.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        left = ttk.Frame(main_pw, width=360)
        main_pw.add(left, weight=0)

        right = ttk.Frame(main_pw)
        main_pw.add(right, weight=1)

        self._build_left_panel(left)
        self._build_right_panel(right)

    def _build_left_panel(self, parent):
        canvas = tk.Canvas(parent, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        scrolly = ttk.Frame(canvas)
        scrolly.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrolly, anchor="nw", tags="inner")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def _bind_mousewheel(event):
            canvas.bind_all("<MouseWheel>", lambda ev: canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units"))
        canvas.bind("<Enter>", _bind_mousewheel)
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        frame = ttk.Frame(scrolly)
        frame.pack(fill=tk.X, padx=6, pady=2)

        # --- SDK Status ---
        sdkf = ttk.LabelFrame(frame, text="SDK Status", padding=6)
        sdkf.pack(fill=tk.X, pady=(0, 4))

        self._sdk_status_var = tk.StringVar(value="SDK: Not loaded")
        ttk.Label(sdkf, textvariable=self._sdk_status_var, foreground="gray").pack(anchor=tk.W)
        self._sdk_ver_var = tk.StringVar(value="")
        ttk.Label(sdkf, textvariable=self._sdk_ver_var, foreground="#555").pack(anchor=tk.W)
        self._platform_var = tk.StringVar(value="")
        ttk.Label(sdkf, textvariable=self._platform_var, foreground="#555").pack(anchor=tk.W)
        ttk.Button(sdkf, text="Reload SDK", command=self._reload_sdk).pack(anchor=tk.W, pady=(2, 0))

        # --- Camera Scan ---
        scanf = ttk.LabelFrame(frame, text="Camera Scan", padding=6)
        scanf.pack(fill=tk.X, pady=4)

        btnf = ttk.Frame(scanf)
        btnf.pack(fill=tk.X)
        ttk.Button(btnf, text="Scan Cameras", command=self._scan_cameras).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btnf, text="Refresh List", command=self._scan_cameras).pack(side=tk.LEFT)
        self._btn_auto_scan = ttk.Button(btnf, text="Auto Init (Scan + Open + Init)",
                                          command=self._auto_init, state=tk.DISABLED)
        self._btn_auto_scan.pack(side=tk.RIGHT)

        self._cam_listbox = tk.Listbox(scanf, height=6, exportselection=False)
        self._cam_listbox.pack(fill=tk.X, pady=(4, 0))
        self._cam_listbox.bind("<<ListboxSelect>>", self._on_camera_select)

        # --- Camera Operations ---
        camf = ttk.LabelFrame(frame, text="Camera Control", padding=6)
        camf.pack(fill=tk.X, pady=4)

        self._cam_btn_frame = ttk.Frame(camf)
        self._cam_btn_frame.pack(fill=tk.X)
        self._btn_open = ttk.Button(self._cam_btn_frame, text="Open Camera",
                                     command=self._open_camera, state=tk.DISABLED)
        self._btn_open.pack(side=tk.LEFT, padx=(0, 4))
        self._btn_close = ttk.Button(self._cam_btn_frame, text="Close Camera",
                                      command=self._close_camera, state=tk.DISABLED)
        self._btn_close.pack(side=tk.LEFT)
        self._btn_init = ttk.Button(self._cam_btn_frame, text="Init Camera",
                                     command=self._init_camera, state=tk.DISABLED)
        self._btn_init.pack(side=tk.LEFT, padx=(4, 0))

        self._btn_auto = ttk.Button(self._cam_btn_frame, text="Auto Init",
                                     command=self._auto_init, state=tk.DISABLED)
        self._btn_auto.pack(side=tk.LEFT, padx=(4, 0))

        self._cam_model_var = tk.StringVar(value="Model: --")
        ttk.Label(camf, textvariable=self._cam_model_var).pack(anchor=tk.W, pady=(4, 0))
        self._cam_color_var = tk.StringVar(value="")
        ttk.Label(camf, textvariable=self._cam_color_var).pack(anchor=tk.W)

        # --- Read Mode ---
        rdf = ttk.LabelFrame(frame, text="Read Mode", padding=6)
        rdf.pack(fill=tk.X, pady=4)
        rmf = ttk.Frame(rdf)
        rmf.pack(fill=tk.X)
        self._read_mode_combo = ttk.Combobox(rmf, state="readonly", width=30)
        self._read_mode_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._read_mode_combo.bind("<<ComboboxSelected>>", self._on_read_mode_select)
        self._read_mode_info_var = tk.StringVar(value="")
        ttk.Label(rdf, textvariable=self._read_mode_info_var, foreground="#555").pack(anchor=tk.W, pady=(2, 0))

        # --- Parameters ---
        paramf = ttk.LabelFrame(frame, text="Parameters", padding=6)
        paramf.pack(fill=tk.X, pady=4)

        self._params = {}
        param_specs = [
            ("Exposure (us)", ControlID.CONTROL_EXPOSURE, 100000),
            ("Gain", ControlID.CONTROL_GAIN, 10),
            ("Offset", ControlID.CONTROL_OFFSET, 140),
            ("USB Traffic", ControlID.CONTROL_USBTRAFFIC, 30),
            ("Speed", ControlID.CONTROL_SPEED, 0),
            ("DDR Buffer", ControlID.CONTROL_DDR, 1),
        ]

        for label_text, ctrl_id, default in param_specs:
            row = ttk.Frame(paramf)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=label_text, width=16, anchor=tk.W).pack(side=tk.LEFT)
            var = tk.StringVar(value=str(default))
            entry = ttk.Entry(row, textvariable=var, width=12)
            entry.pack(side=tk.LEFT, padx=(4, 2))
            ttk.Button(row, text="Set", width=4,
                       command=lambda cid=ctrl_id, v=var: self._set_param(cid, v)).pack(side=tk.LEFT, padx=1)
            ttk.Button(row, text="Get", width=4,
                       command=lambda cid=ctrl_id, v=var: self._get_param(cid, v)).pack(side=tk.LEFT, padx=1)
            self._params[ctrl_id] = (entry, var)

        # --- Temperature ---
        trow = ttk.Frame(paramf)
        trow.pack(fill=tk.X, pady=1)
        ttk.Label(trow, text="Target Temp (C)", width=16, anchor=tk.W).pack(side=tk.LEFT)
        self._temp_var = tk.StringVar(value="0")
        ttk.Entry(trow, textvariable=self._temp_var, width=12).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Button(trow, text="Set", width=4, command=self._set_temp).pack(side=tk.LEFT, padx=1)
        ttk.Button(trow, text="Get", width=4, command=self._get_temp).pack(side=tk.LEFT, padx=1)

        # --- ROI ---
        resf = ttk.LabelFrame(frame, text="Resolution / ROI", padding=6)
        resf.pack(fill=tk.X, pady=4)

        r1 = ttk.Frame(resf)
        r1.pack(fill=tk.X, pady=1)
        ttk.Label(r1, text="X", width=4).pack(side=tk.LEFT)
        self._res_x_var = tk.StringVar(value="0")
        ttk.Entry(r1, textvariable=self._res_x_var, width=6).pack(side=tk.LEFT, padx=2)
        ttk.Label(r1, text="Y", width=4).pack(side=tk.LEFT, padx=(6, 0))
        self._res_y_var = tk.StringVar(value="0")
        ttk.Entry(r1, textvariable=self._res_y_var, width=6).pack(side=tk.LEFT, padx=2)

        r2 = ttk.Frame(resf)
        r2.pack(fill=tk.X, pady=1)
        ttk.Label(r2, text="W", width=4).pack(side=tk.LEFT)
        self._res_w_var = tk.StringVar(value="0")
        ttk.Entry(r2, textvariable=self._res_w_var, width=6).pack(side=tk.LEFT, padx=2)
        ttk.Label(r2, text="H", width=4).pack(side=tk.LEFT, padx=(6, 0))
        self._res_h_var = tk.StringVar(value="0")
        ttk.Entry(r2, textvariable=self._res_h_var, width=6).pack(side=tk.LEFT, padx=2)

        ttk.Button(resf, text="Set Resolution", command=self._set_resolution).pack(pady=(4, 0))
        ttk.Button(resf, text="Fill Effective Area", command=self._fill_effective_area).pack(pady=(2, 0))

        # --- Bin / Bits ---
        binf = ttk.LabelFrame(frame, text="Binning / Bits", padding=6)
        binf.pack(fill=tk.X, pady=4)

        brf = ttk.Frame(binf)
        brf.pack(fill=tk.X, pady=1)
        ttk.Label(brf, text="Bin X").pack(side=tk.LEFT, padx=(0, 2))
        self._binx_var = tk.StringVar(value="1")
        ttk.Entry(brf, textvariable=self._binx_var, width=6).pack(side=tk.LEFT)
        ttk.Label(brf, text="Bin Y").pack(side=tk.LEFT, padx=(6, 2))
        self._biny_var = tk.StringVar(value="1")
        ttk.Entry(brf, textvariable=self._biny_var, width=6).pack(side=tk.LEFT)
        ttk.Button(brf, text="Set Bin", command=self._set_binning).pack(side=tk.LEFT, padx=(4, 0))

        btf = ttk.Frame(binf)
        btf.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(btf, text="Bits").pack(side=tk.LEFT, padx=(0, 2))
        self._bits_combo = ttk.Combobox(btf, values=["8", "16"], state="readonly", width=8)
        self._bits_combo.set("8")
        self._bits_combo.pack(side=tk.LEFT)
        ttk.Button(btf, text="Set Bits", command=self._set_bits).pack(side=tk.LEFT, padx=(4, 0))

        # --- Capture ---
        capf = ttk.LabelFrame(frame, text="Capture", padding=6)
        capf.pack(fill=tk.X, pady=4)

        bcap = ttk.Frame(capf)
        bcap.pack(fill=tk.X)
        self._btn_single = ttk.Button(bcap, text="Single Frame", command=self._capture_single, state=tk.DISABLED)
        self._btn_single.pack(side=tk.LEFT, padx=(0, 4))
        self._btn_live = ttk.Button(bcap, text="Start Live", command=self._toggle_live, state=tk.DISABLED)
        self._btn_live.pack(side=tk.LEFT)

        bsave = ttk.Frame(capf)
        bsave.pack(fill=tk.X, pady=(4, 0))
        self._btn_save = ttk.Button(bsave, text="Save Image", command=self._save_image, state=tk.DISABLED)
        self._btn_save.pack(side=tk.LEFT)

        self._capture_info_var = tk.StringVar(value="")
        ttk.Label(capf, textvariable=self._capture_info_var, foreground="#555").pack(anchor=tk.W, pady=(4, 0))

    def _build_right_panel(self, parent):
        viewf = ttk.LabelFrame(parent, text="Image View", padding=4)
        viewf.pack(fill=tk.BOTH, expand=True)
        self._image_canvas = tk.Canvas(viewf, bg="#1a1a1a", highlightthickness=0)
        self._image_canvas.pack(fill=tk.BOTH, expand=True)
        self._image_canvas.bind("<Configure>", self._on_canvas_resize)

        logf = ttk.LabelFrame(parent, text="Log", padding=4)
        logf.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(logf, text="Clear", command=self._clear_log).pack(side=tk.RIGHT)
        self._log_text = tk.Text(logf, height=8, state=tk.DISABLED, wrap=tk.WORD,
                                  font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self._log_text.pack(fill=tk.X, expand=True)

    # ==============================================================
    # Logging
    # ==============================================================
    def _log(self, msg):
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self._log_text.see(tk.END)
        self._log_text.configure(state=tk.DISABLED)

    def _clear_log(self):
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.delete("1.0", tk.END)
        self._log_text.configure(state=tk.DISABLED)

    # ==============================================================
    # SDK Management
    # ==============================================================
    def _refresh_sdk_status(self):
        pinfo = platform_info()
        self._platform_var.set(f"Platform: {pinfo['system']}  |  Library: {pinfo['lib_name']}")
        if pinfo["loaded"]:
            self._sdk_status_var.set("SDK: Loaded")
            ver = self.sdk.get_sdk_version_string()
            self._sdk_ver_var.set(f"SDK Version: {ver}")
            self._btn_auto_scan.configure(state=tk.NORMAL)
        else:
            self._sdk_status_var.set("SDK: Not Loaded (camera hardware/device not present)")
            self._sdk_ver_var.set("Connect a QHYCCD camera to load the SDK.")

    def _reload_sdk(self):
        self._sdk_status_var.set("SDK: Loading...")
        self.root.update()
        self.sdk = QHYCCDSDK()
        self.sdk._ensure_lib()
        self._refresh_sdk_status()
        self._log("SDK reloaded.")
        if self.sdk.lib:
            self._log("Tip: click 'Auto Init' for one-click setup.")

    def _check_sdk(self):
        if not self.sdk.lib:
            messagebox.showwarning("SDK Not Available",
                                    "The QHYCCD SDK library is not loaded.\n"
                                    "This typically means no QHYCCD camera is connected,\n"
                                    "or the driver/sdk files are not installed.")
            return False
        return True

    def _auto_init(self):
        """One-click auto initialization: scan, open first camera, init.

        Full sequence matching SDK demos:
          InitQHYCCDResource -> ScanQHYCCD -> GetQHYCCDId(0) ->
          OpenQHYCCD -> SetQHYCCDReadMode(0) -> SetQHYCCDStreamMode ->
          InitQHYCCD -> [chip info/params/resolution] -> ready
        """
        if not self._check_sdk():
            return

        self._log("=== Auto Init started ===")

        # 1. Init SDK resource
        ret = self.sdk.init_resource()
        if ret != QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"ERROR: InitQHYCCDResource failed: {error_string(ret)}")
            messagebox.showerror("Auto Init", f"InitQHYCCDResource failed: {error_string(ret)}")
            return
        self._log("InitQHYCCDResource: OK")
        self.sdk.enable_message(True)

        # 2. Scan cameras
        count = self.sdk.scan()
        self._log(f"ScanQHYCCD: found {count} camera(s)")
        if count == 0:
            self._log("ERROR: No camera found.")
            messagebox.showwarning("Auto Init", "No QHYCCD camera found.\nPlease check USB connection and power.")
            self.sdk.release_resource()
            return

        # 3. Refresh list box
        self._camera_list.clear()
        self._cam_listbox.delete(0, tk.END)
        for i in range(count):
            cid = self.sdk.get_camera_id(i)
            model = self.sdk.get_camera_model(cid) if cid else "Unknown"
            self._cam_listbox.insert(tk.END, f"[{i}] {model} ({cid})")
            self._camera_list.append(cid)
        self._cam_listbox.selection_set(0)

        # 4. Open first camera
        cid = self._camera_list[0]
        self._log(f"OpenQHYCCD({cid})...")
        handle = self.sdk.open(cid)
        if not handle:
            self._log("ERROR: OpenQHYCCD failed.")
            messagebox.showerror("Auto Init", "Failed to open camera.")
            self.sdk.release_resource()
            return

        model = self.sdk.get_camera_model(cid)
        fw = self.sdk.get_fw_version()
        self._cam_model_var.set(f"Model: {model}  FW: {fw}  ({cid})")
        self._log(f"Camera opened: {model}, FW: {fw}")

        bayer = self.sdk.get_color_bayer()
        color_str = f"Color ({bayer.name})" if bayer else "Mono"
        extra = []
        if self.sdk.has_cooler():
            extra.append("Cooler")
        if self.sdk.has_gps():
            extra.append("GPS")
        self._cam_color_var.set(f"{color_str}  {' | '.join(extra)}")

        # 5. Populate read modes
        self._populate_read_modes()

        # 6. Init the camera (full sequence)
        self._init_camera()

        # Enable buttons
        self._btn_open.configure(state=tk.DISABLED)
        self._btn_close.configure(state=tk.NORMAL)

        self._log("=== Auto Init complete ===")

    # ==============================================================
    # Camera Discovery
    # ==============================================================
    def _scan_cameras(self):
        if not self._check_sdk():
            self._log("Cannot scan: SDK not loaded.")
            return

        self._log("Scanning for cameras...")
        count = self.sdk.scan()
        self._log(f"Found {count} camera(s).")

        self._camera_list.clear()
        self._cam_listbox.delete(0, tk.END)

        for i in range(count):
            cid = self.sdk.get_camera_id(i)
            model = self.sdk.get_camera_model(cid) if cid else "Unknown"
            entry = f"[{i}] {model} ({cid})"
            self._cam_listbox.insert(tk.END, entry)
            self._camera_list.append(cid)

        if count == 0:
            self._cam_listbox.insert(tk.END, "(No cameras found)")

    # ==============================================================
    # Camera Open / Init (matches SDK demo sequence)
    #
    # Demos sequence:
    #   InitQHYCCDResource() -> ScanQHYCCD() -> GetQHYCCDId() ->
    #   OpenQHYCCD() -> [FW version] -> [check mode support] ->
    #   SetQHYCCDReadMode() -> SetQHYCCDStreamMode() ->
    #   InitQHYCCD() -> [chip info / params / resolution] -> capture
    # ==============================================================
    def _on_camera_select(self, event):
        sel = self._cam_listbox.curselection()
        if sel:
            self._btn_open.configure(state=tk.NORMAL)
        else:
            self._btn_open.configure(state=tk.DISABLED)

    def _get_selected_camera_id(self):
        sel = self._cam_listbox.curselection()
        if not sel:
            messagebox.showwarning("No Camera", "Please select a camera from the list.")
            return None
        idx = sel[0]
        if idx >= len(self._camera_list):
            return None
        return self._camera_list[idx]

    def _open_camera(self):
        cid = self._get_selected_camera_id()
        if not cid or not self._check_sdk():
            return

        self._log(f"Opening camera: {cid}")
        handle = self.sdk.open(cid)
        if not handle:
            self._log("ERROR: Failed to open camera.")
            messagebox.showerror("Error", "Failed to open camera.")
            return

        model = self.sdk.get_camera_model(cid)
        fw = self.sdk.get_fw_version()
        self._cam_model_var.set(f"Model: {model}  FW: {fw}  ({cid})")
        self._log(f"Camera opened: {model}, FW: {fw}")

        # Detect color / Bayer pattern
        bayer = self.sdk.get_color_bayer()
        if bayer:
            color_str = f"Color ({bayer.name})"
        else:
            color_str = "Mono"

        has_cool = self.sdk.has_cooler()
        has_gps = self.sdk.has_gps()
        extra = []
        if has_cool:
            extra.append("Cooler")
        if has_gps:
            extra.append("GPS")
        self._cam_color_var.set(f"{color_str}  {' | '.join(extra)}")

        # Populate read modes
        self._populate_read_modes()

        self._btn_init.configure(state=tk.NORMAL)
        self._btn_close.configure(state=tk.NORMAL)
        self._btn_open.configure(state=tk.DISABLED)

    def _populate_read_modes(self):
        num = self.sdk.get_number_of_read_modes()
        if num <= 0:
            self._read_mode_combo["values"] = ["Default"]
            self._read_mode_combo.set("Default")
            return

        modes = []
        for i in range(num):
            name = self.sdk.get_read_mode_name(i) or f"Mode {i}"
            res = self.sdk.get_read_mode_resolution(i)
            if res:
                modes.append(f"[{i}] {name} ({res[0]}x{res[1]})")
            else:
                modes.append(f"[{i}] {name}")
        self._read_mode_combo["values"] = modes
        self._read_mode_combo.current(0)
        self._on_read_mode_select()

    def _on_read_mode_select(self, event=None):
        sel = self._read_mode_combo.current()
        if sel >= 0:
            self._current_read_mode = sel
            res = self.sdk.get_read_mode_resolution(sel)
            if res:
                self._read_mode_info_var.set(f"Base resolution: {res[0]} x {res[1]}")
            else:
                self._read_mode_info_var.set("")

    def _init_camera(self):
        if not self._check_sdk():
            return

        # 1. Set read mode (must come before stream mode, per demos)
        self._log(f"SetQHYCCDReadMode({self._current_read_mode})...")
        ret = self.sdk.set_read_mode(self._current_read_mode)
        if ret != QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"WARNING: SetQHYCCDReadMode: {error_string(ret)}")

        # 2. Set stream mode (also before InitQHYCCD, per demos)
        self._log("SetQHYCCDStreamMode(SINGLE_MODE)...")
        ret = self.sdk.set_stream_mode(SINGLE_MODE)
        if ret != QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"WARNING: SetQHYCCDStreamMode: {error_string(ret)}")

        # 3. Init camera
        self._log("InitQHYCCD...")
        ret = self.sdk.init()
        if ret != QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"ERROR: Init failed: {error_string(ret)}")
            messagebox.showerror("Error", f"Camera init failed: {error_string(ret)}")
            return
        self._log("Camera initialized successfully.")

        # 4. Get chip info
        chip = self.sdk.get_chip_info()
        if chip:
            cw, ch, iw, ih, pw, ph, bpp = chip
            self._log(f"Chip: {cw:.1f}x{ch:.1f}mm, Max: {iw}x{ih}, Pixel: {pw:.1f}x{ph:.1f}um, {bpp}bit")

        # 5. Get effective / overscan areas
        area = self.sdk.get_effective_area()
        if area and area[2] > 0 and area[3] > 0:
            self._log(f"Effective area: ({area[0]},{area[1]}) {area[2]}x{area[3]}")
            self._res_x_var.set(str(area[0]))
            self._res_y_var.set(str(area[1]))
            self._res_w_var.set(str(area[2]))
            self._res_h_var.set(str(area[3]))

        oscan = self.sdk.get_overscan_area()
        if oscan:
            self._log(f"Overscan area: ({oscan[0]},{oscan[1]}) {oscan[2]}x{oscan[3]}")

        # 6. Set default params (matching demos)
        self.sdk.set_param(ControlID.CONTROL_USBTRAFFIC, 30.0)
        self.sdk.set_param(ControlID.CONTROL_GAIN, 10.0)
        self.sdk.set_param(ControlID.CONTROL_OFFSET, 140.0)
        self.sdk.set_param(ControlID.CONTROL_EXPOSURE, 100000.0)
        self.sdk.set_param(ControlID.CONTROL_DDR, 1.0)
        self.sdk.set_bits_mode(8)
        self.sdk.set_debayer_onoff(False)

        # Set default resolution
        if area and area[2] > 0:
            self.sdk.set_resolution(area[0], area[1], area[2], area[3])

        self._btn_single.configure(state=tk.NORMAL)
        self._btn_live.configure(state=tk.NORMAL)
        self._enable_params(True)

    def _close_camera(self):
        if self._live_running:
            self._stop_live()
        if self.sdk.handle:
            self._log("Closing camera...")
            self.sdk.close()
            self._log("Camera closed.")

        self._cam_model_var.set("Model: --")
        self._cam_color_var.set("")
        self._read_mode_combo["values"] = []
        self._read_mode_combo.set("")
        self._read_mode_info_var.set("")
        self._btn_close.configure(state=tk.DISABLED)
        self._btn_init.configure(state=tk.DISABLED)
        self._btn_single.configure(state=tk.DISABLED)
        self._btn_live.configure(state=tk.DISABLED)
        self._btn_save.configure(state=tk.DISABLED)
        self._enable_params(False)
        self._btn_open.configure(state=tk.NORMAL)

    def _enable_params(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        for _cid, (entry, _var) in self._params.items():
            entry.configure(state=state)

    # ==============================================================
    # Parameters
    # ==============================================================
    def _set_param(self, control_id, var):
        if not self._check_sdk() or not self.sdk.handle:
            return
        try:
            val = float(var.get())
        except ValueError:
            self._log("ERROR: Invalid numeric value.")
            return
        ctrl_name = ControlID(control_id).name
        ret = self.sdk.set_param(control_id, val)
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"Set {ctrl_name} = {val}")
        else:
            self._log(f"ERROR set {ctrl_name}: {error_string(ret)}")

    def _get_param(self, control_id, var):
        if not self._check_sdk() or not self.sdk.handle:
            return
        val = self.sdk.get_param(control_id)
        ctrl_name = ControlID(control_id).name
        if val is not None:
            var.set(str(int(val)) if val == int(val) else f"{val:.2f}")
            self._log(f"Get {ctrl_name} = {val}")
        else:
            self._log(f"ERROR get {ctrl_name}: no value")

    def _set_temp(self):
        if not self._check_sdk() or not self.sdk.handle:
            return
        try:
            val = float(self._temp_var.get())
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
            self._temp_var.set(f"{t:.1f}")
            self._log(f"Current temp: {t:.1f} C, PWM: {p}")

    # ==============================================================
    # Resolution / ROI
    # ==============================================================
    def _set_resolution(self):
        if not self._check_sdk() or not self.sdk.handle:
            return
        try:
            x = int(self._res_x_var.get() or 0)
            y = int(self._res_y_var.get() or 0)
            w = int(self._res_w_var.get() or 0)
            h = int(self._res_h_var.get() or 0)
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
            self._res_x_var.set(str(area[0]))
            self._res_y_var.set(str(area[1]))
            self._res_w_var.set(str(area[2]))
            self._res_h_var.set(str(area[3]))
            self._log(f"Effective area: ({area[0]},{area[1]}) {area[2]}x{area[3]}")

    # ==============================================================
    # Binning / Bits
    # ==============================================================
    def _set_binning(self):
        if not self._check_sdk() or not self.sdk.handle:
            return
        try:
            bx = int(self._binx_var.get())
            by = int(self._biny_var.get())
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
        bits = int(self._bits_combo.get())
        ret = self.sdk.set_bits_mode(bits)
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"Bits mode set: {bits}bit")
        else:
            self._log(f"ERROR set bits: {error_string(ret)}")

    # ==============================================================
    # Single frame capture
    # ==============================================================
    def _capture_single(self):
        if not self._check_sdk() or not self.sdk.handle:
            return

        self._log("Starting single frame exposure...")
        self.sdk.set_stream_mode(SINGLE_MODE)
        ret = self.sdk.exp_single_frame()
        if ret == QHYCCDError.QHYCCD_READ_DIRECTLY:
            self._log("Camera needs READ_DIRECTLY mode, reading immediately...")
            self._read_single_frame()
        elif ret == QHYCCDError.QHYCCD_SUCCESS:
            self._log("Exposure started, waiting before read...")
            self.root.after(100, self._read_single_frame)
        else:
            self._log(f"ERROR exposure: {error_string(ret)}")

    def _read_single_frame(self):
        result = self.sdk.get_single_frame()
        if result is None:
            self._log("WARNING: No frame data (still exposing?)")
            self.root.after(500, self._read_single_frame)
            return
        w, h, bpp, ch, imgdata = result
        self._log(f"Frame: {w}x{h}, {bpp}bit, {ch}ch, {len(imgdata)} bytes")
        self._display_image(w, h, bpp, ch, imgdata)
        self._btn_save.configure(state=tk.NORMAL)

    # ==============================================================
    # Live capture
    # ==============================================================
    def _toggle_live(self):
        if self._live_running:
            self._stop_live()
        else:
            self._start_live()

    def _start_live(self):
        if not self._check_sdk() or not self.sdk.handle:
            return
        self._log("Starting live video...")
        self.sdk.set_stream_mode(LIVE_MODE)
        ret = self.sdk.begin_live()
        if ret != QHYCCDError.QHYCCD_SUCCESS:
            self._log(f"ERROR begin live: {error_string(ret)}")
            return
        self._live_running = True
        self._btn_live.configure(text="Stop Live")
        self._btn_single.configure(state=tk.DISABLED)
        self._btn_save.configure(state=tk.NORMAL)
        self._live_thread = threading.Thread(target=self._live_loop, daemon=True)
        self._live_thread.start()

    def _live_loop(self):
        while self._live_running:
            result = self.sdk.get_live_frame()
            if result is None:
                time.sleep(0.05)
                continue
            w, h, bpp, ch, imgdata = result
            self.root.after(0, self._update_live_display, w, h, bpp, ch, imgdata)
            time.sleep(0.03)

    def _stop_live(self):
        self._log("Stopping live video...")
        self._live_running = False
        if self._live_thread:
            self._live_thread.join(timeout=2)
            self._live_thread = None
        self.sdk.stop_live()
        self._btn_live.configure(text="Start Live")
        self._btn_single.configure(state=tk.NORMAL)
        self._log("Live video stopped.")

    def _update_live_display(self, w, h, bpp, ch, imgdata):
        self._display_image(w, h, bpp, ch, imgdata)

    # ==============================================================
    # Image display & save
    # ==============================================================
    def _display_image(self, w, h, bpp, ch, imgdata):
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
            self._show_on_canvas(img)
        except Exception as e:
            self._log(f"ERROR rendering image: {e}")

    def _show_on_canvas(self, pil_img):
        cw = self._image_canvas.winfo_width()
        ch = self._image_canvas.winfo_height()
        if cw < 10 or ch < 10:
            return
        iw, ih = pil_img.size
        scale = min(cw / iw, ch / ih, 1.0)
        nw, nh = int(iw * scale), int(ih * scale)
        resized = pil_img.resize((nw, nh), Image.NEAREST)
        self._photo = ImageTk.PhotoImage(resized)
        self._image_canvas.delete("all")
        self._image_canvas.create_image(cw // 2, ch // 2, image=self._photo, anchor=tk.CENTER)
        self._capture_info_var.set(f"Image: {iw}x{ih}  (scaled {nw}x{nh})")

    def _on_canvas_resize(self, event):
        if self._captured_image:
            self._show_on_canvas(self._captured_image)

    def _save_image(self):
        if self._captured_image is None:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG Image", "*.png"), ("FITS", "*.fits"), ("TIFF", "*.tiff"), ("All", "*.*")]
        )
        if path:
            self._captured_image.save(path)
            self._log(f"Image saved: {path}")

    # ==============================================================
    # Cleanup
    # ==============================================================
    def on_close(self):
        if self._live_running:
            self._stop_live()
        if self.sdk.handle:
            self.sdk.close()
        self.sdk.release_resource()
        self.root.destroy()


def main():
    auto_init = "--auto" in sys.argv
    root = tk.Tk()
    app = QHYCamApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    if auto_init:
        root.after(500, app._auto_init)
    root.mainloop()


if __name__ == "__main__":
    main()
