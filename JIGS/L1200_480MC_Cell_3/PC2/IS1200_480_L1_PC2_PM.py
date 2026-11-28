import json
import os
import queue
import re
import shutil
import socket
import sys
import threading
import time
import tkinter as tk
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from datetime import datetime, time as dt_time, timedelta
from queue import Empty
from tkinter import messagebox

import cv2
import numpy as np
import torch
from PIL import Image, ImageTk
from torchvision import transforms
from ultralytics import YOLO

from comgrab3 import enumerate_cameras
from patchcore.models.patchcore224 import PatchCore

sys.path.insert(0, os.path.dirname(__file__))

os.environ["OV_CACHE_DIR"] = "./ov_cache"
os.environ["OV_CPU_BIND_THREAD"] = "YES"
os.environ["OV_CPU_THREADS_NUM"] = "8"
os.environ["OV_HINT_NUM_REQUESTS"] = "1"
os.environ["OV_ENABLE_PROFILING"] = "NO"


class ResultCheck(tk.Frame):
    def __init__(self, parent, pm_mode):
        super().__init__(parent)
        self.pm_mode = pm_mode
        self.grid(sticky="nsew")
        self.configure_grid()
        self.create_widgets()

    def configure_grid(self):
        for i in range(4):
            self.rowconfigure(i, minsize=10 if i % 2 == 0 else 100, weight=1)
        for j in range(4):
            size = 500 if j == 0 else 100 if j < 3 else 200
            self.columnconfigure(j, minsize=size, weight=1)

    def create_widgets(self):
        if self.pm_mode:
            self.label_image_1 = tk.Label(
                self, text="CHẾ ĐỘ PM MÁY\nHÃY CHO MÁY PM VÀO!! ", font=("Arial", 50),
                fg='blue', borderwidth=1, relief='solid'
            )
        else:
            self.label_image_1 = tk.Label(
                self, text="ĐÃ PM MÁY OK\nBẮT ĐẦU CHẠY.", font=("Arial", 50),
                fg='blue', borderwidth=1, relief='solid')
        self.label_image_1.grid(row=0, column=0, rowspan=4, columnspan=3, sticky=tk.NSEW)

        self.label1 = tk.Label(self, text="RESULT", font=("Arial Bold", 15),
                               borderwidth=1, relief='solid', bg='yellow')
        self.label1.grid(row=0, column=3, sticky=tk.NSEW)

        self.label2 = tk.Label(self, text="None", font=("Arial", 50), bg='orange')
        self.label2.grid(row=1, column=3, sticky=tk.NSEW)

        self.label3 = tk.Label(self, text="NG POINT", font=("Arial Bold", 15),
                               borderwidth=1, relief='solid', bg='yellow')
        self.label3.grid(row=2, column=3, sticky=tk.NSEW)

        self.listNGbox = tk.Listbox(
            self, selectmode=tk.SINGLE, bg='pink', borderwidth=1,
            relief='solid', height=15, selectbackground='red',
            exportselection=False
        )
        self.listNGbox.grid(row=3, column=3, sticky=tk.NSEW)

        self.scroll = tk.Scrollbar(self, orient=tk.VERTICAL, borderwidth=1, relief='solid')
        self.listNGbox['yscrollcommand'] = self.scroll.set
        self.scroll.grid(row=3, column=3, sticky=tk.NS + tk.E)


class StationInfor(tk.Frame):
    def __init__(self, parent, pm_mode):
        super().__init__(parent, bd=2, relief="groove")
        self.grid()
        self.pm_mode = pm_mode
        self.columnconfigure(0, minsize=150, weight=1)
        self.columnconfigure(1, minsize=100, weight=1)
        self.columnconfigure(2, minsize=100, weight=1)
        self.columnconfigure(3, minsize=100, weight=1)
        self.columnconfigure(4, minsize=150, weight=1)
        self.columnconfigure(5, minsize=150, weight=1)
        self.columnconfigure(6, minsize=150, weight=1)
        self.rowconfigure(0, minsize=50, weight=1)
        self.rowconfigure(1, minsize=50, weight=1)

        self.label_1 = tk.Label(self, text="Unit:", font=("Arial", 20), fg='orange')
        self.label_1.grid(column=0, row=0, sticky=tk.N + tk.S + tk.E + tk.W)
        self.label_4 = tk.Label(self, text="Inside_L2_1200_PC2", font=("bold", 20), fg='black')
        self.label_4.grid(column=1, row=0, sticky=tk.N + tk.S + tk.E + tk.W)

        self.label_2 = tk.Label(self, text="Body No:", font=("Arial", 17), fg='orange')
        self.label_2.grid(column=2, row=0, sticky=tk.N + tk.S + tk.E + tk.W)
        self.label_5 = tk.Label(self, text="None", font=("Arial", 20))
        self.label_5.grid(column=3, row=0, sticky=tk.N + tk.S + tk.E + tk.W)

        self.label_3 = tk.Label(self, text="Time check:", font=("Arial", 20), fg='orange')
        self.label_3.grid(column=0, row=1, sticky=tk.N + tk.S + tk.E + tk.W)
        self.label_6 = tk.Label(self, text="0", font=("Arial", 20))
        self.label_6.grid(column=1, row=1, sticky=tk.N + tk.S + tk.E + tk.W)

        self.label_20 = tk.Label(self, text="Checkpoint:", font=("Arial", 20), fg='orange')
        self.label_20.grid(column=2, row=1, sticky=tk.N + tk.S + tk.E + tk.W)
        self.label_7 = tk.Label(self, text="None", font=("Arial", 20))
        self.label_7.grid(column=3, row=1, sticky=tk.N + tk.S + tk.E + tk.W)

        self.label_8 = tk.Label(self, text="Q'ty OK", font=("Arial", 20), fg='orange')
        self.label_8.grid(column=4, row=0, sticky=tk.N + tk.S + tk.E + tk.W)
        self.label_11 = tk.Label(self, text="None", font=("Arial", 20))
        self.label_11.grid(column=4, row=1, sticky=tk.N + tk.S + tk.E + tk.W)

        self.label_9 = tk.Label(self, text="Q'ty NG", font=("Arial", 20), fg='red')
        self.label_9.grid(column=5, row=0, sticky=tk.N + tk.S + tk.E + tk.W)
        self.label_12 = tk.Label(self, text="None", font=("Arial", 20))
        self.label_12.grid(column=5, row=1, sticky=tk.N + tk.S + tk.E + tk.W)

        self.label_10 = tk.Label(self, text="Total Check", font=("Arial", 20), fg='orange')
        self.label_10.grid(column=6, row=0, sticky=tk.N + tk.S + tk.E + tk.W)
        self.label_13 = tk.Label(self, text="None", font=("Arial", 20), bd='12')
        self.label_13.grid(column=6, row=1, sticky=tk.N + tk.S + tk.E + tk.W)


