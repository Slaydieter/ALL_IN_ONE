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
from tkinter import messagebox, ttk

import cv2
import numpy as np
import serial
import torch
from PIL import Image, ImageTk
from ReadBarcode import ScanHome
from comgrab3 import enumerate_cameras
from patchcore.models.patchcore224 import PatchCore
from torchvision import transforms
from ultralytics import YOLO

sys.path.insert(0, os.path.dirname(__file__))

from is480_statistics import (
    APPROVAL_REASONS,
    DATABASE_PATH,
    ShiftSchedule,
    confirm_ng_check,
    create_inspection_check,
    get_recent_production_dates,
    init_statistics_database,
    query_approvable_ng_checks,
    query_current_shift_summary,
    query_statistics_checks,
    update_final_status,
)

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
        self.pm_mode = pm_mode
        self.grid()
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
        self.label_4 = tk.Label(self, text="Inside_L2_1200_PC1", font=("bold", 20), fg='black')
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

        self.CAM0_VT1 = tk.Button(self, text="CAM0_VT1", command=self.hienthiCAM0_VT1, **btn_style)
        self.CAM0_VT1.grid(column=0, row=0, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM1_VT1 = tk.Button(self, text="CAM1_VT1", command=self.hienthiCAM1_VT1, **btn_style)
        self.CAM1_VT1.grid(column=1, row=0, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM2_VT1 = tk.Button(self, text="CAM2_VT1", command=self.hienthiCAM2_VT1, **btn_style)
        self.CAM2_VT1.grid(column=2, row=0, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM3_VT1 = tk.Button(self, text="CAM3_VT1", command=self.hienthiCAM3_VT1, **btn_style)
        self.CAM3_VT1.grid(column=3, row=0, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM4_VT1 = tk.Button(self, text="CAM4_VT1", command=self.hienthiCAM4_VT1, **btn_style)
        self.CAM4_VT1.grid(column=4, row=0, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM5_VT1 = tk.Button(self, text="CAM5_VT1", command=self.hienthiCAM5_VT1, **btn_style)
        self.CAM5_VT1.grid(column=5, row=0, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM6_VT1 = tk.Button(self, text="CAM6_VT1", command=self.hienthiCAM6_VT1, **btn_style)
        self.CAM6_VT1.grid(column=6, row=0, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM7_VT1 = tk.Button(self, text="CAM7_VT1", command=self.hienthiCAM7_VT1, **btn_style)
        self.CAM7_VT1.grid(column=7, row=0, sticky=tk.NSEW, padx=3, pady=3)

        ########################### VT2 ###########################

        self.CAM0_VT2 = tk.Button(self, text="CAM0_VT2", command=self.hienthiCAM0_VT2, **btn_style)
        self.CAM0_VT2.grid(column=0, row=1, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM1_VT2 = tk.Button(self, text="CAM1_VT2", command=self.hienthiCAM1_VT2, **btn_style)
        self.CAM1_VT2.grid(column=1, row=1, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM2_VT2 = tk.Button(self, text="CAM2_VT2", command=self.hienthiCAM2_VT2, **btn_style)
        self.CAM2_VT2.grid(column=2, row=1, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM3_VT2 = tk.Button(self, text="CAM3_VT2", command=self.hienthiCAM3_VT2, **btn_style)
        self.CAM3_VT2.grid(column=3, row=1, sticky=tk.NSEW, padx=3, pady=3)

        self.CAM5_VT2 = tk.Button(self, text="CAM5_VT2", command=self.hienthiCAM5_VT2, **btn_style)
        self.CAM5_VT2.grid(column=4, row=1, sticky=tk.NSEW, padx=3, pady=3)

        # ✅ Nút XEM ẢNH NG — hiển thị grid tổng hợp tất cả camera NG của lần kiểm tra cuối
        self.btn_xem_ng = tk.Button(
            self, text="📷 XEM ẢNH NG", fg='white', bg='red',
            font=("Arial Bold", 13), width=15, height=1,
            command=self.hienthi_anh_ng
        )
        self.btn_xem_ng.grid(column=6, row=1, columnspan=2, sticky=tk.N + tk.S + tk.E + tk.W)

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
            0: "CAM0_VT1",
            1: "CAM1_VT1",
            2: "CAM2_VT1",
            3: "CAM3_VT1",
            4: "CAM4_VT1",
            5: "CAM5_VT1",
            6: "CAM6_VT1",
            7: "CAM7_VT1",
            8: "CAM0_VT2",
            9: "CAM1_VT2",
            10: "CAM2_VT2",
            11: "CAM5_VT2",
            12: "CAM3_VT2",
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

    def hienthiCAM0_VT1(self):
        self._show_cam(0)

    def hienthiCAM1_VT1(self):
        self._show_cam(1)

    def hienthiCAM2_VT1(self):
        self._show_cam(2)

    def hienthiCAM3_VT1(self):
        self._show_cam(3)

    def hienthiCAM4_VT1(self):
        self._show_cam(4)

    def hienthiCAM5_VT1(self):
        self._show_cam(5)

    def hienthiCAM6_VT1(self):
        self._show_cam(6)

    def hienthiCAM7_VT1(self):
        self._show_cam(7)

    def hienthiCAM0_VT2(self):
        self._show_cam(8)

    def hienthiCAM1_VT2(self):
        self._show_cam(9)

    def hienthiCAM2_VT2(self):
        self._show_cam(10)

    def hienthiCAM3_VT2(self):
        self._show_cam(12)

    def hienthiCAM5_VT2(self):
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
    SAVE_ALL_DIR = r"E:\save_all_file"
    SAVE_NG_DIR = r"E:\save_file_ng"
    SAVE_CROP_ALL_DIR = r"E:\save_crop_all"
    SAVE_CROP_NG_DIR = r"E:\save_crop_ng"

    APPROVAL_CARD_COM_PORT = "COM5"
    APPROVAL_CARD_BAUD_RATE = 9600
    # Same UID->name JSON schema as L1200_C6 PC3; update only if site differs.
    NG_CONFIRM_USER_FILE = r"E:\database\L1200_C1.json"
    NG_CONFIRM_CARD_DEBOUNCE_SEC = 1.0

    # C6 parity: repair scanner is physically independent from BODY COM3.
    # Default USB HID (keyboard wedge); for separate COM set e.g. 'COM5'.
    BODY_REPAIR_SCANNER_PORT = ''
    BODY_REPAIR_SCANNER_BAUD = 9600

    def clean_approval_card_data(self, data):
        if isinstance(data, bytes):
            text = data.decode("utf-8", errors="ignore")
        else:
            text = str(data)

        return "".join(re.findall(r"\d+", text))

    def read_approval_card_serial_loop(self, ser, on_card, stop_event):
        """C6-compatible STX/ETX RFID frames, with CR/LF fallback.

        Events are pushed to UI queue; the RFID worker never touches Tk directly.
        """
        if ser is None or not callable(on_card):
            raise ValueError('Chua mo duoc cong COM4 hoac callback RFID khong hop le')
        buffer = bytearray()
        while not stop_event.is_set():
            chunk = ser.read(64)
            if not chunk:
                continue
            buffer.extend(chunk)
            while True:
                stx = buffer.find(b'\x02')
                if stx < 0:
                    break
                if stx > 0:
                    del buffer[:stx]
                etx = buffer.find(b'\x03', 1)
                if etx < 0:
                    break
                payload = bytes(buffer[1:etx])
                del buffer[:etx + 1]
                uid = self.clean_approval_card_data(payload)
                if uid:
                    on_card(uid)
            if b'\x02' not in buffer and (b'\r' in buffer or b'\n' in buffer):
                normalized = bytes(buffer).replace(b'\r', b'\n')
                parts = normalized.split(b'\n')
                buffer = bytearray(parts[-1])
                for part in parts[:-1]:
                    uid = self.clean_approval_card_data(part.strip())
                    if uid:
                        on_card(uid)
            if len(buffer) > 2048:
                pos = buffer.rfind(b'\x02')
                buffer = buffer[pos:] if pos >= 0 else bytearray()

    def _load_ng_confirm_user(self, card_id):
        return self._is480_load_ng_confirm_user(card_id)

    def _is_ng_confirmation_waiting(self):
        return self._is480_is_ng_confirmation_waiting()

    def start_ng_confirmation_wait(self, barcode, check_no, cycle_id):
        return self._is480_start_ng_confirmation_wait(barcode, check_no, cycle_id)

    def handle_card_scan(self, card_id):
        return self._is480_handle_card_scan(card_id)

    def _confirm_ng_card_worker(self, card_id):
        return self._is480_confirm_ng_card_worker(card_id)

    def __init__(self):
        super().__init__()
        self.title("1200_L2_PC1_v2.py")
        self.geometry('1200x900')
        self.grid()
        self.state("zoomed")
        self.rowconfigure(0, minsize=100, weight=1)
        self.rowconfigure(1, minsize=500, weight=1)
        self.rowconfigure(2, minsize=100, weight=1)
        self.columnconfigure(0, minsize=800, weight=1)
        self.PM = True
        self.da_pm_ok = False
        self.show_pm()

        # self.widget_station_infor = StationInfor(self)
        # self.widget_station_infor.grid(row=0, column=0, sticky=tk.N + tk.S + tk.E + tk.W)
        # self.widget_result_check = ResultCheck(self)
        # self.widget_result_check.grid(row=1, column=0, sticky=tk.N + tk.S + tk.E + tk.W)
        # self.widget_buttom = Buttom_frame(self)
        # self.widget_buttom.grid(row=2, column=0, sticky=tk.N + tk.S + tk.E + tk.W)

        self.initProcess = ScanHome('COM3')
        self.so_cam_da_chup = 0
        self.camera_counter_lock = threading.Lock()
        self.DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
        self.xl = 0
        self.ng = 0
        self.lanxl = 0
        self.lankt = 0
        self.barcodes = None
        self.databacode = None
        self.queue_xl_anh = queue.Queue(maxsize=24)
        self.results_queue = queue.Queue(maxsize=128)
        self.ui_queue = __import__("is480_runtime").CoalescedUIQueue()
        self.frame_lock = threading.Lock()
        self.latest_frames = {i: None for i in range(8)}
        self.xoafile()
        self.camera_running = True
        self.list_ng = []
        self.so_nut_an = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        self.stop = False
        self.kq_ng = False
        self.D_NG = False
        self.current_cam_idx = 0
        self.clients = {}
        self.ng_cams = []
        self.client_list = [
            ("10.255.128.197", 23456),
            ("10.255.129.53", 23456)
        ]
        # self.client_list = [
        #     ("169.254.182.130", 23456),
        #     ("169.254.150.228", 23456)
        # ]
        self.transform = transforms.Compose([
            transforms.Resize((224, 224)),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        ])
        self.CAM_LOCATION_REF = {
            "cam0": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(1)#USBMI(0)",#
            "cam1": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(2)#USBMI(0)",#
            "cam2": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(4)#USB(3)#USBMI(0)",#
            "cam3": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(5)#USB(1)#USBMI(0)",#
            "cam4": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(9)#USBMI(0)",#
            "cam5": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(3)#USBMI(0)",#
            "cam6": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(6)#USBMI(0)",#
            "cam7": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(10)#USB(4)#USB(3)#USBMI(0)",
        }
        self.map_cam()
        self.orb = cv2.ORB_create(500)
        self.start = False

        # ✅ FIX: Khai báo TẤT CẢ biến liên quan đến camera_reader
        #         TRƯỚC KHI gọi init_all_cameras() để tránh AttributeError
        #         khi thread reader khởi động và truy cập các thuộc tính này
        self.camera_error_flags = {}
        self.current_camera_errors = set()
        self.camera_popup_showing = False
        self.MAX_RECONNECT = 3
        cam_list = [0, 1, 2, 3, 4, 5, 6, 7]
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
        self.current_barcode = None
        self.current_check_no = None
        self.statistics_window = None
        self.statistics_tree = None
        self.approval_window = None
        self.approval_check_entry = None
        self.approval_operator_var = None
        self.approval_operator_entry = None
        self.approval_reason_combo = None
        self.approval_rows_by_display = {}
        self.latest_approval_operator = ""
        self.approval_card_stop_event = threading.Event()
        self.approval_card_serial = None
        self.approval_card_error = ""
        self.approval_date_var = tk.StringVar()
        self.approval_shift_var = tk.StringVar(value="Tat ca")
        self.statistics_barcode_var = tk.StringVar()
        self.statistics_date_from_var = tk.StringVar()
        self.statistics_date_to_var = tk.StringVar()
        self.statistics_shift_var = tk.StringVar(value="Tat ca")
        self.statistics_filter_status_var = tk.StringVar(value="Tat ca")
        self.statistics_status_var = tk.StringVar(value="Hien thi tat ca du lieu")
        self.statistics_current_shift_var = tk.StringVar(value="Ca hien tai: --")
        self.statistics_current_ok_var = tk.StringVar(value="OK: 0")
        self.statistics_current_ng_var = tk.StringVar(value="NG: 0")
        self.set_current_shift_statistics_filter()
        self.set_current_shift_approval_filter()
        self.init_statistics_database()
        self.create_menu_bar()
        self.ng_points = []
        self.ng_frames = {}
        self.ng_frames_lock = threading.Lock()
        self.gui_kq = False

        # ✅ Lưu trạng thái hiển thị để nút XEM LẠI / XEM NG có thể khôi phục
        self.last_display_imgtk = None  # ImageTk cuối cùng hiển thị
        self.last_ng_imgtk = None  # ImageTk grid NG — không bị ghi đè khi xem cam đơn
        self.last_display_mode = None  # 'cam' | 'ng_grid' | 'text' | None

        try:
            self.ser = serial.Serial(port='COM1', baudrate=9600, timeout=1, write_timeout=1)
        except serial.SerialException as e:
            print(f"Không mở được COM1: {e}")
            self.ser = None

        try:
            self.approval_card_serial = serial.Serial(
                port=self.APPROVAL_CARD_COM_PORT,
                baudrate=self.APPROVAL_CARD_BAUD_RATE,
                timeout=0.1,
            )
            self.approval_card_error = ""
        except Exception as e:
            self.approval_card_error = (
                f"Khong mo duoc cong doc the xac nhan: "
                f"port={self.APPROVAL_CARD_COM_PORT}, baudrate={self.APPROVAL_CARD_BAUD_RATE}, error={e}"
            )
            print(self.approval_card_error)
            self.approval_card_serial = None

        self.last_reset_date = datetime.now().date()
        self.total_points = 69
        self.executor = ThreadPoolExecutor(max_workers=10)
        self.executor_points = ThreadPoolExecutor(max_workers=8)
        self.executor_ai = ThreadPoolExecutor(max_workers=10)
        self.executor_save = __import__("is480_runtime").BoundedExecutor(max_workers=4,max_pending=320)
        self.after(20, self.ui_loop)
        self.Init_orb()
        self.Init_load_orb()

        self.ORB_ROI_LARGE_CAM0_VT1 = [
            {"name": "VÙNG1", "xywh": (246, 227, 946, 555)},
            {"name": "VÙNG1", "xywh": (168, 54, 455, 323)},
            {"name": "VÙNG1", "xywh": (126, 742, 434, 294)}
        ]

        self.ORB_ROI_LARGE_CAM1_VT1 = [
            {"name": "VÙNG1", "xywh": (301, 6, 416, 325)},
            {"name": "VÙNG2", "xywh": (1297, 246, 461, 802)}
        ]

        self.ORB_ROI_LARGE_CAM2_VT1 = [
            {"name": "VÙNG1", "xywh": (888, 363, 970, 651)}
        ]

        self.ORB_ROI_LARGE_CAM3_VT1 = [
            {"name": "VÙNG1", "xywh": (52, 358, 975, 358)}
        ]

        self.ORB_ROI_LARGE_CAM4_VT1 = [
            {"name": "VÙNG1", "xywh": (7, 61, 492, 378)},
            {"name": "VÙNG2", "xywh": (1357, 178, 529, 383)},
            {"name": "VÙNG3", "xywh": (1357, 561, 462, 459)}
        ]

        self.ORB_ROI_LARGE_CAM5_VT1 = [
            # {"name": "VÙNG1", "xywh": (457, 264, 880, 470)}
            {"name": "VÙNG1", "xywh": (604, 294, 392, 450)}
        ]

        self.ORB_ROI_LARGE_CAM6_VT1 = [
            {"name": "VÙNG1", "xywh": (57, 217, 676, 839)},
            {"name": "VÙNG2", "xywh": (1678, 12, 230, 447)}
        ]

        self.ORB_ROI_LARGE_CAM7_VT1 = [
            {"name": "VÙNG1", "xywh": (444, 232, 1100, 495)}
        ]

        self.ORB_ROI_LARGE_CAM0_VT2 = [
            {"name": "VÙNG1", "xywh": (10, 189, 1239, 723)}
        ]

        self.ORB_ROI_LARGE_CAM1_VT2 = [
            {"name": "VÙNG1", "xywh": (1030, 363, 864, 705)}
        ]

        self.ORB_ROI_LARGE_CAM2_VT2 = [
            {"name": "VÙNG1", "xywh": (829, 192, 848, 675)}
        ]

        self.ORB_ROI_LARGE_CAM3_VT2 = [
            {"name": "VÙNG1", "xywh": (1, 226, 230, 206)}
        ]

        self.ORB_ROI_LARGE_CAM5_VT2 = [
            {"name": "VÙNG1", "xywh": (67, 289, 1664, 595)}
        ]

        self.ORB_ROI_LARGE_CAM1_VT1_PM = [
            {"name": "VÙNG1", "xywh": (345, 70, 362, 298)},
            {"name": "VÙNG2", "xywh": (1299, 15, 490, 706)},
        ]

        self.ORB_ROI_LARGE_CAM3_VT1_PM = [
            {"name": "VÙNG1", "xywh": (60, 515, 239, 219)},
            {"name": "VÙNG2", "xywh": (727, 460, 269, 186)},
        ]

        self.ORB_ROI_LARGE_CAM4_VT1_PM = [
            {"name": "VÙNG1", "xywh": (34, 73, 398, 290)},
        ]

        self.ORB_ROI_LARGE_CAM6_VT1_PM = [
            {"name": "VÙNG1", "xywh": (31, 459, 324, 591)},
            {"name": "VÙNG2", "xywh": (1663, 4, 251, 350)},
        ]

        self.ORB_ROI_LARGE_CAM7_VT1_PM = [
            {"name": "VÙNG1", "xywh": (1233, 571, 186, 179)},
            {"name": "VÙNG2", "xywh": (531, 507, 204, 184)},
        ]

        self.ORB_ROI_LARGE_CAM0_VT2_PM = [
            {"name": "VÙNG1", "xywh": (912, 393, 347, 232)},
            {"name": "VÙNG2", "xywh": (214, 447, 326, 199)},
        ]

        self.ORB_ROI_LARGE_CAM2_VT2_PM = [
            {"name": "VÙNG1", "xywh": (873, 406, 416, 205)},
            {"name": "VÙNG2", "xywh": (1338, 318, 151, 223)},
        ]

        self.ORB_ROI_LARGE_CAM5_VT2_PM = [
            {"name": "VÙNG4", "xywh": (421, 679, 807, 186)},
        ]

        # self.load_checkpoints()
        self._is480_setup()

    def create_menu(self):

        menubar = tk.Menu(self, tearoff=0)

        mode_menu = tk.Menu(
            menubar,
            tearoff=0
        )

        mode_menu.add_command(
            label="RUN MODE",
            command=self._is480_request_run
        )

        menubar.add_cascade(
            label="MODE",
            menu=mode_menu
        )

        self.config(menu=menubar)
        self._is480_add_pc_menu()

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
        self.da_pm_ok = False

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

        self.widget_buttom.CAM0_VT1.grid_remove()
        self.widget_buttom.CAM2_VT1.grid_remove()
        self.widget_buttom.CAM1_VT2.grid_remove()
        self.widget_buttom.CAM3_VT2.grid_remove()
        self.widget_buttom.CAM5_VT1.grid_remove()

        self.title(
            "INSIDE L1200_PC1 - PM MODE"
        )

        print("PM MODE")

        if hasattr(self, "is480_bridge"):
            self._is480_refresh_screen()

    # ======================================================
    # RUN MODE
    # ======================================================

    def show_run(self):

        self.PM = False
        self.da_pm_ok = True
        self.clear_ui()

        self.create_menu_bar()

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
            "INSIDE L1200_PC1 - RUN MODE"
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
        duongdan2 = self.SAVE_CROP_ALL_DIR
        duongdan3 = self.SAVE_CROP_NG_DIR
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

        cam_indices = [self.cam0, self.cam1, self.cam2, self.cam3,
                       self.cam4, self.cam5, self.cam6, self.cam7]

        with ThreadPoolExecutor(max_workers=7) as executor:
            results = list(executor.map(self.Init_camera, cam_indices))

        self.cap0, self.cap1, self.cap2, self.cap3, \
        self.cap4, self.cap5, self.cap6, self.cap7, = results
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
        for idx, (cam_so, cap) in enumerate(zip([0, 1, 2, 3, 4, 5, 6, 7], results)):
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
        for i in [0, 1, 2, 3, 4, 5, 6, 7]:  # ← Sửa ở đây
            cap = getattr(self, f"cap{i}", None)
            if cap is not None:
                try:
                    cap.release()
                except:
                    pass
                setattr(self, f"cap{i}", None)

    # ──────────────────────────────────────────────
    # CAPTURE AND PROCESS
    # ──────────────────────────────────────────────

    def capture_and_process(self, cam_so, cap, vitri, toado, anhgoc, roi_large, *models):
        try:
            print(f"[CAP] cam {cam_so} vt{vitri}")
            if not self._is480_capture_fresh(cam_so,vitri):
                self._is480_fault(f'FRAME_STALE {self.is480_pc_id} CAM{cam_so} VT{vitri}: no fresh frame')
                return

            # ==================== XỬ LÝ ẢNH ĐEN / MẤT FRAME ====================
            MAX_BLACK_RETRY = 8
            RETRY_DELAY = 0.3
            MAX_WAIT_RECONNECT = 12.0

            frame1 = None
            black_count = 0
            reconnect_requested = False

            for attempt in range(1, MAX_BLACK_RETRY + 1):
                current_cap = getattr(self, f"cap{cam_so}", cap)
                if current_cap is None and attempt == 1:
                    print(f"[CAM {cam_so}] cap None -> force reconnect trước khi kiểm tra frame")
                    self.force_reconnect_camera(cam_so)
                    reconnect_requested = True

                with self.frame_lock:
                    cached_frame = self.latest_frames.get(cam_so)

                if (
                        cached_frame is None
                        or self.is_black_image(cached_frame, threshold=12)
                        or self.is_too_bright(cached_frame)
                ):
                    black_count += 1
                    print(f"[CAM {cam_so}] ⚫ Ảnh đen/rỗng/chói (lần {black_count}/{MAX_BLACK_RETRY})")
                    time.sleep(RETRY_DELAY)
                    continue

                frame1 = cached_frame.copy()
                print(f"[CAM {cam_so}] ✅ Frame OK")
                break

            if frame1 is None or self.is_black_image(frame1) or self.is_too_bright(frame1):
                msg = f"CAM{cam_so} mất kết nối hoặc ảnh đen sau {MAX_BLACK_RETRY} lần kiểm tra"
                print(f"[WARNING] {msg} -> thử reconnect trước khi báo lỗi")
                if not reconnect_requested:
                    self.force_reconnect_camera(cam_so)

                print(f"[CAM {cam_so}] Đang chờ frame mới sau reconnect...")
                waited = 0.0
                while waited < MAX_WAIT_RECONNECT:
                    time.sleep(0.4)
                    waited += 0.4

                    with self.frame_lock:
                        cached_frame = self.latest_frames.get(cam_so)

                    if (
                            cached_frame is None
                            or self.is_black_image(cached_frame, threshold=12)
                            or self.is_too_bright(cached_frame)
                    ):
                        print(f"[CAM {cam_so}] Frame sau reconnect chưa ổn định ({waited:.1f}s)")
                        continue

                    frame1 = cached_frame.copy()
                    self.reconnect_waiting[cam_so] = False
                    self.cam_reconnect_attempts[cam_so] = 0
                    print(f"[CAM {cam_so}] ✅ Frame OK sau reconnect ({waited:.1f}s)")
                    break

                if frame1 is None or self.is_black_image(frame1) or self.is_too_bright(frame1):
                    reconnect_msg = f"CAM{cam_so} reconnect thất bại, vẫn không có frame ổn định"
                    print(f"[ERROR] {reconnect_msg}")
                    self.show_camera_error([reconnect_msg])
                    return

            # ====================== TIẾP TỤC XỬ LÝ ======================
            if cam_so in [0, 1, 2]:
                print('xoay',cam_so)
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

        except Exception:
            print(traceback.format_exc())

    def is_too_bright(self, frame, threshold=255):
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
            print(self.cam_reconnect_attempts[cam_id] >= self.MAX_RECONNECT)
            print(f"[CAM {cam_id}] Đã force reconnect {self.cam_reconnect_attempts[cam_id]} lần")

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
        if hasattr(self,'is480_repair_scan_stop'):
            self.is480_repair_scan_stop.set()
        self.approval_card_stop_event.set()
        if hasattr(self, 'is480_repair_scan_stop'):
            self.is480_repair_scan_stop.set()
        if self.approval_card_serial is not None:
            try:
                self.approval_card_serial.close()
            except Exception as exc:
                print(
                    f"Loi dong cong doc the xac nhan: "
                    f"port={self.APPROVAL_CARD_COM_PORT}, error={exc}"
                )

        time.sleep(0.5)

        for i in [0, 1, 2, 3, 4, 5, 6, 7]:
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

    def is_black_image(self, frame, threshold=0):  # Tăng lên 25
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
        self.cam0 = resolved.get("cam0", -1)
        self.cam1 = resolved.get("cam1", -1)
        self.cam2 = resolved.get("cam2", -1)
        self.cam3 = resolved.get("cam3", -1)
        self.cam4 = resolved.get("cam4", -1)
        self.cam5 = resolved.get("cam5", -1)
        self.cam6 = resolved.get("cam6", -1)
        self.cam7 = resolved.get("cam7", -1)
        print(f'{self.cam0},{self.cam1},{self.cam2},{self.cam3},{self.cam4},{self.cam5},{self.cam6},{self.cam7}')

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

    def create_menu_bar(self):
        menu_bar = tk.Menu(self, tearoff=0)

        home_menu = tk.Menu(menu_bar, tearoff=0)
        home_menu.add_command(label="Thống kê (O)", command=self.open_statistics_window)
        home_menu.add_command(label="Xác nhận NG (F1)", command=self.open_approval_window)
        menu_bar.add_cascade(label="Home", menu=home_menu)

        mode_menu = tk.Menu(menu_bar, tearoff=0)
        mode_menu.add_command(label="RUN Mode", command=self._is480_request_run)
        menu_bar.add_cascade(label="Mode", menu=mode_menu)

        self.config(menu=menu_bar)
        self._is480_add_pc_menu()

    def init_statistics_database(self):
        init_statistics_database()

    def create_inspection_check(self, barcode):
        return create_inspection_check(barcode)

    def update_current_final_status(self, final_status, serial_data):
        if self.PM:
            return None
        barcode = self.current_barcode
        check_no = self.current_check_no
        if not barcode:
            raise RuntimeError(
                f"Khong update final_status vi thieu barcode RUN: "
                f"barcode={barcode!r}, check_no={check_no!r}, serial_data={serial_data!r}, "
                f"final_status={final_status!r}, path={DATABASE_PATH}"
            )
        return update_final_status(
            barcode=barcode,
            final_status=final_status,
            check_no=check_no,
        )

    def record_current_pc_error(self, step, exc):
        if self.PM:
            return None
        print(
            f"Loi RUN PC1 khong ghi DB theo flow moi: step={step!r}, "
            f"barcode={self.current_barcode!r}, check_no={self.current_check_no!r}, "
            f"error={exc!r}, path={DATABASE_PATH}"
        )
        return None

    def update_current_shift_summary(self):
        production_date, shift, ok_count, ng_count = query_current_shift_summary()
        self.statistics_current_shift_var.set(f"Ca hien tai: {production_date} {shift}")
        self.statistics_current_ok_var.set(f"OK: {ok_count}")
        self.statistics_current_ng_var.set(f"NG: {ng_count}")

    def set_current_shift_statistics_filter(self):
        shift_info = ShiftSchedule.get_shift_info()
        self.statistics_date_from_var.set(shift_info["date"])
        self.statistics_date_to_var.set(shift_info["date"])
        self.statistics_shift_var.set(shift_info["shift"])
        self.statistics_filter_status_var.set("Tat ca")
        self.statistics_status_var.set("Hien thi du lieu ngay/ca hien tai")

    def set_current_shift_approval_filter(self):
        shift_info = ShiftSchedule.get_shift_info()
        self.approval_date_var.set(shift_info["date"])
        self.approval_shift_var.set(shift_info["shift"])

    def build_approval_display(self, row):
        return (
            f"{row['barcode']} | Lan {row['check_no']} | "
            f"Final={row['final_status']} | "
            f"{row['updated_at'] or ''}"
        )

    def format_approval_detail(self, row):
        return (
            f"ID: {row['id']}\n"
            f"Barcode: {row['barcode']}\n"
            f"Số lần kiểm tra: {row['check_no']}\n"
            f"Trạng thái: {row['final_status']}\n"
            f"Ca làm việc: {row['shift'] or ''}\n"
            f"Thời gian ghi nhận lần 1: {row['created_at'] or ''}\n"
            f"Thời gian cập nhật mới nhất: {row['updated_at'] or ''}"
        )

    def close_approval_window(self):
        if self.approval_window is not None and self.approval_window.winfo_exists():
            self.approval_window.destroy()
        self.approval_window = None
        self.approval_check_entry = None
        self.approval_operator_var = None
        self.approval_operator_entry = None
        self.approval_reason_combo = None
        self.approval_rows_by_display = {}

    def focus_approval_check_entry(self):
        if self.approval_check_entry is not None and self.approval_check_entry.winfo_exists():
            self.approval_check_entry.focus_force()
            self.approval_check_entry.icursor(tk.END)

    def fill_approval_operator_from_card(self, card):
        # PM_OK and PM_NG must NEVER require/consume RFID or modify RUN data.
        if self.PM:
            return
        operator = card.strip()
        if not operator:
            raise ValueError(f"Du lieu nguoi xac nhan rong: card={card!r}")

        self.latest_approval_operator = operator
        if hasattr(self, 'widget_result_check'):
            self.widget_result_check.label3.config(text='RFID: ' + operator)
        if self.approval_operator_var is not None:
            self.approval_operator_var.set(operator)
        if self.approval_operator_entry is not None and self.approval_operator_entry.winfo_exists():
            self.approval_operator_entry.icursor(tk.END)
        if self.approval_reason_combo is not None and self.approval_reason_combo.winfo_exists():
            self.approval_reason_combo.focus_set()

    def read_approval_card_serial(self):
        """C6-style COM4 reconnect; all scans go through WAIT CARD admission."""
        old_error = None
        while not self.approval_card_stop_event.is_set():
            try:
                ser = self.approval_card_serial
                if ser is None or not getattr(ser, 'is_open', False):
                    ser = serial.Serial(
                        port=self.APPROVAL_CARD_COM_PORT,
                        baudrate=self.APPROVAL_CARD_BAUD_RATE,
                        bytesize=8, parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_ONE, timeout=0.25)
                    self.approval_card_serial = ser
                self.approval_card_error = ''
                if old_error is not None:
                    self.ui_queue.put({'type':'is480_rfid_notice',
                                       'message':'COM4 đã kết nối lại, sẵn sàng nhận RFID'})
                old_error = None
                self.read_approval_card_serial_loop(
                    ser,
                    lambda uid: self.ui_queue.put({'type':'is480_rfid_card', 'card_id':uid}),
                    stop_event=self.approval_card_stop_event)
            except Exception as exc:
                self.approval_card_error = (
                    f'COM4 RFID loi/mat ket noi: {exc}')
                if str(exc) != old_error:
                    self.ui_queue.put({'type':'is480_rfid_notice',
                                       'message':self.approval_card_error})
                    old_error = str(exc)
                try:
                    if self.approval_card_serial is not None:
                        self.approval_card_serial.close()
                except Exception:
                    pass
                self.approval_card_serial = None
                self.approval_card_stop_event.wait(1.0)

    def open_approval_window(self, event=None):
        # Manual F1 cannot bypass automatic NG RFID handshake (and never PM).
        if (self.PM or self._is_ng_confirmation_waiting()
                or getattr(self, 'is480_rfid_gate_active', None) is not None
                and self.is480_rfid_gate_active.is_set()):
            return 'break'
        if self.approval_window is not None and self.approval_window.winfo_exists():
            self.approval_window.lift()
            self.approval_window.focus_force()
            self.focus_approval_check_entry()
            return "break"

        window = tk.Toplevel(self)
        window.title("Xác nhận máy NG")
        window.geometry("900x500")
        window.minsize(760, 420)
        window.protocol("WM_DELETE_WINDOW", self.close_approval_window)
        self.approval_window = window
        self.set_current_shift_approval_filter()

        window.columnconfigure(1, weight=1)
        window.rowconfigure(3, weight=1)

        check_var = tk.StringVar()
        selected_check_var = tk.StringVar()
        operator_var = tk.StringVar(value=self.latest_approval_operator)
        self.approval_operator_var = operator_var
        reason_var = tk.StringVar()
        detail_var = tk.StringVar(value="")
        display_values = []
        check_search_after_id = {"id": None}
        approve_button_holder = {"button": None}

        filter_frame = tk.Frame(window, padx=12)
        filter_frame.grid(row=0, column=0, columnspan=2, sticky=tk.EW, pady=(12, 4))
        filter_frame.columnconfigure(1, weight=1)
        filter_frame.columnconfigure(3, weight=1)

        recent_dates = get_recent_production_dates()
        tk.Label(filter_frame, text="Ngày làm việc:", font=("Arial", 12)).grid(row=0, column=0, sticky=tk.W, padx=(0, 6))
        approval_date_combo = ttk.Combobox(
            filter_frame,
            textvariable=self.approval_date_var,
            values=recent_dates,
            state="readonly",
            width=14,
        )
        approval_date_combo.grid(row=0, column=1, sticky=tk.W, padx=(0, 12))

        tk.Label(filter_frame, text="Ca làm việc:", font=("Arial", 12)).grid(row=0, column=2, sticky=tk.W, padx=(0, 6))
        approval_shift_combo = ttk.Combobox(
            filter_frame,
            textvariable=self.approval_shift_var,
            values=("Tat ca", "ca_ngay", "ca_dem"),
            state="readonly",
            width=14,
        )
        approval_shift_combo.grid(row=0, column=3, sticky=tk.W, padx=(0, 12))

        tk.Label(window, text="Quét Barcode Máy NG:", font=("Arial", 12)).grid(row=1, column=0, sticky=tk.W, padx=12, pady=6)
        check_entry = tk.Entry(window, textvariable=check_var, font=("Arial", 12))
        check_entry.grid(row=1, column=1, sticky=tk.EW, padx=(0, 12), pady=6)
        self.approval_check_entry = check_entry

        detail_frame = tk.LabelFrame(window, text="Thông tin máy NG", padx=10, pady=8)
        detail_frame.grid(row=2, column=0, columnspan=2, sticky=tk.NSEW, padx=12, pady=6)
        detail_frame.columnconfigure(0, weight=1)
        detail_label = tk.Label(detail_frame, textvariable=detail_var, justify=tk.LEFT, anchor=tk.NW, wraplength=820)
        detail_label.grid(row=0, column=0, sticky=tk.NSEW)

        input_frame = tk.Frame(window, padx=12, pady=6)
        input_frame.grid(row=3, column=0, columnspan=2, sticky=tk.NSEW)
        input_frame.columnconfigure(1, weight=1)

        tk.Label(input_frame, text="Người xác nhận:", font=("Arial", 12)).grid(row=0, column=0, sticky=tk.W, padx=(0, 8),
                                                                         pady=6)
        operator_entry = tk.Entry(
            input_frame,
            textvariable=operator_var,
            font=("Arial", 12),
            width=40,
        )
        operator_entry.grid(row=0, column=1, sticky=tk.EW, pady=6)
        self.approval_operator_entry = operator_entry

        if self.approval_card_error:
            tk.Label(
                input_frame,
                text=f"Loi doc the: {self.approval_card_error}",
                font=("Arial", 10),
                fg="red",
                wraplength=700,
                justify=tk.LEFT,
            ).grid(row=1, column=1, sticky=tk.W, pady=(0, 6))

        tk.Label(input_frame, text="Lý do:", font=("Arial", 12)).grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=6)
        reason_combo = ttk.Combobox(
            input_frame,
            textvariable=reason_var,
            values=APPROVAL_REASONS,
            state="readonly",
            width=80,
        )
        reason_combo.grid(row=2, column=1, sticky=tk.EW, pady=6)
        self.approval_reason_combo = reason_combo

        def set_approve_button_enabled(enabled):
            approve_button = approve_button_holder["button"]
            if approve_button is not None:
                approve_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

        def set_selected_approval(display):
            selected_check_var.set(display)
            row = self.approval_rows_by_display.get(display)
            if row is None:
                detail_var.set("Vui lòng chọn máy NG cần xác nhận")
                set_approve_button_enabled(False)
                return
            detail_var.set(self.format_approval_detail(row))
            set_approve_button_enabled(True)

        def clear_approval_selection(message):
            selected_check_var.set("")
            detail_var.set(message)
            set_approve_button_enabled(False)

        def apply_approval_search():
            check_search_after_id["id"] = None
            query = check_var.get().strip().lower()
            if not query:
                clear_approval_selection(
                    "Nhap barcode hoac thong tin may NG cần xác nhận."
                    if display_values else
                    "Không có máy NG phù hợp với điều kiện lọc"
                )
                return

            matches = [display for display in display_values if query in display.lower()]
            if not matches:
                clear_approval_selection("Không tìm thấy máy NG")
                return

            set_selected_approval(matches[0])

        def schedule_approval_search(*_args):
            after_id = check_search_after_id["id"]
            if after_id is not None:
                window.after_cancel(after_id)
            check_search_after_id["id"] = window.after(200, apply_approval_search)

        def refresh_approval_rows():
            rows = query_approvable_ng_checks(
                production_date=self.approval_date_var.get().strip(),
                shift=self.approval_shift_var.get().strip(),
            )
            self.approval_rows_by_display = {}
            display_values.clear()
            for row in rows:
                display = self.build_approval_display(row)
                self.approval_rows_by_display[display] = row
                display_values.append(display)
            check_var.set("")
            clear_approval_selection(
                "Nhap barcode hoac thong tin may NG cần xác nhận."
                if display_values else
                "Khong co may NG da hoan tat cần xác nhận trong ngay/ca dang loc."
            )

        def approve_selected(close_after_success=False):
            row = self.approval_rows_by_display.get(selected_check_var.get())
            approved_by = operator_var.get().strip()
            approved_reason = reason_var.get().strip()
            if row is None:
                messagebox.showerror("Chưa chọn máy NG", "Chua chon may NG cần xác nhận.", parent=window)
                return
            if not approved_by:
                messagebox.showerror("Thiếu thông tin người xác nhận", "Chưa chọn người xác nhận", parent=window)
                return
            self.latest_approval_operator = approved_by
            if not approved_reason:
                messagebox.showerror("Chưa có lý do xác nhận", "Vui lòng chọn lý do xác nhận máy NG", parent=window)
                return
            try:
                confirm_ng_check(
                    row["barcode"],
                    confirmed_by=approved_by,
                    check_no=row["check_no"],
                )
                refresh_approval_rows()
                if self.statistics_window is not None and self.statistics_window.winfo_exists():
                    self.search_statistics()
                self.update_current_shift_summary()
                if close_after_success:
                    self.close_approval_window()
                    return
                messagebox.showinfo("Thành công", "Đã xác nhận máy NG", parent=window)
            except Exception as exc:
                messagebox.showerror("Loi approve", str(exc), parent=window)

        check_var.trace_add("write", schedule_approval_search)
        approval_date_combo.bind("<<ComboboxSelected>>", lambda _event=None: refresh_approval_rows())
        approval_shift_combo.bind("<<ComboboxSelected>>", lambda _event=None: refresh_approval_rows())
        reason_combo.bind("<<ComboboxSelected>>", lambda _event=None: approve_selected(close_after_success=True))

        button_frame = tk.Frame(window, padx=12, pady=10)
        button_frame.grid(row=4, column=0, columnspan=2, sticky=tk.E)
        tk.Button(button_frame, text="Tải lại", width=12, command=refresh_approval_rows).grid(row=0, column=0,
                                                                                              padx=(0, 8))
        approve_button_holder["button"] = tk.Button(
            button_frame,
            text="Approve",
            width=14,
            command=approve_selected,
            state=tk.DISABLED,
        )
        approve_button_holder["button"].grid(row=0, column=1, padx=(0, 8))
        tk.Button(button_frame, text="Đóng", width=12, command=self.close_approval_window).grid(row=0, column=2)

        refresh_approval_rows()
        window.after(0, self.focus_approval_check_entry)
        return "break"

    def open_statistics_window(self, event=None):
        if self.statistics_window is not None and self.statistics_window.winfo_exists():
            self.statistics_window.lift()
            self.statistics_window.focus_force()
            return "break"

        window = tk.Toplevel(self)
        window.title("Thống kê IS1200 Cell1")
        window.geometry("1500x650")
        window.minsize(1100, 500)
        window.protocol("WM_DELETE_WINDOW", self.close_statistics_window)
        self.statistics_window = window
        self.set_current_shift_statistics_filter()

        window.rowconfigure(1, weight=1)
        window.columnconfigure(0, weight=1)

        search_frame = tk.Frame(window, padx=10, pady=10)
        search_frame.grid(row=0, column=0, sticky=tk.EW)
        for col in (1, 3, 5):
            search_frame.columnconfigure(col, weight=1)

        tk.Label(search_frame, text="Barcode:", font=("Arial", 12)).grid(row=0, column=0, sticky=tk.W, padx=(0, 6))
        barcode_entry = tk.Entry(search_frame, textvariable=self.statistics_barcode_var, font=("Arial", 12))
        barcode_entry.grid(row=0, column=1, sticky=tk.EW, padx=(0, 12))
        barcode_entry.bind("<Return>", self.search_statistics)

        recent_dates = get_recent_production_dates()
        tk.Label(search_frame, text="Tu ngay:", font=("Arial", 12)).grid(row=0, column=2, sticky=tk.W, padx=(0, 6))
        date_from_combo = ttk.Combobox(
            search_frame,
            textvariable=self.statistics_date_from_var,
            values=recent_dates,
            state="readonly",
            width=14,
        )
        date_from_combo.grid(row=0, column=3, sticky=tk.W, padx=(0, 12))
        date_from_combo.bind("<<ComboboxSelected>>", self.search_statistics)

        tk.Label(search_frame, text="Den ngay:", font=("Arial", 12)).grid(row=0, column=4, sticky=tk.W, padx=(0, 6))
        date_to_combo = ttk.Combobox(
            search_frame,
            textvariable=self.statistics_date_to_var,
            values=recent_dates,
            state="readonly",
            width=14,
        )
        date_to_combo.grid(row=0, column=5, sticky=tk.W, padx=(0, 12))
        date_to_combo.bind("<<ComboboxSelected>>", self.search_statistics)

        tk.Label(search_frame, text="Ca:", font=("Arial", 12)).grid(row=1, column=0, sticky=tk.W, pady=(8, 0),
                                                                    padx=(0, 6))
        shift_combo = ttk.Combobox(
            search_frame,
            textvariable=self.statistics_shift_var,
            values=("Tat ca", "ca_ngay", "ca_dem"),
            state="readonly",
            width=14,
        )
        shift_combo.grid(row=1, column=1, sticky=tk.W, pady=(8, 0), padx=(0, 12))
        shift_combo.bind("<<ComboboxSelected>>", self.search_statistics)

        tk.Label(search_frame, text="Ket qua:", font=("Arial", 12)).grid(row=1, column=2, sticky=tk.W, pady=(8, 0),
                                                                         padx=(0, 6))
        status_combo = ttk.Combobox(
            search_frame,
            textvariable=self.statistics_filter_status_var,
            values=("Tat ca", "PENDING", "OK", "NG", "ERROR"),
            state="readonly",
            width=14,
        )
        status_combo.grid(row=1, column=3, sticky=tk.W, pady=(8, 0), padx=(0, 12))
        status_combo.bind("<<ComboboxSelected>>", self.search_statistics)

        tk.Button(search_frame, text="Loc", width=10, command=self.search_statistics).grid(row=1, column=4, pady=(8, 0),
                                                                                           padx=(0, 8))
        tk.Button(search_frame, text="Xoa loc", width=10, command=self.clear_statistics_search).grid(row=1, column=5,
                                                                                                     sticky=tk.W,
                                                                                                     pady=(8, 0))

        table_frame = tk.Frame(window, padx=10)
        table_frame.grid(row=1, column=0, sticky=tk.NSEW)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        columns = (
            "stt",
            "barcode",
            "check_no",
            "shift",
            "final_status",
            "approved_by",
            "approved_at",
            "approved_reason",
            "created_at",
            "updated_at",
        )
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=18)
        y_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        headings = {
            "stt": "STT",
            "barcode": "Barcode",
            "check_no": "Lan",
            "shift": "Ca",
            "final_status": "Tong",
            "approved_by": "Operator",
            "approved_at": "Approve time",
            "approved_reason": "Approve reason",
            "created_at": "Created",
            "updated_at": "Updated",
        }
        widths = {
            "stt": 50,
            "barcode": 160,
            "check_no": 55,
            "shift": 85,
            "final_status": 75,
            "approved_by": 150,
            "approved_at": 145,
            "approved_reason": 260,
            "created_at": 145,
            "updated_at": 145,
        }
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(
                column,
                width=widths[column],
                minwidth=50,
                anchor=tk.CENTER if column == "stt" else tk.W,
                stretch=column in {"barcode", "approved_reason"},
            )

        tree.grid(row=0, column=0, sticky=tk.NSEW)
        y_scroll.grid(row=0, column=1, sticky=tk.NS)
        x_scroll.grid(row=1, column=0, sticky=tk.EW)
        self.statistics_tree = tree

        summary_frame = tk.Frame(window, padx=10, pady=6)
        summary_frame.grid(row=2, column=0, sticky=tk.EW)
        summary_frame.columnconfigure(3, weight=1)
        tk.Label(summary_frame, textvariable=self.statistics_current_shift_var, font=("Arial", 12, "bold")).grid(row=0,
                                                                                                                 column=0,
                                                                                                                 sticky=tk.W,
                                                                                                                 padx=(
                                                                                                                 0, 20))
        tk.Label(summary_frame, textvariable=self.statistics_current_ok_var, font=("Arial", 12, "bold"),
                 fg="green").grid(row=0, column=1, sticky=tk.W, padx=(0, 20))
        tk.Label(summary_frame, textvariable=self.statistics_current_ng_var, font=("Arial", 12, "bold"), fg="red").grid(
            row=0, column=2, sticky=tk.W)

        status_label = tk.Label(window, textvariable=self.statistics_status_var, anchor=tk.W, padx=10, pady=6)
        status_label.grid(row=3, column=0, sticky=tk.EW)

        self.search_statistics()
        return "break"

    def close_statistics_window(self):
        if self.statistics_window is not None and self.statistics_window.winfo_exists():
            self.statistics_window.destroy()
        self.statistics_window = None
        self.statistics_tree = None

    def clear_statistics_search(self):
        self.statistics_barcode_var.set("")
        self.set_current_shift_statistics_filter()
        self.search_statistics()

    def search_statistics(self, event=None):
        barcode = self.statistics_barcode_var.get().strip()
        date_from = self.statistics_date_from_var.get().strip()
        date_to = self.statistics_date_to_var.get().strip()
        shift = self.statistics_shift_var.get().strip()
        final_status = self.statistics_filter_status_var.get().strip()
        try:
            rows = query_statistics_checks(
                barcode=barcode,
                date_from=date_from,
                date_to=date_to,
                shift=shift,
                final_status=final_status,
            )
            self.update_current_shift_summary()
        except Exception as exc:
            messagebox.showerror("Loi tim kiem thong ke", str(exc), parent=self.statistics_window)
            return "break"

        if self.statistics_tree is None:
            raise RuntimeError("Khong tim thay bang hien thi thong ke de cap nhat ket qua")

        for item in self.statistics_tree.get_children():
            self.statistics_tree.delete(item)

        for index, row in enumerate(rows, start=1):
            self.statistics_tree.insert(
                "",
                tk.END,
                values=(
                    index,
                    row["barcode"],
                    row["check_no"],
                    row["shift"] or "",
                    row["final_status"],
                    row["approved_by"] or "",
                    row["approved_at"] or "",
                    row["approved_reason"] or "",
                    row["created_at"] or "",
                    row["updated_at"] or "",
                ),
            )

        self.statistics_status_var.set(
            f"So dong: {len(rows)} | barcode={barcode or 'Tat ca'} | "
            f"ngay={date_from or '...'}..{date_to or '...'} | ca={shift} | status={final_status}"
        )
        return "break"

    def Init_load_orb(self):
        try:
            paths = {
                "cam0vt1": r"E:\inside_1200\orb\cam0vt1.jpg",
                "cam0vt2": r"E:\inside_1200\orb\cam0vt2.jpg",
                "cam1vt1": r"E:\inside_1200\orb\cam1vt1.png",
                "cam1vt2": r"E:\inside_1200\orb\cam1vt2.jpg",
                "cam2vt1": r"E:\inside_1200\orb\cam2vt1.jpg",
                "cam2vt2": r"E:\inside_1200\orb\cam2vt2.jpg",
                "cam3vt1": r"E:\inside_1200\orb\cam3vt1.jpg",
                "cam3vt2": r"E:\inside_1200\orb\cam3vt2.jpg",
                "cam4vt1": r"E:\inside_1200\orb\cam4vt1.jpg",
                "cam5vt1": r"E:\inside_1200\orb\cam5vt1.jpg",
                "cam5vt2": r"E:\inside_1200\orb\cam5vt2.jpg",
                "cam6vt1": r"E:\inside_1200\orb\cam6vt1.jpg",
                "cam7vt1": r"E:\inside_1200\orb\cam7vt1.jpg",

                "cam0vt2pm": r"E:\inside_1200\orb_pm\cam0vt2.jpg",
                "cam1vt1pm": r"E:\inside_1200\orb_pm\cam1vt1.jpg",
                "cam2vt2pm": r"E:\inside_1200\orb_pm\cam2vt2.jpg",
                "cam3vt1pm": r"E:\inside_1200\orb_pm\cam3vt1.jpg",
                "cam4vt1pm": r"E:\inside_1200\orb_pm\cam4vt1.jpg",
                "cam5vt2pm": r"E:\inside_1200\orb_pm\cam5vt2.jpg",
                "cam6vt1pm": r"E:\inside_1200\orb_pm\cam6vt1.jpg",
                "cam7vt1pm": r"E:\inside_1200\orb_pm\cam7vt1.jpg",
            }
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {name: executor.submit(self.load_orrigin_image_orb, path) for name, path in paths.items()}
                for name, future in futures.items():
                    img, kp, des = future.result()
                    setattr(self, f"anh_goc_{name}", img)
                    setattr(self, f"kp_{name}", kp)
                    setattr(self, f"des_{name}", des)
        except Exception:
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
            ("model_CAM0_VT1_0", self.model_CAM0_VT1_0),
            ("model_CAM0_VT1_1", self.model_CAM0_VT1_1),
            ("model_CAM0_VT1_2", self.model_CAM0_VT1_2),
            ("model_CAM0_VT2_0", self.model_CAM0_VT2_0),
            ("model_CAM0_VT2_1", self.model_CAM0_VT2_1),
            ("model_CAM0_VT2_2", self.model_CAM0_VT2_2),
            ("model_CAM1_VT1_0", self.model_CAM1_VT1_0),
            ("model_CAM1_VT2_0", self.model_CAM1_VT2_0),
            ("model_CAM1_VT2_1", self.model_CAM1_VT2_1),
            ("model_CAM2_VT1_0", self.model_CAM2_VT1_0),
            ("model_CAM2_VT1_1", self.model_CAM2_VT1_1),
            ("model_CAM2_VT2_0", self.model_CAM2_VT2_0),
            ("model_CAM2_VT2_1", self.model_CAM2_VT2_1),
            ("model_CAM3_VT1_0", self.model_CAM3_VT1_0),
            ("model_CAM4_VT1_0", self.model_CAM4_VT1_0),
            ("model_CAM4_VT1_1", self.model_CAM4_VT1_1),
            ("model_CAM5_VT1_0", self.model_CAM5_VT1_0),
            ("model_CAM5_VT2_0", self.model_CAM5_VT2_0),
            ("model_CAM5_VT2_1", self.model_CAM5_VT2_1),
            ("model_CAM6_VT1_0", self.model_CAM6_VT1_0),
            ("model_CAM7_VT1_0", self.model_CAM7_VT1_0),

            ("model_VERIFY_CLS", self.model_VERIFY_CLS),
            ("model_CAM0_VT2_0_PM", self.model_CAM0_VT2_0_PM),
            ("model_CAM1_VT1_0_PM", self.model_CAM1_VT1_0_PM),
            ("model_CAM2_VT2_1_PM", self.model_CAM2_VT2_1_PM),
            ("model_CAM3_VT1_0_PM", self.model_CAM3_VT1_0_PM),
            ("model_CAM3_VT1_1_PM", self.model_CAM3_VT1_1_PM),
            ("model_CAM4_VT1_0_PM", self.model_CAM4_VT1_0_PM),
            ("model_CAM5_VT2_0_PM", self.model_CAM5_VT2_0_PM),
            ("model_CAM5_VT2_1_PM", self.model_CAM5_VT2_1_PM),
            ("model_CAM6_VT1_0_PM", self.model_CAM6_VT1_0_PM),
            ("model_CAM7_VT1_0_PM", self.model_CAM7_VT1_0_PM),
            ("model_CAM7_VT1_1_PM", self.model_CAM7_VT1_1_PM),
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
                'toado_CAM0_VT1': r"E:\inside_1200\point_config\cam0vt1.txt",
                'toado_CAM0_VT2': r"E:\inside_1200\point_config\cam0vt2.txt",
                'toado_CAM1_VT1': r"E:\inside_1200\point_config\cam1vt1.txt",
                'toado_CAM1_VT2': r"E:\inside_1200\point_config\cam1vt2.txt",
                'toado_CAM2_VT1': r"E:\inside_1200\point_config\cam2vt1.txt",
                'toado_CAM2_VT2': r"E:\inside_1200\point_config\cam2vt2.txt",
                'toado_CAM3_VT1': r"E:\inside_1200\point_config\cam3vt1.txt",
                'toado_CAM3_VT2': r"E:\inside_1200\point_config\cam3vt2.txt",
                'toado_CAM4_VT1': r"E:\inside_1200\point_config\cam4vt1.txt",
                'toado_CAM5_VT1': r"E:\inside_1200\point_config\cam5vt1.txt",
                'toado_CAM5_VT2': r"E:\inside_1200\point_config\cam5vt2.txt",
                'toado_CAM6_VT1': r"E:\inside_1200\point_config\cam6vt1.txt",
                'toado_CAM7_VT1': r"E:\inside_1200\point_config\cam7vt1.txt",

                'toado_CAM0_VT2_PM': r"E:\inside_1200\point_config_PM\cam0vt2.txt",
                'toado_CAM1_VT1_PM': r"E:\inside_1200\point_config_PM\cam1vt1.txt",
                'toado_CAM2_VT2_PM': r"E:\inside_1200\point_config_PM\cam2vt2.txt",
                'toado_CAM3_VT1_PM': r"E:\inside_1200\point_config_PM\cam3vt1.txt",
                'toado_CAM4_VT1_PM': r"E:\inside_1200\point_config_PM\cam4vt1.txt",
                'toado_CAM5_VT2_PM': r"E:\inside_1200\point_config_PM\cam5vt2.txt",
                'toado_CAM6_VT1_PM': r"E:\inside_1200\point_config_PM\cam6vt1.txt",
                'toado_CAM7_VT1_PM': r"E:\inside_1200\point_config_PM\cam7vt1.txt",
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
            'model_CAM0_VT1_0': r'E:\inside_1200\model\openvino\cam0vt1_0_openvino_model',
            'model_CAM0_VT1_1': r'E:\inside_1200\model\openvino\cam0vt1_1_openvino_model',
            'model_CAM0_VT1_2': r'E:\inside_1200\model\openvino\cam0vt1_2_openvino_model',
            'model_CAM0_VT2_0': r'E:\inside_1200\model\openvino\cam0vt2_0_openvino_model',
            'model_CAM0_VT2_1': r'E:\inside_1200\model\openvino\cam0vt2_1_openvino_model',
            'model_CAM0_VT2_2': r'E:\inside_1200\model\openvino\cam0vt2_2_1806_openvino_model',
            'model_CAM1_VT1_0': r'E:\inside_1200\model\openvino\cam1vt1_0_openvino_model',
            'model_CAM1_VT2_0': r'E:\inside_1200\model\openvino\cam1vt2_0_openvino_model',
            'model_CAM1_VT2_1': r'E:\inside_1200\model\openvino\cam1vt2_1_openvino_model',
            'model_CAM2_VT1_0': r'E:\inside_1200\model\openvino\cam2vt1_0_openvino_model',
            'model_CAM2_VT1_1': r'E:\inside_1200\model\openvino\cam2vt1_1_openvino_model',
            'model_CAM2_VT2_0': r'E:\inside_1200\model\openvino\cam2vt2_0_openvino_model',
            'model_CAM2_VT2_1': r'E:\inside_1200\model\openvino\cam2vt2_1_openvino_model',
            'model_CAM3_VT1_0': r'E:\inside_1200\model\openvino\cam3vt1_0_openvino_model',
            'model_CAM3_VT2_0': r'E:\inside_1200\model\openvino\cam3vt2_0_openvino_model',
            'model_CAM4_VT1_0': r'E:\inside_1200\model\openvino\cam4vt1_0_openvino_model',
            'model_CAM4_VT1_1': r'E:\inside_1200\model\openvino\cam4vt1_1_openvino_model',
            'model_CAM5_VT1_0': r'E:\inside_1200\model\openvino\cam5vt1_0_openvino_model',
            'model_CAM5_VT2_0': r'E:\inside_1200\model\openvino\cam5vt2_0_openvino_model',
            'model_CAM5_VT2_1': r'E:\inside_1200\model\openvino\cam5vt2_1_openvino_model',
            'model_CAM6_VT1_0': r'E:\inside_1200\model\openvino\cam6vt1_0_openvino_model',
            'model_CAM7_VT1_0': r'E:\inside_1200\model\openvino\cam7vt1_0_openvino_model',

            'model_VERIFY_CLS': r"model/check_chot_inside.pt",
            'model_CAM0_VT2_0_PM': r'model_pm/openvino/cam0vt2_0_openvino_model',
            'model_CAM1_VT1_0_PM': r'model_pm/openvino/cam1vt1_0_openvino_model',
            'model_CAM2_VT2_1_PM': r'model_pm/openvino/cam2vt2_1_openvino_model',
            'model_CAM3_VT1_0_PM': r'model_pm/openvino/cam3vt1_0_openvino_model',
            'model_CAM3_VT1_1_PM': r'model_pm/openvino/cam3vt1_1_openvino_model',
            'model_CAM4_VT1_0_PM': r'model_pm/openvino/cam4vt1_0_openvino_model',
            'model_CAM5_VT2_0_PM': r'model_pm/openvino/cam5vt2_0_openvino_model',
            'model_CAM5_VT2_1_PM': r'model_pm/openvino/cam5vt2_1_openvino_model',
            'model_CAM6_VT1_0_PM': r'model_pm/openvino/cam6vt1_0_openvino_model',
            'model_CAM7_VT1_0_PM': r'model_pm/openvino/cam7vt1_0_openvino_model',
            'model_CAM7_VT1_1_PM': r'model_pm/openvino/cam7vt1_1_openvino_model',
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
            self.lankt = 0
            self.dem = 0
            self.kq_ng = 0
            self.pointcheck = 0
            self.stop = True
            self.gui_kqdocbarcode = False
            with self.allow_put_lock:
                self.allow_put = True
            self.queue_put_count_vt1 = 0
            self.queue_put_count_vt2 = 0
            self.current_cam_idx = 0
            self.lable_ng.clear()
            self.ng_cams.clear()
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
            print("\n===== PROCESS CAMERA 1 RUN=====")
            self.queue_put_count_vt1 = 0
            with self.futures_lock:
                self.process_futures.clear()

            if len(self.list_ng) > 0:
                self.list_ng.clear()

            modelCAM0_VT1 = (self.model_CAM0_VT1_0, self.model_CAM0_VT1_1, self.model_CAM0_VT1_2)
            modelCAM1_VT1 = (self.model_CAM1_VT1_0,)
            modelCAM2_VT1 = (self.model_CAM2_VT1_0, self.model_CAM2_VT1_1,)
            modelCAM3_VT1 = (self.model_CAM3_VT1_0,)
            modelCAM4_VT1 = (self.model_CAM4_VT1_0, self.model_CAM4_VT1_1,)
            modelCAM5_VT1 = (self.model_CAM5_VT1_0,)
            modelCAM6_VT1 = (self.model_CAM6_VT1_0,)
            modelCAM7_VT1 = (self.model_CAM7_VT1_0,)

            futures = []

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 0, self.cap0, 1, self.toado_CAM0_VT1, self.anh_goc_cam0vt1,
                self.ORB_ROI_LARGE_CAM0_VT1, *modelCAM0_VT1
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 1, self.cap1, 1, self.toado_CAM1_VT1, self.anh_goc_cam1vt1,
                self.ORB_ROI_LARGE_CAM1_VT1, *modelCAM1_VT1
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 2, self.cap2, 1, self.toado_CAM2_VT1, self.anh_goc_cam2vt1,
                self.ORB_ROI_LARGE_CAM2_VT1, *modelCAM2_VT1
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 3, self.cap3, 1, self.toado_CAM3_VT1, self.anh_goc_cam3vt1,
                self.ORB_ROI_LARGE_CAM3_VT1, *modelCAM3_VT1
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 4, self.cap4, 1, self.toado_CAM4_VT1, self.anh_goc_cam4vt1,
                self.ORB_ROI_LARGE_CAM4_VT1, *modelCAM4_VT1
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 5, self.cap5, 1, self.toado_CAM5_VT1, self.anh_goc_cam5vt1,
                self.ORB_ROI_LARGE_CAM5_VT1, *modelCAM5_VT1
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 6, self.cap6, 1, self.toado_CAM6_VT1, self.anh_goc_cam6vt1,
                self.ORB_ROI_LARGE_CAM6_VT1, *modelCAM6_VT1
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 7, self.cap7, 1, self.toado_CAM7_VT1, self.anh_goc_cam7vt1,
                self.ORB_ROI_LARGE_CAM7_VT1, *modelCAM7_VT1
            ))

            # đợi capture xong
            done = 0
            for f in as_completed(futures):
                f.result()
                done += 1
                print(f"✅ VT1 cam done {done}/8")

            # ---- CHỜ ĐỦ 3 ẢNH ĐƯỢC PUT ----
            t0 = time.time()
            while True:
                with self.queue_put_lock:
                    if self.queue_put_count_vt1 >= 8:
                        break

                if time.time() - t0 > 2:
                    print("⚠ timeout wait VT1 put")
                    break

                time.sleep(0.001)

            if not self._is480_stage_capture_valid(1, 8):
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

            modelCAM1_VT1_PM = (self.model_CAM1_VT1_0_PM,)
            modelCAM3_VT1_PM = (self.model_CAM3_VT1_0_PM, self.model_CAM3_VT1_1_PM,)
            modelCAM4_VT1_PM = (self.model_CAM4_VT1_0_PM,)
            modelCAM6_VT1_PM = (self.model_CAM6_VT1_0_PM,)
            modelCAM7_VT1_PM = (self.model_CAM7_VT1_0_PM, self.model_CAM7_VT1_1_PM,)

            futures = []

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 1, self.cap1, 1, self.toado_CAM1_VT1_PM, self.anh_goc_cam1vt1pm,
                self.ORB_ROI_LARGE_CAM1_VT1_PM, *modelCAM1_VT1_PM
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 3, self.cap3, 1, self.toado_CAM3_VT1_PM, self.anh_goc_cam3vt1pm,
                self.ORB_ROI_LARGE_CAM3_VT1_PM, *modelCAM3_VT1_PM
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 4, self.cap4, 1, self.toado_CAM4_VT1_PM, self.anh_goc_cam4vt1pm,
                self.ORB_ROI_LARGE_CAM4_VT1_PM, *modelCAM4_VT1_PM
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 6, self.cap6, 1, self.toado_CAM6_VT1_PM, self.anh_goc_cam6vt1pm,
                self.ORB_ROI_LARGE_CAM6_VT1_PM, *modelCAM6_VT1_PM
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 7, self.cap7, 1, self.toado_CAM7_VT1_PM, self.anh_goc_cam7vt1pm,
                self.ORB_ROI_LARGE_CAM7_VT1_PM, *modelCAM7_VT1_PM
            ))

            # đợi capture xong
            done = 0
            for f in as_completed(futures):
                f.result()
                done += 1
                print(f"✅ VT1 cam done {done}/8")

            # ---- CHỜ ĐỦ 3 ẢNH ĐƯỢC PUT ----
            t0 = time.time()
            while True:
                with self.queue_put_lock:
                    if self.queue_put_count_vt1 >= 5:
                        break

                if time.time() - t0 > 2:
                    print("⚠ timeout wait VT1 put")
                    break

                time.sleep(0.001)

            if not self._is480_stage_capture_valid(1, 5):
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
            print("\n===== PROCESS CAMERA 2 RUN=====")

            self.queue_put_count_vt2 = 0

            modelCAM0_VT2 = (self.model_CAM0_VT2_0, self.model_CAM0_VT2_1, self.model_CAM0_VT2_2)
            modelCAM1_VT2 = (self.model_CAM1_VT2_0, self.model_CAM1_VT2_1,)
            modelCAM2_VT2 = (self.model_CAM2_VT2_0, self.model_CAM2_VT2_1,)
            modelCAM3_VT2 = (self.model_CAM3_VT2_0,)
            modelCAM5_VT2 = (self.model_CAM5_VT2_0, self.model_CAM5_VT2_1)

            futures = []

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 0, self.cap0, 2,
                self.toado_CAM0_VT2, self.anh_goc_cam0vt2, self.ORB_ROI_LARGE_CAM0_VT2, *modelCAM0_VT2
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 1, self.cap1, 2,
                self.toado_CAM1_VT2, self.anh_goc_cam1vt2, self.ORB_ROI_LARGE_CAM1_VT2, *modelCAM1_VT2
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 2, self.cap2, 2,
                self.toado_CAM2_VT2, self.anh_goc_cam2vt2, self.ORB_ROI_LARGE_CAM2_VT2, *modelCAM2_VT2
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 3, self.cap3, 2,
                self.toado_CAM3_VT2, self.anh_goc_cam3vt2, self.ORB_ROI_LARGE_CAM3_VT2, *modelCAM3_VT2
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 5, self.cap5, 2,
                self.toado_CAM5_VT2, self.anh_goc_cam5vt2, self.ORB_ROI_LARGE_CAM5_VT2, *modelCAM5_VT2
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
            print("\n===== PROCESS CAMERA 2 PM=====")

            self.queue_put_count_vt2 = 0

            modelCAM0_VT2_PM = (self.model_CAM0_VT2_0_PM,)
            modelCAM2_VT2_PM = (self.model_CAM2_VT2_1_PM,)
            modelCAM5_VT2_PM = (self.model_CAM5_VT2_0_PM, self.model_CAM5_VT2_1_PM,)

            futures = []

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 0, self.cap0, 2,
                self.toado_CAM0_VT2_PM, self.anh_goc_cam0vt2pm, self.ORB_ROI_LARGE_CAM0_VT2_PM, *modelCAM0_VT2_PM
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 2, self.cap2, 2,
                self.toado_CAM2_VT2_PM, self.anh_goc_cam2vt2pm, self.ORB_ROI_LARGE_CAM2_VT2_PM, *modelCAM2_VT2_PM
            ))

            futures.append(self.executor.submit(
                self._is480_capture_checked, cid, 5, self.cap5, 2,
                self.toado_CAM5_VT2_PM, self.anh_goc_cam5vt2pm, self.ORB_ROI_LARGE_CAM5_VT2_PM, *modelCAM5_VT2_PM
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
                    if self.queue_put_count_vt2 >= 3:
                        break

                if time.time() - t0 > 3:
                    print("⚠ timeout wait VT2 put")
                    break

                time.sleep(0.001)

            if not self._is480_stage_capture_valid(2, 3):
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
            print('che do pm')
            if vitri == 1:
                print('vttttttttttttttttttttttttttt1')
                try:
                    if cam == 1:
                        print('cam1')
                        current_model = models[0]
                        model_type = "yolo"

                    if cam == 3:
                        if point["POINT"] in [0]:
                            current_model = models[0]
                            model_type = "yolo"
                        if point["POINT"] in [1]:
                            current_model = models[1]
                            model_type = "yolo"

                    if cam == 4:
                        current_model = models[0]
                        model_type = "yolo"

                    if cam == 6:
                        current_model = models[0]
                        model_type = "yolo"

                    if cam == 7:
                        if point["POINT"] in [0]:
                            current_model = models[0]
                            model_type = "yolo"
                        if point["POINT"] in [1]:
                            current_model = models[1]
                            model_type = "yolo"
                except:
                    print(traceback.format_exc())

            if vitri == 2:
                try:
                    if cam == 0:
                        current_model = models[0]
                        model_type = "yolo"

                    if cam == 2:
                        current_model = models[0]
                        model_type = "yolo"

                    if cam == 5:
                        if point["POINT"] in [1]:
                            current_model = models[0]
                            model_type = "yolo"
                        if point["POINT"] in [2]:
                            current_model = models[1]
                            model_type = "yolo"
                except:
                    print(traceback.format_exc())
        else:
            print('che do run')
            if vitri == 1:
                print('vttttttttttttttttttttttttttt1')
                try:
                    if cam == 0:
                        print('cam0')
                        if point["POINT"] in [0, 1, 2, 4, 7]:
                            current_model = models[0]
                            model_type = "yolo"

                        if point["POINT"] in [3, 5, 6, 8]:
                            current_model = models[1]
                            model_type = "yolo"

                        if point["POINT"] in [10, 11, 12]:
                            current_model = models[2]
                            model_type = "yolo"

                    if cam == 1:
                        print('cam1')
                        if point["POINT"] in [0, 1]:
                            current_model = models[0]
                            model_type = "yolo"

                    if cam == 2:
                        if point["POINT"] in [1, 5, 6, 7]:
                            current_model = models[0]
                            model_type = "yolo"

                        if point["POINT"] in [2, 3, 4, 8, 9]:
                            current_model = models[1]
                            model_type = "yolo"

                    if cam == 3:
                        if point["POINT"] in [0, 1]:
                            current_model = models[0]
                            model_type = "yolo"

                    if cam == 4:
                        if point["POINT"] in [0, 1, 2, 3, 4, 5]:
                            current_model = models[0]
                            model_type = "yolo"

                    if cam == 5:
                        if point["POINT"] in [0]:
                            current_model = models[0]
                            model_type = "yolo"

                    if cam == 6:
                        if point["POINT"] in [0, 1, 2, 3, 4]:
                            current_model = models[0]
                            model_type = "yolo"

                    if cam == 7:
                        if point["POINT"] in [0, 1, 2]:
                            current_model = models[0]
                            model_type = "yolo"

                except:
                    print(traceback.format_exc())

            if vitri == 2:
                try:
                    if cam == 0:
                        if point["POINT"] in [0, 6, 7, 10, 12]:
                            current_model = models[0]
                            model_type = "yolo"

                        if point["POINT"] in [2, 5, 8, 11]:
                            current_model = models[1]
                            model_type = "yolo"

                        if point["POINT"] in [1, 3, 4, 9]:
                            current_model = models[2]
                            model_type = "yolo"

                    if cam == 1:
                        if point["POINT"] in [1, 2]:
                            current_model = models[0]
                            model_type = "yolo"

                        if point["POINT"] in [0, 3]:
                            current_model = models[1]
                            model_type = "yolo"

                    if cam == 2:
                        if point["POINT"] in [1, 2, 3, 6]:
                            current_model = models[0]
                            model_type = "yolo"

                        if point["POINT"] in [4, 5, 7, 8]:
                            current_model = models[1]
                            model_type = "yolo"

                    if cam == 3:
                        if point["POINT"] in [0, 1]:
                            current_model = models[0]
                            model_type = "yolo"

                    if cam == 5:
                        if point["POINT"] in [0, 1, 2]:
                            current_model = models[0]
                            model_type = "yolo"

                        if point["POINT"] in [3, 4]:
                            current_model = models[1]
                            model_type = "yolo"
                except:
                    print(traceback.format_exc())

        return current_model, model_type

    def process_single_point(self, frame, cam, vitri, point, zones, models):
        try:
            with self.camera_counter_lock:
                self.pointcheck += 1
            self.ui_queue.put({"type": "progress", "current": self.pointcheck, "total": self.total_points})

            cx = point["X"] + point["SIZE"] / 2
            cy = point["Y"] + point["SIZE"] / 2
            dx, dy = 0.0, 0.0
            for z in zones:
                zx, zy, zw, zh = z["xywh"]
                if zx <= cx <= zx + zw and zy <= cy <= zy + zh:
                    dx, dy = z["dxdy"]
                    break

            new_x = int(point["X"] + dx)
            new_y = int(point["Y"] + dy)
            h, w = frame.shape[:2]
            new_x = max(0, min(new_x, w - point["SIZE"]))
            new_y = max(0, min(new_y, h - point["SIZE"]))
            newbox = (new_x, new_y, point["SIZE"])

            crop_image = frame[new_y:new_y + point["SIZE"], new_x:new_x + point["SIZE"]].copy()
            crop_image_224 = cv2.resize(crop_image, (224, 224))
            if crop_image.size == 0:
                return None

            self.executor_save.submit(self.save_crop_all, cam, crop_image_224, vitri, point['POINT'])
            current_model, model_type = self.select_model(cam, vitri, point, models)
            if current_model is None:
                print("KHONG CO MODEL CHO CAM-POINT-vitri", cam, point["POINT"], vitri)
                return None

            if model_type == "yolo":
                results = current_model.predict(crop_image, imgsz=224)
                probs = results[0].probs.data.cpu().numpy()
                _class = int(np.argmax(probs))
                _prob = round(float(probs[_class]), 4)
                _name = results[0].names[_class]
                print(f'cam{cam}_D{point["POINT"]}_acc={_prob}')
                return (point, crop_image_224, _name, _prob, vitri, newbox, model_type)

            elif model_type == "patchcore":
                if current_model.memory_bank is None:
                    print("PatchCore chưa load!")
                    return None
                img_cvt = cv2.cvtColor(crop_image_224, cv2.COLOR_BGR2RGB)
                img_pil = Image.fromarray(img_cvt)
                img_tensor = self.transform(img_pil).unsqueeze(0).to(self.DEVICE)
                score, anomaly_map = current_model.predict(img_tensor)
                print(f'[PATCHCORE] cam{cam}_P{point["POINT"]} score={score}')
                _name = "NG" if score > point['THRESHOLD'] else "OK"
                _prob = score if _name == "NG" else 1 - score
                return (point, crop_image_224, _name, _prob, vitri, newbox, model_type)

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
                elif data["type"] == "button_ng":
                    # ✅ FIX: đổi màu button từ main thread (thread-safe với Tkinter)
                    widget = getattr(self.widget_buttom, data["attr"], None)
                    if widget:
                        self.widget_buttom.set_cam_button_color(widget, 'violet', 'red')
                elif data["type"] == "final_result":
                    # PC1 local result is NOT the 3-PC production verdict.
                    # Show final OK/NG only after the PLC's 0x15 ACK arrives.
                    self.widget_result_check.label2.configure(text='CHỜ TỔNG',bg='#F2B95E')
                elif data["type"] == "ng_grid":
                    if self.is480_selected_pc != "PC1":
                        continue
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
                    # self.widget_station_infor.label_5.config(text=data.get("barcode", ""))
                    self.widget_result_check.listNGbox.delete(0, tk.END)

                elif data["type"] == "hienthibarcode":
                    barcode = data["barcode"]
                    self.widget_station_infor.label_5.config(text=barcode)
                elif data["type"] == "barcodeng":
                    self.widget_result_check.label_image_1.configure(text="DOC BARCODE NG", image='')

                elif data["type"] == "statistics_refresh_summary":
                    if self.statistics_window is not None and self.statistics_window.winfo_exists():
                        self.update_current_shift_summary()

                elif data["type"] == "close_approval_window":
                    self.close_approval_window()

                elif data["type"] == "approval_card_error":
                    if not self.PM:
                        messagebox.showerror("Loi doc the xac nhan", data["message"])

                elif data["type"] == "pm_ng":
                    # Chua du 3 ket qua AI + PLC 0x15 thi KHONG duoc bao PM xong.
                    self.widget_result_check.label_image_1.configure(
                        text="PM CAMERA NG\nCHỜ CHỐT KẾT QUẢ PM", image='')

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
        if self.is480_selected_pc != "PC1":
            return
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
                    if (cam == 1 and point['POINT'] == 0) or \
                            (cam == 3 and point['POINT'] == 0) or \
                            (cam == 4 and point['POINT'] == 0) or \
                            (cam == 6 and point['POINT'] == 2) or \
                            (cam == 7 and point['POINT'] == 0):
                        color = (255, 0, 255)  # Cyan - PM đặc biệt
                    else:
                        color = (0, 255, 0)
                elif vitri == 2:
                    if (cam == 0 and point['POINT'] == 12) or \
                            (cam == 2 and point['POINT'] == 5) or \
                            (cam == 5 and point['POINT'] == 2):
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
        try:
            self.thuchien()
        except Exception:
            print(traceback.format_exc())

    # def kiemtra_VT1(self):
    #     try:
    #         if self.step_cam == 0:
    #             if self.PM :
    #                 self.process_camera_1_pm()
    #             else:
    #                 self.process_camera_1()
    #         else:
    #             print(' chua chup xong VT1')
    #     except Exception:
    #         print(traceback.format_exc())

    def kiemtra_VT2(self):
        try:
            if self.step_cam == 1:
                if self.PM:
                    self.process_camera_2_pm()
                else:
                    self.process_camera_2()
            else:
                print(' chua chup xong VT1 o vt 2')
        except Exception:
            print(traceback.format_exc())

    def guibarcodeok(self):
        while self.gui_kqdocbarcode and self.camera_running:
            try:
                with self.serial_lock:
                    if not self.gui_kqdocbarcode:
                        return
                    self.ser.write(b'\x31')
                time.sleep(0.3)
            except Exception as exc:
                self._is480_fault(f'Lỗi gửi barcode ACK 0x31: {exc}')
                return

    def guibarcodeng(self):
        while self.gui_kqdocbarcode:
            self.ser.write(b'\x32')
            time.sleep(0.3)

    def guibangduocVT1(self):
        while self.guithVT1:
            self.ser.write(b'\x33')
            print('send 0x33')
            time.sleep(0.3)

    def guibangduocVT2(self):
        # Legacy compatibility only. PLC VT2 byte 0x34 retired in v1.7.
        # Completion goes through Bridge._release_vt2_without_plc().
        return

    def guikqok(self):
        while self.gui_kq:
            self.ser.write(b'\x35')
            print('send 0x35')
            time.sleep(0.3)

    def guikqng(self):
        while self.gui_kq:
            self.ser.write(b'\x36')
            print('send 0x36')
            time.sleep(0.3)

    def read_serial(self):
        from is480_app import read_master_serial
        return read_master_serial(self)

    def doimau(self):
        self.widget_buttom._clear_cam_button_highlight()
        if self.PM:
            cam_vt_list = [(0, 1), (1, 1), (2, 1), (3, 1), (3, 2), (4, 1), (5, 1), (6, 1), (7, 1),
                           (0, 2), (1, 2), (2, 2), (5, 2)]
        else:
            cam_vt_list = [(0, 1), (1, 1), (2, 1), (3, 1), (3, 2), (4, 1), (5, 1), (6, 1), (7, 1),
                           (0, 2), (1, 2), (2, 2), (5, 2)]

        for cam, vt in cam_vt_list:
            attr_name = f"CAM{cam}_VT{vt}"
            widget = getattr(self.widget_buttom, attr_name, None)
            if widget:
                self.widget_buttom.set_cam_button_color(widget, 'green', 'yellow')

    # def check_barcode(self):
    #     print('🔍 Vào đọc barcode')
    #
    #     try:
    #         # Gửi lệnh bật scan
    #         self.initProcess.send(self.initProcess.command['scan_enable'])
    #
    #         # Gửi lệnh continuous mode (nếu cần)
    #         self.initProcess.send(self.initProcess.command['continues_mode'])
    #
    #         # Đọc barcode
    #         # readSuccess = self.initProcess.readBarcode()
    #         readSuccess = datetime.now().strftime("%H-%M-%S")
    #         # === XỬ LÝ DỮ LIỆU SAU KHI ĐỌC ===
    #         if readSuccess:
    #             # Chuyển sang string và làm sạch
    #             if isinstance(readSuccess, bytes):
    #                 barcode_str = readSuccess.decode('utf-8', errors='replace').strip()
    #             else:
    #                 barcode_str = str(readSuccess).strip()
    #
    #             # Lọc bỏ ký tự trắng, ký tự không in được, ô vuông...
    #             import re
    #             barcode_clean = re.sub(r'[\x00-\x1F\x7F-\x9F□\s]+', '', barcode_str)
    #
    #             print(f'Raw data    : {repr(readSuccess)}')  # Debug quan trọng
    #             print(f'After clean : {barcode_clean}')
    #
    #             # Kiểm tra barcode có hợp lệ không
    #             if barcode_clean and len(barcode_clean) >= 4:  # tùy theo loại barcode của bạn
    #                 self.databacode = barcode_clean
    #                 self.ui_queue.put({
    #                     "type": "hienthibarcode",
    #                     "barcode": self.databacode
    #                 })
    #                 self.gui_kqdocbarcode = True
    #                 print('✅ Gửi barcode thành công')
    #                 self.executor.submit(self.guibarcodeok)
    #             else:
    #                 print('⚠️ Barcode không hợp lệ sau khi lọc')
    #                 self._handle_read_fail()
    #         else:
    #             print('❌ Không đọc được barcode')
    #             self._handle_read_fail()
    #
    #     except Exception as e:
    #         print(f'❌ Lỗi khi đọc barcode: {e}')
    #         self._handle_read_fail()
    #
    #     return
    def _is480_read_body_barcode(self):
        """Only PLC 0x10 / explicit 0x41 starts COM3 scanning."""
        self.databacode = None
        self.current_barcode = None
        self.gui_kqdocbarcode = False
        scanner = self.initProcess
        try:
            if not scanner.getSerial():
                self._handle_read_fail()
                return
            # The ReadBarcode.py parser expects clean bytes BEFORE scan_enable.
            if not scanner.clearBuffer():
                self._handle_read_fail()
                return
            if not scanner.send('continues_mode'):
                self._handle_read_fail()
                return
            if not scanner.send('scan_enable'):
                self._handle_read_fail()
                return
            barcode = scanner.readBarcode(timeout=15)
            if not barcode:
                self._handle_read_fail()
                return
            barcode = str(barcode).strip()
            if not barcode:
                self._handle_read_fail()
                return
            self.databacode = barcode
            self.current_barcode = barcode
            self.ui_queue.put({'type': 'hienthibarcode', 'barcode': barcode})
            self.gui_kqdocbarcode = True
            # Keep legacy 0x31 repeat until PLC enters VT1 (0x11).
            self.executor.submit(self.guibarcodeok)
        except Exception as exc:
            print(f'[IS480][BODY_COM3_ERROR] {exc}')
            self._handle_read_fail()
        finally:
            # Important for errors, timeouts and successful scans alike.
            try:
                if not scanner.send('scan_disable'):
                    self.ui_queue.put({'type':'is480_warning',
                        'message':'Không gửi được scan_disable COM3; kiểm tra đầu đọc'})
            except Exception as exc:
                self.ui_queue.put({'type':'is480_warning',
                    'message':f'COM3 scan_disable lỗi: {exc}'})

    def check_barcode(self):
        """Exclusive production COM3 reading; never started by repair scanner."""
        locked = self.is480_scan_lock.acquire(timeout=4.0)
        try:
            if not locked:
                self._handle_read_fail()
                return
            self._is480_read_body_barcode()
        finally:
            if locked:
                self.is480_scan_lock.release()
            if hasattr(self, 'is480_body_scan_active'):
                self.is480_body_scan_active.clear()

    def _handle_read_fail(self):
        """Barcode read failure must NEVER fabricate a BODY number or send 0x31."""
        self.databacode = None
        self.current_barcode = None
        self.gui_kqdocbarcode = False
        self.ui_queue.put({'type': 'is480_warning',
                           'message': 'Không đọc được barcode thật từ COM3; chờ xử lý'})

    def send_direct_all(self, message):
        for ip, port in self.client_list:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                sock.connect((ip, port))
                sock.send(message.encode('utf-8'))
                print(f"✅ Đã gửi '{message}' tới {ip}:{port}")
                sock.close()
            except Exception as e:
                print(f"❌ Lỗi gửi tới {ip}:{port} -> {e}")

    def switch_cam(self, event=None):
        self.widget_result_check.listNGbox.selection_clear(0, tk.END)
        # self.current_cam_idx = (self.current_cam_idx + 1) % 7

        if self.ng_cams is not None and len(self.ng_cams) == 0:
            self.ng_cams = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]

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

    def print_running_threads(self):
        threads = threading.enumerate()
        print(f"🧵 Số luồng đang chạy: {len(threads)}")


from is480_app import install_controller
install_controller(Main_Application, 'PC1')

app_ui = Main_Application()
if __name__ == '__main__':
    thread_nhan = threading.Thread(target=app_ui.read_serial, daemon=True)
    thread_nhan.start()
    # Start even if COM4 is initially offline; background loop reconnects.
    thread_approval_card = threading.Thread(target=app_ui.read_approval_card_serial,
                                            daemon=True, name='IS480-RFID-COM4')
    thread_approval_card.start()
    app_ui.bind("<Control-Shift-p>", lambda e: app_ui._is480_warning('PM/RUN chỉ được đổi qua điều phối PC1'))
    app_ui.bind_all("<KeyPress>", app_ui._is480_global_keypress, add="+")
    app_ui.bind("<KeyPress-o>", app_ui.open_statistics_window)
    app_ui.bind("<KeyPress-O>", app_ui.open_statistics_window)
    app_ui.bind("<F1>", app_ui.open_approval_window)
    app_ui.focus_force()
    app_ui.bind("<space>", app_ui.handle_space_switch_cam)
    app_ui.protocol("WM_DELETE_WINDOW", app_ui.on_closing)
    app_ui.mainloop()