class Buttom_frame(tk.Frame):
    def __init__(self, parent, pm_mode):
        super().__init__(parent, bd=5, relief="groove")
        self.grid()
        self.pm_mode = pm_mode
        self.selected_cam_button = None
        self.selected_cam_button_normal_bg = None
        self.selected_cam_button_normal_fg = None
        self.selected_cam_button_active_bg = None
        self.selected_cam_button_active_fg = None
        self.selected_cam_button_normal_relief = None
        for i in range(7):
            self.columnconfigure(i, minsize=10, weight=1)
        self.rowconfigure(0, minsize=10, weight=1)
        self.rowconfigure(1, minsize=10, weight=1)

        btn_style = {
            "fg": "yellow",
            "bg": "GREEN",  # xanh dịu
            "activebackground": "#27ae60",
            "activeforeground": "white",
            "width": 18,
            "height": 2,
            "bd": 2,
            "relief": "flat",
            "font": ("Times New Roman", 12, "bold"),
            "cursor": "hand2",
            "highlightthickness": 0,
            "takefocus": 0
        }

        ##################################### VT1 ###########################
        self.CAM12_VT1 = tk.Button(self, text="CAM12_VT1", command=self.hienthiCAM12_VT1, **btn_style)
        self.CAM12_VT1.grid(column=0, row=0, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM13_VT1 = tk.Button(self, text="CAM13_VT1", command=self.hienthiCAM13_VT1, **btn_style)
        self.CAM13_VT1.grid(column=1, row=0, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM14_VT1 = tk.Button(self, text="CAM14_VT1", command=self.hienthiCAM14_VT1, **btn_style)
        self.CAM14_VT1.grid(column=2, row=0, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM15_VT1 = tk.Button(self, text="CAM15_VT1", command=self.hienthiCAM15_VT1, **btn_style)
        self.CAM15_VT1.grid(column=3, row=0, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM16_VT1 = tk.Button(self, text="CAM16_VT1", command=self.hienthiCAM16_VT1, **btn_style)
        self.CAM16_VT1.grid(column=4, row=0, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM17_VT1 = tk.Button(self, text="CAM17_VT1", command=self.hienthiCAM17_VT1, **btn_style)
        self.CAM17_VT1.grid(column=5, row=0, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM18_VT1 = tk.Button(self, text="CAM18_VT1", command=self.hienthiCAM18_VT1, **btn_style)
        self.CAM18_VT1.grid(column=6, row=0, sticky=tk.NSEW, padx=3, pady=3)

        ########################### VT2 ###########################
        self.CAM14_VT2 = tk.Button(self, text="CAM14_VT2", command=self.hienthiCAM14_VT2, **btn_style)
        self.CAM14_VT2.grid(column=0, row=1, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM15_VT2 = tk.Button(self, text="CAM15_VT2", command=self.hienthiCAM15_VT2, **btn_style)
        self.CAM15_VT2.grid(column=1, row=1, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM17_VT2 = tk.Button(self, text="CAM17_VT2", command=self.hienthiCAM17_VT2, **btn_style)
        self.CAM17_VT2.grid(column=2, row=1, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM18_VT2 = tk.Button(self, text="CAM18_VT2", command=self.hienthiCAM18_VT2, **btn_style)
        self.CAM18_VT2.grid(column=3, row=1, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM19_VT2 = tk.Button(self, text="CAM19_VT2", command=self.hienthiCAM19_VT2, **btn_style)
        self.CAM19_VT2.grid(column=4, row=1, sticky=tk.NSEW, padx=3, pady=3)

        # ✅ Nút XEM ẢNH NG — hiển thị grid tổng hợp tất cả camera NG của lần kiểm tra cuối
        self.btn_xem_ng = tk.Button(
            self, text="📷 XEM ẢNH NG", fg='white', bg='red',
            font=("Arial Bold", 13), width=15, height=1,
            command=self.hienthi_anh_ng
        )
        self.btn_xem_ng.grid(column=5, row=1, columnspan=2, sticky=tk.N + tk.S + tk.E + tk.W)

        # ──────────────────────────────────────────────
        # Hiển thị từng camera theo nút bấm
        # ──────────────────────────────────────────────

    def _clear_cam_button_highlight(self):
        if self.selected_cam_button is None:
            return

        self.selected_cam_button.config(
            bg=self.selected_cam_button_normal_bg,
            fg=self.selected_cam_button_normal_fg,
            activebackground=self.selected_cam_button_active_bg,
            activeforeground=self.selected_cam_button_active_fg,
            relief=self.selected_cam_button_normal_relief
        )
        self.selected_cam_button = None

    def _highlight_cam_button(self, button_name):
        self._clear_cam_button_highlight()
        widget = getattr(self, button_name, None)
        if widget is None:
            return

        self.selected_cam_button = widget
        self.selected_cam_button_normal_bg = widget.cget("bg")
        self.selected_cam_button_normal_fg = widget.cget("fg")
        self.selected_cam_button_active_bg = widget.cget("activebackground")
        self.selected_cam_button_active_fg = widget.cget("activeforeground")
        self.selected_cam_button_normal_relief = widget.cget("relief")
        widget.config(bg='white', fg='black', activebackground='white', activeforeground='black')

    def set_cam_button_color(self, widget, bg, fg):
        if widget is None:
            return

        if widget is self.selected_cam_button:
            self.selected_cam_button_normal_bg = bg
            self.selected_cam_button_normal_fg = fg
            widget.config(bg='white', fg='black', activebackground='white', activeforeground='black')
            return

        widget.config(bg=bg, fg=fg)

    def _show_cam(self, index):
        """Hiển thị ảnh processed của camera chỉ định. Lưu lại để xem lại."""
        labels = {
            0: "CAM12_VT1",
            1: "CAM13_VT1",
            2: "CAM14_VT1",
            3: "CAM15_VT1",
            4: "CAM16_VT1",
            5: "CAM17_VT1",
            6: "CAM18_VT1",
            7: "CAM14_VT2",
            8: "CAM15_VT2",
            9: "CAM17_VT2",
            10: "CAM18_VT2",
            11: "CAM19_VT2",
        }

        label = labels[index]
        self._highlight_cam_button(label)

        try:
            if app_ui.so_nut_an[index] is not None and not isinstance(app_ui.so_nut_an[index], int):
                frame = app_ui.so_nut_an[index].copy()

                cv2.putText(frame, label, (100, 100), cv2.FONT_HERSHEY_COMPLEX, 1, (0, 0, 255), 1, cv2.LINE_AA)
                im = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                rs_im = cv2.resize(im, (1000, 528))
                ima = Image.fromarray(rs_im)
                new = ImageTk.PhotoImage(ima)
                app_ui.widget_result_check.label_image_1.configure(image=new, text='')
                app_ui.widget_result_check.label_image_1.image = new
                # ✅ Lưu lại ảnh đang hiển thị để nút XEM LẠI có thể khôi phục
                app_ui.last_display_imgtk = new
                app_ui.last_display_mode = "cam"
            else:
                print(f"{labels[index]} image is not available.")
        except Exception:
            print(traceback.format_exc())

    def hienthiCAM12_VT1(self):
        self._show_cam(0)

    def hienthiCAM13_VT1(self):
        self._show_cam(1)

    def hienthiCAM14_VT1(self):
        self._show_cam(2)

    def hienthiCAM15_VT1(self):
        self._show_cam(3)

    def hienthiCAM16_VT1(self):
        self._show_cam(4)

    def hienthiCAM17_VT1(self):
        self._show_cam(5)

    def hienthiCAM18_VT1(self):
        self._show_cam(6)

    def hienthiCAM14_VT2(self):
        self._show_cam(7)

    def hienthiCAM15_VT2(self):
        self._show_cam(8)

    def hienthiCAM17_VT2(self):
        self._show_cam(9)

    def hienthiCAM18_VT2(self):
        self._show_cam(10)

    def hienthiCAM19_VT2(self):
        self._show_cam(11)

    # ──────────────────────────────────────────────
    # Nút XEM ẢNH NG — grid tổng hợp tất cả camera NG
    # ──────────────────────────────────────────────

    def hienthi_anh_ng(self):
        """Hiển thị grid ảnh tổng hợp — PM: tất cả cam, RUN: chỉ cam NG."""
        self._clear_cam_button_highlight()
        try:
            with app_ui.ng_frames_lock:
                ng_snapshot = dict(app_ui.ng_frames)

            if not ng_snapshot:
                app_ui.widget_result_check.label_image_1.configure(
                    image='', text="Không có ảnh trong lần kiểm tra này",
                    font=("Arial", 30), fg='gray'
                )
                app_ui.last_display_mode = "text"
                return

            # ✅ PM mode → build_all_grid (tất cả cam, viền xanh/đỏ)
            # ✅ RUN mode → build_ng_grid (chỉ cam NG)
            if app_ui.PM:
                grid_img = app_ui.build_all_grid(ng_snapshot)
            else:
                grid_img = app_ui.build_ng_grid(ng_snapshot)

            if grid_img is None:
                return

            image = cv2.cvtColor(grid_img, cv2.COLOR_BGR2RGB)
            im_pil = Image.fromarray(image)
            imgtk = ImageTk.PhotoImage(image=im_pil)
            app_ui.widget_result_check.label_image_1.configure(image=imgtk, text='')
            app_ui.widget_result_check.label_image_1.imgtk = imgtk

            app_ui.last_display_imgtk = imgtk
            app_ui.last_ng_imgtk = imgtk
            app_ui.last_display_mode = "ng_grid"
        except Exception:
            print(traceback.format_exc())


class Main_Application(tk.Tk):
    SAVE_ALL_DIR = r"F:\save_all_file"
    SAVE_NG_DIR = r"F:\save_file_ng"
    SAVE_CROP_ALL_DIR = r"F:\save_crop_all"
    SAVE_CROP_NG_DIR = r"F:\save_crop_ng"

    def __init__(self):
        super().__init__()
        self.title("Giao Dien Inside 2025")
        self.geometry('1200x800')
        self.grid()
        self.state("zoomed")
        self.rowconfigure(0, minsize=100, weight=1)
        self.rowconfigure(1, minsize=500, weight=1)
        self.rowconfigure(2, minsize=100, weight=1)
        self.columnconfigure(0, minsize=800, weight=1)
        self.PM = True
        self.show_pm()
        # self.widget_station_infor = StationInfor(self)
        # self.widget_station_infor.grid(row=0, column=0, sticky=tk.N + tk.S + tk.E + tk.W)
        # self.widget_result_check = ResultCheck(self)
        # self.widget_result_check.grid(row=1, column=0, sticky=tk.N + tk.S + tk.E + tk.W)
        # self.widget_buttom = Buttom_frame(self)
        # self.widget_buttom.grid(row=2, column=0, sticky=tk.N + tk.S + tk.E + tk.W)

        self.so_cam_da_chup = 0
        self.camera_counter_lock = threading.Lock()
        self.DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
        self.xl = 0
        self.ng = 0
        self.lanxl = 0
        self.lankt = 0
        self.barcodes = None
        self.databacode = None
        self.current_cam_idx = 0
        self.queue_xl_anh = queue.Queue(maxsize=24)
        self.results_queue = queue.Queue(maxsize=128)
        self.ui_queue = __import__("is480_runtime").CoalescedUIQueue()
        self.frame_lock = threading.Lock()
        self.latest_frames = {cam: None for cam in [12, 13, 14, 15, 16, 17, 18, 19]}
        self.xoafile()
        self.camera_running = True
        self.list_ng = []
        self.ng_cams = []
        self.so_nut_an = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        self.stop = False
        self.kq_ng = False
        self.server_ip = '169.254.218.167'  # IP server
        self.server_port = 12345
        self.listen_port = 23456  # Port để chờ server gửi trực tiếp

        self.transform = transforms.Compose([
            transforms.Resize((224, 224)),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        ])
        self.CAM_LOCATION_REF = {
            "cam12": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(3)#USBMI(0)",
            "cam13": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(2)#USBMI(0)",#
            "cam14": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(4)#USBMI(0)",#
            "cam15": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(10)#USBMI(0)",#
            "cam16": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(6)#USBMI(0)",#
            "cam17": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(9)#USBMI(0)",#
            "cam18": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(1)#USBMI(0)",#
            "cam19": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(5)#USB(1)#USBMI(0)",
        }
        self.map_cam()
        self.orb = cv2.ORB_create(500)
        self.start = False

        # ✅ FIX: Khai báo TẤT CẢ biến liên quan đến camera_reader
        #         TRƯỚC KHI gọi init_all_cameras() để tránh AttributeError
        #         khi thread reader khởi động và truy cập các thuộc tính này
        self.camera_error_flags = {}
        self.current_camera_errors = set()
        self.MAX_RECONNECT = 3
        self.camera_popup_showing = False

        cam_list = [12, 13, 14, 15, 16, 17, 18, 19]
        self.cam_reconnect_attempts = {cam: 0 for cam in cam_list}
        self.cam_reconnect_failed = {cam: 0 for cam in cam_list}
        self.reconnect_waiting = {cam: False for cam in cam_list}
        self.is480_frame_times = {}
        self.init_all_cameras()
        self.Init_load_all_models()
        self.warmup_all_models()
        self.Init_load_all_models_patchcore()
        self.Init_load_toado()
        self.lable_ng = None
        self.allow_put = False
        self.process_futures = []
        self.camera_mapping = []
        self.queue_put_count = 0
        self.queue_put_lock = threading.Lock()
        self.futures_lock = threading.Lock()
        self.serial_lock = threading.Lock()
        self.allow_put_lock = threading.Lock()
        self.mocam = False
        self.make_file()
        self.docfile()
        self.lanan = 0
        self.queue_put_count_vt1 = 0
        self.queue_put_count_vt2 = 0
        self.guithVT1 = False
        self.guithVT2 = False
        self.gui_kq = False
        self.gui_kqdocbarcode = False
        self.barcode_scanned = False
        self.databacode = None
        self.start_time = None
        self.lable_ng = []
        self.ng_points = []
        self.ng_frames = {}
        self.ng_frames_lock = threading.Lock()
        self.gui_kq = False

        # ✅ Lưu trạng thái hiển thị để nút XEM LẠI / XEM NG có thể khôi phục
        self.last_display_imgtk = None  # ImageTk cuối cùng hiển thị
        self.last_ng_imgtk = None  # ImageTk grid NG — không bị ghi đè khi xem cam đơn
        self.last_display_mode = None  # 'cam' | 'ng_grid' | 'text' | None

        self.ser = None  # PC2/PC3 have NO physical PLC Serial

        self.last_reset_date = datetime.now().date()
        self.total_points = 133
        self.executor = ThreadPoolExecutor(max_workers=10)
        self.executor_points = ThreadPoolExecutor(max_workers=8)
        self.executor_ai = ThreadPoolExecutor(max_workers=10)
        self.executor_save = __import__("is480_runtime").BoundedExecutor(max_workers=4,max_pending=320)
        self.after(20, self.ui_loop)
        self.Init_orb()
        self.Init_load_orb()

        self.ORB_ROI_LARGE_CAM12_VT1 = [
            {"name": "VÙNG1", "xywh": (499, 1, 1206, 1077)}
        ]
        self.ORB_ROI_LARGE_CAM13_VT1 = [
            {"name": "VÙNG1", "xywh": (48, 15, 1079, 828)},
            {"name": "VÙNG2", "xywh": (1138, 217, 734, 551)}
        ]
        self.ORB_ROI_LARGE_CAM14_VT1 = [
            {"name": "VÙNG1", "xywh": (438, 3, 1252, 778)},
            {"name": "VÙNG2", "xywh": (543, 781, 714, 236)}
        ]
        self.ORB_ROI_LARGE_CAM15_VT1 = [
            {"name": "VÙNG1", "xywh": (1074, 15, 676, 987)},
            {"name": "VÙNG2", "xywh": (57, 331, 994, 656)}
        ]
        self.ORB_ROI_LARGE_CAM16_VT1 = [
            {"name": "VÙNG1", "xywh": (104, 475, 498, 345)},
            {"name": "VÙNG2", "xywh": (620, 475, 488, 343)},
            {"name": "VÙNG2", "xywh": (1117, 475, 454, 350)},
            {"name": "VÙNG2", "xywh": (1594, 602, 292, 362)}
        ]
        self.ORB_ROI_LARGE_CAM17_VT1 = [
            {"name": "VÙNG1", "xywh": (327, 117, 1564, 582)},
            {"name": "VÙNG2", "xywh": (327, 718, 1564, 314)}
        ]
        self.ORB_ROI_LARGE_CAM18_VT1 = [
            {"name": "VÙNG1", "xywh": (5, 8, 1584, 707)},
            {"name": "VÙNG2", "xywh": (1590, 217, 320, 660)},
            {"name": "VÙNG3", "xywh": (91, 715, 1411, 317)},
        ]
        self.ORB_ROI_LARGE_CAM14_VT2 = [
            {"name": "VÙNG1", "xywh": (65, 24, 821, 1051)}
        ]
        self.ORB_ROI_LARGE_CAM15_VT2 = [
            {"name": "VÙNG1", "xywh": (1139, 81, 480, 990)}
        ]
        self.ORB_ROI_LARGE_CAM17_VT2 = [
            {"name": "VÙNG1", "xywh": (40, 13, 1077, 941)},
            {"name": "VÙNG2", "xywh": (1161, 714, 326, 352)},
            {"name": "VÙNG3", "xywh": (1167, 133, 419, 452)},
        ]
        self.ORB_ROI_LARGE_CAM18_VT2 = [
            {"name": "VÙNG1", "xywh": (87, 409, 184, 638)},
            {"name": "VÙNG2", "xywh": (291, 409, 971, 331)},
            {"name": "VÙNG3", "xywh": (998, 16, 507, 638)},
            {"name": "VÙNG4", "xywh": (291, 751, 1414, 297)},
        ]
        self.ORB_ROI_LARGE_CAM19_VT2 = [
            {"name": "VÙNG1", "xywh": (532, 246, 474, 594)},
            {"name": "VÙNG2", "xywh": (1024, 246, 527, 747)}
        ]

        self.ORB_ROI_LARGE_CAM12_VT1_PM = [
            {"name": "VÙNG1", "xywh": (561, 538, 858, 515)}
        ]
        self.ORB_ROI_LARGE_CAM13_VT1_PM = [
            {"name": "VÙNG1", "xywh": (52, 18, 797, 294)},
        ]
        self.ORB_ROI_LARGE_CAM14_VT1_PM = [
            {"name": "VÙNG1", "xywh": (439, 634, 930, 357)}
        ]
        self.ORB_ROI_LARGE_CAM15_VT1_PM = [
            {"name": "VÙNG1", "xywh": (724, 748, 119, 212)},
            {"name": "VÙNG2", "xywh": (99, 753, 313, 234)}
        ]
        self.ORB_ROI_LARGE_CAM16_VT1_PM = [
            {"name": "VÙNG1", "xywh": (1780, 762, 105, 103)},
            {"name": "VÙNG2", "xywh": (825, 357, 460, 400)}
        ]
        self.ORB_ROI_LARGE_CAM17_VT1_PM = [
            {"name": "VÙNG1", "xywh": (1338, 117, 552, 288)},
            {"name": "VÙNG2", "xywh": (1675, 702, 216, 361)}
        ]
        self.ORB_ROI_LARGE_CAM18_VT2_PM = [
            {"name": "VÙNG1", "xywh": (87, 751, 1618, 294)},
        ]
        self.ORB_ROI_LARGE_CAM19_VT2_PM = [
            {"name": "VÙNG1", "xywh": (1260, 697, 292, 254)},
            {"name": "VÙNG4", "xywh": (645, 394, 492, 356)},
        ]
        # self.load_checkpoints()
        self._is480_setup()

    def create_menu(self):

        menubar = tk.Menu(self)

        mode_menu = tk.Menu(
            menubar,
            tearoff=0
        )

        mode_menu.add_command(
            label="RUN MODE",
            command=lambda: None

        )

        menubar.add_cascade(
            label="MODE",
            menu=mode_menu
        )

        self.config(menu=menubar)

    # ======================================================
    # CLEAR
    # ======================================================

    def clear_ui(self):

        try:
            self.widget_station_infor.destroy()
        except:
            pass

        try:
            self.widget_result_check.destroy()
        except:
            pass

        try:
            self.widget_buttom.destroy()
        except:
            pass

    def bind_result_check_events(self):
        listbox = self.widget_result_check.listNGbox
        listbox.bind("<space>", self.handle_space_switch_cam)
        listbox.bind("<ButtonRelease-1>", self.show_image_click_list)
        listbox.bind("<<ListboxSelect>>", self.show_image_click_list)

    # ======================================================
    # PM MODE
    # ======================================================

    def show_pm(self):
        self.create_menu()

        self.PM = True

        self.clear_ui()

        self.widget_station_infor = StationInfor(self, pm_mode=True)
        self.widget_station_infor.grid(
            row=0,
            column=0,
            sticky=tk.N + tk.S + tk.E + tk.W
        )

        self.widget_result_check = ResultCheck(self, pm_mode=True)
        self.widget_result_check.grid(
            row=1,
            column=0,
            sticky=tk.N + tk.S + tk.E + tk.W
        )
        self.bind_result_check_events()

        self.widget_buttom = Buttom_frame(self, pm_mode=True)
        self.widget_buttom.grid(
            row=2,
            column=0,
            sticky=tk.N + tk.S + tk.E + tk.W
        )

        self.widget_buttom.CAM18_VT1.grid_remove()
        self.widget_buttom.CAM14_VT2.grid_remove()
        self.widget_buttom.CAM15_VT2.grid_remove()
        self.widget_buttom.CAM17_VT2.grid_remove()

        self.title(
            "1200_L1_PC2_PM - PM MODE"
        )

        print("PM MODE")

        if hasattr(self, "is480_bridge"):
            self._is480_refresh_screen()

    # ======================================================
    # RUN MODE
    # ======================================================

    def show_run(self):

        self.PM = False

        self.clear_ui()

        self.config(menu="")

        self.widget_station_infor = StationInfor(self, pm_mode=False)
        self.widget_station_infor.grid(
            row=0,
            column=0,
            sticky=tk.N + tk.S + tk.E + tk.W
        )

        self.widget_result_check = ResultCheck(self, pm_mode=False)
        self.widget_result_check.grid(
            row=1,
            column=0,
            sticky=tk.N + tk.S + tk.E + tk.W
        )
        self.bind_result_check_events()

        self.widget_buttom = Buttom_frame(self, pm_mode=False)
        self.widget_buttom.grid(
            row=2,
            column=0,
            sticky=tk.N + tk.S + tk.E + tk.W
        )

        self.title(
            "INSIDE L1200_PC2 - RUN MODE"
        )

        print("RUN MODE")

        if hasattr(self, "is480_bridge"):
            self._is480_refresh_screen()

    def kiemtra_xoafile(self, duongdan):
        danh_sach_file = os.listdir(duongdan)
        for tenfile in danh_sach_file:
            duong_dan_file = os.path.join(duongdan, tenfile)
            print(duong_dan_file)
            thoi_gian_tao = os.path.getctime(duong_dan_file)
            ngay_tao = datetime.fromtimestamp(thoi_gian_tao)
            print(ngay_tao)
            ngayconlai = datetime.now() - ngay_tao
            if datetime.now() - ngay_tao > timedelta(10):
                print(f'{duong_dan_file} qua lau')
                shutil.rmtree(duong_dan_file)
                print(f'{duong_dan_file}_da xoa')
            else:
                print(f'{duong_dan_file}_chua du ngay con {timedelta(10) - ngayconlai}')

    def xoafile(self):
        duongdan = self.SAVE_ALL_DIR
        duongdan1 = self.SAVE_NG_DIR
        duongdan2 = self.SAVE_CROP_NG_DIR
        duongdan3 = self.SAVE_CROP_ALL_DIR
        self.kiemtra_xoafile(duongdan)
        self.kiemtra_xoafile(duongdan1)
        self.kiemtra_xoafile(duongdan2)
        self.kiemtra_xoafile(duongdan3)

    # ──────────────────────────────────────────────
    # CAMERA INIT & READER
    # ──────────────────────────────────────────────

    def Init_camera(self, cam_index):
        if cam_index is None:
            print(f"cam_index is None, skip")
            return None
        print(f"Opening cam index {cam_index}")
        cap = cv2.VideoCapture(cam_index, cv2.CAP_DSHOW)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
        if cap.isOpened():
            print(f' mở cam {cam_index} ok')
            return cap
        else:
            cap.release()
            print(f' mở cam {cam_index} NG')
            return None

    def init_all_cameras(self):
        """
        Mở tất cả camera và khởi động thread đọc liên tục.
        Nếu camera nào không mở được ngay từ đầu → hiện popup cảnh báo luôn.
        """
        print("🔄 Initializing all cameras...")

        cam_indices = [self.cam12, self.cam13, self.cam14, self.cam15,
                       self.cam16, self.cam17, self.cam18, self.cam19]

        with ThreadPoolExecutor(max_workers=7) as executor:
            results = list(executor.map(self.Init_camera, cam_indices))

        self.cap12, self.cap13, self.cap14, self.cap15, \
        self.cap16, self.cap17, self.cap18, self.cap19, = results
        print(results)

        # ✅ Kiểm tra ngay sau khi mở — camera nào None → báo popup ngay lập tức
        failed_cams = [cam_id for cam_id, cap in enumerate(results) if cap is None]
        if failed_cams:
            # Đẩy vào ui_queue để hiện popup từ main thread (an toàn với Tkinter)
            self.ui_queue.put({
                "type": "camera_startup_error",
                "cam_ids": failed_cams
            })

        # Chỉ tạo thread MỚI nếu chưa có thread đang chạy cho cam đó
        for idx, (cam_so, cap) in enumerate(zip([12, 13, 14, 15, 16, 17, 18, 19], results)):
            thread_name = f"CamReader_{cam_so}"
            alive = any(t.name == thread_name and t.is_alive() for t in threading.enumerate())
            if not alive:
                print(f"🚀 Starting new reader thread for CAM {cam_so}")
                threading.Thread(
                    target=self.camera_reader,
                    args=(cam_so,),  # ← SỬA: dùng cam_so
                    daemon=True,
                    name=thread_name
                ).start()
            else:
                print(f"♻CAM {cam_so} reader thread already alive — skipping new thread")

    def camera_reader(self, cam_id):
        """
        Thread đọc camera liên tục — KHÔNG BAO GIỜ thoát trừ khi app đóng.

        Cơ chế reconnect:
          - Thử reconnect tối đa MAX_RECONNECT lần (mặc định 3)
          - Mỗi lần thất bại: chờ RECONNECT_DELAY rồi thử lại
          - Sau MAX_RECONNECT lần thất bại liên tiếp: hiện popup báo lỗi,
            sau đó vẫn tiếp tục thử vô hạn (popup chỉ hiện 1 lần cho đợt đó)
          - Khi reconnect thành công: reset bộ đếm, xóa trạng thái lỗi
        """
        print(f"🚀 Camera Reader CAM {cam_id} started")
        RECONNECT_DELAY = 2.0  # giây chờ giữa mỗi lần reconnect
        MAX_FAIL_READ = 15  # số read liên tiếp thất bại → trigger reconnect
        READ_INTERVAL = 0.008  # ~120 FPS

        fail_read = 0  # đếm read() thất bại liên tiếp
        popup_shown = False  # tránh hiện popup nhiều lần trong 1 đợt mất kết nối

        while self.camera_running:
            try:
                cap = getattr(self, f"cap{cam_id}", None)

                # ══════════════════════════════════════════
                # TRƯỜNG HỢP 1: cap = None → cần reconnect
                # ══════════════════════════════════════════
                if cap is None:
                    cam_index = getattr(self, f"cam{cam_id}", None)
                    if cam_index is None:
                        time.sleep(RECONNECT_DELAY * 2)
                        continue

                    print(f"[CAM {cam_id}] Reconnect attempt "
                          f"{self.cam_reconnect_attempts[cam_id] + 1}/{self.MAX_RECONNECT}...")

                    new_cap = self.Init_camera(cam_index)

                    if new_cap is not None:
                        # ✅ Reconnect thành công
                        setattr(self, f"cap{cam_id}", new_cap)
                        self.cam_reconnect_attempts[cam_id] = 0
                        fail_read = 0
                        popup_shown = False
                        print(f"[CAM {cam_id}] ✅ Reconnected successfully")
                    else:
                        # ❌ Lần này thất bại
                        self.cam_reconnect_attempts[cam_id] += 1
                        attempts = self.cam_reconnect_attempts[cam_id]
                        print(f"[CAM {cam_id}] ❌ Reconnect failed ({attempts}/{self.MAX_RECONNECT})")

                        if attempts >= self.MAX_RECONNECT and not popup_shown:
                            # Hiện popup sau đúng MAX_RECONNECT lần thất bại
                            popup_shown = True
                            self.ui_queue.put({
                                "type": "camera_error_popup",
                                "cam_id": cam_id,
                                "attempts": attempts
                            })

                        time.sleep(RECONNECT_DELAY)
                    continue

                # ══════════════════════════════════════════
                # TRƯỜNG HỢP 2: isOpened() = False
                # ══════════════════════════════════════════
                if not cap.isOpened():
                    print(f"[CAM {cam_id}] isOpened=False → release & retry")
                    try:
                        cap.release()
                    except Exception:
                        pass
                    setattr(self, f"cap{cam_id}", None)
                    fail_read = 0
                    time.sleep(RECONNECT_DELAY)
                    continue

                # ══════════════════════════════════════════
                # TRƯỜNG HỢP 3: đọc frame bình thường
                # ══════════════════════════════════════════
                ret, frame = cap.read()

                if ret and frame is not None and frame.size > 0:
                    fail_read = 0
                    # Reconnect thành công rồi đọc được frame → reset hoàn toàn
                    if self.cam_reconnect_attempts[cam_id] > 0:
                        self.cam_reconnect_attempts[cam_id] = 0
                        popup_shown = False
                        print(f"[CAM {cam_id}] ✅ Reading OK — reconnect counter reset")
                    with self.frame_lock:
                        self.latest_frames[cam_id] = frame.copy()
                        self.is480_frame_times[cam_id] = time.monotonic()
                else:
                    fail_read += 1
                    if fail_read % 5 == 0:
                        print(f"[CAM {cam_id}] read failed × {fail_read}")

                    if fail_read >= MAX_FAIL_READ:
                        print(f"[CAM {cam_id}] too many read failures → force reconnect")
                        try:
                            cap.release()
                        except Exception:
                            pass
                        setattr(self, f"cap{cam_id}", None)
                        fail_read = 0
                        time.sleep(RECONNECT_DELAY)
                        continue

            except Exception as e:
                print(f"[CAM {cam_id}] ❌ Exception: {e}")
                try:
                    cap = getattr(self, f"cap{cam_id}", None)
                    if cap:
                        cap.release()
                except Exception:
                    pass
                setattr(self, f"cap{cam_id}", None)
                fail_read = 0
                time.sleep(RECONNECT_DELAY)
                continue

            time.sleep(READ_INTERVAL)

        print(f"🛑 Camera Reader CAM {cam_id} exited (app closing)")

    def close_allcam(self):
        print("🔒 Releasing all cameras...")
        for i in [12, 13, 14, 15, 16, 17, 18, 19]:  # ← Sửa ở đây
            cap = getattr(self, f"cap{i}", None)
            if cap is not None:
                try:
                    cap.release()
                except:
                    pass
                setattr(self, f"cap{i}", None)

    def capture_and_process(self, cam_so, cap, vitri, toado, anhgoc, roi_large, *models):
        try:
            print(f"[CAP] cam {cam_so} vt{vitri}")
            if not self._is480_capture_fresh(cam_so,vitri):
                self._is480_fault(f'FRAME_STALE {self.is480_pc_id} CAM{cam_so} VT{vitri}: no fresh frame')
                return
            if cap is None:
                self.show_camera_error([f"CAM{cam_so} mất kết nối"])
                return

            MAX_BLACK_RETRY = 10
            RETRY_DELAY = 0.20
            MAX_WAIT_RECONNECT = 18  # tăng thời gian chờ

            black_count = 0
            frame1 = None

            for attempt in range(1, MAX_BLACK_RETRY + 1):
                with self.frame_lock:
                    frame1 = self.latest_frames.get(cam_so)

                if frame1 is None or self.is_black_image(frame1, threshold=20):
                    black_count += 1
                    print(f"[CAM {cam_so}] ⚫ Black frame #{black_count}/{MAX_BLACK_RETRY}")

                    if black_count >= MAX_BLACK_RETRY:
                        print(f"[CAM {cam_so}] Đen liên tục → Force Reconnect")
                        self.force_reconnect_camera(cam_so)

                        # ==================== CHỜ FRAME MỚI SAU RECONNECT ====================
                        print(f"[CAM {cam_so}] Đang chờ frame ổn định sau reconnect...")
                        waited = 0.0
                        stable_frame = None
                        max_attempts = int(MAX_WAIT_RECONNECT / 0.3)

                        for i in range(max_attempts):
                            time.sleep(0.3)  # delay vừa phải
                            waited += 0.3

                            with self.frame_lock:
                                temp_frame = self.latest_frames.get(cam_so)

                            if temp_frame is None:
                                continue

                            if self.is_black_image(temp_frame, threshold=20):
                                print(f"[CAM {cam_so}] Vẫn đen (lần {i + 1})")
                                continue

                            if self.is_too_bright(temp_frame, threshold=230):
                                print(f"[CAM {cam_so}] Frame quá sáng")
                                continue

                            # Đã có frame tốt
                            stable_frame = temp_frame.copy()
                            print(f"[CAM {cam_so}] ✅ Frame ổn định sau {waited:.1f}s")
                            break

                        if stable_frame is None:
                            print(f"❌ [CAM {cam_so}] TIMEOUT - Không lấy được ảnh mới sau reconnect")
                            self.show_camera_error([f"CAM{cam_so} timeout: không có frame hợp lệ"])
                            return  # Quan trọng: Dừng, không xử lý ảnh cũ

                        frame1 = stable_frame
                        break
                    else:
                        time.sleep(RETRY_DELAY)
                        continue

                # Frame tốt ngay từ đầu
                print(f"[CAM {cam_so}] ✅ Frame OK")
                break

            # Kiểm tra cuối cùng
            if frame1 is None or self.is_black_image(frame1, threshold=20) or self.is_too_bright(frame1, 230):
                print(f"❌ [CAM {cam_so}] Không có frame hợp lệ → BỎ QUA")
                self.show_camera_error([f"CAM{cam_so} không có ảnh hợp lệ"])
                return

            # ====================== TIẾP TỤC XỬ LÝ ======================
            if cam_so in [18, 19]:
                frame = cv2.rotate(frame1, cv2.ROTATE_180)
            else:
                frame = frame1

            if not self._is480_current_thread_cycle():
                return
            self._is480_capture_raw(cam_so, vitri, frame)
            self.executor_save.submit(self.save_picture, cam_so, frame, vitri)

            with self.allow_put_lock:
                if not self.allow_put:
                    return

            self.queue_xl_anh.put((self._is480_tls.cid, frame, cam_so, vitri, toado, anhgoc, roi_large, *models), timeout=1.0)

            with self.queue_put_lock:
                if vitri == 1:
                    self.queue_put_count_vt1 += 1
                else:
                    self.queue_put_count_vt2 += 1

        except Exception as e:
            print(f"[capture_and_process] CAM{cam_so} ERROR: {e}")
            traceback.print_exc()

    def is_too_bright(self, frame, threshold=235):
        """Kiểm tra ảnh bị chói (quá sáng)"""
        if frame is None or frame.size == 0:
            return False
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean_value = np.mean(gray)
        return mean_value > threshold

    def force_reconnect_camera(self, cam_id):
        """Force reconnect camera"""
        if cam_id not in self.cam_reconnect_attempts:
            print(f"[WARNING] cam_id {cam_id} không tồn tại trong reconnect dict!")
            self.cam_reconnect_attempts[cam_id] = 0
            self.cam_reconnect_failed[cam_id] = 0
            self.reconnect_waiting[cam_id] = False

        print(f"[RECONNECT FORCE] CAM {cam_id} - Lần {self.cam_reconnect_attempts[cam_id] + 1}/{self.MAX_RECONNECT}")

        try:
            cap = getattr(self, f"cap{cam_id}", None)
            if cap is not None:
                cap.release()
        except:
            pass

        setattr(self, f"cap{cam_id}", None)
        with self.frame_lock:
            self.latest_frames[cam_id] = None  # ← Lưu ý: latest_frames cũng phải là key theo cam_id thật

        self.reconnect_waiting[cam_id] = True
        self.cam_reconnect_attempts[cam_id] += 1

        if self.cam_reconnect_attempts[cam_id] >= self.MAX_RECONNECT:
            self.show_reconnect_failed_popup(cam_id)

    # ──────────────────────────────────────────────
    # ON CLOSING
    # ──────────────────────────────────────────────

    def on_closing(self):
        """Đóng app — set camera_running=False để tất cả reader thread tự thoát"""
        print("🛑 Shutting down application...")
        self.camera_running = False
        self.guithVT1 = False
        self.guithVT2 = False
        self.gui_kq = False
        self.gui_kqdocbarcode = False

        time.sleep(0.5)

        for i in [12, 13, 14, 15, 16, 17, 18, 19]:
            cap = getattr(self, f"cap{i}", None)
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
                setattr(self, f"cap{i}", None)

        self.executor_points.shutdown(wait=False)
        self.executor.shutdown(wait=False)
        self.executor_ai.shutdown(wait=False)
        self.executor_save.shutdown(wait=False)
        self.destroy()

    # ──────────────────────────────────────────────
    # UTILITIES
    # ──────────────────────────────────────────────

    def is_black_image(self, frame, threshold=25):  # Tăng lên 25
        if frame is None or frame.size == 0:
            return True
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean_value = np.mean(gray)
        # print(f"[DEBUG] CAM brightness = {mean_value:.1f}")   # Bật khi debug
        return mean_value < threshold

    def show_camera_error(self, error_list):
        for err in error_list:
            self.current_camera_errors.add(err)
        if self.camera_popup_showing:
            return
        self.camera_popup_showing = True

        def _show():
            try:
                msg = "\n".join(sorted(self.current_camera_errors))
                messagebox.showerror("LỖI CAMERA", msg)
            finally:
                self.current_camera_errors.clear()
                self.camera_popup_showing = False

        self.after(0, _show)

    def resolve(self, cam_ref):
        cameras = enumerate_cameras()
        loc_to_idx = {}
        for cam in cameras:
            loc = (cam["location_id"] or "").strip().upper()
            if loc and loc != "N/A":
                loc_to_idx[loc] = cam["index"]
        result = {}
        for cam_name, ref_loc in cam_ref.items():
            key = (ref_loc or "").strip().upper()
            result[cam_name] = loc_to_idx.get(key)
        return result

    def map_cam(self):
        resolved = self.resolve(self.CAM_LOCATION_REF)
        print("\n[2] Kết quả mapping:")
        print("-" * 40)
        for cam_name in sorted(resolved):
            idx = resolved[cam_name]
            status = f"{idx}" if idx is not None else "9999"
            print(f'"{cam_name}": {status},')
        print("-" * 40)
        self.cam12 = resolved.get("cam12", -1)
        self.cam13 = resolved.get("cam13", -1)
        self.cam14 = resolved.get("cam14", -1)
        self.cam15 = resolved.get("cam15", -1)
        self.cam16 = resolved.get("cam16", -1)
        self.cam17 = resolved.get("cam17", -1)
        self.cam18 = resolved.get("cam18", -1)
        self.cam19 = resolved.get("cam19", -1)
        print(
            f'{self.cam12},{self.cam13},{self.cam14},{self.cam15},{self.cam16},{self.cam17},{self.cam18},{self.cam19}')

    def make_file(self):
        base_path = self.SAVE_ALL_DIR
        now = datetime.now()
        current_time = now.time()
        if dt_time(7, 0) <= current_time <= dt_time(19, 45):
            shift = "ca_ngay"
        elif current_time >= dt_time(20, 0) or current_time <= dt_time(6, 30):
            shift = "ca_dem"
        else:
            shift = "ngoai_ca"
        if shift == "ca_dem" and current_time <= dt_time(6, 30):
            save_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            save_date = now.strftime("%Y-%m-%d")
        shift_path = os.path.join(base_path, save_date, shift)
        if not os.path.exists(shift_path):
            print(f"Tạo ca mới: {shift}")
            self.ghifile(0, 0, 0)
        os.makedirs(shift_path, exist_ok=True)

    def docfile(self):
        with open("sanluong.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        self.qty_ok = data["ok"]
        self.qty_ng = data["ng"]
        self.qty_all = data["all"]
        self.widget_station_infor.label_11.config(text=self.qty_ok)
        self.widget_station_infor.label_12.config(text=self.qty_ng)
        self.widget_station_infor.label_13.config(text=self.qty_all)
        with open("sanluong.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)

    def ghifile(self, ok, ng, all):
        data = {"ok": ok, "ng": ng, "all": all}
        with open("sanluong.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)

    def Init_load_orb(self):
        try:
            paths = {
                "cam12vt1": r"orb/cam12vt1.jpg",
                "cam13vt1": r"orb/cam13vt1.jpg",
                "cam14vt1": r"orb/cam14vt1.jpg",
                "cam14vt2": r"orb/cam14vt2.jpg",
                "cam15vt1": r"orb/cam15vt1.jpg",
                "cam15vt2": r"orb/cam15vt2.jpg",
                "cam16vt1": r"orb/cam16vt1.jpg",
                "cam17vt1": r"orb/cam17vt1.jpg",
                "cam17vt2": r"orb/cam17vt2.jpg",
                "cam18vt1": r"orb/cam18vt1.jpg",
                "cam18vt2": r"orb/cam18vt2.jpg",
                "cam19vt2": r"orb/cam19vt2.jpg",

                "cam12vt1pm": r"orb_PM/cam12vt1.jpg",
                "cam13vt1pm": r"orb_PM/cam13vt1.jpg",
                "cam14vt1pm": r"orb_PM/cam14vt1.jpg",
                "cam15vt1pm": r"orb_PM/cam15vt1.jpg",
                "cam16vt1pm": r"orb_PM/cam16vt1.jpg",
                "cam17vt1pm": r"orb_PM/cam17vt1.jpg",
                "cam18vt2pm": r"orb_PM/cam18vt2.jpg",
                "cam19vt2pm": r"orb_PM/cam19vt2.jpg",
            }

            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = {
                    name: executor.submit(self.load_orrigin_image_orb, path)
                    for name, path in paths.items()
                }
                # Lấy kết quả từ từng thread
                for name, future in futures.items():
                    img, kp, des = future.result()
                    setattr(self, f"anh_goc_{name}", img)
                    setattr(self, f"kp_{name}", kp)
                    setattr(self, f"des_{name}", des)

        except:
            print(traceback.format_exc())

    def load_orrigin_image_orb(self, path):
        ref_img = cv2.imread(path)
        ref_gray = cv2.cvtColor(ref_img, cv2.COLOR_BGR2GRAY)
        kp1, des1 = self.orb.detectAndCompute(ref_gray, None)
        return ref_img, kp1, des1

    def Init_orb(self):
        self.orb = cv2.ORB_create(nfeatures=500, fastThreshold=10, edgeThreshold=15)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    def compute_orb_dxdy(self, gray_ref, gray_new, roi_large):
        zones = []
        for zone in roi_large:
            x, y, w, h = zone["xywh"]
            ref_zone = gray_ref[y:y + h, x:x + w]
            new_zone = gray_new[y:y + h, x:x + w]
            kp1, des1 = self.orb.detectAndCompute(ref_zone, None)
            kp2, des2 = self.orb.detectAndCompute(new_zone, None)
            if des1 is None or des2 is None:
                zone["dxdy"] = (0.0, 0.0)
                continue
            matches = self.bf.match(des1, des2)
            if len(matches) < 20:
                zone["dxdy"] = (0.0, 0.0)
                continue
            dxs, dys = [], []
            for m in matches:
                x1, y1 = kp1[m.queryIdx].pt
                x2, y2 = kp2[m.trainIdx].pt
                dxs.append(x2 - x1)
                dys.append(y2 - y1)
            zone["dxdy"] = (float(np.median(dxs)), float(np.median(dys)))
            zones.append(zone)
        return zones

    def draw_orb_zones(self, img, zones, cam):
        for z in zones:
            x, y, w, h = z["xywh"]
            dx, dy = z.get("dxdy", (0.0, 0.0))
            color = (0, 255, 255) if abs(dx) < 1 and abs(dy) < 1 else (255, 255, 0)
            cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)
            cv2.putText(img, f"CAM{cam} dx={dx:.1f}, dy={dy:.1f}", (x, y - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

    def warmup_yolo_openvino_fast(self, model, name, index, total):
        try:
            if not isinstance(model, YOLO):
                return
            t0 = time.time()
            try:
                ov_model = model.model.model
                dummy = np.zeros((1, 3, 224, 224), dtype=np.float32)
                ov_model(dummy)
            except Exception:
                dummy = np.zeros((224, 224, 3), dtype=np.uint8)
                model.predict(dummy, imgsz=224, verbose=False)
            cost = round(time.time() - t0, 3)
            percent = int((index + 1) * 100 / total)
            print(f"[WARMUP] {name:12s} | {index + 1}/{total} | {percent}% | {cost}s")
        except Exception as e:
            print(f"[WARMUP-ERROR] {name}: {e}")

    def warmup_all_models(self):
        print("🔥 WARMUP FAST MODE")
        starttime = time.time()
        all_models = [
            ("model_CAM12vt1_0", self.model_CAM12vt1_0),
            ("model_CAM13vt1_0", self.model_CAM13vt1_0),
            ("model_CAM13vt1_1", self.model_CAM13vt1_1),
            ("model_CAM14vt1_0", self.model_CAM14vt1_0),
            ("model_CAM14vt1_1", self.model_CAM14vt1_1),
            ("model_CAM14vt2_0", self.model_CAM14vt2_0),
            ("model_CAM15vt1_0", self.model_CAM15vt1_0),
            ("model_CAM15vt1_1", self.model_CAM15vt1_1),
            ("model_CAM15vt1_2", self.model_CAM15vt1_2),
            ("model_CAM15vt2_0", self.model_CAM15vt2_0),
            ("model_CAM16vt1_0", self.model_CAM16vt1_0),
            ("model_CAM16vt1_1", self.model_CAM16vt1_1),
            ("model_CAM16vt1_2", self.model_CAM16vt1_2),
            ("model_CAM16vt1_3", self.model_CAM16vt1_3),
            ("model_CAM16vt1_4", self.model_CAM16vt1_4),
            ("model_CAM17vt1_0", self.model_CAM17vt1_0),
            ("model_CAM17vt1_1", self.model_CAM17vt1_1),
            ("model_CAM17vt1_2", self.model_CAM17vt1_2),
            ("model_CAM17vt1_3", self.model_CAM17vt1_3),
            ("model_CAM17vt2_0", self.model_CAM17vt2_0),
            ("model_CAM17vt2_1", self.model_CAM17vt2_1),
            ("model_CAM17vt2_2", self.model_CAM17vt2_2),
            ("model_CAM18vt1_0", self.model_CAM18vt1_0),
            ("model_CAM18vt1_1", self.model_CAM18vt1_1),
            ("model_CAM18vt1_2", self.model_CAM18vt1_2),
            ("model_CAM18vt2_0", self.model_CAM18vt2_0),
            ("model_CAM18vt2_1", self.model_CAM18vt2_1),
            ("model_CAM18vt2_2", self.model_CAM18vt2_2),
            ("model_CAM18vt2_3", self.model_CAM18vt2_3),
            ("model_CAM18vt2_4", self.model_CAM18vt2_4),
            ("model_CAM19vt2_0", self.model_CAM19vt2_0),

            ("model_CAM12vt1_0_PM", self.model_CAM12vt1_0_PM),
            ("model_CAM13vt1_0_PM", self.model_CAM13vt1_0_PM),
            ("model_CAM13vt1_1_PM", self.model_CAM13vt1_1_PM),
            ("model_CAM14vt1_0_PM", self.model_CAM14vt1_0_PM),
            ("model_CAM15vt1_0_PM", self.model_CAM15vt1_0_PM),
            ("model_CAM15vt1_1_PM", self.model_CAM15vt1_1_PM),
            ("model_CAM16vt1_0_PM", self.model_CAM16vt1_0_PM),
            ("model_CAM17vt1_1_PM", self.model_CAM17vt1_1_PM),
            ("model_CAM17vt1_3_PM", self.model_CAM17vt1_3_PM),
            ("model_CAM18vt2_2_PM", self.model_CAM18vt2_2_PM),
            ("model_CAM18vt2_4_PM", self.model_CAM18vt2_4_PM),
            ("model_CAM19vt2_0_PM", self.model_CAM19vt2_0_PM)
        ]
        all_models = [(n, m) for n, m in all_models if isinstance(m, YOLO)]
        total = len(all_models)
        with ThreadPoolExecutor(max_workers=2) as exe:
            futures = [exe.submit(self.warmup_yolo_openvino_fast, m, n, i, total) for i, (n, m) in
                       enumerate(all_models)]
            for f in as_completed(futures):
                f.result()
        print("✅ WARMUP DONE")
        print(f'thoigian={time.time() - starttime:.2f}s')

    def load_model_patchcore(self, model_path):
        try:
            model = PatchCore(device=self.DEVICE)
            model.load(model_path)
            model.eval()
            return model
        except Exception as e:
            print(f"Load patchcore failed: {e}")
            return None

    def Init_load_all_models_patchcore(self):
        model_paths = {
            "model_CAM15vt1_D5_patchcore": r'save_mode_pathcore/cam15_vt1_d5.pth',
            "model_CAM15vt1_D10_patchcore": r'save_mode_pathcore/cam15_vt1_d10.pth',
            "model_CAM15vt2_D2_patchcore": r'save_mode_pathcore/cam15_vt2_d2.pth',
        }
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {key: executor.submit(self.load_model_patchcore, path) for key, path in model_paths.items()}
            for key, future in futures.items():
                try:
                    model = future.result()
                    setattr(self, key, model if isinstance(model, PatchCore) else None)
                    print(f"{key} {'loaded ✅' if model else 'load FAILED'}")
                except Exception as e:
                    print(f"{key} ERROR: {e}")
                    setattr(self, key, None)

    def load_coordinates(self, path):
        with open(path) as f:
            lines = f.readlines()
            return [json.loads(line.strip()) for line in lines]

    def Init_load_toado(self):
        try:
            toado_paths = {
                'toado_CAM12_VT1': r"point_config/cam12vt1.txt",
                'toado_CAM13_VT1': r"point_config/cam13vt1.txt",
                'toado_CAM14_VT1': r"point_config/cam14vt1.txt",
                'toado_CAM15_VT1': r"point_config/cam15vt1.txt",
                'toado_CAM16_VT1': r"point_config/cam16vt1.txt",
                'toado_CAM17_VT1': r"point_config/cam17vt1.txt",
                'toado_CAM18_VT1': r"point_config/cam18vt1.txt",
                'toado_CAM14_VT2': r"point_config/cam14vt2.txt",
                'toado_CAM15_VT2': r"point_config/cam15vt2.txt",
                'toado_CAM17_VT2': r"point_config/cam17vt2.txt",
                'toado_CAM18_VT2': r"point_config/cam18vt2.txt",
                'toado_CAM19_VT2': r"point_config/cam19vt2.txt",

                'toado_CAM12_VT1_PM': r"point_config_PM/cam12vt1.txt",
                'toado_CAM13_VT1_PM': r"point_config_PM/cam13vt1.txt",
                'toado_CAM14_VT1_PM': r"point_config_PM/cam14vt1.txt",
                'toado_CAM15_VT1_PM': r"point_config_PM/cam15vt1.txt",
                'toado_CAM16_VT1_PM': r"point_config_PM/cam16vt1.txt",
                'toado_CAM17_VT1_PM': r"point_config_PM/cam17vt1.txt",
                'toado_CAM18_VT2_PM': r"point_config_PM/cam18vt2.txt",
                'toado_CAM19_VT2_PM': r"point_config_PM/cam19vt2.txt",
            }
            with ThreadPoolExecutor(max_workers=12) as executor:
                futures = {key: executor.submit(self.load_coordinates, path) for key, path in toado_paths.items()}
                for key, future in futures.items():
                    setattr(self, key, future.result())
                    print(f"{key} loaded ✅")
            print("Init tọa độ OK ✅")
        except Exception:
            print(traceback.format_exc())

    def load_model(self, model_path):
        return YOLO(model_path, task='classify')

    def Init_load_all_models(self):
        model_paths = {
            "model_CAM12vt1_0": r'model/openvino/cam12vt1_0_openvino_model',
            "model_CAM13vt1_0": r"model/openvino/cam13vt1_0_openvino_model",
            "model_CAM13vt1_1": r"model/openvino/cam13vt1_1_openvino_model",
            "model_CAM14vt1_0": r"model/openvino/cam14vt1_0_openvino_model",
            "model_CAM14vt1_1": r"model/openvino/cam14vt1_1_openvino_model",
            "model_CAM14vt2_0": r"model/openvino/cam14vt2_0_openvino_model",
            "model_CAM15vt1_0": r"model/openvino/cam15vt1_0_openvino_model",
            "model_CAM15vt1_1": r"model/openvino/cam15vt1_1_openvino_model",
            "model_CAM15vt1_2": r"model/openvino/cam15vt1_2_openvino_model",
            "model_CAM15vt2_0": r"model/openvino/cam15vt2_0_openvino_model",
            "model_CAM16vt1_0": r"model/openvino/cam16vt1_0_openvino_model",
            "model_CAM16vt1_1": r"model/openvino/cam16vt1_1_openvino_model",
            "model_CAM16vt1_2": r"model/openvino/cam16vt1_2_openvino_model",
            "model_CAM16vt1_3": r"model/openvino/cam16vt1_3_openvino_model",
            "model_CAM16vt1_4": r"model/openvino/cam16vt1_4_openvino_model",
            "model_CAM17vt1_0": r"model/openvino/cam17vt1_0_openvino_model",
            "model_CAM17vt1_1": r"model/openvino/cam17vt1_1_openvino_model",
            "model_CAM17vt1_2": r"model/openvino/cam17vt1_2_openvino_model",
            "model_CAM17vt1_3": r"model/openvino/cam17vt1_3_openvino_model",
            "model_CAM17vt2_0": r"model/openvino/cam17vt2_0_openvino_model",
            "model_CAM17vt2_1": r"model/openvino/cam17vt2_1_openvino_model",
            "model_CAM17vt2_2": r"model/openvino/cam17vt2_2_openvino_model",
            "model_CAM18vt1_0": r"model/openvino/cam18vt1_0_openvino_model",
            "model_CAM18vt1_1": r"model/openvino/cam18vt1_1_openvino_model",
            "model_CAM18vt1_2": r"model/openvino/cam18vt1_2_openvino_model",
            "model_CAM18vt2_0": r"model/openvino/cam18vt2_0_openvino_model",
            "model_CAM18vt2_1": r"model/openvino/cam18vt2_1_openvino_model",
            "model_CAM18vt2_2": r"model/openvino/cam18vt2_2_openvino_model",
            "model_CAM18vt2_3": r"model/openvino/cam18vt2_3_openvino_model",
            "model_CAM18vt2_4": r"model/openvino/cam18vt2_4_openvino_model",
            "model_CAM19vt2_0": r"model/openvino/cam19vt2_0_openvino_model",

            "model_CAM12vt1_0_PM": r'model_PM/openvino/cam12vt1_0_openvino_model',
            "model_CAM13vt1_0_PM": r"model_PM/openvino/cam13vt1_0_openvino_model",
            "model_CAM13vt1_1_PM": r"model_PM/openvino/cam13vt1_1_openvino_model",
            "model_CAM14vt1_0_PM": r"model_PM/openvino/cam14vt1_0_openvino_model",
            "model_CAM15vt1_0_PM": r"model_PM/openvino/cam15vt1_0_openvino_model",
            "model_CAM15vt1_1_PM": r"model_PM/openvino/cam15vt1_1_openvino_model",
            "model_CAM16vt1_0_PM": r"model_PM/openvino/cam16vt1_0_openvino_model",
            "model_CAM17vt1_1_PM": r"model_PM/openvino/cam17vt1_1_openvino_model",
            "model_CAM17vt1_3_PM": r"model_PM/openvino/cam17vt1_3_openvino_model",
            "model_CAM18vt2_2_PM": r"model_PM/openvino/cam18vt2_2_openvino_model",
            "model_CAM18vt2_4_PM": r"model_PM/openvino/cam18vt2_4_openvino_model",
            "model_CAM19vt2_0_PM": r"model_PM/openvino/cam19vt2_0_openvino_model"
        }
        with ThreadPoolExecutor(max_workers=30) as executor:
            futures = {key: executor.submit(self.load_model, path) for key, path in model_paths.items()}
            for key, future in futures.items():
                try:
                    model = future.result()
                    setattr(self, key, model if isinstance(model, YOLO) else None)
                    print(f"{key} {'loaded ✅' if isinstance(model, YOLO) else 'load FAILED'}")
                except Exception as e:
                    print(f"{key} ERROR: {e}")
                    setattr(self, key, None)
        print("Init models OK")

    def save_total_check(self):
        data = {
            "total_check": self.qty_all,
            "qty_ok": self.qty_ok,
            "qty_ng": self.qty_ng,
            "last_reset_date": self.last_reset_date.strftime("%Y-%m-%d")
        }
        with open("point_config/config.json", "w") as f:
            json.dump(data, f)

    def load_checkpoints(self):
        folder_path = "point_config"
        for file_name in os.listdir(folder_path):
            if file_name.endswith(".txt"):
                with open(os.path.join(folder_path, file_name), "r", encoding="utf-8") as f:
                    content = f.read()
                    self.total_points += len(re.findall(r"\{.*?\}", content))

    def thuchien(self, event=None):
        try:
            self.start = True
            self.start_time = time.time()
            self.widget_result_check.label_image_1.configure(text="Starting check", image='')
            self.widget_result_check.label2.configure(text="NONE", bg='yellow')
            self.widget_station_infor.label_5.configure(text=self.databacode, fg='blue')
            self.widget_result_check.listNGbox.delete(0, tk.END)
            self.list_ng.clear()
            self.so_cam_da_chup = 0
            self.chup_vi_tri = 0
            self.step_cam = 0
            self.xl = 0
            self.ng = 0
            self.lanxl = 0
            self.dem = 0
            self.kq_ng = 0
            self.pointcheck = 0
            self.current_cam_idx = 0
            self.ng_cams.clear()
            self.stop = True
            self.gui_kqdocbarcode = False
            with self.allow_put_lock:
                self.allow_put = True
            self.queue_put_count_vt1 = 0
            self.queue_put_count_vt2 = 0
            self.lable_ng.clear()
            self.ng_frames = {}
            self.ng_cam_keys_pm = set()
            # ✅ Reset trạng thái hiển thị khi bắt đầu chu kỳ mới
            self.last_display_imgtk = None
            self.last_ng_imgtk = None
            self.last_display_mode = None
            with self.futures_lock:
                self.process_futures.clear()
            while not self.queue_xl_anh.empty():
                try:
                    self.queue_xl_anh.get_nowait()
                except Exception:
                    pass
            if self.PM:
                self.executor_ai.submit(self.process_camera_1_pm)
            else:
                self.executor_ai.submit(self.process_camera_1)
        except Exception:
            print(traceback.format_exc())

    def process_camera_1(self):
        cid = self.is480_bridge.machine.cycle.cid if self.is480_bridge.machine.cycle else None
        if not cid or not self.is480_bridge.machine.current(cid):
            return
        try:
            print("\n===== PROCESS CAMERA 1 =====")
            self.queue_put_count_vt1 = 0
            with self.futures_lock:
                self.process_futures.clear()

            if len(self.list_ng) > 0:
                self.list_ng.clear()

            modelCAM12_VT1 = (self.model_CAM12vt1_0,)
            modelCAM13_VT1 = (self.model_CAM13vt1_0, self.model_CAM13vt1_1,)
            modelCAM14_VT1 = (self.model_CAM14vt1_0, self.model_CAM14vt1_1,)
            modelCAM15_VT1 = (self.model_CAM15vt1_0, self.model_CAM15vt1_1, self.model_CAM15vt1_2,
                              self.model_CAM15vt1_D5_patchcore, self.model_CAM15vt1_D10_patchcore,)
            modelCAM16_VT1 = (self.model_CAM16vt1_0, self.model_CAM16vt1_1, self.model_CAM16vt1_2,
                              self.model_CAM16vt1_3, self.model_CAM16vt1_4)
            modelCAM17_VT1 = (self.model_CAM17vt1_0, self.model_CAM17vt1_1, self.model_CAM17vt1_2,
                              self.model_CAM17vt1_3,)
            modelCAM18_VT1 = (self.model_CAM18vt1_0, self.model_CAM18vt1_1, self.model_CAM18vt1_2,)

            futures = []

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 12, self.cap12, 1, self.toado_CAM12_VT1,
                self.anh_goc_cam12vt1, self.ORB_ROI_LARGE_CAM12_VT1, *modelCAM12_VT1
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 13, self.cap13, 1, self.toado_CAM13_VT1,
                self.anh_goc_cam13vt1, self.ORB_ROI_LARGE_CAM13_VT1, *modelCAM13_VT1
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 14, self.cap14, 1, self.toado_CAM14_VT1,
                self.anh_goc_cam14vt1, self.ORB_ROI_LARGE_CAM14_VT1, *modelCAM14_VT1
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 15, self.cap15, 1, self.toado_CAM15_VT1,
                self.anh_goc_cam15vt1, self.ORB_ROI_LARGE_CAM15_VT1, *modelCAM15_VT1
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 16, self.cap16, 1, self.toado_CAM16_VT1,
                self.anh_goc_cam16vt1, self.ORB_ROI_LARGE_CAM16_VT1, *modelCAM16_VT1
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 17, self.cap17, 1, self.toado_CAM17_VT1,
                self.anh_goc_cam17vt1, self.ORB_ROI_LARGE_CAM17_VT1, *modelCAM17_VT1
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 18, self.cap18, 1, self.toado_CAM18_VT1,
                self.anh_goc_cam18vt1, self.ORB_ROI_LARGE_CAM18_VT1, *modelCAM18_VT1
            ))

            # đợi capture xong
            done = 0
            for f in as_completed(futures):
                f.result()
                done += 1
                print(f"✅ VT1 cam done {done}/7")

            # ---- CHỜ ĐỦ 3 ẢNH ĐƯỢC PUT ----
            t0 = time.time()
            while True:
                with self.queue_put_lock:
                    if self.queue_put_count_vt1 >= 7:
                        break

                if time.time() - t0 > 2:
                    print("⚠ timeout wait VT1 put")
                    break

                time.sleep(0.001)

            if not self._is480_stage_capture_valid(1, 7):
                return

            print("🚀 VT1 queue ready -> AI")

            # xử lý AI song song với cơ cấu chạy
            self.step_cam = 1
            self.xl_queue()

            if self.is480_bridge.machine.current(cid):
                self.is480_bridge.local_stage_done(1)
        except Exception as exc:
            self._is480_fault(f'process_camera_1 failed: {exc}')
            print(traceback.format_exc())

    def process_camera_1_pm(self):
        cid = self.is480_bridge.machine.cycle.cid if self.is480_bridge.machine.cycle else None
        if not cid or not self.is480_bridge.machine.current(cid):
            return
        try:
            print("\n===== PROCESS CAMERA 1 PM =====")
            self.queue_put_count_vt1 = 0
            with self.futures_lock:
                self.process_futures.clear()

            if len(self.list_ng) > 0:
                self.list_ng.clear()

            modelCAM12_VT1_PM = (self.model_CAM12vt1_0_PM,)
            modelCAM13_VT1_PM = (self.model_CAM13vt1_0_PM, self.model_CAM13vt1_1_PM,)
            modelCAM14_VT1_PM = (self.model_CAM14vt1_0_PM,)
            modelCAM15_VT1_PM = (self.model_CAM15vt1_0_PM, self.model_CAM15vt1_1_PM,)
            modelCAM16_VT1_PM = (self.model_CAM16vt1_0_PM,)
            modelCAM17_VT1_PM = (self.model_CAM17vt1_1_PM, self.model_CAM17vt1_3_PM,)

            futures = []

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 12, self.cap12, 1, self.toado_CAM12_VT1_PM,
                self.anh_goc_cam12vt1pm, self.ORB_ROI_LARGE_CAM12_VT1_PM, *modelCAM12_VT1_PM
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 13, self.cap13, 1, self.toado_CAM13_VT1_PM,
                self.anh_goc_cam13vt1pm, self.ORB_ROI_LARGE_CAM13_VT1_PM, *modelCAM13_VT1_PM
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 14, self.cap14, 1, self.toado_CAM14_VT1_PM,
                self.anh_goc_cam14vt1pm, self.ORB_ROI_LARGE_CAM14_VT1_PM, *modelCAM14_VT1_PM
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 15, self.cap15, 1, self.toado_CAM15_VT1_PM,
                self.anh_goc_cam15vt1pm, self.ORB_ROI_LARGE_CAM15_VT1_PM, *modelCAM15_VT1_PM
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 16, self.cap16, 1, self.toado_CAM16_VT1_PM,
                self.anh_goc_cam16vt1pm, self.ORB_ROI_LARGE_CAM16_VT1_PM, *modelCAM16_VT1_PM
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 17, self.cap17, 1, self.toado_CAM17_VT1_PM,
                self.anh_goc_cam17vt1pm, self.ORB_ROI_LARGE_CAM17_VT1_PM, *modelCAM17_VT1_PM
            ))

            # đợi capture xong
            done = 0
            for f in as_completed(futures):
                f.result()
                done += 1
                print(f"✅ VT1 cam done {done}/7")

            # ---- CHỜ ĐỦ 3 ẢNH ĐƯỢC PUT ----
            t0 = time.time()
            while True:
                with self.queue_put_lock:
                    if self.queue_put_count_vt1 >= 6:
                        break

                if time.time() - t0 > 2:
                    print("⚠ timeout wait VT1 put")
                    break

                time.sleep(0.001)

            if not self._is480_stage_capture_valid(1, 6):
                return

            print("🚀 VT1 queue ready -> AI")

            # xử lý AI song song với cơ cấu chạy
            self.step_cam = 1
            self.xl_queue()

            if self.is480_bridge.machine.current(cid):
                self.is480_bridge.local_stage_done(1)
        except Exception as exc:
            self._is480_fault(f'process_camera_1_pm failed: {exc}')
            print(traceback.format_exc())

    def process_camera_2(self):
        cid = self.is480_bridge.machine.cycle.cid if self.is480_bridge.machine.cycle else None
        if not cid or not self.is480_bridge.machine.current(cid):
            return
        try:
            print("\n===== PROCESS CAMERA 2 =====")

            self.queue_put_count_vt2 = 0

            modelCAM14_VT2 = (self.model_CAM14vt2_0,)
            modelCAM15_VT2 = (self.model_CAM15vt2_0, self.model_CAM15vt2_D2_patchcore,)
            modelCAM17_VT2 = (self.model_CAM17vt2_0, self.model_CAM17vt2_1, self.model_CAM17vt2_2,)
            modelCAM18_VT2 = (self.model_CAM18vt2_0, self.model_CAM18vt2_1, self.model_CAM18vt2_2,
                              self.model_CAM18vt2_3, self.model_CAM18vt2_4,)
            modelCAM19_VT2 = (self.model_CAM19vt2_0,)

            futures = []

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 14, self.cap14, 2, self.toado_CAM14_VT2,
                self.anh_goc_cam14vt2, self.ORB_ROI_LARGE_CAM14_VT2, *modelCAM14_VT2
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 15, self.cap15, 2, self.toado_CAM15_VT2,
                self.anh_goc_cam15vt2, self.ORB_ROI_LARGE_CAM15_VT2, *modelCAM15_VT2
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 17, self.cap17, 2, self.toado_CAM17_VT2,
                self.anh_goc_cam17vt2, self.ORB_ROI_LARGE_CAM17_VT2, *modelCAM17_VT2
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 18, self.cap18, 2, self.toado_CAM18_VT2,
                self.anh_goc_cam18vt2, self.ORB_ROI_LARGE_CAM18_VT2, *modelCAM18_VT2
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 19, self.cap19, 2, self.toado_CAM19_VT2,
                self.anh_goc_cam19vt2, self.ORB_ROI_LARGE_CAM19_VT2, *modelCAM19_VT2
            ))

            done = 0
            for future in as_completed(futures):
                future.result()
                done += 1
                print(f"✅ VT2 cam done {done}/5")

            # ⬇️ CHỈ SAU KHI 8 CAM XONG

            print("queue size before wait:", self.queue_xl_anh.qsize())

            # ---- CHỜ ĐỦ 8 PUT ----
            t0 = time.time()
            while True:
                with self.queue_put_lock:
                    if self.queue_put_count_vt2 >= 5:
                        break

                if time.time() - t0 > 3:
                    print("⚠ timeout wait VT2 put")
                    break

                time.sleep(0.001)

            if not self._is480_stage_capture_valid(2, 5):
                return

            print("🚀 VT2 queue ready -> AI")

            # ====== QUAN TRỌNG ======
            # KHÓA TRƯỚC – không nhận ảnh mới
            with self.allow_put_lock:
                self.allow_put = False

            # xử lý AI toàn bộ
            self.xl_queue()
            if self.is480_bridge.machine.current(cid):
                self.is480_bridge.local_stage_done(2)


            # chờ queue rỗng có timeout
            t1 = time.time()
            while not self.queue_xl_anh.empty():
                if time.time() - t1 > 3:
                    print("⚠ timeout clear queue")
                    break

                self.xl_queue()
                time.sleep(0.001)

            # FINISH CHU KỲ
            if self.is480_bridge.machine.current(cid):
                self.finish_one_cycle()
            self.lankt += 1
            print(f"Sau finish_one_cycle lan{self.lankt}: threads = {threading.active_count()}")
        except Exception as exc:
            self._is480_fault(f'process_camera_2 failed: {exc}')
            print(traceback.format_exc())

    def process_camera_2_pm(self):
        cid = self.is480_bridge.machine.cycle.cid if self.is480_bridge.machine.cycle else None
        if not cid or not self.is480_bridge.machine.current(cid):
            return
        try:
            print("\n===== PROCESS CAMERA 2 PM =====")

            self.queue_put_count_vt2 = 0

            modelCAM18_VT2_PM = (self.model_CAM18vt2_2_PM, self.model_CAM18vt2_4_PM,)
            modelCAM19_VT2_PM = (self.model_CAM19vt2_0_PM,)

            futures = []

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 18, self.cap18, 2, self.toado_CAM18_VT2_PM,
                self.anh_goc_cam18vt2pm, self.ORB_ROI_LARGE_CAM18_VT2_PM, *modelCAM18_VT2_PM
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 19, self.cap19, 2, self.toado_CAM19_VT2_PM,
                self.anh_goc_cam19vt2pm, self.ORB_ROI_LARGE_CAM19_VT2_PM, *modelCAM19_VT2_PM
            ))

            done = 0
            for future in as_completed(futures):
                future.result()
                done += 1
                print(f"✅ VT2 cam done {done}/5")

            # ⬇️ CHỈ SAU KHI 8 CAM XONG

            print("queue size before wait:", self.queue_xl_anh.qsize())

            # ---- CHỜ ĐỦ 8 PUT ----
            t0 = time.time()
            while True:
                with self.queue_put_lock:
                    if self.queue_put_count_vt2 >= 2:
                        break

                if time.time() - t0 > 3:
                    print("⚠ timeout wait VT2 put")
                    break

                time.sleep(0.001)

            if not self._is480_stage_capture_valid(2, 2):
                return

            print("🚀 VT2 queue ready -> AI")

            # ====== QUAN TRỌNG ======
            # KHÓA TRƯỚC – không nhận ảnh mới
            with self.allow_put_lock:
                self.allow_put = False

            # xử lý AI toàn bộ
            self.xl_queue()
            if self.is480_bridge.machine.current(cid):
                self.is480_bridge.local_stage_done(2)


            # chờ queue rỗng có timeout
            t1 = time.time()
            while not self.queue_xl_anh.empty():
                if time.time() - t1 > 3:
                    print("⚠ timeout clear queue")
                    break

                self.xl_queue()
                time.sleep(0.001)

            # FINISH CHU KỲ
            if self.is480_bridge.machine.current(cid):
                self.finish_one_cycle()
            self.lankt += 1
            print(f"Sau finish_one_cycle lan{self.lankt}: threads = {threading.active_count()}")
        except Exception as exc:
            self._is480_fault(f'process_camera_2_pm failed: {exc}')
            print(traceback.format_exc())

    def finish_one_cycle(self):
        return self._is480_finish()

    def xl_queue(self):
        print(">>> enter xl_queue")
        while True:
            try:
                args = self.queue_xl_anh.get_nowait()
            except Empty:
                print(">>> xl_queue done")
                break
            future = self.executor.submit(self._is480_process_checked, *args)
            with self.futures_lock:
                self.process_futures.append(future)

    def select_model(self, cam, vitri, point, models):
        current_model = None
        model_type = None
        if self.PM:
            if vitri == 1:
                print('vttttttttttttttttttttttttttt1')
                try:
                    if cam == 12:
                        current_model = models[0]
                        model_type = "yolo"

                    if cam == 13:
                        if point["POINT"] in [1]:
                            current_model = models[0]
                            model_type = "yolo"
                        if point["POINT"] in [2]:
                            current_model = models[1]
                            model_type = "yolo"

                    if cam == 14:
                        current_model = models[0]
                        model_type = "yolo"

                    if cam == 15:
                        if point["POINT"] in [0]:
                            current_model = models[0]
                            model_type = "yolo"
                        if point["POINT"] in [1]:
                            current_model = models[1]
                            model_type = "yolo"

                    if cam == 16:
                        current_model = models[0]
                        model_type = "yolo"

                    if cam == 17:
                        if point["POINT"] in [6]:
                            current_model = models[0]
                            model_type = "yolo"
                        if point["POINT"] in [21]:
                            current_model = models[1]
                            model_type = "yolo"
                except:
                    print(traceback.format_exc())

            if vitri == 2:
                try:
                    if cam == 18:
                        if point["POINT"] in [7]:
                            current_model = models[0]
                            model_type = "yolo"
                        if point["POINT"] in [25]:
                            current_model = models[1]
                            model_type = "yolo"
                    if cam == 19:
                        current_model = models[0]
                        model_type = "yolo"
                except:
                    print(traceback.format_exc())
        else:
            if vitri == 1:
                print('vttttttttttttttttttttttttttt1')
                try:
                    if cam == 12:
                        if point["POINT"] in [0, 1]:#2, 3, 4, 7
                            current_model = models[0]
                            model_type = "yolo"

                    if cam == 13:
                        # print('cam13')
                        if point["POINT"] in [0, 1, 3, 4, 6, 7]:
                            current_model = models[0]
                            model_type = "yolo"
                        if point["POINT"] in [2, 5, 8, 9, 15]:
                            current_model = models[1]
                            model_type = "yolo"

                        # if point["POINT"] == 3:
                        #     current_model = models[2]
                        #     model_type = "patchcore"

                    if cam == 14:
                        if point["POINT"] in [0, 1, 2, 5, 6]:
                            current_model = models[0]
                            model_type = "yolo"

                        if point["POINT"] in [3]:
                            current_model = models[1]
                            model_type = "yolo"

                    if cam == 15:
                        if point["POINT"] in [0, 1, 9, 13]:
                            current_model = models[0]
                            model_type = "yolo"

                        if point["POINT"] in [2, 7, 8, 11]:
                            current_model = models[1]
                            model_type = "yolo"

                        if point["POINT"] in [6, 3, 12]:
                            current_model = models[2]
                            model_type = "yolo"

                        if point["POINT"] == 5:
                            current_model = models[3]
                            model_type = "patchcore"

                        # if point["POINT"] == 10:
                        #     current_model = models[4]
                        #     model_type = "patchcore"

                    if cam == 16:
                        if point["POINT"] in [0, 1, 2, 3, 4, 30]:
                            current_model = models[0]
                            model_type = "yolo"

                        if point["POINT"] in [5, 6, 7, 8, 27, 31]:
                            current_model = models[1]
                            model_type = "yolo"

                        if point["POINT"] in [26, 29, 9, 10]:
                            current_model = models[2]
                            model_type = "yolo"

                        if point["POINT"] in [28, 32, 33, 34, 35]:
                            current_model = models[3]
                            model_type = "yolo"

                        if point["POINT"] in [11, 14]:
                            current_model = models[4]
                            model_type = "yolo"

                    if cam == 17:
                        if point["POINT"] in [0, 1, 3, 4, 14]:
                            current_model = models[0]
                            model_type = "yolo"

                        if point["POINT"] in [5, 6, 7, 8, 9]:
                            current_model = models[1]
                            model_type = "yolo"

                        if point["POINT"] in [10, 12, 13, 2]:
                            current_model = models[2]
                            model_type = "yolo"

                        if point["POINT"] in [18, 19, 20, 21]:
                            current_model = models[3]
                            model_type = "yolo"

                    if cam == 18:
                        # print('cam18')
                        if point["POINT"] in [0, 1, 4, 7]:
                            current_model = models[0]
                            model_type = "yolo"

                        if point["POINT"] in [2, 6, 9, 11]:
                            current_model = models[1]
                            model_type = "yolo"

                        if point["POINT"] in [5, 8, 12, 14]:
                            current_model = models[2]
                            model_type = "yolo"

                except:
                    print(traceback.format_exc())

            if vitri == 2:
                try:
                    if cam == 14:
                        if point["POINT"] in [0]:
                            current_model = models[0]
                            model_type = "yolo"

                    if cam == 15:
                        if point["POINT"] in [0, 1, 2, 3, 4]:
                            current_model = models[0]
                            model_type = "yolo"

                    if cam == 17:
                        if point["POINT"] in [0, 3, 4, 5]:
                            current_model = models[0]
                            model_type = "yolo"

                        if point["POINT"] in [1, 6, 9, 2]:
                            current_model = models[1]
                            model_type = "yolo"

                        if point["POINT"] in [7, 8, 10, 11]:
                            current_model = models[2]
                            model_type = "yolo"

                    if cam == 18:
                        if point["POINT"] in [0, 1, 17, 22]:
                            current_model = models[0]
                            model_type = "yolo"

                        if point["POINT"] in [2, 3, 4, 5, 6]:
                            current_model = models[1]
                            model_type = "yolo"

                        if point["POINT"] in [7, 8, 9, 18]:
                            current_model = models[2]
                            model_type = "yolo"

                        if point["POINT"] in [10, 15, 20, 24]:
                            current_model = models[3]
                            model_type = "yolo"

                        if point["POINT"] in [16, 25]:
                            current_model = models[4]
                            model_type = "yolo"

                    if cam == 19:
                        if point["POINT"] in [1, 2, 3, 4]:
                            current_model = models[0]
                            model_type = "yolo"
                except:
                    print(traceback.format_exc())

        return current_model, model_type

    def load_checkpoints(self):
        folder_path = "point_config"

        for file_name in os.listdir(folder_path):
            if file_name.endswith(".txt"):
                with open(os.path.join(folder_path, file_name), "r", encoding="utf-8") as f:
                    content = f.read()
                    self.total_points += len(re.findall(r"\{.*?\}", content))

    def process_single_point(self, frame, cam, vitri, point, zones, models):
        try:
            with self.camera_counter_lock:
                self.pointcheck += 1

            # ==== UI progress ====
            self.ui_queue.put({
                "type": "progress",
                "current": self.pointcheck,
                "total": self.total_points
            })

            # ---- TÂM ROI REF ----
            cx = point["X"] + point["SIZE"] / 2
            cy = point["Y"] + point["SIZE"] / 2

            dx, dy = 0.0, 0.0

            # ---- TÌM ORB ZONE ----
            for z in zones:
                zx, zy, zw, zh = z["xywh"]
                if zx <= cx <= zx + zw and zy <= cy <= zy + zh:
                    dx, dy = z["dxdy"]
                    break

            # ---- APPLY DỊCH ----
            new_x = int(point["X"] + dx)
            new_y = int(point["Y"] + dy)

            h, w = frame.shape[:2]
            new_x = max(0, min(new_x, w - point["SIZE"]))
            new_y = max(0, min(new_y, h - point["SIZE"]))

            newbox = (new_x, new_y, point["SIZE"])

            crop_image = frame[
                         new_y:new_y + point["SIZE"],
                         new_x:new_x + point["SIZE"]
                         ].copy()

            crop_image_224 = cv2.resize(crop_image, (224, 224))

            if crop_image.size == 0:
                return None
            self.executor_save.submit(self.save_crop_all, cam, crop_image_224, vitri, point['POINT'])
            current_model, model_type = self.select_model(cam, vitri, point, models)
            if current_model is None:
                print("KHONG CO MODEL CHO CAM-POINT", cam, point["POINT"])
                return None

            # ====================================
            # ============ YOLO ==================
            # ====================================
            if model_type == "yolo":
                results = current_model.predict(crop_image, imgsz=224)
                probs = results[0].probs.data.cpu().numpy()
                _class = int(np.argmax(probs))
                _prob = round(float(probs[_class]), 4)
                _name = results[0].names[_class]
                print(f'cam{cam}_D{point["POINT"]}_acc={_prob}  {_name}')
                return point, crop_image_224, _name, _prob, vitri, newbox, model_type

            # ====================================
            # ========== PATCHCORE ===============
            # ====================================
            elif model_type == "patchcore":
                print('MODE PATCHCORE')

                if current_model.memory_bank is None:
                    print("PatchCore chưa load!")
                    return None

                img_cvt = cv2.cvtColor(crop_image_224, cv2.COLOR_BGR2RGB)
                img_pil = Image.fromarray(img_cvt)

                img_tensor = self.transform(img_pil).unsqueeze(0).to(self.DEVICE)

                score, anomaly_map = current_model.predict(img_tensor)

                print(f'[PATCHCORE] cam{cam}_P{point["POINT"]} score={score}')

                if score > point['THRESHOLD']:
                    _name = "NG"
                    _prob = score

                else:
                    _name = "OK"
                    _prob = 1 - score

                return point, crop_image_224, _name, _prob, vitri, newbox, model_type

        except Exception as e:
            print(f"Point {point['POINT']} error: {e}")
            return None

    def process_image(self, frame, cam, vitri, toado, anhgoc, roi_large, *models):
        print(f'Processing image from camera {cam}.')
        processed_frame = frame.copy()
        gray_ref = cv2.cvtColor(anhgoc, cv2.COLOR_BGR2GRAY)
        gray_new = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        zones = self.compute_orb_dxdy(gray_ref, gray_new, roi_large)
        self.draw_orb_zones(processed_frame, zones, cam)

        futures = []
        for point in toado:
            future = self.executor_points.submit(self._is480_point_checked, self._is480_tls.cid, frame, cam, vitri, point, zones, models)
            futures.append(future)
            if "NUTSO" in point:
                self.so_nut_an[point["NUTSO"]] = processed_frame

        local_results = []
        for future in as_completed(futures):
            result = future.result()
            if result:
                local_results.append(result)

        # if not toado or len(local_results) != len(toado):
        #     raise RuntimeError(f"AI_INCOMPLETE {self.is480_pc_id} CAM{cam} VT{vitri}: {len(local_results)}/{len(toado)} points")
        if not self._is480_current_thread_cycle():
            return
        cam_has_ng = False
        for point, crop_image, _name, _prob, vitri, newbox, model_type in local_results:
            point_is_ng = self.draw_result_on_frame(
                processed_frame, crop_image, point, _name, _prob, vitri, cam, newbox, model_type
            )
            if point_is_ng:
                cam_has_ng = True

        # ✅ Vẽ label CAM sau khi đã vẽ xong tất cả box
        cv2.putText(processed_frame, f'CAM {cam} VT{vitri}', (950, 100),
                    cv2.FONT_HERSHEY_COMPLEX, 2, (0, 255, 0), 2)

        if not self._is480_current_thread_cycle():
            return
        # ✅ Gộp 1 lần ghi ng_frames: PM lưu tất cả, RUN chỉ lưu cam NG
        if self.PM or cam_has_ng:
            with self.ng_frames_lock:
                self.ng_frames[f"CAM{cam}_VT{vitri}"] = processed_frame.copy()

        # ✅ Track riêng cam NG để build_all_grid tô màu đúng (dùng cho cả PM lẫn RUN)
        if cam_has_ng:
            self.ng_cam_keys_pm.add(f"CAM{cam}_VT{vitri}")
            print(f'cam {cam} VT{vitri} NGGGGGGGGG')
            self.executor_save.submit(self.save_picture_ng, cam, processed_frame.copy(), vitri)

        resized = cv2.resize(processed_frame, (1050, 500), interpolation=cv2.INTER_AREA)
        self.ui_queue.put({"type": "image", "frame": resized})

    def build_ng_grid(self, ng_frames_dict):
        """
        Ghép tất cả processed_frame của camera NG thành 1 ảnh.
        - 1 camera NG  → hiển thị full kích thước label (1000x528), không chia ô
        - 2+ camera NG → grid 2 cột, mỗi ô có viền đỏ + label tên camera
        Kết quả luôn trả về ảnh BGR kích thước (1000x528) khớp label_image_1.
        """
        OUTPUT_W, OUTPUT_H = 1000, 528  # kích thước label_image_1

        if not ng_frames_dict:
            return None

        items = sorted(ng_frames_dict.items())
        n = len(items)

        def _draw_label(img, text):
            """Vẽ label tên camera góc trên trái với nền đen bán trong."""
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.9, 2)
            cv2.rectangle(img, (4, 4), (tw + 16, th + 16), (0, 0, 0), -1)
            cv2.putText(img, text, (8, th + 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2, cv2.LINE_AA)

        # ── TRƯỜNG HỢP 1 CAM NG: hiển thị full, không chia ô ──────────────────
        if n == 1:
            cam_label, frame = items[0]
            full = cv2.resize(frame, (OUTPUT_W, OUTPUT_H), interpolation=cv2.INTER_AREA)
            # Viền đỏ mỏng quanh toàn ảnh
            cv2.rectangle(full, (0, 0), (OUTPUT_W - 1, OUTPUT_H - 1), (0, 0, 255), 4)
            _draw_label(full, cam_label)
            return full

        # ── TRƯỜNG HỢP 2+ CAM NG: grid 2 cột ─────────────────────────────────
        COLS = 2
        rows = (n + COLS - 1) // COLS
        CELL_W = OUTPUT_W // COLS  # 500
        CELL_H = OUTPUT_H // rows  # tự co theo số hàng

        grid = np.zeros((rows * CELL_H, COLS * CELL_W, 3), dtype=np.uint8)

        for idx, (cam_label, frame) in enumerate(items):
            row_idx = idx // COLS
            col_idx = idx % COLS
            x0 = col_idx * CELL_W
            y0 = row_idx * CELL_H

            cell = cv2.resize(frame, (CELL_W, CELL_H), interpolation=cv2.INTER_AREA)
            cv2.rectangle(cell, (0, 0), (CELL_W - 1, CELL_H - 1), (0, 0, 255), 3)
            _draw_label(cell, cam_label)
            grid[y0:y0 + CELL_H, x0:x0 + CELL_W] = cell

        # Resize về đúng OUTPUT để khớp label (phòng trường hợp lẻ pixel)
        final = cv2.resize(grid, (OUTPUT_W, OUTPUT_H), interpolation=cv2.INTER_AREA)
        return final

    def build_all_grid(self, all_frames_dict):
        OUTPUT_W, OUTPUT_H = 1000, 528
        if not all_frames_dict:
            return None

        items = sorted(all_frames_dict.items())
        n = len(items)

        COLS = 4
        rows = (n + COLS - 1) // COLS
        CELL_W = OUTPUT_W // COLS
        CELL_H = OUTPUT_H // rows

        # ✅ Lấy danh sách key của cam NG để so sánh
        with self.ng_frames_lock:
            ng_keys = set()
            for point, crop_image, _name, _prob, vitri, newbox, model_type in []:
                pass
        # ✅ Cách đúng: dựa vào list_ng — nếu cam có trong ng_cams thì là NG
        # Key NG là những cam đã được lưu vì cam_has_ng = True
        # → Ta cần phân biệt: frame nào được lưu do PM (tất cả) vs do NG
        # → Thêm 1 set riêng để track

        grid = np.zeros((rows * CELL_H, COLS * CELL_W, 3), dtype=np.uint8)

        for idx, (cam_label, frame) in enumerate(items):
            row_idx = idx // COLS
            col_idx = idx % COLS
            x0 = col_idx * CELL_W
            y0 = row_idx * CELL_H

            cell = cv2.resize(frame, (CELL_W, CELL_H), interpolation=cv2.INTER_AREA)

            # ✅ Xác định cam này OK hay NG
            is_ng = cam_label in self.ng_cam_keys_pm  # set mới cần thêm

            border_color = (0, 0, 255) if is_ng else (0, 255, 0)
            cv2.rectangle(cell, (0, 0), (CELL_W - 1, CELL_H - 1), border_color, 3)
            cv2.putText(cell, cam_label, (8, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)

            # ✅ Vẽ tích V hoặc X góc trên phải
            icon_x = CELL_W - 50
            icon_y = 10
            if is_ng:
                # X đỏ
                cv2.line(cell, (icon_x, icon_y), (icon_x + 35, icon_y + 35), (0, 0, 255), 4)
                cv2.line(cell, (icon_x + 35, icon_y), (icon_x, icon_y + 35), (0, 0, 255), 4)
            else:
                # V xanh
                cv2.polylines(cell,
                              [np.array([[icon_x, icon_y + 18], [icon_x + 12, icon_y + 33], [icon_x + 35, icon_y]],
                                        np.int32)],
                              False, (0, 255, 0), 4)

            grid[y0:y0 + CELL_H, x0:x0 + CELL_W] = cell

        final = cv2.resize(grid, (OUTPUT_W, OUTPUT_H), interpolation=cv2.INTER_AREA)
        return final

    def ui_loop(self):
        try:
            while not self.ui_queue.empty():
                data = self.ui_queue.get_nowait()
                if self._is480_ui_event(data):
                    continue
                if data["type"] == "progress":
                    self.widget_station_infor.label_7.config(text=f'{data["current"]}/{data["total"]}', fg='blue')
                elif data["type"] == "image":
                    self.update_image(data["frame"])
                elif data["type"] == "counter":
                    if "ok" in data: self.widget_station_infor.label_11.config(text=data["ok"])
                    if "ng" in data: self.widget_station_infor.label_12.config(text=data["ng"])
                    if "all" in data: self.widget_station_infor.label_13.config(text=data["all"])
                elif data["type"] == "ng_point":
                    self.widget_result_check.listNGbox.insert(tk.END, data["text"])

                elif data["type"] == "pm_ng":
                    self.widget_result_check.label_image_1.configure(text="PM CAMERA NG \nHÃY PM LẠI", image='')

                elif data["type"] == "button_ng":
                    # ✅ FIX: đổi màu button từ main thread (thread-safe với Tkinter)
                    widget = getattr(self.widget_buttom, data["attr"], None)
                    if widget:
                        self.widget_buttom.set_cam_button_color(widget, 'violet', 'red')
                elif data["type"] == "final_result":
                    result = data["result"]
                    self.widget_result_check.label2.configure(
                        text=result, bg='red' if 'NG' in result else 'green'
                    )
                elif data["type"] == "ng_grid":
                    grid_frame = data["frame"]
                    image = cv2.cvtColor(grid_frame, cv2.COLOR_BGR2RGB)
                    im_pil = Image.fromarray(image)
                    imgtk = ImageTk.PhotoImage(image=im_pil)
                    self.widget_result_check.label_image_1.imgtk = imgtk
                    self.widget_result_check.label_image_1.configure(image=imgtk, text='')
                    # ✅ Lưu lại grid NG để nút XEM ẢNH NG / XEM LẠI có thể khôi phục
                    self.last_ng_imgtk = imgtk
                    self.last_display_imgtk = imgtk
                    self.last_display_mode = "ng_grid"
                elif data["type"] == "START CHECK":
                    self.widget_result_check.label_image_1.configure(text="START CHECK", image='')
                    self.widget_result_check.label2.configure(text="NONE", bg='blue')
                    self.doimau()
                    self.widget_station_infor.label_5.config(text=data.get("barcode", ""))
                    self.widget_result_check.listNGbox.delete(0, tk.END)
                elif data["type"] == "barcodeng":
                    self.widget_result_check.label_image_1.configure(text="DOC BARCODE NG", image='')
                elif data["type"] == "camera_error_popup":
                    cam_id = data["cam_id"]
                    attempts = data["attempts"]
                    if not self.camera_popup_showing:
                        self.camera_popup_showing = True
                        msg = (
                            f"⚠️  CAM {cam_id} KHÔNG KẾT NỐI ĐƯỢC!\n\n"
                            f"Đã thử {attempts} lần nhưng thất bại.\n"
                            f"Vui lòng kiểm tra dây cáp USB và camera."
                        )

                        def _show_popup(m=msg):
                            messagebox.showerror("LỖI CAMERA", m)
                            self.camera_popup_showing = False

                        self.after(0, _show_popup)

                elif data["type"] == "camera_startup_error":
                    # ✅ Popup ngay khi khởi động — camera không mở được từ đầu
                    cam_ids = data["cam_ids"]
                    if not self.camera_popup_showing:
                        self.camera_popup_showing = True
                        cam_list = ", ".join([f"CAM {c}" for c in cam_ids])
                        msg = (
                            f"⚠️  KHÔNG MỞ ĐƯỢC CAMERA KHI KHỞI ĐỘNG!\n\n"
                            f"Camera lỗi: {cam_list}\n\n"
                            f"Vui lòng kiểm tra:\n"
                            f"  • Dây cáp USB đã cắm chưa?\n"
                            f"  • Camera có bị chiếm bởi ứng dụng khác không?\n\n"
                            f"Hệ thống sẽ tự thử kết nối lại liên tục."
                        )

                        def _show_startup_popup(m=msg):
                            messagebox.showerror("LỖI KHỞI ĐỘNG CAMERA", m)
                            self.camera_popup_showing = False

                        self.after(0, _show_startup_popup)
        except Exception as e:
            print("UI loop error:", e)
        self.after(20, self.ui_loop)

    def update_image(self, frame):
        image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        im_pil = Image.fromarray(image)
        imgtk = ImageTk.PhotoImage(image=im_pil)
        self.widget_result_check.label_image_1.imgtk = imgtk
        self.widget_result_check.label_image_1.configure(image=imgtk, text='')
        # ✅ Lưu lại ảnh camera vừa hiển thị — không ghi đè last_ng_imgtk
        self.last_display_imgtk = imgtk
        if self.last_display_mode != "ng_grid":
            self.last_display_mode = "cam"

    def draw_result_on_frame(self, frame, crop_image, point, _name, _prop, vitri, cam, newbox, model_type):
        if not self._is480_current_thread_cycle():
            return False
        """Vẽ kết quả lên frame. Trả về True nếu điểm này là NG, False nếu OK."""
        x_new, y_new, size_new = newbox
        if model_type == 'yolo':
            is_ok = ('OK' in _name and _prop > point['THRESHOLD'])
        else:
            is_ok = ('OK' in _name)

        if is_ok:
            if self.PM:
                if vitri == 1:
                    if (cam == 12 and point['POINT'] == 0) or \
                            (cam == 13 and point['POINT'] == 1) or \
                            (cam == 14 and point['POINT'] == 2) or \
                            (cam == 15 and point['POINT'] == 0) or \
                            (cam == 16 and point['POINT'] == 30) or \
                            (cam == 17 and point['POINT'] == 21):
                        color = (255, 0, 255)  # Cyan - PM đặc biệt
                    else:
                        color = (0, 255, 0)

                elif vitri == 2:
                    if (cam == 18 and point['POINT'] == 7) or \
                            (cam == 19 and point['POINT'] == 2):
                        color = (255, 0, 255)  # Cyan
                    else:
                        color = (0, 255, 0)

                cv2.rectangle(frame, (x_new, y_new), (x_new + size_new, y_new + size_new), color, 5)
                cv2.putText(frame, f"D_{point['POINT']}", (x_new, y_new - 10), cv2.FONT_HERSHEY_COMPLEX, 1, color, 2)
                return False  # ✅ OK → không phải NG

            else:
                color = (0, 255, 0)
                cv2.rectangle(frame, (x_new, y_new), (x_new + size_new, y_new + size_new), color, 5)
                cv2.putText(frame, f"D_{point['POINT']}", (x_new, y_new - 10), cv2.FONT_HERSHEY_COMPLEX, 1, color, 2)
                return False  # ✅ OK → không phải NG
        else:
            if point['NUTSO'] not in self.ng_cams:
                self.ng_cams.append(point['NUTSO'])

            self.executor_save.submit(self.save_crop_ng, cam, crop_image, vitri, point['POINT'], _name, _prop)
            color = (0, 0, 255)
            # ✅ FIX: đổi màu button qua ui_queue thay vì gọi trực tiếp từ worker thread
            attr_name = f"CAM{cam}_VT{vitri}"
            self.ui_queue.put({"type": "button_ng", "attr": attr_name})
            self._is480_ng_point(cam, vitri, point)
            self.list_ng.append(crop_image)
            label = f'D{point["POINT"]}_CAM{cam}'
            self.lable_ng.append(label)
            self.ui_queue.put({"type": "ng_point", "text": f"D{point['POINT']}_CAM {cam}"})
            cv2.rectangle(frame, (x_new, y_new), (x_new + size_new, y_new + size_new), color, 5)
            cv2.putText(frame, f"D_{point['POINT']}", (x_new, y_new - 10), cv2.FONT_HERSHEY_COMPLEX, 1, color, 2)
            return True  # ✅ NG

    def save_picture(self, cam, frame, vitri):
        try:
            if frame is None or frame.size == 0:
                return
            base_path = self.SAVE_ALL_DIR
            now = datetime.now()
            current_time = now.time()
            if dt_time(7, 0) <= current_time <= dt_time(19, 45):
                self.shift = "ca_ngay"
            elif current_time >= dt_time(20, 0) or current_time <= dt_time(6, 30):
                self.shift = "ca_dem"
            else:
                self.shift = "ngoai_ca"
            if self.shift == "ca_dem" and current_time <= dt_time(6, 30):
                self.save_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                self.save_date = now.strftime("%Y-%m-%d")
            save_path = os.path.join(base_path, self.save_date, self.shift, f"cam{cam}", f"vitri{vitri}")
            os.makedirs(save_path, exist_ok=True)
            timestamp = now.strftime("%H%M%S_%f")[:-3]
            filename = f"{self.databacode}_{timestamp}.jpg"
            cv2.imwrite(os.path.join(save_path, filename), frame)
        except Exception as e:
            print(f"Lỗi lưu ảnh camera {cam}: {e}")

    def save_picture_ng(self, cam, frame, vitri):
        try:
            if frame is None or frame.size == 0:
                return
            base_path = self.SAVE_NG_DIR
            now = datetime.now()
            current_time = now.time()
            if dt_time(7, 0) <= current_time <= dt_time(19, 45):
                self.shift = "ca_ngay"
            elif current_time >= dt_time(20, 0) or current_time <= dt_time(6, 30):
                self.shift = "ca_dem"
            else:
                self.shift = "ngoai_ca"
            if self.shift == "ca_dem" and current_time <= dt_time(6, 30):
                self.save_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                self.save_date = now.strftime("%Y-%m-%d")
            save_path = os.path.join(base_path, self.save_date, self.shift, f"cam{cam}", f"vitri{vitri}")
            os.makedirs(save_path, exist_ok=True)
            timestamp = now.strftime("%H%M%S_%f")[:-3]
            cv2.imwrite(os.path.join(save_path, f"{self.databacode}_{timestamp}.jpg"), frame)
        except Exception as e:
            print(f"Lỗi lưu ảnh NG camera {cam}: {e}")

    def save_crop_ng(self, cam, frame, vitri, point, _name, _prop):
        try:
            if frame is None or frame.size == 0:
                return
            base_path = self.SAVE_CROP_NG_DIR
            now = datetime.now()
            current_time = now.time()
            shift = "ca_ngay" if dt_time(7, 0) <= current_time <= dt_time(20, 0) else (
                "ca_dem" if current_time >= dt_time(21, 0) or current_time <= dt_time(6, 0) else "ngoai_ca")
            save_date = (now - timedelta(days=1)).strftime("%Y-%m-%d") if shift == "ca_dem" and current_time <= dt_time(
                6, 0) else now.strftime("%Y-%m-%d")
            save_path = os.path.join(base_path, save_date, shift, f"cam{cam}", f"vitri{vitri}", f"D{point}")
            os.makedirs(save_path, exist_ok=True)
            timestamp = now.strftime("%H%M%S_%f")[:-3]
            cv2.imwrite(os.path.join(save_path, f"{self.databacode}_{timestamp}_{_name}_{_prop}.png"), frame)
        except Exception as e:
            print(f"Lỗi save_crop_ng camera {cam}: {e}")

    def save_crop_all(self, cam, frame, vitri, point):
        try:
            if frame is None or frame.size == 0:
                return
            base_path = self.SAVE_CROP_ALL_DIR
            now = datetime.now()
            current_time = now.time()
            shift = "ca_ngay" if dt_time(7, 0) <= current_time <= dt_time(20, 0) else (
                "ca_dem" if current_time >= dt_time(21, 0) or current_time <= dt_time(6, 0) else "ngoai_ca")
            save_date = (now - timedelta(days=1)).strftime("%Y-%m-%d") if shift == "ca_dem" and current_time <= dt_time(
                6, 0) else now.strftime("%Y-%m-%d")
            save_path = os.path.join(base_path, save_date, shift, f"cam{cam}", f"vitri{vitri}", f"D{point}")
            os.makedirs(save_path, exist_ok=True)
            timestamp = now.strftime("%H%M%S_%f")[:-3]
            cv2.imwrite(os.path.join(save_path, f"{self.databacode}_{timestamp}.png"), frame)
        except Exception as e:
            print(f"Lỗi save_crop_all camera {cam}: {e}")

    def demtime(self):
        def update_timer():
            if self.stop:
                self.dem += 1
                self.update_label()
                self.after(1000, update_timer)

        update_timer()

    def update_label(self):
        self.widget_station_infor.label_6.configure(text=str(self.dem) + " s")

    def show_image_click_list(self, event):
        listbox = event.widget if event is not None else self.widget_result_check.listNGbox
        selected_index = listbox.curselection()
        if not selected_index:
            return
        i = selected_index[0]
        if i >= len(self.list_ng):
            print(f"Index {i} vượt phạm vi list_ng ({len(self.list_ng)})")
            return
        cv2.destroyAllWindows()
        crop_image = self.list_ng[i]
        image = cv2.resize(crop_image, (224, 224), interpolation=cv2.INTER_LINEAR)
        selected_lang = listbox.get(i)
        cv2.imshow(selected_lang, image)
        cv2.waitKey(1)

    def bat_dau(self):
        print('bat dau nhe')
        try:
            self.thuchien()
        except:
            print(traceback.format_exc())

    def kiemtra_VT1(self):
        try:
            if self.step_cam == 0:
                print(' kiemtra_VT1')
                if self.PM:
                    self.process_camera_1_pm()
                else:
                    self.process_camera_1()
            else:
                print(' chua chup xong VT1')
        except:
            print(traceback.format_exc())

    def kiemtra_VT2(self):
        try:
            if self.step_cam == 1:
                print(' kiemtra_VT2')
                if self.PM:
                    self.process_camera_2_pm()
                else:
                    self.process_camera_2()
            else:
                print(' chua chup xong VT1 2 ')
        except:
            print(traceback.format_exc())

    def guibangduocVT1(self):
        raise RuntimeError('Worker must report events to PC1, not write PLC Serial')

    def guibangduocVT2(self):
        raise RuntimeError('Worker must report events to PC1, not write PLC Serial')

    def guikqok(self):
        raise RuntimeError('Worker must report events to PC1, not write PLC Serial')

    def guikqng(self):
        raise RuntimeError('Worker must report events to PC1, not write PLC Serial')

    def read_serial(self):
        raise RuntimeError('Serial PLC was removed from workers; only PC1 uses COM1')

    def doimau(self):
        self.widget_buttom._clear_cam_button_highlight()
        self.dem = 0
        if self.PM:
            cam_vt_list = [(12, 1), (13, 1), (14, 1), (15, 1), (16, 1), (17, 1), (18, 1), (14, 2), (15, 2), (17, 2),
                           (18, 2), (19, 2)]
        else:
            cam_vt_list = [(12, 1), (13, 1), (14, 1), (15, 1), (16, 1), (17, 1), (18, 1), (14, 2), (15, 2), (17, 2),
                           (18, 2), (19, 2)]

        for cam, vt in cam_vt_list:
            attr_name = f"CAM{cam}_VT{vt}"
            widget = getattr(self.widget_buttom, attr_name, None)
            if widget:
                self.widget_buttom.set_cam_button_color(widget, 'green', 'yellow')
            else:
                print(f"Không tìm thấy widget: {attr_name}")

    def nhanbarcode(self):
        raise RuntimeError('Legacy barcode listener removed; Bridge receives structured CAPTURE_VT1/VT2')

    def switch_cam(self, event=None):
        # self.current_cam_idx = (self.current_cam_idx + 1) % 7
        if self.ng_cams is not None and len(self.ng_cams) == 0:
            self.ng_cams = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]

        if not self.ng_cams:
            return
        ng = sorted(self.ng_cams)

        try:
            pos = ng.index(self.current_cam_idx)
            next_pos = (pos + 1) % len(ng)
        except ValueError:
            next_pos = 0

        self.current_cam_idx = ng[next_pos]
        self.widget_buttom._show_cam(self.current_cam_idx)

    def handle_space_switch_cam(self, event=None):
        self.switch_cam(event)
        return "break"


from is480_app import install_controller
install_controller(Main_Application, 'PC2')

app_ui = Main_Application()
if __name__ == '__main__':



    app_ui.focus_force()

    app_ui.bind("<space>", app_ui.handle_space_switch_cam)

    app_ui.protocol("WM_DELETE_WINDOW", app_ui.on_closing)
    app_ui.mainloop()
