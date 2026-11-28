from database.app_logging import MAIN_LOG

import copy
import gc
import glob
import json
import math
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
import unicodedata
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from queue import Empty
from tkinter import filedialog, messagebox, ttk
from uuid import uuid4

import cv2
import numpy as np
import torch
from PIL import Image, ImageTk

from database.inspection_statistics import (
    InspectionFrameStore, ShiftSchedule, create_inspection_check, delete_inspection_check,
    get_recent_production_dates, get_statistics_recipes, init_statistics_database,
    query_body_history, query_statistics_checks,
    update_final_status
)
from comgrab3 import enumerate_cameras

os.environ["YOLO_OFFLINE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MAIN_LOG.info("PyTorch version: %s; CUDA build: %s; module: %s", torch.__version__, torch.version.cuda, torch.__file__)


# ================= SYSTEM CONFIG =================
class SystemConfig:
    WINDOW_TITLE = "Jig AI camera check FG_Bias_and_M_Roller"
    BODY_HISTORY_NG_ONLY = False  # False: show both OK and NG bodies
    ALLOW_DELETE_STATISTICS_RECORDS = False
    UNIT_SELECTOR_COLUMNS = 4  # 2 or 4

    BASE_DIR = r"D:\FG_Bias_and_M_Roller"
    RECIPES_DIR = os.path.join(BASE_DIR, "recipes")
    SAVE_ROOT_DIR = BASE_DIR
    SANLUONG_FILE = os.path.join(BASE_DIR, "database", "sanluong.json")
    BARCODE_COUNTER_FILE = os.path.join(BASE_DIR, "database", "barcode_counter.json")

    AUTO_BARCODE_RUN = False
    AUTO_BARCODE_PM = True
    BARCODE_TIMEOUT_SECONDS = 5.0
    # Thứ tự camera tương ứng với BODY NO1, BODY NO2, ...
    # ROI là (x1, y1, x2, y2). None dùng toàn bộ khung hình.
    BARCODE_CAMERAS = (
        ("cam6", (980, 390, 1310, 580)),
        ("cam8", (730, 435, 1065, 625)),
    )

    AI_DEVICE = 0

    SAVE_CATEGORIES = (
        "save_all_file", "save_file_ng", "save_crop_all",
        "save_crop_ng",
    )

    # --- PLC ---
    PLC_SIMULATOR_ENABLED = False
    PLC_HOST = "192.168.0.5"
    PLC_PORT = 502
    PLC_SLAVE_ID = 1
    PLC_POLL_SECONDS = 0.05

    # Thanh ghi PLC
    PLC_CMD_REG = "D1"  # PLC trigger step
    PLC_DONE_REG = "D0"  # PC báo xong step
    PLC_RESULT_REG = "D3"  # PC gửi KQ: OK=1 NG=2
    PLC_READY_REG = "D4"  # D4=0 PLC yêu cầu thị trường
    PLC_PROG_REG = "D5"  # PC gửi tín hiệu thị trường
    PLC_RESET_REG = "D6"  # D6=1 PLC yêu cầu dừng chu trình

    PLC_RESULT_OK = 1
    PLC_RESULT_NG = 2
    PLC_ERR_THRESHOLD = 10
    PLC_RECONNECT_INTERVAL = 3.0

    PLC_D5_SEND_DELAY_SECONDS = 0.05  # delay 50ms trước khi ghi D5
    PLC_D5_RESEND_INTERVAL_SECONDS = 0.5

    # 1: 4VT - K2
    # 2: 4VT - K3

    PLC_PROGRAM_GROUPS = {
        1: ["FG_Bias_Unit"],
        2: ["M_Roller"],
    }

    NO_BARCODE_PROGRAM_GROUPS = ["FG_Bias_Unit"]

    DEFAULT_PLC_PROGRAM = 1
    DEFAULT_CAPTURE_DELAY_SECONDS = 0.1

    @classmethod
    def get_plc_program_for_model(cls, model_name):
        if not model_name:
            return cls.DEFAULT_PLC_PROGRAM
        clean = str(model_name).strip().lower()
        for prog, models in cls.PLC_PROGRAM_GROUPS.items():
            for m in models:
                if clean == str(m).strip().lower():
                    return prog
        for prog, models in cls.PLC_PROGRAM_GROUPS.items():
            for m in models:
                mc = str(m).strip().lower()
                if mc and mc in clean:
                    return prog
        return cls.DEFAULT_PLC_PROGRAM

    @classmethod
    def should_skip_barcode(cls, model_name):
        clean = str(model_name).strip().lower()
        return any(clean == model.strip().lower() for model in cls.NO_BARCODE_PROGRAM_GROUPS)

    CAMERA_LOCATION_REFS = {
        "cam1": "PCIROOT(0)#PCI(1B04)#PCI(0000)#PCI(0100)#PCI(0000)#USBROOT(0)#USB(3)#USBMI(0)",
        "cam2": "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(10)#USBMI(0)",
        "cam3": "PCIROOT(0)#PCI(1D00)#PCI(0000)#PCI(0400)#PCI(0000)#USBROOT(0)#USB(3)#USBMI(0)",
        "cam4": "PCIROOT(0)#PCI(1D00)#PCI(0000)#PCI(0200)#PCI(0000)#USBROOT(0)#USB(3)#USBMI(0)",
        "cam5": "PCIROOT(0)#PCI(1D00)#PCI(0000)#PCI(0300)#PCI(0000)#USBROOT(0)#USB(3)#USBMI(0)",
        "cam6": "PCIROOT(0)#PCI(1B04)#PCI(0000)#PCI(0300)#PCI(0000)#USBROOT(0)#USB(3)#USBMI(0)",
        "cam7": "PCIROOT(0)#PCI(1B04)#PCI(0000)#PCI(0200)#PCI(0000)#USBROOT(0)#USB(3)#USBMI(0)",
        "cam8": "PCIROOT(0)#PCI(1B04)#PCI(0000)#PCI(0400)#PCI(0000)#USBROOT(0)#USB(3)#USBMI(0)"
    }
    CAMERAS_ROTATE_180 = ["cam2"]  # VD: ["cam1"]

    EXECUTOR_WORKERS = 10
    EXECUTOR_CAPTURE_WORKERS = 4
    CAMERA_WIDTH = 1920
    CAMERA_HEIGHT = 1080
    CAMERA_BUFFER_SIZE = 2
    CAMERA_STARTUP_TIMEOUT_PER_CAMERA_SECONDS = 5.0
    
    CAMERA_FOURCC = None
    CAMERA_SHUTDOWN_WARN_SECONDS = 2.0

    CAMERA_IDLE_INTERVAL = 0.1
    CAMERA_ACTIVE_INTERVAL = 0.008
    CAMERA_ACTIVE_WINDOW = 0.5

    ORB_FEATURES = 500
    ORB_FAST_THRESHOLD = 10
    ORB_EDGE_THRESHOLD = 15
    ORB_MIN_MATCHES = 15
    MODEL_WARMUP_WORKERS = 1

    CAPTURE_FRAME_RETRIES = 60
    CAPTURE_FRAME_RETRY_SLEEP = 0.05

    UI_IMAGE_UPDATE_INTERVAL = 0.1

    AI_POINT_TIMEOUT_SECONDS = 8.0
    AI_MAX_CONCURRENT_WORKERS = 4
    ENABLE_BATCH_INFERENCE = False
    AI_MAX_BATCH_SIZE = 1
    AI_BATCH_TOTAL_TIMEOUT = 30.0
    REQUIRE_ALL_POINTS = True
    MAX_PENDING_SAVE_TASKS = 250
    SAVE_EXECUTOR_SHUTDOWN_TIMEOUT = 10.0

    @staticmethod
    def point_dimensions(point):
        if "WIDTH" in point or "HEIGHT" in point:
            if "WIDTH" not in point or "HEIGHT" not in point:
                raise ValueError(f"Rectangle point requires both WIDTH and HEIGHT: {point!r}")
            width, height = point["WIDTH"], point["HEIGHT"]
        else:
            if "SIZE" not in point:
                raise ValueError(f"Point requires SIZE or WIDTH/HEIGHT: {point!r}")
            width = height = point["SIZE"]
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            raise ValueError(f"Invalid point dimensions: {point!r}; use positive integer SIZE or WIDTH/HEIGHT")
        return width, height

    @classmethod
    def point_box(cls, point):
        width, height = cls.point_dimensions(point)
        return int(point["X"]), int(point["Y"]), width, height


class UnitConfiguration:
    def __init__(self, points_cfg, presence_points, context):
        self.context = str(context)
        self.points_cfg = points_cfg
        self.points = {}
        self.units = {}
        self.point_units = {}
        for group, points in points_cfg.items():
            for point in points:
                if not isinstance(point, dict):
                    raise ValueError(f"{context}: {group}, expected point object, got {point!r}")
                point_id = point.get("POINT")
                if type(point_id) is not int or point_id < 0:
                    raise ValueError(f"{context}: {group}, invalid POINT={point_id!r}")
                key = f"{group}_D{point_id}"
                if key in self.points:
                    raise ValueError(f"{context}: duplicate point {key}")
                unit = self.unit_id(point, f"{context}: {key}")
                self.points[key] = point
                self.point_units[key] = unit
                self.units.setdefault(unit, {"presence": None, "inspection": set()})
                self.units[unit]["inspection"].add(key)

        self.presence_points = set()
        for key in self.validate_presence_points(presence_points, context):
            if key not in self.points:
                raise ValueError(f"{context}: presence point {key} is missing from active point configs")
            unit = self.point_units[key]
            group = self.units[unit]
            if group["presence"] is not None:
                raise ValueError(f"{context}: unit {self.unit_label(unit)} has multiple presence points: {group['presence']}, {key}")
            group["presence"] = key
            group["inspection"].remove(key)
            self.presence_points.add(key)
        for unit, group in self.units.items():
            if not group["inspection"]:
                raise ValueError(f"{context}: unit {self.unit_label(unit)} has no inspection points")

    @staticmethod
    def unit_id(point, context):
        if "UNIT_ID" not in point:
            return None
        value = point["UNIT_ID"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{context}: UNIT_ID must be a nonempty string, got {value!r}")
        return value

    @staticmethod
    def unit_label(unit):
        return "DEFAULT" if unit is None else unit

    @staticmethod
    def validate_presence_points(points, context):
        if not isinstance(points, list):
            raise ValueError(f"{context}: PRESENCE_POINTS must be a list of strings")
        seen = set()
        for index, key in enumerate(points):
            if not isinstance(key, str) or not re.fullmatch(r"CAM(0|[1-9][0-9]*)_VT(0|[1-9][0-9]*)_D(0|[1-9][0-9]*)", key):
                raise ValueError(f"{context}: PRESENCE_POINTS[{index}]={key!r}; expected CAM1_VT1_D10")
            if key in seen:
                raise ValueError(f"{context}: duplicate presence point {key}")
            seen.add(key)
        return points

    @staticmethod
    def load_presence_points(recipe_dir):
        path = Path(recipe_dir) / "special_points.json"
        try:
            with path.open(encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as error:
            raise ValueError(f"Cannot read {path}: {error}") from error
        if not isinstance(data, dict) or not isinstance(data.get("PRESENCE_POINTS"), list):
            raise ValueError(f"{path}: expected an object with PRESENCE_POINTS list")
        return UnitConfiguration.validate_presence_points(data["PRESENCE_POINTS"], path)


class PresencePointEvaluator:
    @staticmethod
    def evaluate(key, result):
        if result is None:
            raise ValueError(f"Presence {key}: missing inference result")
        point, _, name, probability, _, _, model_type, error = result
        if model_type == "error":
            raise ValueError(f"Presence {key}: {error or name}")
        label = name.upper()
        has_ok, has_ng = "OK" in label, "NG" in label
        if has_ok == has_ng:
            raise ValueError(
                f"Presence {key}: invalid class={name!r}, probability={probability}, "
                f"threshold={point.get('THRESHOLD', 0.85)}; expected OK or NG")
        # Confidence handling for presence is deferred; quality thresholds remain unchanged.
        return "PRESENT" if has_ok else "ABSENT"


class CycleUnitEvaluator:
    def __init__(self, configuration):
        self.configuration = configuration

    def evaluate(self, results, is_point_ok):
        units = {}
        points = {}
        ng_points = []
        for unit, group in self.configuration.units.items():
            presence = group["presence"]
            state = (PresencePointEvaluator.evaluate(presence, results.get(presence))
                     if presence else "PRESENT")
            if presence:
                points[presence] = state
            if state == "ABSENT":
                units[unit] = "ABSENT"
                points.update({key: "SKIPPED" for key in group["inspection"]})
                continue
            failed = False
            for key in sorted(group["inspection"]):
                result = results.get(key)
                if result is None:
                    raise ValueError(f"Unit {self.configuration.unit_label(unit)}, {key}: missing inspection result")
                if result[6] == "error":
                    raise ValueError(f"Unit {self.configuration.unit_label(unit)}, {key}: {result[7] or result[2]}")
                ok = is_point_ok(result[0], result[2], result[3], result[6])
                points[key] = "OK" if ok else "NG"
                if not ok:
                    failed = True
                    ng_points.append(key)
            units[unit] = "NG" if failed else "OK"
        return {
            "units": units,
            "points": points,
            "ng_points": tuple(sorted(ng_points)),
            "ok": sum(state == "OK" for state in units.values()),
            "ng": sum(state == "NG" for state in units.values()),
            "empty": bool(units) and all(state == "ABSENT" for state in units.values()),
        }


# ================= PLC SIMULATOR =================
class PlcSimulator:
    def __init__(self):
        self._lock = threading.RLock()
        self._connected = False
        self._registers = {
            SystemConfig.PLC_DONE_REG: 0,
            SystemConfig.PLC_CMD_REG: 0,
            SystemConfig.PLC_RESULT_REG: 0,
            SystemConfig.PLC_READY_REG: 1,
            SystemConfig.PLC_PROG_REG: 0,
            SystemConfig.PLC_RESET_REG: 0,
        }
        self._d1_pulse = []

    def connect(self):
        with self._lock:
            self._connected = True
        MAIN_LOG.info("PLC simulator connected")
        return True

    def close(self):
        with self._lock:
            self._connected = False
            self._d1_pulse.clear()

    def _check_register(self, reg):
        if not self._connected:
            raise ConnectionError(f"PLC simulator is disconnected: register={reg!r}")
        if reg not in self._registers:
            raise ValueError(f"Unknown PLC simulator register: register={reg!r}")

    def read_reg(self, reg):
        with self._lock:
            self._check_register(reg)
            if reg == SystemConfig.PLC_CMD_REG and self._d1_pulse:
                self._registers[reg] = self._d1_pulse.pop(0)
                MAIN_LOG.info("PLC simulator pulse: %s=%s", reg, self._registers[reg])
            return self._registers[reg]

    def write_int16(self, reg, value):
        with self._lock:
            self._check_register(reg)
            if type(value) is not int or not -32768 <= value <= 32767:
                raise ValueError(f"Invalid PLC simulator int16: register={reg!r}, value={value!r}")
            if reg == SystemConfig.PLC_CMD_REG:
                self._d1_pulse.clear()
            self._registers[reg] = value
        MAIN_LOG.info("PLC simulator write: %s=%s", reg, value)

    def pulse_d1(self):
        with self._lock:
            self._check_register(SystemConfig.PLC_CMD_REG)
            if self._d1_pulse:
                raise RuntimeError("PLC simulator D1 pulse is still active; wait for the next polls")
            self._d1_pulse = [0, 1, 0]
        MAIN_LOG.info("PLC simulator requested D1 pulse")

    def snapshot(self):
        with self._lock:
            self._check_register(SystemConfig.PLC_CMD_REG)
            return dict(self._registers)


class CameraKey:
    @staticmethod
    def cam_token(name):
        """'CAM3' / 'cam3' / 3 -> '3' (giữ nguyên dạng chuỗi như recipe khai báo)."""
        return str(name).upper().replace("CAM", "")

    @staticmethod
    def point_group_key(cam, vitri):
        """Khóa nhóm điểm kiểm tra, ví dụ 'CAM3_VT2'."""
        return f"CAM{CameraKey.cam_token(cam)}_VT{vitri}"


# ================= RECIPE MANAGER =================
class RecipeManager:
    def __init__(self):
        self.recipes_dir = SystemConfig.RECIPES_DIR
        self.available_models = self._scan_models()
        self.current_model_display = None
        self.current_model_folder = None
        self.current_plc_program = SystemConfig.DEFAULT_PLC_PROGRAM
        self.plc_step_delays = {}

        self.plc_steps = {}
        self.orb_rois = {}
        self.points_cfg = {}
        self.presence_points = []
        self.unit_configuration = None
        self.ref_imgs = {}
        self.orb_ref_cache = {}

        self.ai_models = {}
        self.model_groups = {}
        self.total_points = 0

        self.orb_local = threading.local()

    def _get_orb_tools(self):
        if not hasattr(self.orb_local, "orb"):
            self.orb_local.orb = cv2.ORB_create(
                nfeatures=SystemConfig.ORB_FEATURES,
                fastThreshold=SystemConfig.ORB_FAST_THRESHOLD,
                edgeThreshold=SystemConfig.ORB_EDGE_THRESHOLD)
            self.orb_local.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        return self.orb_local.orb, self.orb_local.bf

    def _scan_models(self):
        models = {}
        if not os.path.exists(self.recipes_dir):
            return models
        for d in sorted(os.listdir(self.recipes_dir)):
            if os.path.isdir(os.path.join(self.recipes_dir, d)):
                models[d.strip()] = d
        return models

    def _resolve_model_path(self, raw_path):
        """Tìm checkpoint .pt trong các vị trí model của recipe."""
        if not isinstance(raw_path, str) or not raw_path.lower().endswith(".pt"):
            raise ValueError(f"MODEL_PATHS must point to a .pt file: {raw_path!r}")
        nr = os.path.normpath(raw_path)
        bn = os.path.basename(nr)
        mdir = (os.path.join(self.recipes_dir, self.current_model_folder)
                if self.current_model_folder else "")
        if os.path.isabs(nr):
            if os.path.isfile(nr):
                return nr
            raise FileNotFoundError(f"Model .pt file not found: {nr}")

        cands = [
            os.path.join(mdir, nr) if mdir else "",
            os.path.join(SystemConfig.BASE_DIR, nr),
            os.path.join(SystemConfig.BASE_DIR, "models", nr),
            os.path.join(SystemConfig.BASE_DIR, "models", bn),
            os.path.join(SystemConfig.BASE_DIR, "model_goc", nr),
            os.path.join(SystemConfig.BASE_DIR, "model_goc", bn),
            os.path.join(SystemConfig.BASE_DIR, "model_train", nr),
            os.path.abspath(nr),
        ]
        for c in cands:
            if c and os.path.isfile(c):
                return c
        raise FileNotFoundError(
            f"Model .pt file '{raw_path}' not found for recipe "
            f"'{self.current_model_display}'. Searched: {cands}")

    def _load_points_file(self, fname):
        std_key = self._standardize_key(os.path.basename(fname))
        pts = []
        errs = 0
        try:
            with open(fname, 'r', encoding='utf-8') as f:
                for line_no, line in enumerate(f, 1):
                    ls = line.strip()
                    if not ls:
                        continue
                    bi = ls.find("{")
                    if bi > 0:
                        ls = ls[bi:]
                    try:
                        point = json.loads(ls)
                        SystemConfig.point_dimensions(point)
                        pts.append(point)
                    except json.JSONDecodeError as e:
                        errs += 1
                        print(f"WARN: Invalid JSON in {os.path.basename(fname)}:{line_no}: {e}")
        except Exception as e:
            raise RuntimeError(f"Unable to read points file {fname}: {e}") from e
        if errs:
            raise ValueError(f"Points file {fname} contains {errs} invalid JSON lines")
        return std_key, pts, errs

    def get_needed_cam_ids(self):
        needed = set()
        for k in self.points_cfg.keys():
            m = re.match(r'CAM(\d+)_VT\d+', k)
            if m:
                needed.add(int(m.group(1)))
        for pairs in self.plc_steps.values():
            if not isinstance(pairs, (list, tuple)):
                continue
            for p in pairs:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    try:
                        needed.add(int(CameraKey.cam_token(p[0])))
                    except ValueError:
                        pass
        return needed

    def _precompute_orb_refs(self):
        self.orb_ref_cache.clear()
        orb, _ = self._get_orb_tools()
        for std_key, roi_list in self.orb_rois.items():
            gray_ref = self.ref_imgs.get(std_key)
            if gray_ref is None or not roi_list:
                continue
            h, w = gray_ref.shape[:2]
            cached = []
            for r in roi_list:
                x, y, rw, rh = r["xywh"]
                x1 = max(0, min(int(x), w - 1))
                y1 = max(0, min(int(y), h - 1))
                x2 = max(0, min(int(x + rw), w))
                y2 = max(0, min(int(y + rh), h))
                rz = gray_ref[y1:y2, x1:x2]
                if rz.size == 0:
                    cached.append({"raw": r, "bbox": (x1, y1, x2, y2), "kp_pts": None, "des": None})
                    continue
                kp1, des1 = orb.detectAndCompute(rz, None)
                kp_pts = np.float32([kp.pt for kp in kp1]) if kp1 and des1 is not None else None
                cached.append({"raw": r, "bbox": (x1, y1, x2, y2), "kp_pts": kp_pts, "des": des1})
            self.orb_ref_cache[std_key] = cached

    @staticmethod
    def _read_json(path, parse=None):
        """Đọc JSON; mọi lỗi (mở file, JSON hỏng, parse) đều thành RuntimeError."""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return parse(data) if parse else data
        except Exception as e:
            raise RuntimeError(f"Unable to read {path}: {e}") from e

    def load_recipe(self, model_display_name, progress_callback=None):
        if not torch.cuda.is_available():
            raise RuntimeError(
                f"Unable to load recipe '{model_display_name}': PyTorch cannot access CUDA. "
                "Check the NVIDIA GPU, driver, and CUDA-enabled PyTorch build.")

        self._reset_for_recipe(model_display_name)
        if not self.current_model_folder:
            raise FileNotFoundError(f"Recipe directory not found: {model_display_name}")
        mdir = os.path.join(self.recipes_dir, self.current_model_folder)

        self._load_plc_steps(mdir)
        self._load_points(mdir)
        self.presence_points = UnitConfiguration.load_presence_points(mdir)
        self._validate_recipe_config()
        model_paths = self._load_model_config(mdir)
        self._load_models(model_paths, progress_callback)
        self._check_models_cover_points()
        self._load_orb_rois(mdir)
        self._load_reference_images(mdir)
        self._precompute_orb_refs()
        print(f"Recipe [{model_display_name}] | Points: {self.total_points} | "
              f"Models: {len(self.ai_models)} | ORBs: {len(self.orb_rois)} | "
              f"Refs: {len(self.ref_imgs)} | PLC {SystemConfig.PLC_PROG_REG} = {self.current_plc_program}")

    def _reset_for_recipe(self, model_display_name):
        self.current_model_display = model_display_name
        self.current_model_folder = self.available_models.get(model_display_name)
        self.current_plc_program = SystemConfig.get_plc_program_for_model(model_display_name)
        self.plc_step_delays.clear()

        self.ai_models.clear()
        self.ref_imgs.clear()
        self.points_cfg.clear()
        self.presence_points = []
        self.unit_configuration = None
        self.orb_rois.clear()
        self.orb_ref_cache.clear()
        self.plc_steps = {}
        self.model_groups = {}
        self.total_points = 0
        gc.collect()

    def _load_plc_steps(self, mdir):
        p_file = os.path.join(mdir, "plc_steps.json")
        if not os.path.exists(p_file):
            return

        def parse(raw):
            steps = raw.get("STEPS")
            if not isinstance(steps, dict) or not steps:
                raise ValueError(f"Invalid STEPS in {p_file}: expected nonempty step/camera object, got {steps!r}")
            self.plc_steps = {int(k): v for k, v in steps.items()}
            delays = raw.get("DELAY_SECONDS", {})
            if not isinstance(delays, dict):
                raise ValueError(
                    f"Invalid DELAY_SECONDS in {p_file}: expected step/seconds object, got {delays!r}")
            for step_key, value in delays.items():
                try:
                    step = int(step_key)
                except ValueError as exc:
                    raise ValueError(
                        f"Invalid DELAY_SECONDS step in {p_file}: step={step_key!r}, value={value!r}") from exc
                if step not in self.plc_steps:
                    continue
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError(
                        f"Invalid DELAY_SECONDS in {p_file}: step={step_key!r}, value={value!r}; "
                        "expected finite seconds >= 0")
                self.plc_step_delays[step] = value
            for step in self.plc_steps:
                self.plc_step_delays.setdefault(step, SystemConfig.DEFAULT_CAPTURE_DELAY_SECONDS)

        self._read_json(p_file, parse)

    def _load_points(self, mdir):
        pts_dir = os.path.join(mdir, "point_configs")
        if not os.path.exists(pts_dir):
            return
        active_groups = {
            CameraKey.point_group_key(pair[0], pair[1])
            for pairs in self.plc_steps.values()
            if isinstance(pairs, (list, tuple))
            for pair in pairs
            if isinstance(pair, (list, tuple)) and len(pair) >= 2
        }
        for fname in sorted(glob.glob(os.path.join(pts_dir, "*.txt"))):
            if self._standardize_key(os.path.basename(fname)) not in active_groups:
                continue
            std_key, pts, _ = self._load_points_file(fname)
            self.points_cfg[std_key] = pts
            self.total_points += len(pts)
            if not pts:
                print(f"WARN: {os.path.basename(fname)} is empty; {std_key} has 0 points")

    def _load_model_config(self, mdir):
        """Đọc MODEL_PATHS và MODEL_GROUPS trong models_used.json."""
        mod_file = os.path.join(mdir, "models_used.json")
        mod = self._read_json(mod_file)
        if not isinstance(mod, dict):
            raise ValueError(f"Invalid model configuration in {mod_file}: expected object, got {mod!r}")
        model_paths_to_load = mod.get("MODEL_PATHS")
        if not isinstance(model_paths_to_load, dict) or not model_paths_to_load:
            raise ValueError(
                f"Recipe '{self.current_model_display}' is missing MODEL_PATHS in {mod_file}")

        self.model_groups = mod.get("MODEL_GROUPS")
        if not isinstance(self.model_groups, dict) or not self.model_groups:
            raise ValueError(
                f"Recipe '{self.current_model_display}' is missing MODEL_GROUPS in {mod_file}")
        self._validate_model_groups(model_paths_to_load, mod_file)
        return model_paths_to_load

    def _validate_model_groups(self, model_paths, config_path):
        for vt_key, cameras in self.model_groups.items():
            if not isinstance(cameras, dict):
                raise ValueError(
                    f"Invalid MODEL_GROUPS in {config_path}: {vt_key}={cameras!r}; expected camera object")
            for cam_key, rules in cameras.items():
                if not isinstance(rules, dict):
                    raise ValueError(
                        f"Invalid MODEL_GROUPS in {config_path}: {vt_key}/{cam_key}={rules!r}; "
                        "expected model/point-list object")
                for model_key, point_ids in rules.items():
                    if model_key not in model_paths:
                        raise ValueError(
                            f"MODEL_GROUPS references a missing MODEL_PATHS entry in {config_path}: "
                            f"{vt_key}/{cam_key}, model={model_key!r}, points={point_ids!r}")
                    if point_ids is not None and (
                            not isinstance(point_ids, list)
                            or any(type(point_id) is not int for point_id in point_ids)):
                        raise ValueError(
                            f"Invalid MODEL_GROUPS in {config_path}: {vt_key}/{cam_key}, "
                            f"model={model_key!r}, points={point_ids!r}; expected integer list or null")

    def _load_models(self, model_paths_to_load, progress_callback=None):
        model_display_name = self.current_model_display
        wc = {"done": 0, "total": max(1, len(model_paths_to_load))}
        wl = threading.Lock()
        if progress_callback:
            progress_callback(0, "Đang nạp dữ liệu...")
        from ultralytics import YOLO

        def _load_and_warmup(item):
            m_name, raw_p = item
            try:
                full_p = self._resolve_model_path(raw_path=raw_p)
                t0 = time.time()
                yolo = YOLO(full_p, task="classify")
                yolo._infer_lock = threading.Lock()
                bs = max(1, SystemConfig.AI_MAX_BATCH_SIZE)
                warm_imgs = [np.zeros((224, 224, 3), dtype=np.uint8) for _ in range(bs)]
                warm_results = yolo.predict(
                    warm_imgs, imgsz=224, device=SystemConfig.AI_DEVICE,
                    verbose=False)
                if not warm_results or any(r.probs is None for r in warm_results):
                    raise RuntimeError("Model .pt did not return classification probabilities")
                el = round(time.time() - t0, 2)
                with wl:
                    wc["done"] += 1
                    pct = int(wc["done"] * 100 / wc["total"])
                # In class names để verify thứ tự OK/NG không bị đảo
                try:
                    cls_names = yolo.names
                except Exception:
                    cls_names = "?"
                print(f"   [{pct:>3}%] {m_name} warmup OK ({el}s) | classes={cls_names}")
                print(f"      Model path: {full_p}")
                if progress_callback:
                    progress_callback(pct, f"Đã nạp {m_name} ({pct}%)")
                return m_name, yolo
            except Exception as e:
                MAIN_LOG.error(f"Load model {m_name} failed", exc_info=True)
                raise RuntimeError(
                    f"Unable to load model '{m_name}' ({raw_p}) for recipe "
                    f"'{model_display_name}': {e}") from e

        with ThreadPoolExecutor(max_workers=SystemConfig.MODEL_WARMUP_WORKERS) as ex:
            for m_name, obj in ex.map(_load_and_warmup, model_paths_to_load.items()):
                self.ai_models[m_name] = obj

    def _check_models_cover_points(self):
        """Mỗi điểm kiểm tra trong plc_steps phải chọn được một model."""
        for step, pairs in self.plc_steps.items():
            for pair in pairs:
                cam = CameraKey.cam_token(pair[0])
                vitri = pair[1]
                sk = CameraKey.point_group_key(pair[0], vitri)
                for point in self.points_cfg[sk]:
                    pid = point.get("POINT", "?")
                    model = self.select_model_for_point(cam, vitri, pid)
                    if model is None:
                        raise ValueError(
                            f"Recipe '{self.current_model_display}' is missing a model in MODEL_GROUPS for "
                            f"step {step}, {sk}, point D{pid}")
                    key = f"{sk}_D{pid}"
                    if key in self.unit_configuration.presence_points:
                        names = model.names
                        labels = names.values() if isinstance(names, dict) else names
                        labels = [str(label).upper() for label in labels]
                        if (any(("OK" in label) == ("NG" in label) for label in labels)
                                or not any("OK" in label for label in labels)
                                or not any("NG" in label for label in labels)):
                            raise ValueError(
                                f"Recipe '{self.current_model_display}', presence {key}: "
                                f"model classes must distinguish OK (present) and NG (empty), got {names!r}")

    def _load_orb_rois(self, mdir):
        o_file = os.path.join(mdir, "orb_rois.json")
        if os.path.exists(o_file):
            try:
                with open(o_file, 'r', encoding='utf-8') as f:
                    raw_orb = json.load(f)
                for k, v in raw_orb.items():
                    self.orb_rois[self._standardize_key(k)] = v
            except Exception as e:
                print(f"ERROR: Failed to read orb_rois.json: {e}")

    def _load_reference_images(self, mdir):
        ref_dir = os.path.join(mdir, "reference_images")
        if os.path.exists(ref_dir):
            for ipath in glob.glob(os.path.join(ref_dir, "**", "*.*"), recursive=True):
                if ipath.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp')):
                    img = cv2.imread(ipath, 0)
                    if img is not None:
                        self.ref_imgs[self._standardize_key(os.path.basename(ipath))] = img

    def _validate_recipe_config(self):
        errors = []
        point_count = 0
        if not self.plc_steps:
            errors.append("Thiếu bước trong plc_steps.json")
        for step, pairs in self.plc_steps.items():
            if not isinstance(pairs, (list, tuple)):
                errors.append(f"Step {step}: danh sách camera không hợp lệ")
                continue
            for p in pairs:
                if not (isinstance(p, (list, tuple)) and len(p) >= 2):
                    errors.append(f"Step {step}: camera/vị trí không hợp lệ: {p!r}")
                    continue
                sk = CameraKey.point_group_key(p[0], p[1])
                if sk not in self.points_cfg:
                    errors.append(f"Step {step}: '{sk}' thiếu points file")
                elif not self.points_cfg[sk]:
                    errors.append(f"Step {step}: '{sk}' has 0 points")
                else:
                    point_count += len(self.points_cfg[sk])
        if self.plc_steps and point_count == 0:
            errors.append("No inspection points configured")
        if errors:
            raise ValueError(
                f"Recipe '{self.current_model_display}' is invalid: "
                + "; ".join(errors))
        active_groups = {
            CameraKey.point_group_key(cam, vt)
            for pairs in self.plc_steps.values() for cam, vt in pairs
        }
        self.unit_configuration = UnitConfiguration(
            {key: self.points_cfg[key] for key in active_groups},
            self.presence_points, self.current_model_display)

    def _standardize_key(self, name):
        m = re.search(r'cam(\d+)[_]?vt(\d+)', name.lower())
        if m:
            return f"CAM{m.group(1)}_VT{m.group(2)}"
        print(f"WARN: Could not parse '{name}'; using fallback: {name.upper()}")
        return name.upper()

    def compute_orb_dxdy(self, std_key, gray_new):
        cached = self.orb_ref_cache.get(std_key)
        zones = []
        if not cached or gray_new is None:
            return zones
        orb, bf = self._get_orb_tools()
        h_new, w_new = gray_new.shape[:2]
        for cz in cached:
            zone = dict(cz["raw"])
            x1, y1, x2, y2 = cz["bbox"]
            kp1_pts = cz["kp_pts"]
            des1 = cz["des"]
            x1c = max(0, min(x1, w_new - 1))
            y1c = max(0, min(y1, h_new - 1))
            x2c = max(0, min(x2, w_new))
            y2c = max(0, min(y2, h_new))
            nz = gray_new[y1c:y2c, x1c:x2c]
            if kp1_pts is None or des1 is None or nz.size == 0:
                zone["dxdy"] = (0.0, 0.0)
                zones.append(zone)
                continue
            kp2, des2 = orb.detectAndCompute(nz, None)
            if not kp2 or des2 is None:
                zone["dxdy"] = (0.0, 0.0)
                zones.append(zone)
                continue
            matches = bf.match(des1, des2)
            if len(matches) < SystemConfig.ORB_MIN_MATCHES:
                zone["dxdy"] = (0.0, 0.0)
                zones.append(zone)
                continue
            dxs, dys = [], []
            for m in matches:
                x1p, y1p = kp1_pts[m.queryIdx]
                x2p, y2p = kp2[m.trainIdx].pt
                dxs.append(x2p - x1p)
                dys.append(y2p - y1p)
            zone["dxdy"] = (float(np.median(dxs)), float(np.median(dys)))
            zones.append(zone)
        return zones

    def select_model_for_point(self, cam, vitri, pt_id):
        vt_key = f"VT{vitri}"
        cam_key = f"CAM{cam}"
        cam_rules = self.model_groups.get(vt_key, {}).get(cam_key, {})
        for m_key, pts_list in cam_rules.items():
            if pts_list is None or pt_id in pts_list:
                return self.ai_models.get(m_key)
        return None


# ================= UI COMPONENTS =================
class ResultImageCanvas(tk.Canvas):
    MIN_ZOOM = 1.0
    MAX_ZOOM = 8.0
    ZOOM_STEP = 1.2

    def __init__(self, parent, text):
        super().__init__(parent, width=1000, height=528, borderwidth=1,
                         relief="solid", highlightthickness=0, cursor="fleur")
        self.source_image = None
        self.original_image = None
        self.roi_views = []
        self.roi_menu = tk.Menu(self, tearoff=0)
        self.zoom = self.MIN_ZOOM
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.drag_position = None
        self.imgtk = None
        self.display_text = None
        self.bind("<MouseWheel>", self.on_mousewheel)
        self.bind("<Button-4>", self.on_mousewheel)
        self.bind("<Button-5>", self.on_mousewheel)
        self.bind("<ButtonPress-1>", self.start_drag)
        self.bind("<B1-Motion>", self.drag_image)
        self.bind("<ButtonRelease-1>", self.stop_drag)
        self.bind("<Button-3>", self.show_roi_menu)
        self.bind("<Configure>", self.render_image)
        self.show_text(text)

    def show_frame(self, frame, reset_view=True, roi_views=()):
        if frame is None or frame.size == 0:
            return
        self.show_image(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)),
                        reset_view, roi_views)

    def show_image(self, image, reset_view=True, roi_views=()):
        self.roi_menu.unpost()
        self.roi_views = roi_views
        self.original_image = image
        self.source_image = image
        self.display_text = None
        if reset_view:
            self.zoom = self.MIN_ZOOM
            self.offset_x = 0.0
            self.offset_y = 0.0
        self.render_roi_colors()

    def show_text(self, text="", **_kw):
        self.roi_menu.unpost()
        self.roi_views = []
        self.original_image = None
        self.source_image = None
        self.imgtk = None
        self.display_text = text
        self.delete(tk.ALL)
        self.create_text(self.winfo_width() // 2, self.winfo_height() // 2, text=text,
                         font=_kw.get("font", ("Arial", 30)),
                         fill=_kw.get("fg", "blue"),
                         justify=tk.CENTER, tags="display_text")

    def show_roi_menu(self, event):
        self.roi_menu.unpost()
        if self.source_image is None:
            return "break"
        _, _, dw, dh, bx, by = self.get_display_geometry()
        iw, ih = self.source_image.size
        x = (event.x - bx - self.offset_x) * iw / dw
        y = (event.y - by - self.offset_y) * ih / dh
        for view in reversed(self.roi_views):
            left, top, width, height = view["bounds"]
            if not (left <= x < left + width and top <= y < top + height):
                continue
            fh, fw = view["frame"].shape[:2]
            rx, ry = (x - left) * fw / width, (y - top) * fh / height
            for roi in reversed(view["rois"]):
                xn, yn, width, height = roi["box"]
                if not roi["manual_ok"] and xn <= rx <= xn + width and yn <= ry <= yn + height:
                    self.roi_menu.delete(0, tk.END)
                    self.roi_menu.add_command(label="Chuyển OK",
                                              command=lambda r=roi: self.mark_roi_ok(r))
                    try:
                        self.roi_menu.tk_popup(event.x_root, event.y_root)
                    finally:
                        self.roi_menu.grab_release()
                    return "break"
        return "break"

    def mark_roi_ok(self, roi):
        roi["manual_ok"] = True
        self.render_roi_colors()

    def render_roi_colors(self):
        if not any(roi["manual_ok"] for view in self.roi_views for roi in view["rois"]):
            self.source_image = self.original_image
            self.render_image()
            return
        image = np.array(self.original_image)
        for view in self.roi_views:
            accepted = [roi for roi in view["rois"] if roi["manual_ok"]]
            if not accepted:
                continue
            mask = np.zeros(view["frame"].shape[:2], dtype=np.uint8)
            for roi in accepted:
                xn, yn, width, height = roi["box"]
                cv2.rectangle(mask, (xn, yn), (xn + width, yn + height), 255, 5)
                cv2.putText(mask, f"D_{roi['point']}", (xn, yn - 10),
                            cv2.FONT_HERSHEY_COMPLEX, 1, 255, 2)
            left, top, width, height = view["bounds"]
            mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_AREA)
            image[top:top + height, left:left + width][mask > 0] = (0, 255, 0)
        self.source_image = Image.fromarray(image)
        self.render_image()

    def get_display_geometry(self):
        iw, ih = self.source_image.size
        cw = max(self.winfo_width(), 1)
        ch = max(self.winfo_height(), 1)
        sc = min(cw / iw, ch / ih)
        dw = max(1, round(iw * sc * self.zoom))
        dh = max(1, round(ih * sc * self.zoom))
        return cw, ch, dw, dh, (cw - dw) / 2, (ch - dh) / 2

    def constrain_offset(self):
        cw, ch, dw, dh, bx, by = self.get_display_geometry()
        self.offset_x = 0.0 if dw <= cw else min(max(self.offset_x, cw - bx - dw), -bx)
        self.offset_y = 0.0 if dh <= ch else min(max(self.offset_y, ch - by - dh), -by)

    def render_image(self, event=None):
        if self.source_image is None:
            if self.display_text is not None:
                self.coords("display_text", self.winfo_width() // 2, self.winfo_height() // 2)
            return
        cw, ch, dw, dh, bx, by = self.get_display_geometry()
        self.constrain_offset()
        iw, ih = self.source_image.size
        sx, sy = dw / iw, dh / ih
        ix, iy = bx + self.offset_x, by + self.offset_y
        sl = max(0, min(iw - 1, int(np.floor(-ix / sx))))
        st = max(0, min(ih - 1, int(np.floor(-iy / sy))))
        sr = max(sl + 1, min(iw, int(np.ceil((cw - ix) / sx))))
        sb = max(st + 1, min(ih, int(np.ceil((ch - iy) / sy))))
        rsp = Image.Resampling.BILINEAR if self.zoom <= 2.0 else Image.Resampling.LANCZOS
        vi = self.source_image.crop((sl, st, sr, sb)).resize(
            (max(1, round((sr - sl) * sx)), max(1, round((sb - st) * sy))), rsp)
        self.imgtk = ImageTk.PhotoImage(vi)
        self.delete(tk.ALL)
        self.create_image(ix + sl * sx, iy + st * sy, anchor=tk.NW, image=self.imgtk)

    def on_mousewheel(self, event):
        if self.source_image is None:
            return "break"
        zi = getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0
        nz = max(self.MIN_ZOOM, min(
            self.zoom * self.ZOOM_STEP if zi else self.zoom / self.ZOOM_STEP, self.MAX_ZOOM))
        if nz == self.zoom:
            return "break"
        _, _, dw, dh, bx, by = self.get_display_geometry()
        ix = (event.x - bx - self.offset_x) / dw
        iy = (event.y - by - self.offset_y) / dh
        self.zoom = nz
        _, _, ndw, ndh, nbx, nby = self.get_display_geometry()
        self.offset_x = event.x - nbx - ix * ndw
        self.offset_y = event.y - nby - iy * ndh
        self.render_image()
        return "break"

    def start_drag(self, event):
        if self.source_image is not None:
            self.drag_position = (event.x, event.y)
        return "break"

    def drag_image(self, event):
        if self.source_image is None or self.drag_position is None:
            return "break"
        lx, ly = self.drag_position
        self.offset_x += event.x - lx
        self.offset_y += event.y - ly
        self.drag_position = (event.x, event.y)
        self.render_image()
        return "break"

    def stop_drag(self, event):
        self.drag_position = None
        return "break"


class ResultCheck(tk.Frame):
    def __init__(self, parent, pm_mode):
        super().__init__(parent)
        self.pm_mode = pm_mode
        self.grid(sticky="nsew")
        for i in range(6):
            self.rowconfigure(i, minsize=100 if i == 1 else 10,
                              weight=1 if i in (3, 5) else 0)
        for j in range(4):
            self.columnconfigure(j, minsize=500 if j == 0 else (100 if j < 3 else 240), weight=1)
        init_text = ("PM MODE\nHÃY CHO MÁY PM VÀO!! "
                     if self.pm_mode else "RUN MODE\nSẴN SÀNG CHẠY")
        self.label_image_1 = ResultImageCanvas(self, init_text)
        self.label_image_1.grid(row=0, column=0, rowspan=6, columnspan=3, sticky=tk.NSEW)
        self.label1 = tk.Label(self, text="RESULT", font=("Arial Bold", 15),
                               borderwidth=1, relief="solid", bg="yellow")
        self.label1.grid(row=0, column=3, sticky=tk.NSEW)
        self.label2 = tk.Label(self, text="---", font=("Arial", 38), bg="orange")
        self.label2.grid(row=1, column=3, sticky=tk.NSEW)
        self.label3 = tk.Label(self, text="NG POINT", font=("Arial Bold", 15),
                               borderwidth=1, relief="solid", bg="yellow")
        self.label3.grid(row=2, column=3, sticky=tk.NSEW)
        self.listNGbox = tk.Listbox(self, selectmode=tk.SINGLE, bg="pink", borderwidth=1,
                                    relief="solid", height=15, selectbackground="red",
                                    exportselection=False)
        self.listNGbox.grid(row=3, column=3, sticky=tk.NSEW)
        self.scroll = tk.Scrollbar(self, orient=tk.VERTICAL, borderwidth=1,
                                   relief="solid", command=self.listNGbox.yview)
        self.listNGbox["yscrollcommand"] = self.scroll.set
        self.scroll.grid(row=3, column=3, sticky=tk.NS + tk.E)
        self.history_label = tk.Label(self, text="BODY NO NG" if SystemConfig.BODY_HISTORY_NG_ONLY else "BODY NO OK / NG",
                                      font=("Arial Bold", 15), borderwidth=1,
                                      relief="solid", bg="yellow")
        self.history_label.grid(row=4, column=3, sticky=tk.NSEW)
        self.history_listbox = tk.Listbox(self, selectmode=tk.SINGLE, borderwidth=1,
                                          relief="solid", exportselection=False)
        self.history_listbox.grid(row=5, column=3, sticky=tk.NSEW)
        self.history_scroll = tk.Scrollbar(self, orient=tk.VERTICAL,
                                           command=self.history_listbox.yview)
        self.history_listbox["yscrollcommand"] = self.history_scroll.set
        self.history_scroll.grid(row=5, column=3, sticky=tk.NS + tk.E)


class StatisticsDetail(ttk.LabelFrame):
    def __init__(self, parent):
        super().__init__(parent, text="Chi tiết", padding=8)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.record = None
        self.image_paths = {}

        self.details = tk.Text(self, height=6, width=1, wrap=tk.WORD,
                               font=("Arial", 11), padx=6, pady=4, state=tk.DISABLED)
        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.details.yview)
        self.details.configure(yscrollcommand=scrollbar.set)
        self.details.grid(row=0, column=0, sticky=tk.EW)
        scrollbar.grid(row=0, column=1, sticky=tk.NS)

        image_picker = ttk.Frame(self)
        image_picker.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(8, 6))
        ttk.Label(image_picker, text="Ảnh NG").pack(side=tk.LEFT, padx=(0, 8))
        self.open_image_button = ttk.Button(image_picker, text="MỞ ẢNH ĐANG CHỌN",
                                            command=self.open_selected_image, state=tk.DISABLED)
        self.open_image_button.pack(side=tk.RIGHT, padx=(8, 0))
        self.image_combo = ttk.Combobox(image_picker, state="disabled", width=1)
        self.image_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.image_combo.bind("<<ComboboxSelected>>", self.show_image)

        self.image_view = ResultImageCanvas(self, "")
        self.image_view.configure(width=1, height=1)
        self.image_view.grid(row=2, column=0, columnspan=2, sticky=tk.NSEW)
        self.image_view.bind("<Double-Button-1>", self.open_selected_image)
        self.image_view.show_text("Chọn một lượt kiểm tra", font=("Arial", 12))

    def show_record(self, record, shift_name):
        if record == self.record:
            return
        previous_image = ""
        if record and self.record and record["id"] == self.record["id"]:
            previous_image = self.image_combo.get()
        self.record = record
        self.image_paths = {}
        self.image_combo.set("")
        self.image_combo.configure(values=(), state="disabled")
        self.open_image_button.configure(state=tk.DISABLED)

        self.details.configure(state=tk.NORMAL)
        self.details.delete("1.0", tk.END)
        if record:
            self.details.insert("1.0",
                                f"Lần kiểm tra: {record['check_no']}\n"
                                f"Ca: {shift_name or '—'}\n"
                                f"Bắt đầu kiểm tra: {record['created_at'] or '—'}\n"
                                f"Cập nhật kết quả: {record['updated_at'] or '—'}\n"
                                f"Danh sách điểm NG: {record['ng_points'] or '—'}")
        self.details.configure(state=tk.DISABLED)
        self.details.yview_moveto(0)
        if record is None:
            self.image_view.show_text("Chọn một lượt kiểm tra", font=("Arial", 12))
            return

        try:
            frames = InspectionFrameStore.query(record["id"])
        except Exception:
            self.image_view.show_text("Không tải được danh sách ảnh NG", font=("Arial", 12), fg="red")
            MAIN_LOG.exception("Không đọc được danh sách ảnh NG: check_id=%s, barcode=%r",
                               record["id"], record["barcode"])
            raise
        if record["image_path"]:
            self.image_paths["Ảnh tổng hợp"] = record["image_path"]
        for frame in frames:
            camera_position = CameraKey.point_group_key(frame["camera"], frame["vitri"])
            self.image_paths[f"{camera_position} | Ảnh {frame['frame_no']}"] = frame["image_path"]
        if not self.image_paths:
            self.image_view.show_text("Không có ảnh NG", font=("Arial", 12))
            return

        self.image_combo.configure(values=tuple(self.image_paths), state="readonly")
        self.image_combo.set(previous_image if previous_image in self.image_paths
                             else next(iter(self.image_paths)))
        self.open_image_button.configure(state=tk.NORMAL)
        self.show_image()

    def show_image(self, event=None):
        path = self.image_paths.get(self.image_combo.get())
        if path is None:
            return
        try:
            with Image.open(path) as image:
                preview = image.convert("RGB")
        except OSError as exc:
            self.image_view.show_text("Không đọc được ảnh NG", font=("Arial", 12), fg="red")
            MAIN_LOG.exception("Không đọc được ảnh NG: check_id=%s, barcode=%r, path=%r",
                               self.record["id"], self.record["barcode"], path)
            raise OSError(f"Không đọc được ảnh NG: check_id={self.record['id']}, path={path!r}") from exc
        self.image_view.show_image(preview)

    def open_selected_image(self, event=None):
        path = self.image_paths.get(self.image_combo.get())
        if path is None:
            return "break"
        try:
            os.startfile(path)
        except OSError as exc:
            error = (f"Không mở được ảnh NG: check_id={self.record['id']}, "
                     f"path={path!r}, error={exc}")
            MAIN_LOG.exception(error)
            messagebox.showerror("Lỗi mở ảnh NG", error, parent=self.winfo_toplevel())
            raise OSError(error) from exc
        return "break"


class StationInfor(tk.Frame):
    def __init__(self, parent, pm_mode):
        super().__init__(parent, bd=2, relief="groove")
        self.pm_mode = pm_mode
        self.grid(sticky="nsew")
        body_count = max(1, len(SystemConfig.BARCODE_CAMERAS))
        checkpoint_col = 2 + body_count
        column_widths = [120, 100] + [120] * body_count + [140, 120, 120, 120]
        for i, width in enumerate(column_widths):
            self.columnconfigure(i, minsize=width, weight=1)
        self.rowconfigure(0, minsize=50, weight=1)
        self.rowconfigure(1, minsize=50, weight=1)

        md = "-"
        if hasattr(parent, 'recipe') and parent.recipe.current_model_display:
            md = f"{parent.recipe.current_model_display} (D5={parent.recipe.current_plc_program})"

        # (tên thuộc tính, text, cột, hàng, màu chữ, cỡ chữ, in đậm)
        widgets = [
            ("label_1", "Unit:", 0, 0, "orange", 20, False),
            ("label_4", f"{md}", 1, 0, "black", 20, True),
            ("label_3", "Time:", 0, 1, "orange", 20, False),
            ("label_6", "0,00 s", 1, 1, "black", 20, False),
            ("label_20", "CHECKPOINT", checkpoint_col, 0, "orange", 17, False),
            ("label_7", "0", checkpoint_col, 1, "black", 20, False),
            ("label_8", "Q'ty OK", checkpoint_col + 1, 0, "orange", 20, False),
            ("label_11", "0", checkpoint_col + 1, 1, "green", 20, False),
            ("label_9", "Q'ty NG", checkpoint_col + 2, 0, "red", 20, False),
            ("label_12", "0", checkpoint_col + 2, 1, "red", 20, False),
            ("label_10", "Total", checkpoint_col + 3, 0, "orange", 20, False),
            ("label_13", "0", checkpoint_col + 3, 1, "blue", 20, False),
        ]
        for attr, txt, col, row, fg, fs, bold in widgets:
            font = ("Arial", fs, "bold") if bold else ("Arial", fs)
            lbl = tk.Label(self, text=txt, font=font, fg=fg)
            lbl.grid(column=col, row=row, sticky=tk.NSEW)
            setattr(self, attr, lbl)
        self.body_no_labels = []
        for index in range(body_count):
            heading = tk.Label(self, text=f"BODY NO{index + 1}", font=("Arial", 17), fg="orange")
            heading.grid(column=2 + index, row=0, sticky=tk.NSEW)
            value = tk.Label(self, text="-", font=("Arial", 20))
            value.grid(column=2 + index, row=1, sticky=tk.NSEW)
            self.body_no_labels.append(value)

    def show_body_numbers(self, body_numbers):
        for index, label in enumerate(self.body_no_labels):
            code = body_numbers[index] if index < len(body_numbers) else None
            label.config(text=code or "-", fg="blue" if code else "black")


class DynamicButtonFrame(tk.Frame):
    BUTTON_ROWS = 2

    def __init__(self, parent, pm_mode):
        super().__init__(parent, bd=5, relief="groove")
        self.grid(sticky="nsew")
        self.pm_mode = pm_mode
        self.selected_cam_buttons = []
        self.buttons = {}
        self.parent_app = parent
        for i in range(11):
            self.columnconfigure(i, minsize=10, weight=1)
        self.rowconfigure(0, minsize=10, weight=1)
        self.rowconfigure(1, minsize=10, weight=1)

    def rebuild_buttons(self, all_cam_keys, active_mode_keys=None):
        for w in self.winfo_children():
            w.destroy()
        self.buttons.clear()
        self.selected_cam_buttons.clear()
        if active_mode_keys is None:
            active_mode_keys = set(all_cam_keys)
        style = {"fg": "yellow", "width": 18, "height": 2, "bd": 2, "relief": "flat",
                 "font": ("Times New Roman", 11, "bold"), "highlightthickness": 0, "takefocus": 0}
        for idx, key in enumerate(all_cam_keys):
            active = key in active_mode_keys
            btn = tk.Button(self, text=key,
                            bg="GREEN" if active else "GRAY",
                            activebackground="#27ae60" if active else "GRAY",
                            activeforeground="white",
                            cursor="hand2" if active else "arrow",
                            state=tk.NORMAL if active else tk.DISABLED,
                            **style,
                            command=lambda k=key: self._show_cam(k))
            btn.grid(column=idx // self.BUTTON_ROWS, row=idx % self.BUTTON_ROWS,
                     sticky=tk.NSEW, padx=2, pady=2)
            self.buttons[key] = btn
        total = len(all_cam_keys)
        ng_pos = total % self.BUTTON_ROWS
        if self.parent_app.PM:
            col, row, rspan = (total + self.BUTTON_ROWS - 1) // self.BUTTON_ROWS, 0, 1
        elif ng_pos == 0:
            col, row, rspan = total // self.BUTTON_ROWS, 0, self.BUTTON_ROWS
        else:
            col, row, rspan = total // self.BUTTON_ROWS, ng_pos, 1
        self.btn_xem_ng = tk.Button(self, text="XEM ẢNH NG", fg="white", bg="red",
                                    font=("Arial Bold", 13), width=15, height=1,
                                    takefocus=0, command=self.show_ng_frames)
        self.btn_xem_ng.grid(column=col, row=row, rowspan=rspan, sticky=tk.NSEW)
        if self.parent_app.PM:
            self.btn_pm_ok = tk.Button(self, text="PM OK", fg="white", bg="blue",
                                       font=("Arial Bold", 13), width=15, height=1,
                                       takefocus=0, command=self.parent_app.toggle_pm_mode)
            self.btn_pm_ok.grid(column=col, row=row + 1, sticky=tk.NSEW)

    def _clear_cam_button_highlight(self):
        for w, bg, fg, abg, afg, rlf in self.selected_cam_buttons:
            w.config(bg=bg, fg=fg, activebackground=abg, activeforeground=afg, relief=rlf)
        self.selected_cam_buttons.clear()

    def _highlight_cam_button(self, name):
        self._clear_cam_button_highlight()
        w = self.buttons.get(name)
        if not w or w.cget("state") == tk.DISABLED:
            return
        self.selected_cam_buttons.append((w, w.cget("bg"), w.cget("fg"),
                                          w.cget("activebackground"),
                                          w.cget("activeforeground"), w.cget("relief")))
        w.config(bg="white", fg="black", activebackground="white", activeforeground="black")

    def set_cam_button_color(self, key, bg, fg):
        w = self.buttons.get(key)
        if not w or w.cget("state") == tk.DISABLED:
            return
        for i, (sw, *_) in enumerate(self.selected_cam_buttons):
            if sw is w:
                self.selected_cam_buttons[i] = (w, bg, fg, w.cget("activebackground"),
                                                w.cget("activeforeground"), w.cget("relief"))
                w.config(bg="white", fg="black", activebackground="white", activeforeground="black")
                return
        w.config(bg=bg, fg=fg)

    def _show_cam(self, name):
        if self.parent_app.selected_body_history is not None:
            self.parent_app.show_body_history_camera(name)
            return
        self._highlight_cam_button(name)
        try:
            with self.parent_app.logical_frames_lock:
                frame = self.parent_app.logical_frames.get(name)
                roi_view = self.parent_app.logical_roi_views.get(name)
            if frame is None:
                print(f"Image is unavailable: {name}")
                return
            frame = frame.copy()
            cv2.putText(frame, name, (100, 100), cv2.FONT_HERSHEY_COMPLEX,
                        1, (0, 0, 255), 1, cv2.LINE_AA)
            self.parent_app.widget_result_check.label_image_1.show_frame(
                frame, roi_views=[roi_view] if roi_view is not None else [])
            self.parent_app.current_cam_name = name
            self.parent_app.last_display_mode = "cam"
        except Exception:
            MAIN_LOG.error("_show_cam exception", exc_info=True)

    def show_ng_frames(self):
        self._clear_cam_button_highlight()
        if self.parent_app.selected_body_history is not None:
            self.parent_app.show_body_history_grid()
            return
        try:
            with self.parent_app.ng_frames_lock:
                snap = self.parent_app.get_run_ng_frames()
            if not snap:
                self.parent_app.widget_result_check.label_image_1.show_text(
                    image="", text="Không có ảnh trong lần kiểm tra này",
                    font=("Arial", 30), fg="gray")
                self.parent_app.last_display_mode = "text"
                return
            g, roi_views = self.parent_app.build_ng_grid(snap)
            if g is not None:
                self.parent_app.widget_result_check.label_image_1.show_frame(g, roi_views=roi_views)
                self.parent_app.last_display_mode = "ng_grid"
        except Exception:
            MAIN_LOG.error("show_ng_frames exception", exc_info=True)


class BarcodeTimeoutError(TimeoutError):
    pass


# ================= MAIN APPLICATION =================
class MainApplication(tk.Tk):
    def __init__(self):
        startup_started_at = time.perf_counter()
        super().__init__()
        MAIN_LOG.info("Startup timing: Tk initialization %.3fs", time.perf_counter() - startup_started_at)
        step_started_at = time.perf_counter()
        self.recipe = RecipeManager()
        MAIN_LOG.info("Startup timing: recipe directory scan %.3fs", time.perf_counter() - step_started_at)
        self.total_points = 0
        self.window_title = SystemConfig.WINDOW_TITLE
        step_started_at = time.perf_counter()
        ip_addr = self.get_local_ip()
        MAIN_LOG.info("Startup timing: local IP lookup %.3fs", time.perf_counter() - step_started_at)
        if ip_addr:
            self.window_title = f"{self.window_title} - IP: {ip_addr}"
        self.title(self.window_title)
        self.geometry("1200x900")
        self.grid()
        self.state("zoomed")
        self.rowconfigure(0, minsize=100, weight=1)
        self.rowconfigure(1, minsize=500, weight=1)
        self.rowconfigure(2, minsize=100, weight=1)
        self.columnconfigure(0, minsize=800, weight=1)

        # Mode state
        self.PM = True
        self.is_loading_recipe = False
        self.recipe_loaded = False

        # Camera state
        self.camera_running = True
        self.camera_popup_showing = False

        # UI state
        self.stop = False
        self.timer_active = False
        self.timer_started_at = 0.0
        self.timer_lock = threading.Lock()
        self.dem = 0
        self.pointcheck = 0
        self.current_cam_name = None

        # Cycle buffers
        self.ng_cams = []
        self.list_ng = []
        self.logical_frames = {}
        self.logical_roi_views = {}
        self.history_ng_frames = []
        self.ng_frame_save_futures = []

        # Barcode
        self.databacode = None
        self.current_barcode = None
        self.current_check_no = None
        self.body_history_records = []
        self.visible_body_history_records = []
        self.selected_body_history = None
        self.body_history_frames = {}
        self.body_history_frame_indices = {}
        self.body_history_cam_name = None
        self._barcode_counter = 0
        self._barcode_date = datetime.now().date()

        # Display cache
        self.last_display_mode = None
        self._last_image_update_ts = 0.0
        self.crop_viewer = None

        # Locks
        self.counter_lock = threading.Lock()
        self.camera_state_lock = threading.Lock()
        self.camera_open_lock = threading.Lock()
        self.frame_lock = threading.Lock()
        self.frame_condition = threading.Condition(self.frame_lock)
        self.ng_frames_lock = threading.RLock()
        self.logical_frames_lock = threading.RLock()
        self.cycle_state_lock = threading.RLock()
        self.stats_lock = threading.RLock()
        self.plc_lock = threading.RLock()
        self.plc_io_lock = threading.RLock()
        self.plc_init_lock = threading.Lock()
        self.queue_put_lock = threading.Lock()
        self.allow_put_lock = threading.Lock()
        self.futures_lock = threading.Lock()
        self.save_tasks_lock = threading.Lock()
        self.pending_save_tasks = 0

        # Queues
        self.queue_xl_anh = queue.Queue()
        self.ui_queue = queue.Queue()

        # PLC state
        self.plc = None
        self.plc_control_running = threading.Event()
        self.allow_put = False
        self.process_futures = []
        self.queue_put_count_step = 0
        self.queue_put_count_failed = 0

        self.plc_last_d1 = 0
        self.plc_cycle_active = False
        self.plc_cycle_aborted = False
        self.plc_steps_captured = set()
        self.plc_step_in_progress = set()
        self.plc_next_step_index = 0

        self.plc_last_d4 = None
        self.plc_d5_next_send_ts = None
        self.plc_d5_send_lock = threading.Lock()

        # Cycle snapshot
        self.cycle_recipe_name = None
        self.cycle_pm_mode = False
        self.cycle_steps_snapshot = {}
        self.cycle_step_delays = {}
        self.cycle_expected_points = set()
        self.cycle_completed_points = set()
        self.cycle_point_errors = {}
        self.cycle_point_results = {}
        self.cycle_unit_configuration = None
        self.cycle_assessment = None
        self.cycle_render_frames = {}
        self.cycle_finish_lock = threading.Lock()
        self.cycle_counts_written = False
        self.cycle_id = None

        # Statistics UI
        self.statistics_window = None
        self.statistics_tree = None
        self.statistics_detail = None
        self.statistics_rows = {}
        self.statistics_barcode_var = tk.StringVar()
        self.statistics_recipe_var = tk.StringVar(value="Tất cả")
        self.statistics_date_from_var = tk.StringVar()
        self.statistics_date_to_var = tk.StringVar()
        self.statistics_shift_var = tk.StringVar(value="All")
        self.statistics_filter_status_var = tk.StringVar(value="All")
        self.statistics_status_var = tk.StringVar(value="Showing all data")
        self.statistics_total_var = tk.StringVar(value="Tổng: 0")
        self.statistics_ok_var = tk.StringVar(value="OK: 0")
        self.statistics_ng_var = tk.StringVar(value="NG: 0")
        self.set_current_shift_statistics_filter()

        # Cameras
        self.camera_ids = tuple(sorted(int(c[3:]) for c in SystemConfig.CAMERA_LOCATION_REFS))
        unknown_rotation_cameras = set(SystemConfig.CAMERAS_ROTATE_180).difference(
            SystemConfig.CAMERA_LOCATION_REFS)
        if unknown_rotation_cameras:
            raise ValueError(
                f"CAMERAS_ROTATE_180 chứa camera không có trong CAMERA_LOCATION_REFS: {unknown_rotation_cameras!r}")
        self.camera_indices = {cid: -1 for cid in self.camera_ids}
        self.camera_generation = 0
        self.latest_frames = {cid: None for cid in self.camera_ids}
        # Monotonic time when the camera read began; updated atomically with frame.
        self.latest_frame_read_started = {cid: 0.0 for cid in self.camera_ids}
        self.camera_reader_threads = {}
        self.capture_activity = {cid: 0.0 for cid in self.camera_ids}
        self.camera_last_error = {cid: "" for cid in self.camera_ids}
        self.camera_last_seen = set()  # camera thấy được ở lần quét gần nhất
        self._camera_stop_event = threading.Event()  # đánh thức các luồng camera khi đóng app

        # Executors
        self.executor_capture = ThreadPoolExecutor(max_workers=SystemConfig.EXECUTOR_CAPTURE_WORKERS)
        self.executor_barcode = ThreadPoolExecutor(max_workers=max(1, len(SystemConfig.BARCODE_CAMERAS)))
        self.executor = ThreadPoolExecutor(max_workers=SystemConfig.EXECUTOR_WORKERS)
        self.executor_ai = ThreadPoolExecutor(max_workers=SystemConfig.AI_MAX_CONCURRENT_WORKERS)
        self.executor_save = ThreadPoolExecutor(max_workers=SystemConfig.EXECUTOR_WORKERS)

        step_started_at = time.perf_counter()
        init_statistics_database(Path(SystemConfig.SAVE_ROOT_DIR) / "save_file_ng")
        MAIN_LOG.info("Startup timing: statistics database ready %.3fs", time.perf_counter() - step_started_at)
        step_started_at = time.perf_counter()
        self._load_barcode_counter()
        MAIN_LOG.info("Startup timing: barcode counter read %.3fs", time.perf_counter() - step_started_at)
        step_started_at = time.perf_counter()
        self.make_file()
        MAIN_LOG.info("Startup timing: production file read/write %.3fs", time.perf_counter() - step_started_at)
        self.qty_ok, self.qty_ng, self.qty_all = 0, 0, 0
        step_started_at = time.perf_counter()
        self.resolve()
        MAIN_LOG.info("Startup timing: camera enumeration %.3fs", time.perf_counter() - step_started_at)
        step_started_at = time.perf_counter()
        self.create_menu_bar()
        self._build_widgets(pm_mode=self.PM)
        self._init_ui_handlers()
        MAIN_LOG.info("Startup timing: UI construction %.3fs", time.perf_counter() - step_started_at)

        self.after(100, self._start_camera_readers)
        self.after(20, self.ui_loop)
        threading.Thread(target=self._safe_start_check, name="StartCheckBg", daemon=True).start()
        threading.Thread(target=self._cleanup_old_files, name="OldFileCleanup", daemon=True).start()
        self.after(300, self.show_model_selector)
        self.after_idle(lambda: MAIN_LOG.info(
            "Startup timing: first UI idle %.3fs", time.perf_counter() - startup_started_at))

        MAIN_LOG.info("Startup timing: MainApplication total %.3fs", time.perf_counter() - startup_started_at)
        print("MainApplication initialized")

    # ================= STARTUP BACKGROUND =================
    def _start_camera_readers(self):
        """Khởi động toàn bộ camera đã cấu hình, không phụ thuộc recipe."""
        started_at = time.perf_counter()
        for cid in self.camera_ids:
            self._start_camera_reader(cid)
        MAIN_LOG.info("Startup timing: camera reader threads %.3fs", time.perf_counter() - started_at)

    def _start_camera_reader(self, cam_id):
        """Mở một camera đã dừng; giữ nguyên reader nếu camera đang chạy."""
        with self.camera_state_lock:
            reader = self.camera_reader_threads.get(cam_id)
        if (reader is not None and reader.is_alive()
                and self.camera_last_error.get(cam_id)):
            reader.join(timeout=SystemConfig.CAMERA_STARTUP_TIMEOUT_PER_CAMERA_SECONDS)
        with self.camera_state_lock:
            if not self.camera_running:
                raise RuntimeError(f"Cannot open CAM{cam_id}: application is closing")
            reader = self.camera_reader_threads.get(cam_id)
            if reader is not None and reader.is_alive():
                return reader
            with self.frame_condition:
                self.latest_frames[cam_id] = None
                self.latest_frame_read_started[cam_id] = 0.0
                self.camera_last_error[cam_id] = ""
            reader = threading.Thread(target=self.camera_reader, args=(cam_id,),
                                      daemon=True, name=f"CamReader_{cam_id}")
            self.camera_reader_threads[cam_id] = reader
            reader.start()
            return reader

    def _ensure_cameras_ready(self, cam_ids):
        """Tránh nạp recipe và chụp step cùng mở lại một camera."""
        with self.camera_open_lock:
            self._open_required_cameras(cam_ids)

    def _open_required_cameras(self, cam_ids):
        """Chỉ mở lại camera cần dùng đang thiếu ảnh; lỗi thì báo popup và raise."""
        needed = set(cam_ids)
        if not needed:
            return
        failed = set(needed)
        try:
            unknown = needed.difference(self.camera_ids)
            if unknown:
                failed = unknown
                raise ValueError(f"Cameras {sorted(unknown)} are not configured in CAMERA_LOCATION_REFS")
            with self.camera_state_lock:
                readers = {cid: self.camera_reader_threads.get(cid) for cid in needed}
                stopped = {cid for cid, reader in readers.items()
                           if reader is None or not reader.is_alive()}
            with self.frame_lock:
                failed = stopped | {cid for cid in needed if self.latest_frames[cid] is None}
            if not failed:
                return

            if stopped:
                self._rescan_cameras_blocking()
                for cid in sorted(stopped):
                    readers[cid] = self._start_camera_reader(cid)
            readers = {cid: readers[cid] for cid in failed}
            timeout = SystemConfig.CAMERA_STARTUP_TIMEOUT_PER_CAMERA_SECONDS
            deadline = time.monotonic() + timeout
            with self.frame_condition:
                while self.camera_running:
                    failed = {cid for cid, reader in readers.items()
                              if not reader.is_alive() or self.latest_frames[cid] is None}
                    if not failed:
                        return
                    pending = {cid for cid in failed
                               if readers[cid].is_alive() and not self.camera_last_error[cid]}
                    if not pending:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.frame_condition.wait(timeout=remaining)
            raise RuntimeError(f"Cameras {sorted(failed)} are not ready; open timeout={timeout}s")
        except Exception as exc:
            reasons = {cid: self.camera_last_error.get(cid) or str(exc) for cid in failed}
            MAIN_LOG.error("Unable to open required cameras %s: %s; reasons=%s",
                           sorted(needed), exc, reasons, exc_info=True)
            if self.camera_running:
                self.ui_queue.put({"type": "camera_startup_error", "cam_ids": sorted(failed),
                                   "reasons": reasons})
            raise RuntimeError(f"Required cameras unavailable: {reasons}") from exc

    def _safe_start_check(self):
        try:
            self.start_check()
        except Exception:
            print("ERROR: start_check failed:")
            MAIN_LOG.error("start_check exception", exc_info=True)

    def _cleanup_old_files(self):
        started_at = time.perf_counter()
        MAIN_LOG.info("Startup timing: old file cleanup started")
        try:
            self.xoafile_tatca()
        except Exception:
            print("ERROR: xoafile_tatca failed:")
            MAIN_LOG.error("xoafile_tatca exception", exc_info=True)
        finally:
            MAIN_LOG.info("Startup timing: old file cleanup finished in %.3fs", time.perf_counter() - started_at)

    # ================= CAMERA STATUS TEXT HELPERS =================
    def _set_camera_status_text(self, text, color="blue"):
        def _do():
            try:
                if hasattr(self, "widget_result_check"):
                    self.widget_result_check.label_image_1.show_text(
                        text=text, font=("Arial", 30), fg=color)
                    self.last_display_mode = "text"
            except Exception:
                pass

        try:
            self.after(0, _do)
        except Exception:
            pass

    def _restore_normal_display(self):
        try:
            if not hasattr(self, "widget_result_check"):
                return
            if self.selected_body_history is not None:
                return
            if self.is_loading_recipe:
                return
            if getattr(self, "plc_cycle_active", False):
                return
            if self.PM:
                txt = "PM MODE\nHÃY CHO MÁY PM VÀO!!"
            else:
                txt = "RUN MODE\nSẴN SÀNG CHẠY."
            self.widget_result_check.label_image_1.show_text(
                text=txt, font=("Arial", 30), fg="blue")
            self.last_display_mode = "text"
        except Exception:
            pass

    def _lazy_init_cameras_for_recipe(self):
        needed = set(self.recipe.get_needed_cam_ids())
        if not SystemConfig.should_skip_barcode(self.recipe.current_model_display):
            needed.update(int(CameraKey.cam_token(camera))
                          for camera, _ in SystemConfig.BARCODE_CAMERAS)
        with self.camera_state_lock:
            self.camera_generation += 1
            generation = self.camera_generation
        if not needed:
            print("WARN: Recipe has no cameras")
            self._set_camera_status_text(
                "UNIT KHÔNG DÙNG CAMERA", color="orange")
            self.after(1500, self._restore_normal_display)
            return

        total = len(needed)
        print(f"Recipe requires {total} cameras: {sorted(needed)}")
        self._set_camera_status_text(f"⏳ ĐANG MỞ CAMERA... ({total} camera)", color="orange")
        try:
            self._ensure_cameras_ready(needed)
        except RuntimeError:
            # _ensure_cameras_ready đã gửi lỗi tới message box trên luồng UI.
            self.after(0, self._restore_normal_display)
            return
        else:
            status_text = f"✅ ĐÃ MỞ CAMERA\n({total}/{total} camera)"
            status_color = "green"
        if not self.camera_running:
            return
        with self.camera_state_lock:
            if generation != self.camera_generation:
                return
        self._set_camera_status_text(status_text, color=status_color)
        self.after(1500, self._restore_normal_display)

    def open_recipe_cameras(self):
        if not self.recipe_loaded or self.is_loading_recipe:
            return
        threading.Thread(target=self._lazy_init_cameras_for_recipe,
                         name="LazyCamInit", daemon=True).start()

    # ================= HELPERS =================
    @staticmethod
    def _sanitize_pairs(pairs):
        if not isinstance(pairs, (list, tuple)):
            return []
        return [(p[0], p[1]) for p in pairs
                if isinstance(p, (list, tuple)) and len(p) >= 2]

    @staticmethod
    def _safe_filename_part(s, maxlen=60):
        return re.sub(r'[\\/:*?"<>|\s]+', '_', str(s))[:maxlen]

    def _submit_save(self, fn, *args):
        with self.save_tasks_lock:
            if self.pending_save_tasks >= SystemConfig.MAX_PENDING_SAVE_TASKS:
                print("WARN: Save queue is full; dropping one task")
                return
            self.pending_save_tasks += 1

        def _w():
            try:
                return fn(*args)
            finally:
                with self.save_tasks_lock:
                    self.pending_save_tasks = max(0, self.pending_save_tasks - 1)

        try:
            return self.executor_save.submit(_w)
        except RuntimeError as exc:
            with self.save_tasks_lock:
                self.pending_save_tasks = max(0, self.pending_save_tasks - 1)
            raise RuntimeError(f"Không thể gửi tác vụ lưu ảnh: function={fn.__name__}, error={exc}") from exc

    def _point_key(self, std_key, point_id):
        return f"{std_key}_D{point_id}"

    def _build_expected_points(self, steps_snapshot):
        expected = set()
        for step, pairs in (steps_snapshot or {}).items():
            for cam_name, vitri in self._sanitize_pairs(pairs):
                sk = CameraKey.point_group_key(cam_name, vitri)
                for pt in self.recipe.points_cfg.get(sk, []):
                    expected.add(self._point_key(sk, pt.get("POINT", "?")))
        return expected

    def _register_point_result(self, pk, result, cycle_id=None):
        with self.cycle_state_lock:
            if cycle_id is not None and cycle_id != self.cycle_id:
                return False
            self.cycle_point_results[pk] = result
            self.cycle_completed_points.add(pk)
            if result and len(result) >= 8 and result[6] == "error":
                self.cycle_point_errors[pk] = result[7] or result[2]
            return True

    def _register_point_error(self, pk, err):
        with self.cycle_state_lock:
            self.cycle_point_errors[pk] = str(err)

    @staticmethod
    def _is_point_ok(point, name, probability, model_type):
        if model_type == "error":
            return False
        if model_type == "yolo":
            return "OK" in name.upper() and probability > point.get("THRESHOLD", 0.85)
        return "OK" in name.upper()

    def get_cycle_ng_points(self):
        with self.cycle_state_lock:
            if self.cycle_assessment is not None:
                return self.cycle_assessment["ng_points"]
            return ()

    def _validate_cycle_points(self):
        with self.cycle_state_lock:
            results = dict(self.cycle_point_results)
            errs = dict(self.cycle_point_errors)
        try:
            assessment = CycleUnitEvaluator(self.cycle_unit_configuration).evaluate(
                results, self._is_point_ok)
            for key, error in errs.items():
                if assessment["points"].get(key) != "SKIPPED":
                    raise ValueError(f"{key}: {error}")
        except ValueError as error:
            return False, str(error)
        with self.cycle_state_lock:
            self.cycle_assessment = assessment
        return True, ""

    def _is_system_busy(self):
        if self.is_loading_recipe:
            return True, "Đang nạp Recipe"
        if self.plc_cycle_active:
            return True, "Đang trong chu trình PLC"
        with self.futures_lock:
            if self.process_futures:
                return True, "Đang xử lý ảnh chu trình trước"
            if not self.queue_xl_anh.empty():
                return True, "Còn ảnh trong queue"
        return False, ""

    # ================= SHIFT / CLEANUP =================
    @staticmethod
    def _get_shift_and_date():
        now = datetime.now()
        ct = now.time()
        if dt_time(7, 0) <= ct <= dt_time(19, 45):
            shift = "ca_ngay"
        elif ct >= dt_time(20, 0) or ct <= dt_time(6, 30):
            shift = "ca_dem"
        else:
            shift = "ngoai_ca"
        if shift == "ca_dem" and ct <= dt_time(6, 30):
            sd = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            sd = now.strftime("%Y-%m-%d")
        return shift, sd, now

    def _get_dynamic_save_path(self, cat, cam, vitri, point=None):
        shift, sd, now = self._get_shift_and_date()
        mn = getattr(self.recipe, 'current_model_display', None) or 'DEFAULT_MODEL'
        parts = [SystemConfig.SAVE_ROOT_DIR, cat, mn, sd, shift, f"cam{cam}", f"vitri{vitri}"]
        if point is not None:
            parts.append(f"D{point}")
        fp = os.path.join(*parts)
        os.makedirs(fp, exist_ok=True)
        return fp, now

    def make_file(self):
        with self.stats_lock:
            data = self._read_production_data()
            self._write_production_data(data)

    def _read_production_data(self):
        shift, sd, _ = self._get_shift_and_date()
        path = SystemConfig.SANLUONG_FILE
        empty_data = {"date": sd, "shift": shift, "recipes": {}}
        try:
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                return empty_data
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Unable to read production file '{path}': {e}") from e
        if (not isinstance(data, dict) or not isinstance(data.get("date"), str)
                or not isinstance(data.get("shift"), str)
                or not isinstance(data.get("recipes"), dict)):
            raise ValueError(f"Production file '{path}' has an invalid date/shift/recipes structure")
        for recipe_name, counts in data["recipes"].items():
            if (not isinstance(recipe_name, str) or not isinstance(counts, dict)
                    or any(type(counts.get(key)) is not int or counts[key] < 0
                           for key in ("ok", "ng", "all"))
                    or counts["all"] != counts["ok"] + counts["ng"]):
                raise ValueError(f"Production file '{path}' has an invalid counter for recipe '{recipe_name}'")
        if data["date"] != sd or data["shift"] != shift:
            return empty_data
        return data

    def _write_production_data(self, data):
        path = SystemConfig.SANLUONG_FILE
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            raise RuntimeError(f"Unable to write production file '{path}': {e}") from e

    def _body_history_directory(self, recipe_name, production_date, shift):
        return os.path.join(SystemConfig.SAVE_ROOT_DIR, "save_file_ng",
                            recipe_name, production_date, shift)

    def load_body_history(self):
        recipe_name = self.recipe.current_model_display
        self.body_history_records = []
        if not recipe_name:
            self.refresh_body_history_display()
            return
        shift_info = ShiftSchedule.get_shift_info()
        self.body_history_records = query_body_history(
            recipe_name, shift_info["date"], shift_info["shift"])
        self.refresh_body_history_display()

    def refresh_body_history_display(self):
        history_listbox = self.widget_result_check.history_listbox
        history_listbox.delete(0, tk.END)
        self.visible_body_history_records = []
        for record in self.body_history_records:
            if SystemConfig.BODY_HISTORY_NG_ONLY and record["result"] != "NG":
                continue
            self.visible_body_history_records.append(record)
            history_listbox.insert(tk.END,
                                   f"{record['time']} | {record['barcode']} | "
                                   f"{record['result']}")
            history_listbox.itemconfig(tk.END, fg="red" if record["result"] == "NG" else "green")
        if self.selected_body_history is not None:
            for index, record in enumerate(self.visible_body_history_records):
                if record["id"] == self.selected_body_history["id"]:
                    history_listbox.selection_set(index)
                    break
            else:
                self.clear_body_history_selection()
                self.widget_result_check.label_image_1.show_text("Chọn Body No để xem ảnh lịch sử")

    def save_body_history_image(self, barcode, recipe_name, ng_grid):
        if ng_grid is None:
            raise ValueError(f"Thiếu ảnh NG để lưu lịch sử: barcode={barcode!r}, recipe={recipe_name!r}")
        shift, production_date, now = self._get_shift_and_date()
        history_dir = self._body_history_directory(recipe_name, production_date, shift)
        image_dir = os.path.join(history_dir, "history_images")
        os.makedirs(image_dir, exist_ok=True)
        image_name = f"{self._safe_filename_part(barcode, 120)}_{now:%H%M%S_%f}.jpg"
        full_image_path = os.path.abspath(os.path.join(image_dir, image_name))
        if not cv2.imwrite(full_image_path, ng_grid):
            raise IOError(f"Unable to save NG history image: path={full_image_path}, barcode={barcode}")
        return full_image_path

    def show_selected_body_history(self, event=None):
        selected = self.widget_result_check.history_listbox.curselection()
        if not selected:
            return
        record = self.visible_body_history_records[selected[0]]
        if self.selected_body_history is not None and self.selected_body_history["id"] == record["id"]:
            return
        frames = InspectionFrameStore.query(record["id"])
        self.selected_body_history = record
        self.body_history_frames = {}
        self.body_history_frame_indices = {}
        self.body_history_cam_name = None
        for frame in frames:
            key = CameraKey.point_group_key(frame["camera"], frame["vitri"])
            self.body_history_frames.setdefault(key, []).append(frame)
        self.rebuild_button_grid()
        self.show_body_history_grid()

    def read_body_history_image(self, image_path):
        record = self.selected_body_history
        frame = cv2.imread(image_path) if image_path else None
        if frame is None:
            error_text = (f"Không đọc được ảnh lịch sử: path={image_path!r}, "
                          f"barcode={record['barcode']!r}, check_no={record['check_no']}")
            self.widget_result_check.label_image_1.show_text("Không đọc được ảnh lịch sử", fg="red")
            messagebox.showerror("Lỗi ảnh lịch sử", error_text, parent=self)
            raise FileNotFoundError(error_text)
        return frame

    def show_body_history_grid(self):
        record = self.selected_body_history
        if record["result"] == "OK":
            self.widget_result_check.label_image_1.show_text(
                f"BODY NO {record['barcode']} OK\nKhông có ảnh NG", fg="green")
            return
        frame = self.read_body_history_image(record["image_path"])
        self.widget_result_check.label_image_1.show_frame(frame)
        self.last_display_mode = "history_grid"

    def show_body_history_camera(self, name):
        record = self.selected_body_history
        frames = self.body_history_frames.get(name, [])
        if not frames:
            self.widget_result_check.label_image_1.show_text(
                f"Body No {record['barcode']}\n{name}: Không có ảnh NG")
            return
        index = (self.body_history_frame_indices.get(name, -1) + 1) % len(frames)
        frame = self.read_body_history_image(frames[index]["image_path"])
        self._label_cell(frame, f"{record['barcode']} | CHECK {record['check_no']} | {name} | {index + 1}/{len(frames)}")
        self.body_history_frame_indices[name] = index
        self.body_history_cam_name = name
        self.widget_button._highlight_cam_button(name)
        self.widget_result_check.label_image_1.show_frame(frame)
        self.last_display_mode = "history_cam"

    def clear_body_history_selection(self):
        if self.selected_body_history is None:
            return
        self.selected_body_history = None
        self.body_history_frames = {}
        self.body_history_frame_indices = {}
        self.body_history_cam_name = None
        self.widget_result_check.history_listbox.selection_clear(0, tk.END)
        self.rebuild_button_grid()
        with self.cycle_state_lock:
            ng_cams = tuple(self.ng_cams)
        for key in ng_cams:
            self.widget_button.set_cam_button_color(key, "violet", "red")

    def docfile(self, recipe_name):
        if not recipe_name:
            raise ValueError("Select a recipe before reading production data")
        with self.stats_lock:
            counts = self._read_production_data()["recipes"].get(recipe_name)
            if counts is None:
                counts = {"ok": 0, "ng": 0, "all": 0}
            self.qty_ok = counts["ok"]
            self.qty_ng = counts["ng"]
            self.qty_all = counts["all"]

    def ghifile(self, recipe_name, delta_ok, delta_ng):
        if not recipe_name:
            raise ValueError("Select a recipe before writing production data")
        if any(type(value) is not int or value < 0 for value in (delta_ok, delta_ng)):
            raise ValueError(f"Invalid production quantities: ok={delta_ok!r}, ng={delta_ng!r}")
        with self.stats_lock:
            data = self._read_production_data()
            counts = data["recipes"].get(recipe_name, {"ok": 0, "ng": 0, "all": 0}).copy()
            counts["ok"] += delta_ok
            counts["ng"] += delta_ng
            counts["all"] += delta_ok + delta_ng
            data["recipes"][recipe_name] = counts
            self._write_production_data(data)
            return counts

    def kiemtra_xoafile(self, cat):
        cp = os.path.join(SystemConfig.SAVE_ROOT_DIR, cat)
        os.makedirs(cp, exist_ok=True)
        retention_days = 70 if cat == "save_crop_all" else 30
        now = datetime.now()
        for model in os.listdir(cp):
            mp = os.path.join(cp, model)
            if not os.path.isdir(mp):
                continue
            for df in os.listdir(mp):
                dp = os.path.join(mp, df)
                if not os.path.isdir(dp):
                    continue
                try:
                    try:
                        ft = datetime.strptime(df, "%Y-%m-%d")
                    except ValueError:
                        ft = datetime.fromtimestamp(os.path.getmtime(dp))
                    if now - ft > timedelta(days=retention_days):
                        shutil.rmtree(dp)
                        print(f"Deleted: {dp}")
                except Exception:
                    pass

    def xoafile_tatca(self):
        for cat in SystemConfig.SAVE_CATEGORIES:
            self.kiemtra_xoafile(cat)

    # ================= CAMERA =================
    def resolve(self, keep_valid_on_miss=False):
        cams = enumerate_cameras()
        l2i = {}
        for c in cams:
            loc = (c["location_id"] or "").strip().upper()
            if loc and loc != "N/A":
                l2i[loc] = c["index"]
        found = {}
        for name, ref in SystemConfig.CAMERA_LOCATION_REFS.items():
            key = (ref or "").strip().upper()
            found[int(name[3:])] = l2i.get(key, -1)
        taken = {idx for idx in found.values() if idx >= 0}
        res = {}
        with self.camera_state_lock:
            for cid, idx in found.items():
                old = self.camera_indices[cid]
                if idx < 0 and keep_valid_on_miss and old >= 0 and old not in taken:
                    idx = old
                res[f"cam{cid}"] = idx
                if old != idx:
                    self.camera_indices[cid] = idx
                    with self.frame_lock:
                        self.latest_frames[cid] = None
                        self.latest_frame_read_started[cid] = 0.0
                    print(f"Resolved camera cam{cid}: {old} -> {idx}")
            self.camera_last_seen = {cid for cid, i in found.items() if i >= 0}
        return res

    def _rescan_cameras_blocking(self, timeout=10.0):
        if threading.current_thread() is threading.main_thread():
            self.resolve()
            return True
        ev = threading.Event()
        request = {"type": "camera_rescan", "reason": "open required cameras", "event": ev}
        self.ui_queue.put(request)
        if not ev.wait(timeout):
            raise TimeoutError(f"Camera rescan did not finish on the UI thread within {timeout}s")
        if request.get("error"):
            raise RuntimeError(f"Camera rescan failed: {request['error']}")
        return True

    def _h_camera_rescan(self, d):
        try:
            reason = d.get("reason")
            if reason:
                print(f"Re-scanning cameras ({reason})")
            self.resolve()
        except Exception as exc:
            d["error"] = str(exc)
            MAIN_LOG.error("Camera rescan failed", exc_info=True)
        finally:
            ev = d.get("event")
            if ev is not None:
                ev.set()

    def _menu_show_resolve(self):
        res = self.resolve(keep_valid_on_miss=True)
        with self.camera_state_lock:
            seen = set(self.camera_last_seen)
        with self.frame_lock:
            live = {cid for cid, f in self.latest_frames.items() if f is not None}
        lines = ["Kết nối camera hiện tại:\n"]
        for k, v in sorted(res.items(), key=lambda kv: int(kv[0][3:])):
            cid = int(k[3:])
            if cid in seen:
                st, idx_txt = "✅", f"chỉ số {v}"
            elif v >= 0:
                st, idx_txt = "⚠️", f"chỉ số {v} (không thấy khi quét, giữ chỉ số cũ)"
            else:
                st, idx_txt = "❌", "không tìm thấy"
            extra = " | đang bật, " + ("có hình" if cid in live else "CHƯA có hình")
            lines.append(f"{st} {k}: {idx_txt}{extra}")
        messagebox.showinfo("Kết nối camera", "\n".join(lines))

    def close_allcam(self):
        with self.camera_state_lock:
            self.camera_generation += 1
        with self.frame_lock:
            for cid in self.camera_ids:
                self.latest_frames[cid] = None
                self.latest_frame_read_started[cid] = 0.0
        print("Camera closure requested")

    def init_camera(self, idx):
        if idx is None or idx < 0:
            return None
        cap = None
        try:
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
            if not cap.isOpened():
                cap.release()
                return None
            if SystemConfig.CAMERA_FOURCC:
                cap.set(cv2.CAP_PROP_FOURCC,
                        cv2.VideoWriter_fourcc(*SystemConfig.CAMERA_FOURCC))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, SystemConfig.CAMERA_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, SystemConfig.CAMERA_HEIGHT)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, SystemConfig.CAMERA_BUFFER_SIZE)
            try:
                aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                if (aw, ah) != (SystemConfig.CAMERA_WIDTH, SystemConfig.CAMERA_HEIGHT):
                    print(f"WARN: Camera index={idx} resolution {aw}x{ah} "
                          f"(requested {SystemConfig.CAMERA_WIDTH}x{SystemConfig.CAMERA_HEIGHT})")
            except Exception:
                pass
            return cap
        except Exception:
            if cap is not None:
                cap.release()
            raise

    def _release_camera(self, cam_id, cap):
        if cap is not None:
            try:
                cap.release()
            except Exception:
                MAIN_LOG.error(f"Failed to release CAM{cam_id}", exc_info=True)
        with self.frame_condition:
            self.latest_frames[cam_id] = None
            self.latest_frame_read_started[cam_id] = 0.0
            self.frame_condition.notify_all()

    def _camera_wait(self, seconds):
        self._camera_stop_event.wait(seconds)

    def camera_reader(self, cam_id):
        """Mở camera một lần và đọc ảnh; gặp lỗi thì dừng, không tự phục hồi."""
        cap = None
        with self.camera_state_lock:
            cam_idx = self.camera_indices[cam_id]
        try:
            if not self.camera_running:
                return
            if cam_idx < 0:
                raise RuntimeError("Không tìm thấy thiết bị (kiểm tra cáp / cổng USB)")
            cap = self.init_camera(cam_idx)
            if cap is None:
                raise RuntimeError("Không mở được camera (đang bị chiếm hoặc lỗi USB)")
            print(f"CAM{cam_id} opened (index={cam_idx})")
            rotate_180 = f"cam{cam_id}" in SystemConfig.CAMERAS_ROTATE_180
            while self.camera_running:
                frame_read_started = time.monotonic()
                ret, frame = cap.read()
                if not ret or frame is None or frame.size == 0:
                    raise RuntimeError(
                        f"Không nhận được hình từ camera: ret={ret}, "
                        f"frame_shape={None if frame is None else frame.shape}")
                if rotate_180:
                    frame = cv2.rotate(frame, cv2.ROTATE_180)
                with self.frame_condition:
                    if not self.camera_running:
                        break
                    self.latest_frames[cam_id] = frame
                    self.latest_frame_read_started[cam_id] = frame_read_started
                    self.camera_last_error[cam_id] = ""
                    self.frame_condition.notify_all()
                active_until = self.capture_activity.get(cam_id, 0.0)
                interval = (SystemConfig.CAMERA_ACTIVE_INTERVAL if time.time() < active_until
                            else SystemConfig.CAMERA_IDLE_INTERVAL)
                self._camera_wait(interval)
        except Exception as exc:
            self.camera_last_error[cam_id] = str(exc)
            MAIN_LOG.error("CAM%s reader stopped (index=%s): %s", cam_id, cam_idx, exc,
                           exc_info=True)
        finally:
            self._release_camera(cam_id, cap)

    # ================= BARCODE =================
    def _load_barcode_counter(self):
        path = SystemConfig.BARCODE_COUNTER_FILE
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (not isinstance(data, dict) or not isinstance(data.get("date"), str)
                    or type(data.get("counter")) is not int or data["counter"] < 0):
                raise ValueError(f"Invalid date/counter structure: {data!r}")
            today = datetime.now().date()
            self._barcode_counter = data["counter"] if data["date"] == today.isoformat() else 0
            self._barcode_date = today
            print(f"Barcode counter: {self._barcode_counter}")
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Unable to load barcode counter: path={path}, error={exc}") from exc

    def _save_barcode_counter(self):
        try:
            data = {"date": self._barcode_date.isoformat(),
                    "counter": self._barcode_counter,
                    "saved_at": datetime.now().isoformat()}
            tmp = SystemConfig.BARCODE_COUNTER_FILE + ".tmp"
            os.makedirs(os.path.dirname(SystemConfig.BARCODE_COUNTER_FILE), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, SystemConfig.BARCODE_COUNTER_FILE)
        except OSError as exc:
            raise RuntimeError(
                f"Unable to save barcode counter: path={SystemConfig.BARCODE_COUNTER_FILE}, "
                f"date={self._barcode_date}, counter={self._barcode_counter}, error={exc}") from exc

    # ================= RECIPE SELECTOR =================
    def show_model_selector(self):
        busy, reason = self._is_system_busy()
        if busy:
            messagebox.showwarning("Không thể đổi cấu hình Unit",
                                   f"{reason}.\nĐợi chu trình xong.")
            return
        columns = SystemConfig.UNIT_SELECTOR_COLUMNS
        if type(columns) is not int or columns not in (2, 4):
            raise ValueError(f"UNIT_SELECTOR_COLUMNS phải là 2 hoặc 4, giá trị hiện tại: {columns!r}")
        w = tk.Toplevel(self)
        w.withdraw()
        w.title("Chọn Unit")
        w.resizable(False, False)
        w.configure(bg="#f0f0f0")
        w.transient(self)

        tk.Label(w, text="CHỌN CỤM UNIT", font=("Arial", 14, "bold"),
                 bg="#f0f0f0").pack(pady=(18, 8))
        keys = list(self.recipe.available_models.keys())
        frame = tk.Frame(w, bg="#f0f0f0", padx=16, pady=12)
        frame.pack(fill="both", expand=True)
        market_buttons = []
        lbl_status = tk.Label(w, text="", font=("Arial", 10, "bold"),
                              bg="#f0f0f0", fg="#e67e22")
        lbl_status.pack(pady=(2, 12))

        def confirm(sel):
            busy2, r2 = self._is_system_busy()
            if busy2:
                messagebox.showwarning("Không thể đổi cấu hình Unit", f"{r2}.",
                                       parent=w)
                return
            self.is_loading_recipe = True
            self.recipe_loaded = False
            for button in market_buttons:
                button.config(state=tk.DISABLED)
            lbl_status.config(text="Đang thiết lập...")
            w.protocol("WM_DELETE_WINDOW", lambda: None)

            def _prog(pct, msg):
                try:
                    self.after(0, lambda: (lbl_status.config(text=msg)
                                           if w.winfo_exists() else None))
                except Exception:
                    pass

            def _bg():
                try:
                    recipe = RecipeManager()
                    self.recipe = recipe
                    gc.collect()
                    recipe.load_recipe(sel, progress_callback=_prog)
                    self.docfile(sel)
                    self.reset_plc_program_sending()
                    self.recipe_loaded = True
                except Exception as exc:
                    MAIN_LOG.error(f"Load recipe '{sel}' failed", exc_info=True)
                    error_text = str(exc)
                    self.recipe = RecipeManager()
                    with self.stats_lock:
                        self.qty_ok, self.qty_ng, self.qty_all = 0, 0, 0
                    gc.collect()

                    def _failed():
                        if not w.winfo_exists():
                            return
                        self.load_body_history()
                        for button in market_buttons:
                            button.config(state=tk.NORMAL)
                        w.protocol("WM_DELETE_WINDOW", w.destroy)
                        lbl_status.config(text="")
                        self.title(self.window_title)
                        self.widget_station_infor.label_4.config(text="Chưa nạp cấu hình")
                        self._h_counter({"ok": 0, "ng": 0, "all": 0})
                        messagebox.showerror("Lỗi nạp cấu hình", error_text, parent=w)

                    self.after(0, _failed)
                    return
                finally:
                    self.is_loading_recipe = False

                def _done():
                    try:
                        if w.winfo_exists():
                            w.destroy()
                    except Exception:
                        pass
                    d5 = self.recipe.current_plc_program
                    self.update_total_points_from_config()
                    self.widget_station_infor.label_4.config(
                        text=f"{sel} (D5={d5})")
                    self.widget_station_infor.label_7.config(text=f"0/{self.total_points}")
                    self._h_counter({"ok": self.qty_ok, "ng": self.qty_ng,
                                     "all": self.qty_all})
                    self.load_body_history()
                    self.rebuild_button_grid()
                    mode = "PM MODE" if self.PM else "RUN MODE"
                    self.title(f"{self.window_title} - {sel} [D5={d5}] - {mode}")
                    self.focus_force()
                    self.open_recipe_cameras()

                self.after(0, _done)

            threading.Thread(target=_bg, name="RecipeLoadBg", daemon=True).start()

        for index, name in enumerate(keys):
            row, column = divmod(index, columns)
            cell = tk.Frame(frame, bg="#f0f0f0", padx=8, pady=8)
            cell.grid(row=row, column=column, sticky="nsew")
            frame.grid_columnconfigure(column, weight=1)
            button = tk.Button(
                cell, text=name, font=("Arial", 14, "bold"),
                bg="#3498db", fg="white", padx=24, pady=20, cursor="hand2",
                command=lambda model=name: confirm(model))
            button.pack(fill="both", expand=True)
            market_buttons.append(button)
            d5 = SystemConfig.get_plc_program_for_model(name)
            tk.Label(cell, text=f"({SystemConfig.PLC_PROG_REG}): {d5}",
                     font=("Arial", 11, "italic"), bg="#f0f0f0", fg="#2c3e50").pack(pady=(6, 0))

        w.update_idletasks()
        width = max(400, w.winfo_reqwidth())
        height = w.winfo_reqheight()
        x = max(0, self.winfo_x() + (self.winfo_width() - width) // 2)
        y = max(0, self.winfo_y() + (self.winfo_height() - height) // 2)
        w.geometry(f"{width}x{height}+{x}+{y}")
        w.deiconify()
        w.grab_set()

    def rebuild_button_grid(self):
        active = set()
        for pairs in self.recipe.plc_steps.values():
            for cn, vt in self._sanitize_pairs(pairs):
                active.add(CameraKey.point_group_key(cn, vt))
        all_cams = sorted(set(self.recipe.points_cfg.keys()) | active)
        if not active:
            active = set(all_cams)
        if self.selected_body_history is not None:
            all_cams = sorted(set(all_cams) | set(self.body_history_frames))
            active = set(self.body_history_frames)
        self.widget_button.rebuild_buttons(all_cams, active)
        if self.selected_body_history is not None:
            for key in active:
                self.widget_button.set_cam_button_color(key, "violet", "red")

    def update_total_points_from_config(self):
        self.total_points = self.recipe.total_points

    # ================= MENU =================
    def create_menu_bar(self):
        mb = tk.Menu(self)
        home = tk.Menu(mb, tearoff=0)
        home.add_command(label="Thống kê(Ctrl + O)",
                         command=self.open_statistics_window)
        home.add_separator()
        home.add_command(label="Kiểm tra kết nối camera",
                         command=self._menu_show_resolve)
        home.add_command(label="Dừng chu trình (Esc)",
                         command=self.emergency_stop_cycle)
        mb.add_cascade(label="Trang chủ", menu=home)

        self.mode_menu = tk.Menu(mb, tearoff=0)
        self.mode_menu.add_command(label="RUN mode", command=self.toggle_pm_mode)
        self.mode_menu.add_command(label="PM mode", command=self.toggle_pm_mode)
        self.update_mode_menu()
        mb.add_cascade(label="MODE", menu=self.mode_menu)

        mb.add_command(label="Đổi Unit", command=self.show_model_selector)

        if SystemConfig.PLC_SIMULATOR_ENABLED:
            self.plc_simulator_menu = tk.Menu(
                mb, tearoff=0, postcommand=self.refresh_plc_simulator_menu)
            self.plc_simulator_menu.add_command(label="Đang khởi tạo simulator...", state=tk.DISABLED)
            self.plc_simulator_menu.add_separator()
            self._add_plc_simulator_shortcut(
                "F1", "D1: Phát xung chụp step", self.pulse_simulator_d1)
            for reg, value, label, shortcut in (
                    (SystemConfig.PLC_CMD_REG, 1, "D1 = 1 (giữ ON)", "F2"),
                    (SystemConfig.PLC_CMD_REG, 0, "D1 = 0 (OFF)", "F3"),
                    (SystemConfig.PLC_READY_REG, 0, "D4 = 0 (yêu cầu gửi D5)", "F4"),
                    (SystemConfig.PLC_READY_REG, 1, "D4 = 1 (dừng yêu cầu D5)", "F5")):
                self._add_plc_simulator_shortcut(
                    shortcut, label, lambda r=reg, v=value: self.set_simulator_signal(r, v))
            self.plc_simulator_menu.add_separator()
            self._add_plc_simulator_shortcut(
                "F6", "Xóa D0 / D3 về 0", self.clear_simulator_outputs)
            mb.add_cascade(label="PLC Simulator", menu=self.plc_simulator_menu)

        self.config(menu=mb)

    # ================= PLC SIMULATOR MENU =================
    def _add_plc_simulator_shortcut(self, shortcut, label, command):
        self.plc_simulator_menu.add_command(label=label, accelerator=shortcut, command=command)
        index = self.plc_simulator_menu.index(tk.END)
        self.bind(f"<{shortcut}>",
                  lambda event, entry=index: self._invoke_plc_simulator_shortcut(entry))

    def _invoke_plc_simulator_shortcut(self, index):
        self.refresh_plc_simulator_menu()
        self.plc_simulator_menu.invoke(index)
        return "break"

    def _get_plc_simulator(self):
        if not SystemConfig.PLC_SIMULATOR_ENABLED or not isinstance(self.plc, PlcSimulator):
            raise RuntimeError("PLC simulator is not ready; enable PLC_SIMULATOR_ENABLED and wait for startup")
        return self.plc

    def refresh_plc_simulator_menu(self):
        with self.plc_io_lock:
            registers = self._get_plc_simulator().snapshot() if self.plc is not None else None
        ready = registers is not None and self.plc_control_running.is_set()
        self.plc_simulator_menu.entryconfigure(
            0, label=(" | ".join(f"{reg}={value}" for reg, value in registers.items())
                      if ready else "Đang khởi tạo simulator..."))
        for index in range(1, self.plc_simulator_menu.index(tk.END) + 1):
            if self.plc_simulator_menu.type(index) == "command":
                self.plc_simulator_menu.entryconfigure(index, state=tk.NORMAL if ready else tk.DISABLED)

    def set_simulator_signal(self, reg, value):
        with self.plc_io_lock:
            self._get_plc_simulator().write_int16(reg, value)

    def pulse_simulator_d1(self):
        with self.plc_io_lock:
            self._get_plc_simulator().pulse_d1()

    def clear_simulator_outputs(self):
        with self.plc_io_lock:
            simulator = self._get_plc_simulator()
            simulator.write_int16(SystemConfig.PLC_DONE_REG, 0)
            simulator.write_int16(SystemConfig.PLC_RESULT_REG, 0)

    # ================= MODE SWITCH =================
    def bind_result_check_events(self):
        lb = self.widget_result_check.listNGbox
        lb.bind("<space>", self.handle_space_switch_cam)
        lb.bind("<ButtonRelease-1>", self.show_image_click_list)
        lb.bind("<<ListboxSelect>>", self.show_image_click_list)
        lb.bind("<Return>", self.show_image_click_list)
        self.widget_result_check.history_listbox.bind(
            "<space>", self.handle_space_switch_cam)
        self.widget_result_check.history_listbox.bind(
            "<<ListboxSelect>>", self.show_selected_body_history)

    def _build_widgets(self, pm_mode):
        self.widget_station_infor = StationInfor(self, pm_mode=pm_mode)
        self.widget_station_infor.grid(row=0, column=0, sticky=tk.NSEW)
        if hasattr(self, 'qty_ok'):
            self.widget_station_infor.label_11.config(text=self.qty_ok)
            self.widget_station_infor.label_12.config(text=self.qty_ng)
            self.widget_station_infor.label_13.config(text=self.qty_all)
        self.widget_result_check = ResultCheck(self, pm_mode=pm_mode)
        self.widget_result_check.grid(row=1, column=0, sticky=tk.NSEW)
        self.bind_result_check_events()
        self.refresh_body_history_display()
        self.widget_button = DynamicButtonFrame(self, pm_mode=pm_mode)
        self.widget_button.grid(row=2, column=0, sticky=tk.NSEW)
        if self.recipe.current_model_display:
            self.rebuild_button_grid()

    def update_mode_menu(self):
        self.mode_menu.entryconfigure("RUN mode", state=tk.NORMAL if self.PM else tk.DISABLED)
        self.mode_menu.entryconfigure("PM mode", state=tk.DISABLED if self.PM else tk.NORMAL)

    def toggle_pm_mode(self, event=None):
        busy, reason = self._is_system_busy()
        if busy:
            messagebox.showwarning("Không thể đổi chế độ", f"{reason}.")
            return "break"
        with self.logical_frames_lock:
            self.logical_frames.clear()
            self.logical_roi_views.clear()
        with self.ng_frames_lock:
            self.history_ng_frames.clear()
            self.ng_frame_save_futures.clear()
        with self.cycle_state_lock:
            self.list_ng.clear()
            self.ng_cams.clear()
        self.current_cam_name = None
        self.widget_result_check.listNGbox.delete(0, tk.END)
        if self.crop_viewer is not None and self.crop_viewer.winfo_exists():
            self.crop_viewer.destroy()
        self.crop_viewer = None
        self.clear_body_history_selection()
        self.PM = not self.PM
        self.update_mode_menu()
        mode = "PM MODE" if self.PM else "RUN MODE"
        recipe_name = self.recipe.current_model_display
        recipe_text = f" - {recipe_name} [D5={self.recipe.current_plc_program}]" if recipe_name else ""
        self.title(f"{self.window_title}{recipe_text} - {mode}")
        self.rebuild_button_grid()
        self._restore_normal_display()
        print(f"PM mode: {self.PM}")
        return "break"

    # ================= PLC COMMUNICATION =================
    def _create_plc_client(self):
        if SystemConfig.PLC_SIMULATOR_ENABLED:
            return PlcSimulator()
        from pc_plc_package.pc_plc import PlcModbusClient
        return PlcModbusClient(host=SystemConfig.PLC_HOST,
                               port=SystemConfig.PLC_PORT,
                               slave_id=SystemConfig.PLC_SLAVE_ID)

    def _read_plc_reg(self, reg):
        with self.plc_io_lock:
            if self.plc is None:
                return None
            return self.plc.read_reg(reg)

    def _write_plc_reg(self, reg, value):
        for attempt in range(3):
            with self.plc_io_lock:
                if self.plc is None:
                    return False
                try:
                    response = self.plc.write_int16(reg, int(value))
                    if not isinstance(self.plc, PlcSimulator) and (response is None or response.isError()):
                        raise RuntimeError(
                            f"Modbus write rejected: register={reg}, value={value}, response={response}")
                    return True
                except Exception as e:
                    last_err = e
            if attempt == 2:
                print(f"ERROR: Failed to write PLC {reg}={value}: {last_err}")
                return False
            time.sleep(0.05)
        return False

    def write_plc_result(self, value):
        ok = self._write_plc_reg(SystemConfig.PLC_RESULT_REG, value)
        if ok:
            MAIN_LOG.info("PLC result written: %s=%s", SystemConfig.PLC_RESULT_REG, value)
            with self.timer_lock:
                if self.stop:
                    self.dem = time.monotonic() - self.timer_started_at
                    self.stop = False
        else:
            self.ui_queue.put({"type": "plc_write_failed",
                               "reg": SystemConfig.PLC_RESULT_REG, "value": value})

    # ================= PLC PROGRAM OUTPUTS =================
    def reset_plc_program_sending(self):
        """Reset lịch gửi D5 khi nạp recipe, kết nối hoặc kết nối lại PLC."""
        with self.plc_lock:
            self.plc_last_d4 = None
            self.plc_d5_next_send_ts = None
        if not self.recipe.current_model_display:
            return
        pv = self.recipe.current_plc_program
        print(f"Model loaded; waiting for PLC {SystemConfig.PLC_READY_REG}=0 "
              f"để ghi {SystemConfig.PLC_PROG_REG}={pv} (gửi lại mỗi 0.5s khi D4=0)")

    def send_program_to_plc(self):
        with self.plc_d5_send_lock:
            if (not self.recipe_loaded or self.is_loading_recipe
                    or not self.recipe.current_model_display):
                return False
            prog = self.recipe.current_plc_program
            ok = self._write_plc_reg(SystemConfig.PLC_PROG_REG, prog)
            if ok:
                self.ui_queue.put({"type": "plc_program_sent", "prog": prog})
                return True
            else:
                raise RuntimeError(
                    f"Failed to write {SystemConfig.PLC_PROG_REG}={prog}; "
                    f"kiểm tra kết nối PLC, sẽ thử lại ở lần poll tiếp theo.")

    def handle_plc_d4(self, d4):
        """D4=0: chờ PLC sẵn sàng rồi gửi D5 mỗi 0.5s; D4 khác 0: dừng gửi."""
        if (d4 is None or not self.recipe_loaded or self.is_loading_recipe
                or not self.recipe.current_model_display or self.plc is None):
            return
        with self.plc_lock:
            now = time.monotonic()
            if d4 == 0:
                if self.plc_last_d4 != 0:
                    self.plc_d5_next_send_ts = now + SystemConfig.PLC_D5_SEND_DELAY_SECONDS
                self.plc_last_d4 = d4
                if (self.plc_d5_next_send_ts is not None
                        and now >= self.plc_d5_next_send_ts):
                    if self.send_program_to_plc():
                        self.plc_d5_next_send_ts = now + SystemConfig.PLC_D5_RESEND_INTERVAL_SECONDS
            else:
                self.plc_last_d4 = d4
                self.plc_d5_next_send_ts = None

    def start_check(self):
        with self.plc_init_lock:
            if self.plc_control_running.is_set():
                return
            with self.plc_io_lock:
                if self.plc is None:
                    cl = self._create_plc_client()
                    self.plc = cl if cl.connect() else None
            if self.plc is not None:
                self.reset_plc_program_sending()
            self.plc_control_running.set()
            threading.Thread(target=self.poll_plc, name="Pc1PlcPoll",
                             daemon=True).start()

    def stop_plc_control(self):
        self.plc_control_running.clear()
        with self.plc_io_lock:
            if self.plc is not None:
                try:
                    self.plc.close()
                except Exception:
                    pass
                self.plc = None

    def _try_reconnect_plc(self):
        print("Reconnecting PLC...")
        connected = False
        with self.plc_io_lock:
            try:
                if self.plc is not None:
                    try:
                        self.plc.close()
                    except Exception:
                        pass
                np = self._create_plc_client()
                if np.connect():
                    self.plc = np
                    connected = True
                else:
                    self.plc = None
            except Exception as e:
                print(f"ERROR: PLC reconnect failed: {e}")
                self.plc = None
        if connected:
            print("PLC reconnected")
            self.reset_plc_program_sending()
            try:
                d1 = self._read_plc_reg(SystemConfig.PLC_CMD_REG)
                with self.plc_lock:
                    self.plc_last_d1 = int(d1) if d1 is not None else 0
            except Exception:
                with self.plc_lock:
                    self.plc_last_d1 = 0

    # ================= PLC INPUTS AND CONDITIONS =================
    def read_plc_inputs(self):
        """Đọc D1, D4, D6 và giữ riêng lỗi từng thanh ghi."""
        values = {}
        errors = {}
        d1_received_at = None
        for reg in (SystemConfig.PLC_CMD_REG, SystemConfig.PLC_READY_REG, SystemConfig.PLC_RESET_REG):
            try:
                value = self._read_plc_reg(reg)
                if value is None:
                    raise RuntimeError("PLC returned no register value")
                values[reg] = value
                if reg == SystemConfig.PLC_CMD_REG and value == 1:
                    d1_received_at = time.monotonic()
            except Exception as exc:
                values[reg] = None
                errors[reg] = str(exc)
        return values, errors, d1_received_at

    def handle_plc_inputs(self, values, d1_received_at):
        """Ưu tiên dừng theo D6, sau đó mới xét cạnh lên D1."""
        d1 = values.get(SystemConfig.PLC_CMD_REG)
        with self.plc_lock:
            prev_d1 = self.plc_last_d1
            if d1 is not None:
                self.plc_last_d1 = d1

            if values.get(SystemConfig.PLC_RESET_REG) == 1:
                if self.plc_cycle_active:
                    self.fail_plc_cycle("PLC_RESET", display_result="PLC_RESET")
                return

            if d1 == 1 and prev_d1 == 0:
                threading.Thread(target=self.handle_plc_d1_trigger,
                                 args=(d1_received_at,),
                                 name="Pc1HandleD1", daemon=True).start()

    def poll_plc(self):
        """Một vòng lặp đọc PLC, xử lý D1 và D6, cập nhật D5."""
        err_cnt = 0
        last_rc = 0.0
        while self.plc_control_running.is_set():
            values = {}
            errors = {}
            d1_received_at = None
            if self.plc is None:
                if time.monotonic() - last_rc > SystemConfig.PLC_RECONNECT_INTERVAL:
                    last_rc = time.monotonic()
                    self._try_reconnect_plc()
            if self.plc is not None:
                values, errors, d1_received_at = self.read_plc_inputs()

            # D6: dừng chu trình. D1: bắt cạnh 0 → 1 để chạy step.
            try:
                self.handle_plc_inputs(values, d1_received_at)
            except Exception:
                MAIN_LOG.exception("PLC input handling failed: values=%s, cycle=%s",
                                   values, self.cycle_id)

            # D4=0: gửi D5 theo recipe mỗi 0.5s.
            try:
                self.handle_plc_d4(values.get(SystemConfig.PLC_READY_REG))
            except Exception:
                MAIN_LOG.exception("PLC D4 handling failed: D4=%s",
                                   values.get(SystemConfig.PLC_READY_REG))

            if errors:
                err_cnt += 1
                if err_cnt % 100 == 1:
                    print(f"WARN: PLC reads failed ({SystemConfig.PLC_HOST}:"
                          f"{SystemConfig.PLC_PORT}): {errors}")
                if (err_cnt >= SystemConfig.PLC_ERR_THRESHOLD
                        and time.monotonic() - last_rc > SystemConfig.PLC_RECONNECT_INTERVAL):
                    last_rc = time.monotonic()
                    self._try_reconnect_plc()
                    err_cnt = 0
            else:
                err_cnt = 0
            time.sleep(SystemConfig.PLC_POLL_SECONDS)

    # ================= CYCLE =================
    def generate_cycle_barcode(self):
        model = getattr(self.recipe, 'current_model_display', None) or "MODEL"
        now = datetime.now()
        with self.counter_lock:
            today = now.date()
            if getattr(self, "_barcode_date", None) != today:
                self._barcode_counter = 0
                self._barcode_date = today
            self._barcode_counter = getattr(self, "_barcode_counter", 0) + 1
            seq = self._barcode_counter
            self._save_barcode_counter()
        return f"{model}_{now:%y%m%d_%H%M}_{seq:04d}"

    def read_cycle_barcodes(self, received_at, cycle_id):
        """Đọc các camera song song, cùng dùng thời hạn tính từ D1."""
        cameras = SystemConfig.BARCODE_CAMERAS
        if not cameras:
            raise ValueError("Chưa cấu hình camera đọc barcode trong BARCODE_CAMERAS")
        for camera, _ in cameras:
            if camera not in SystemConfig.CAMERA_LOCATION_REFS:
                raise ValueError(f"Barcode camera {camera!r} is not configured in CAMERA_LOCATION_REFS")

        deadline = received_at + SystemConfig.BARCODE_TIMEOUT_SECONDS
        barcodes = [None] * len(cameras)
        futures = {}
        try:
            for index, (camera, roi) in enumerate(cameras):
                cam = int(CameraKey.cam_token(camera))
                future = self.executor_barcode.submit(
                    self._read_camera_barcode, cam, roi, received_at, deadline, cycle_id)
                futures[future] = index

            completed = as_completed(futures, timeout=max(0, deadline - time.monotonic()))
            while True:
                try:
                    future = next(completed)
                except (StopIteration, FuturesTimeoutError):
                    break
                code = future.result()
                if code is not None:
                    with self.plc_lock:
                        if self.cycle_id != cycle_id or self.plc_cycle_aborted or not self.camera_running:
                            break
                        barcodes[futures[future]] = code
                        self.ui_queue.put({"type": "barcode_progress", "body_numbers": tuple(barcodes)})
        finally:
            for future in futures:
                future.cancel()
        return barcodes

    def _read_camera_barcode(self, cam, roi, received_at, deadline, cycle_id):
        """Chỉ giải mã các frame mới của một camera trong cửa sổ đọc."""
        from barcode_reader import read_barcode

        last_read_started = received_at
        while True:
            with self.frame_condition:
                while (self.camera_running and not self.plc_cycle_aborted
                       and self.cycle_id == cycle_id):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    frame = self.latest_frames[cam]
                    read_started = self.latest_frame_read_started[cam]
                    if frame is not None and read_started > last_read_started:
                        last_read_started = read_started
                        break
                    self.frame_condition.wait(timeout=remaining)
                else:
                    return None

            if roi is None:
                height, width = frame.shape[:2]
                frame_roi = (0, 0, width, height)
            else:
                frame_roi = roi
            barcode = read_barcode(frame, roi=frame_roi)
            if isinstance(barcode, str) and barcode.strip() and time.monotonic() <= deadline:
                return barcode

    def handle_plc_d1_trigger(self, received_at=None):
        received_at = time.monotonic() if received_at is None else received_at
        if self.is_loading_recipe:
            print("WARN: Ignoring D1 trigger while loading recipe")
            return
        if not self.recipe_loaded:
            print("WARN: Ignoring D1 trigger; no valid recipe is loaded")
            return

        pm_mode = self.PM
        steps_snapshot = None
        step = None
        start_new = False

        with self.plc_lock:
            if self.plc_cycle_active and self.plc_cycle_aborted:
                return
            if self.plc_step_in_progress:
                print("WARN: Ignoring D1 trigger; the previous step is still in progress")
                return

            if self.plc_cycle_active:
                pm_mode = self.cycle_pm_mode
                steps_snapshot = dict(self.cycle_steps_snapshot or {})
            else:
                with self.timer_lock:
                    self.dem = 0
                    self.timer_started_at = received_at
                    self.stop = True
                self.ui_queue.put({"type": "start_timer"})
                steps_snapshot = dict(self.recipe.plc_steps)

            steps = sorted(steps_snapshot.keys())
            if not steps:
                print(f"WARN: No plc_steps configured (PM={pm_mode})")
                self.plc_steps_captured.clear()
                self.plc_step_in_progress.clear()
                self.plc_next_step_index = 0
                self.plc_cycle_active = False
                self.plc_cycle_aborted = True
            elif not self.plc_cycle_active:
                start_new = True
                self.plc_cycle_active = True
                self.plc_cycle_aborted = False
                self.cycle_id = uuid4().hex
                self.current_barcode = None
                self.databacode = None
                self.current_check_no = None
                self.plc_next_step_index = 1
                step = steps[0]
                if step in self.plc_steps_captured or step in self.plc_step_in_progress:
                    return
                self.plc_step_in_progress.add(step)
                self.ui_queue.put({"type": "reset_cycle_display", "total": self.total_points})
            else:
                if self.plc_next_step_index >= len(steps):
                    print("WARN: Ignoring D1 trigger; the current cycle is finishing")
                    return
                step = steps[self.plc_next_step_index]
                self.plc_next_step_index += 1
                if step in self.plc_steps_captured or step in self.plc_step_in_progress:
                    return
                self.plc_step_in_progress.add(step)
            cycle_id = self.cycle_id

        if not steps:
            self.write_plc_result(SystemConfig.PLC_RESULT_NG)
            self.ui_queue.put({"type": "inspection_error",
                               "text": "Thiếu bước trong plc_steps.json"})
            self.ui_queue.put({"type": "final_result", "result": "CYCLE ERROR"})
            return

        try:
            scan_barcode = start_new and not SystemConfig.should_skip_barcode(self.recipe.current_model_display)
            barcodes = self.read_cycle_barcodes(received_at, cycle_id) if scan_barcode else []

            with self.plc_lock:
                if self.cycle_id != cycle_id or self.plc_cycle_aborted or not self.camera_running:
                    return
                if start_new:
                    if scan_barcode and None in barcodes:
                        configured_reads = zip(SystemConfig.BARCODE_CAMERAS, barcodes)
                        missing = ", ".join(
                            f"BODY NO{index} ({camera}, ROI={roi}, "
                            f"camera_error={self.camera_last_error[int(CameraKey.cam_token(camera))]!r})"
                            for index, ((camera, roi), code) in enumerate(configured_reads, 1)
                            if code is None
                        )
                        reason = (f"Không đọc được barcode trong "
                                  f"{SystemConfig.BARCODE_TIMEOUT_SECONDS:g}s: {missing}")
                        auto_barcode = SystemConfig.AUTO_BARCODE_PM if pm_mode else SystemConfig.AUTO_BARCODE_RUN
                        if not auto_barcode:
                            raise BarcodeTimeoutError(reason)
                        MAIN_LOG.warning("%s. Tự sinh mã cho vị trí thiếu.", reason)
                    if not scan_barcode:
                        barcodes = [self.generate_cycle_barcode()]
                    body_numbers = tuple(code if code is not None else self.generate_cycle_barcode()
                                         for code in barcodes)
                    if not self.prepare_network_cycle(body_numbers, pm_mode=pm_mode,
                                                      steps_snapshot=steps_snapshot):
                        self.write_plc_result(SystemConfig.PLC_RESULT_NG)
                        return
                self.start_plc_step(step, cycle_id)
        except Exception as exc:
            MAIN_LOG.exception("Cycle preparation failed: recipe=%s, cycle=%s",
                               self.recipe.current_model_display, cycle_id)
            with self.plc_lock:
                if self.cycle_id == cycle_id and not self.plc_cycle_aborted:
                    display_result = "barcode_ng" if isinstance(exc, BarcodeTimeoutError) else "CYCLE ERROR"
                    self.fail_plc_cycle(f"Không thể tạo chu trình: {exc}",
                                        display_result=display_result)

    def start_plc_step(self, step, cycle_id):
        self.ui_queue.put({"type": "result_step", "step": step})
        threading.Thread(target=self.run_plc_step, args=(step, cycle_id),
                         name=f"Pc1CaptureStep{step}", daemon=True).start()

    def run_plc_step(self, step, cycle_id):
        try:
            if self.plc_cycle_aborted or self.cycle_id != cycle_id:
                print(f"WARN: Skipping step {step} (cycle aborted)")
                return
            steps_snapshot = self.cycle_steps_snapshot or self.recipe.plc_steps
            pairs = self._sanitize_pairs(steps_snapshot.get(step, []))

            if len(pairs) > 0:
                self._ensure_cameras_ready({int(CameraKey.cam_token(cn)) for cn, _ in pairs})
                delay = self.cycle_step_delays[step]
                MAIN_LOG.info("Delay before capture: recipe=%s, step=%s, delay=%ss, cycle=%s",
                              self.cycle_recipe_name, step, delay, cycle_id)
                if delay > 0:
                    time.sleep(delay)
                if self.plc_cycle_aborted or self.cycle_id != cycle_id:
                    return
                self.run_plc_capture(step, pairs, cycle_id)
            else:
                print(f"Step {step} is empty; writing D0=1")

            with self.plc_lock:
                if self.plc_cycle_aborted or self.cycle_id != cycle_id or not self.camera_running:
                    return
                if not self._write_plc_reg(SystemConfig.PLC_DONE_REG, 1):
                    raise RuntimeError(f"Failed to write D0=1 at step {step}")
                if len(pairs) > 0:
                    self.xl_queue()
                self.plc_steps_captured.add(step)
                all_cap = len(self.plc_steps_captured) >= len(steps_snapshot)
            if all_cap:
                self.finish_one_cycle(cycle_id)
        except Exception as exc:
            with self.plc_lock:
                if self.cycle_id == cycle_id:
                    self.fail_plc_cycle(f"PC1 step {step} lỗi: {exc}")
                else:
                    MAIN_LOG.error("Step %s from ended cycle %s failed: %s", step, cycle_id, exc)
        finally:
            with self.plc_lock:
                if self.cycle_id == cycle_id:
                    self.plc_step_in_progress.discard(step)

    def run_plc_capture(self, step, pairs, cycle_id):
        with self.queue_put_lock:
            self.queue_put_count_step = 0
            self.queue_put_count_failed = 0
        futures = self.capture_plc_step(pairs, cycle_id)
        for future in futures:
            future.result()
        with self.queue_put_lock:
            failed = self.queue_put_count_failed
        if failed > 0:
            raise RuntimeError(f"Capture missing step {step}: {failed}/{len(pairs)}")

    def capture_plc_step(self, pairs, cycle_id):
        # This boundary is after the settling delay. Accept only reads begun later.
        fresh_after = time.monotonic()
        now = time.time()
        futures = []
        for cn, vt in pairs:
            try:
                cam = int(CameraKey.cam_token(cn))
            except (ValueError, TypeError):
                print(f"WARN: Invalid camera name: {cn}")
                with self.queue_put_lock:
                    self.queue_put_count_step += 1
                    self.queue_put_count_failed += 1
                continue
            if cam in self.capture_activity:
                self.capture_activity[cam] = now + SystemConfig.CAMERA_ACTIVE_WINDOW
            sk = f"CAM{cam}_VT{vt}"
            futures.append(self.executor_capture.submit(
                self.capture_and_process, cam, vt, sk, fresh_after, cycle_id))
        return futures

    def capture_and_process(self, cam, vitri, sk, fresh_after, cycle_id):
        try:
            cached = None
            timeout = SystemConfig.CAPTURE_FRAME_RETRIES * SystemConfig.CAPTURE_FRAME_RETRY_SLEEP
            deadline = time.monotonic() + timeout
            with self.frame_condition:
                while True:
                    if self.plc_cycle_aborted or self.cycle_id != cycle_id or not self.camera_running:
                        return
                    candidate = self.latest_frames.get(cam)
                    read_started = self.latest_frame_read_started.get(cam, 0.0)
                    if candidate is not None and read_started > fresh_after:
                        cached = candidate
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.frame_condition.wait(timeout=remaining)

            with self.plc_lock:
                if self.plc_cycle_aborted or self.cycle_id != cycle_id or not self.camera_running:
                    return
                if cached is None:
                    reason = (f"No fresh frame from CAM{cam} ({sk}) after {timeout}s; "
                              f"camera_error={self.camera_last_error.get(cam)}")
                    print(f"ERROR: {reason}")
                    self.ui_queue.put({"type": "camera_startup_error", "cam_ids": [cam],
                                       "reasons": {cam: reason}})
                    with self.queue_put_lock:
                        self.queue_put_count_step += 1
                        self.queue_put_count_failed += 1
                    return
                frame = cached  # chỉ đọc từ đây: không ai ghi lên frame gốc
                bc = self.current_barcode
                self._submit_save(self.save_picture, cam, frame, vitri, bc)
                with self.allow_put_lock:
                    allow = self.allow_put
                with self.queue_put_lock:
                    self.queue_put_count_step += 1
                    if not allow:
                        self.queue_put_count_failed += 1
                        return
                self.queue_xl_anh.put((frame, cam, vitri, sk, bc, cycle_id))
        except Exception:
            with self.plc_lock, self.queue_put_lock:
                if self.cycle_id == cycle_id and not self.plc_cycle_aborted:
                    self.queue_put_count_step += 1
                    self.queue_put_count_failed += 1
            MAIN_LOG.error("capture_and_process exception", exc_info=True)

    def fail_plc_cycle(self, reason, *, display_result="CYCLE ERROR"):
        with self.plc_lock:
            if self.plc_cycle_aborted:
                return
            print(f"ERROR: Cycle failed: {reason}")
            prepared = self.plc_cycle_active
            pm_mode = self.cycle_pm_mode
            barcode = self.current_barcode
            check_no = self.current_check_no
            self.plc_cycle_aborted = True
            self.plc_steps_captured.clear()
            self.plc_step_in_progress.clear()
            self.plc_next_step_index = 0
        self.stop_cycle_runtime()
        self.write_plc_result(SystemConfig.PLC_RESULT_NG)
        self.ui_queue.put({"type": "inspection_error", "text": str(reason)})
        self.ui_queue.put({"type": "final_result", "result": display_result})
        try:
            if prepared and not pm_mode and barcode is not None:
                if check_no is None:
                    check_no = create_inspection_check(barcode, self.cycle_recipe_name)
                with self.ng_frames_lock:
                    frame_saves = tuple(self.ng_frame_save_futures)
                self.record_network_final_result(
                    barcode, check_no, "NG", self.get_cycle_ng_points(), frame_saves=frame_saves)
        finally:
            with self.plc_lock:
                self.reset_plc_cycle_locked()

    def reset_plc_cycle_locked(self):
        self.plc_cycle_active = False
        self.plc_steps_captured.clear()
        self.plc_step_in_progress.clear()
        self.plc_next_step_index = 0
        self.cycle_recipe_name = None
        self.cycle_pm_mode = False
        self.cycle_steps_snapshot = {}
        self.cycle_step_delays = {}
        with self.cycle_state_lock:
            self.cycle_expected_points = set()
            self.cycle_completed_points = set()
            self.cycle_point_errors = {}
            self.cycle_point_results = {}
            self.cycle_unit_configuration = None
            self.cycle_assessment = None
            self.cycle_render_frames = {}
        self.cycle_id = None
        self.ui_queue.put({"type": "result_step", "step": 0})

    def stop_cycle_runtime(self):
        with self.allow_put_lock:
            self.allow_put = False
        with self.frame_condition:
            self.frame_condition.notify_all()
        with self.futures_lock:
            self.process_futures.clear()
        while not self.queue_xl_anh.empty():
            try:
                self.queue_xl_anh.get_nowait()
            except Exception:
                pass
        self.ui_queue.put({"type": "stop_cycle"})

    # ================= FINISH CYCLE =================
    def finish_one_cycle(self, cycle_id):
        with self.cycle_finish_lock:
            return self._finish_one_cycle(cycle_id)

    def _finish_one_cycle(self, cycle_id):
        try:
            with self.plc_lock:
                if (not self.plc_cycle_active or self.plc_cycle_aborted
                        or self.cycle_id != cycle_id):
                    return "CYCLE ERROR"
                barcode = self.current_barcode
                check_no = self.current_check_no
                pm_mode = self.cycle_pm_mode
                self.xl_queue()
                with self.futures_lock:
                    futures = list(self.process_futures)
            total_to = SystemConfig.AI_BATCH_TOTAL_TIMEOUT * max(1, len(futures) // 4)
            sw = time.time()
            for f in futures:
                rem = total_to - (time.time() - sw)
                if rem <= 0:
                    raise TimeoutError(f"Cycle {cycle_id}: processing timed out after {total_to:.1f}s")
                try:
                    f.result(timeout=max(1.0, rem))
                except FuturesTimeoutError as exc:
                    raise TimeoutError(f"Cycle {cycle_id}: processing future timed out after {rem:.1f}s") from exc
                except Exception as e:
                    raise RuntimeError(f"Cycle {cycle_id}: processing future failed: {e}") from e
            with self.plc_lock:
                if (self.plc_cycle_aborted or not self.plc_cycle_active
                        or self.cycle_id != cycle_id):
                    return "CYCLE ERROR"
                valid, reason = self._validate_cycle_points()
                if not valid:
                    self.fail_plc_cycle(f"Validate failed: {reason}", display_result="NG")
                    return "CYCLE ERROR"
                self.databacode = None
                assessment = self.cycle_assessment
            self.render_cycle_results(cycle_id)
            has_ng = assessment["ng"] > 0
            empty_tray = assessment["empty"]
            ng_points = self.get_cycle_ng_points()
            recipe_name = self.cycle_recipe_name
            result = "NG" if has_ng or empty_tray else "OK"

            with self.ng_frames_lock:
                frames_snap = self.get_run_ng_frames()
                frame_saves = tuple(self.ng_frame_save_futures)
            image_path = None
            if has_ng:
                gi, roi_views = self.build_ng_grid(frames_snap)
                if gi is not None:
                    self.ui_queue.put({"type": "ng_grid", "frame": gi, "roi_views": roi_views})
            if not pm_mode and has_ng:
                image_path = self.save_body_history_image(barcode, recipe_name, gi)
            for future in frame_saves:
                future.result()
            with self.plc_lock:
                if (self.plc_cycle_aborted or not self.plc_cycle_active
                        or self.cycle_id != cycle_id):
                    return "CYCLE ERROR"
                if not pm_mode and not empty_tray:
                    check_no = create_inspection_check(barcode, recipe_name)
                    self.current_check_no = check_no
                    if not self.cycle_counts_written:
                        counts = self.ghifile(recipe_name, assessment["ok"], assessment["ng"])
                        self.cycle_counts_written = True
                        self.qty_ok = counts["ok"]
                        self.qty_ng = counts["ng"]
                        self.qty_all = counts["all"]
                if empty_tray:
                    self.ui_queue.put({"type": "ng_point", "text": "EMPTY_TRAY"})
                    MAIN_LOG.info("EMPTY_TRAY: recipe=%s, barcode=%s; no counters updated", recipe_name, barcode)
                self.ui_queue.put({"type": "final_result", "result": result})
                if not pm_mode:
                    self.ui_queue.put({"type": "counter", "ok": self.qty_ok,
                                       "ng": self.qty_ng, "all": self.qty_all})
                with self.futures_lock:
                    self.process_futures.clear()
                fs = "OK" if result == "OK" else "NG"
                if not pm_mode and not empty_tray:
                    self.record_network_final_result(barcode, check_no, fs, ng_points, image_path, frame_saves)
                self.reset_plc_cycle_locked()
                self.write_plc_result(SystemConfig.PLC_RESULT_OK if fs == "OK"
                                      else SystemConfig.PLC_RESULT_NG)
            return result
        except Exception as exc:
            with self.plc_lock:
                if cycle_id is not None and cycle_id != self.cycle_id:
                    MAIN_LOG.exception("Finish failed for inactive cycle %s", cycle_id)
                else:
                    self.fail_plc_cycle(f"Finish cycle lỗi: {exc}")
            return "CYCLE ERROR"

    def record_network_final_result(self, barcode, check_no, fs, ng_points, image_path=None, frame_saves=()):
        try:
            self.executor.submit(self.write_db_async, barcode, check_no, fs, ng_points, image_path, frame_saves)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Không thể lưu kết quả: barcode={barcode!r}, check_no={check_no!r}, status={fs!r}") from exc

    def write_db_async(self, bc, cn, stt, ng_points, image_path, frame_saves):
        try:
            frames = [future.result() for future in frame_saves]
            update_final_status(bc, stt, cn, ng_points, image_path, frames)
            self.ui_queue.put({"type": "statistics_refresh_summary"})
        except Exception as e:
            MAIN_LOG.exception("Failed to write database row: barcode=%s, check_no=%s, status=%s", bc, cn, stt)
            self.ui_queue.put({"type": "inspection_error", "text": str(e)})
            raise

    def xl_queue(self):
        while True:
            try:
                args = self.queue_xl_anh.get_nowait()
            except Empty:
                break
            try:
                fut = self.executor.submit(self.process_image, *args)
                with self.futures_lock:
                    self.process_futures.append(fut)
            except RuntimeError:
                break

    # ================= IMAGE PROCESSING =================
    def draw_orb_zones(self, img, zones, cam):
        for z in zones:
            x, y, w, h = z["xywh"]
            dx, dy = z.get("dxdy", (0.0, 0.0))
            color = (0, 255, 255) if abs(dx) < 1 and abs(dy) < 1 else (255, 255, 0)
            cv2.rectangle(img, (int(x), int(y)), (int(x + w), int(y + h)), color, 2)
            cv2.putText(img, f"CAM{cam} dx={dx:.1f}, dy={dy:.1f}",
                        (int(x), int(y) - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        color, 1, cv2.LINE_AA)

    def _prepare_point_crop(self, frame, cam, vitri, point, zones, sk):
        pid = point.get("POINT", "?")
        nbd = SystemConfig.point_box(point)
        _, _, roi_width, roi_height = nbd
        cx = point["X"] + roi_width / 2
        cy = point["Y"] + roi_height / 2
        dx, dy = 0.0, 0.0
        for z in zones:
            zx, zy, zw, zh = z["xywh"]
            if zx <= cx <= zx + zw and zy <= cy <= zy + zh:
                dx, dy = z.get("dxdy", (0.0, 0.0))
                break
        else:
            if len(zones) == 1:
                dx, dy = zones[0].get("dxdy", (0.0, 0.0))
        h, w = frame.shape[:2]
        if roi_width > w or roi_height > h:
            raise ValueError(
                f"ROI {sk}:D{pid} ({roi_width}x{roi_height}) exceeds frame dimensions ({w}x{h}); point={point!r}"
            )
        nx = int(point["X"] + dx)
        ny = int(point["Y"] + dy)
        nx = max(0, min(nx, w - roi_width))
        ny = max(0, min(ny, h - roi_height))
        nbox = (nx, ny, roi_width, roi_height)
        ci = frame[ny:ny + roi_height, nx:nx + roi_width]  # view; cv2.resize bên dưới tạo mảng mới
        if ci.size == 0:
            raise RuntimeError("EMPTY_CROP")
        ci224 = cv2.resize(ci, (224, 224))
        model = self.recipe.select_model_for_point(cam, vitri, pid)
        if model is None:
            raise RuntimeError(f"MODEL_NOT_FOUND:{sk}:D{pid}")
        return ci224, nbox, model, nbd

    @staticmethod
    def _predict(model, images):
        """Gọi model.predict, đi qua _infer_lock nếu model có."""
        il = getattr(model, "_infer_lock", None)
        if il is not None:
            with il:
                return model.predict(images, imgsz=224, device=SystemConfig.AI_DEVICE,
                                     verbose=False)
        return model.predict(images, imgsz=224, device=SystemConfig.AI_DEVICE,
                             verbose=False)

    @staticmethod
    def _top_class(pred):
        """Kết quả phân loại của một ảnh"""
        if getattr(pred, "probs", None) is None:
            raise RuntimeError("AI_NO_PROBS")
        probs = pred.probs.data.detach().cpu().numpy()
        if probs.size == 0:
            raise RuntimeError("AI_EMPTY_PROBS")
        cls = int(np.argmax(probs))
        return pred.names[cls], round(float(probs[cls]), 4)

    def process_single_point(self, frame, cam, vitri, point, zones, sk, barcode, mcid):
        pid = point.get("POINT", "?")
        nbd = SystemConfig.point_box(point)
        if mcid is None or mcid != self.cycle_id:
            return (point, None, "ERROR:NO_ACTIVE_CYCLE", 0.0,
                    vitri, nbd, "error", "no active cycle")
        try:
            with self.counter_lock:
                self.pointcheck += 1
                cp = self.pointcheck
            te = max(1, len(self.cycle_expected_points) or self.total_points)
            self.ui_queue.put({"type": "progress", "current": cp, "total": te})
            ci224, nbox, model, _ = self._prepare_point_crop(frame, cam, vitri, point, zones, sk)
            res = self._predict(model, ci224)
            if not res:
                raise RuntimeError("AI_NO_PROBS")
            _name, _prob = self._top_class(res[0])
            self._submit_save(self.save_crop_all, cam, ci224, vitri, pid, _name, _prob, barcode)
            if self.cycle_id != mcid:
                return (point, None, "ERROR:CYCLE_CHANGED", 0.0,
                        vitri, nbox, "error", "cycle changed")
            return (point, ci224, _name, _prob, vitri, nbox, "yolo", None)
        except Exception as exc:
            et = type(exc).__name__
            em = str(exc)
            return (point, None, f"ERROR:{et}:{em}", 0.0, vitri, nbd, "error", em)

    def _run_batch_group_inference(self, model, items, cam, vitri, barcode, mcid):
        MB = SystemConfig.AI_MAX_BATCH_SIZE
        out = {}
        if len(items) > MB:
            for i in range(0, len(items), MB):
                si = items[i:i + MB]
                sr = self._run_batch_group_inference(model, si, cam, vitri, barcode, mcid)
                out.update(sr)
            return out
        crops = [it["crop_224"] for it in items]
        try:
            preds = self._predict(model, crops)
            if not preds or len(preds) != len(items):
                raise RuntimeError("BATCH_PREDICT_MISMATCH")
            for it, pred in zip(items, preds):
                pt = it["point"]
                pid = pt.get("POINT", "?")
                c224 = it["crop_224"]
                nb = it["newbox"]
                _name, _prob = self._top_class(pred)
                self._submit_save(self.save_crop_all, cam, c224, vitri, pid, _name, _prob, barcode)
                if self.cycle_id != mcid:
                    out[it["idx"]] = (pt, None, "ERROR:CYCLE_CHANGED", 0.0,
                                      vitri, nb, "error", "cycle changed")
                else:
                    out[it["idx"]] = (pt, c224, _name, _prob, vitri, nb, "yolo", None)
            return out
        except Exception as bexc:
            print(f"WARN: Batch inference failed; using single-item inference: {bexc}")
            for it in items:
                pt = it["point"]
                pid = pt.get("POINT", "?")
                c224 = it["crop_224"]
                nb = it["newbox"]
                try:
                    r = self._predict(model, c224)
                    _name, _prob = self._top_class(r[0])
                    self._submit_save(self.save_crop_all, cam, c224, vitri, pid, _name, _prob, barcode)
                    out[it["idx"]] = (pt, c224, _name, _prob, vitri, nb, "yolo", None)
                except Exception as se:
                    et = type(se).__name__
                    em = str(se)
                    out[it["idx"]] = (pt, None, f"ERROR:{et}:{em}", 0.0,
                                      vitri, nb, "error", em)
            return out

    def draw_result_on_frame(self, frame, crop_image, point, _name, _prop,
                             vitri, cam, newbox, mt, barcode=None):
        xn, yn, width, height = newbox
        is_ok = self._is_point_ok(point, _name, _prop, mt)
        if is_ok:
            color = (0, 255, 0)
            cv2.rectangle(frame, (xn, yn), (xn + width, yn + height), color, 5)
            cv2.putText(frame, f"D_{point['POINT']}", (xn, yn - 10),
                        cv2.FONT_HERSHEY_COMPLEX, 1, color, 2)
            return False
        else:
            nk = f"CAM{cam}_VT{vitri}"
            with self.cycle_state_lock:
                if nk not in self.ng_cams:
                    self.ng_cams.append(nk)
                self.list_ng.append(crop_image)
            self._submit_save(self.save_crop_ng, cam, crop_image, vitri,
                              point["POINT"], _name, _prop, barcode)
            color = (0, 0, 255)
            self.ui_queue.put({"type": "button_ng", "attr": nk})
            self.ui_queue.put({"type": "ng_point",
                               "text": f"D{point['POINT']}_CAM {cam}"})
            cv2.rectangle(frame, (xn, yn), (xn + width, yn + height), color, 5)
            cv2.putText(frame, f"D_{point['POINT']}", (xn, yn - 10),
                        cv2.FONT_HERSHEY_COMPLEX, 1, color, 2)
            return True

    def process_image(self, frame, cam, vitri, sk, barcode, mcid):
        with self.plc_lock:
            if mcid != self.cycle_id or self.plc_cycle_aborted:
                return
            points_cfg = self.cycle_unit_configuration.points_cfg
        pf = frame.copy()
        zones = []
        if self.recipe.orb_rois.get(sk):
            gn = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            zones = self.recipe.compute_orb_dxdy(sk, gn)
        self.draw_orb_zones(pf, zones, cam)
        toado = points_cfg.get(sk, [])
        lres = [None] * len(toado)

        if SystemConfig.ENABLE_BATCH_INFERENCE and toado:
            mb = {}
            for idx, pt in enumerate(toado):
                nbd = SystemConfig.point_box(pt)
                try:
                    with self.counter_lock:
                        self.pointcheck += 1
                        cp = self.pointcheck
                    te = max(1, len(self.cycle_expected_points) or self.total_points)
                    self.ui_queue.put({"type": "progress", "current": cp, "total": te})
                    c224, nb, model, _ = self._prepare_point_crop(frame, cam, vitri, pt, zones, sk)
                    mid = id(model)
                    if mid not in mb:
                        mb[mid] = (model, [])
                    mb[mid][1].append({"idx": idx, "point": pt,
                                       "crop_224": c224, "newbox": nb})
                except Exception as exc:
                    et = type(exc).__name__
                    em = str(exc)
                    lres[idx] = (pt, None, f"ERROR:{et}:{em}", 0.0,
                                 vitri, nbd, "error", em)
            bfuts = []
            for _, (model, items) in mb.items():
                fut = self.executor_ai.submit(self._run_batch_group_inference,
                                              model, items, cam, vitri, barcode, mcid)
                nsb = (len(items) + SystemConfig.AI_MAX_BATCH_SIZE - 1) // SystemConfig.AI_MAX_BATCH_SIZE
                bto = SystemConfig.AI_POINT_TIMEOUT_SECONDS * max(1, nsb)
                bfuts.append((fut, items, bto))
            for fut, items, bto in bfuts:
                try:
                    gr = fut.result(timeout=bto)
                    for ii, rt in gr.items():
                        lres[ii] = rt
                except FuturesTimeoutError:
                    for it in items:
                        pt = it["point"]
                        lres[it["idx"]] = (pt, None, "ERROR:AI_BATCH_TIMEOUT", 0.0,
                                           vitri, it["newbox"], "error",
                                           f"batch timeout {bto:.1f}s")
                except Exception as exc:
                    for it in items:
                        pt = it["point"]
                        lres[it["idx"]] = (pt, None, f"ERROR:AI_BATCH_EXC:{exc}", 0.0,
                                           vitri, it["newbox"], "error", str(exc))
            for idx, pt in enumerate(toado):
                pid = pt.get("POINT", "?")
                pk = self._point_key(sk, pid)
                self._register_point_result(pk, lres[idx], cycle_id=mcid)
        else:
            futs = []
            for pt in toado:
                fut = self.executor_ai.submit(self.process_single_point,
                                              frame, cam, vitri, pt, zones, sk, barcode, mcid)
                futs.append((fut, pt))
            for idx, (fut, pt) in enumerate(futs):
                pid = pt.get("POINT", "?")
                pk = self._point_key(sk, pid)
                try:
                    r = fut.result(timeout=SystemConfig.AI_POINT_TIMEOUT_SECONDS)
                except FuturesTimeoutError:
                    r = (pt, None, "ERROR:AI_TIMEOUT", 0.0, vitri,
                         SystemConfig.point_box(pt), "error", "timeout")
                except Exception as exc:
                    r = (pt, None, f"ERROR:AI_EXECUTOR:{exc}", 0.0, vitri,
                         SystemConfig.point_box(pt), "error", str(exc))
                lres[idx] = r
                self._register_point_result(pk, r, cycle_id=mcid)

        with self.plc_lock, self.cycle_state_lock:
            if self.cycle_id != mcid or self.plc_cycle_aborted:
                return
            self.cycle_render_frames[sk] = (pf.copy(), lres, cam, vitri, barcode)
            cv2.putText(pf, f"CAM {cam} VT{vitri}.", (30, 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)
            with self.logical_frames_lock:
                self.logical_frames[sk] = pf.copy()
                self.logical_roi_views[sk] = {"frame": pf, "rois": [], "bounds": (0, 0, pf.shape[1], pf.shape[0])}
            self.ui_queue.put({"type": "image", "frame": pf, "roi_views": []})

    def render_cycle_results(self, cycle_id):
        with self.cycle_state_lock:
            frames = dict(self.cycle_render_frames)
        for sk, (frame, results, cam, vitri, barcode) in sorted(frames.items()):
            with self.plc_lock:
                if self.cycle_id != cycle_id or self.plc_cycle_aborted:
                    raise RuntimeError(f"Cycle {cycle_id}: canceled before rendering {sk}")
                self.render_cycle_camera(frame.copy(), results, cam, vitri, sk, barcode, cycle_id)

    def render_cycle_camera(self, pf, lres, cam, vitri, sk, barcode, mcid):
        recipe_name = self.cycle_recipe_name
        pm_mode = self.cycle_pm_mode
        ng_rois = []
        cam_ng = False
        for r in lres:
            if r is None:
                continue
            pt, ci, _name, _prob, vr, nb, mt, etxt = r
            pk = self._point_key(sk, pt["POINT"])
            state = self.cycle_assessment["points"][pk]
            if state in ("SKIPPED", "ABSENT", "PRESENT"):
                xn, yn, width, height = nb
                color = (0, 255, 0) if state == "PRESENT" else (160, 160, 160)
                label = "EMPTY" if state == "ABSENT" else state
                cv2.rectangle(pf, (xn, yn), (xn + width, yn + height), color, 3)
                cv2.putText(pf, f"D{pt['POINT']} {label}", (xn, max(20, yn - 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
                continue
            ng = self.draw_result_on_frame(pf, ci, pt, _name, _prob, vr, cam, nb, mt, barcode)
            if ng:
                cam_ng = True
                ng_rois.append({"box": nb, "point": pt["POINT"], "manual_ok": False})
        cv2.putText(pf, f"CAM {cam} VT{vitri}", (950, 100),
                    cv2.FONT_HERSHEY_COMPLEX, 2, (0, 255, 0), 2)

        fh, fw = pf.shape[:2]
        roi_view = {"frame": pf, "rois": ng_rois, "bounds": (0, 0, fw, fh)}
        if cam_ng:
            with self.ng_frames_lock:
                if self.cycle_id != mcid or self.plc_cycle_aborted:
                    return
                self.history_ng_frames.append((sk, pf, roi_view))
                if not pm_mode:
                    shift, production_date, _ = self._get_shift_and_date()
                    history_dir = self._body_history_directory(recipe_name, production_date, shift)
                    future = self._submit_save(
                        self.save_history_camera_image, pf, cam, vitri, barcode,
                        history_dir, len(self.history_ng_frames))
                    if future is None:
                        error_text = f"Không thể xếp lịch lưu ảnh NG: barcode={barcode!r}, CAM={cam}, VT={vitri}"
                        self._register_point_error(f"{sk}_SAVE", error_text)
                        raise RuntimeError(error_text)
                    self.ng_frame_save_futures.append(future)
        with self.logical_frames_lock:
            self.logical_frames[sk] = pf
            self.logical_roi_views[sk] = roi_view
        if cam_ng and pm_mode:
            self._submit_save(self.save_picture_ng, cam, pf, vitri, barcode)
        self.ui_queue.put({"type": "image", "frame": pf, "roi_views": [roi_view]})

    # ================= GRID BUILDER =================
    def _label_cell(self, img, text):
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.9, 2)
        cv2.rectangle(img, (4, 4), (tw + 16, th + 16), (0, 0, 0), -1)
        cv2.putText(img, text, (8, th + 10), cv2.FONT_HERSHEY_SIMPLEX,
                    0.9, (0, 0, 255), 2, cv2.LINE_AA)

    def build_ng_grid(self, d):
        if not d:
            return None, []
        W, H = 1000, 528
        cols = 2
        items = sorted(d.items())
        n = len(items)
        if n == 1:
            label, (frame, roi_view) = items[0]
            full = cv2.resize(frame, (W, H), interpolation=cv2.INTER_AREA)
            cv2.rectangle(full, (0, 0), (W - 1, H - 1), (0, 0, 255), 4)
            self._label_cell(full, label)
            return full, [dict(roi_view, bounds=(0, 0, W, H))]
        rows = (n + cols - 1) // cols
        cw = W // cols
        grid = np.zeros((H, W, 3), dtype=np.uint8)
        roi_views = []
        for i, (label, (frame, roi_view)) in enumerate(items):
            r, c = divmod(i, cols)
            top, bottom = r * H // rows, (r + 1) * H // rows
            ch = bottom - top
            cell = cv2.resize(frame, (cw, ch), interpolation=cv2.INTER_AREA)
            cv2.rectangle(cell, (0, 0), (cw - 1, ch - 1), (0, 0, 255), 3)
            self._label_cell(cell, label)
            grid[top:bottom, c * cw:(c + 1) * cw] = cell
            roi_views.append(dict(roi_view, bounds=(c * cw, top, cw, ch)))
        return grid, roi_views

    def get_run_ng_frames(self):
        return {f"{label} #{index}": (frame, roi_view)
                for index, (label, frame, roi_view) in enumerate(self.history_ng_frames, 1)}

    # ================= UI LOOP + DISPATCH =================
    def _init_ui_handlers(self):
        self._ui_handlers = {
            "progress": lambda d: self.widget_station_infor.label_7.config(
                text=f'{d["current"]}/{d["total"]}', fg="blue"),
            "image": lambda d: self.update_image(d["frame"], d["roi_views"]),
            "counter": self._h_counter,
            "ng_point": lambda d: self.widget_result_check.listNGbox.insert(tk.END, d["text"]),
            "button_ng": self._h_button_ng,
            "final_result": self._h_final_result,
            "result_step": self._h_result_step,
            "ng_grid": self._h_ng_grid,
            "START CHECK": self._h_start_check,
            "barcode_progress": lambda d: self.widget_station_infor.show_body_numbers(d["body_numbers"]),
            "reset_cycle_display": self._h_reset_cycle_display,
            "start_timer": lambda d: self._start_timer_ui(),
            "stop_cycle": self._h_stop_cycle,
            "statistics_refresh_summary": self._h_stats_refresh,
            "inspection_error": self._h_inspection_error,
            "plc_write_failed": self._h_plc_failed,
            "camera_startup_error": self._h_cam_error,
            "camera_rescan": self._h_camera_rescan,
            "plc_program_sent": self._h_plc_program_sent,
        }

    def ui_loop(self):
        try:
            while not self.ui_queue.empty():
                try:
                    data = self.ui_queue.get_nowait()
                except Empty:
                    break
                mtype = data.get("type")
                h = self._ui_handlers.get(mtype)
                if not h:
                    continue
                try:
                    h(data)
                except (tk.TclError, AttributeError):
                    pass
                except Exception:
                    print(f"UI error [{mtype}]:")
                    print(traceback.format_exc())
        except Exception as e:
            print("UI loop error:", e)
        self.after(20, self.ui_loop)

    def _h_counter(self, d):
        if "ok" in d: self.widget_station_infor.label_11.config(text=d["ok"])
        if "ng" in d: self.widget_station_infor.label_12.config(text=d["ng"])
        if "all" in d: self.widget_station_infor.label_13.config(text=d["all"])

    def _h_final_result(self, d):
        r = d["result"]
        font_size = 50 if r in ("OK", "NG") else 32
        if r in ("CYCLE ERROR", "PLC_RESET"):
            font_size = -30  # Tk uses pixels for negative font sizes.
        self.widget_result_check.label2.configure(
            text=r,
            bg="green" if r == "OK" else "red", font=("Arial", font_size))

    def _h_result_step(self, d):
        s = d["step"]
        t = (len(self.cycle_steps_snapshot) if self.cycle_steps_snapshot
             else len(self.recipe.plc_steps))
        self.widget_result_check.label1.configure(
            text=f"KẾT QUẢ ({s}/{t})" if s else f"KẾT QUẢ (0/{t})")

    def _h_ng_grid(self, d):
        if self.selected_body_history is not None:
            return
        self.widget_result_check.label_image_1.show_frame(d["frame"], roi_views=d["roi_views"])
        self.last_display_mode = "ng_grid"

    def _h_reset_cycle_display(self, d):
        self.widget_station_infor.show_body_numbers(())
        self.widget_station_infor.label_7.config(text=f'0/{d["total"]}', fg="black")

    def _h_start_check(self, d):
        self.clear_body_history_selection()
        self.widget_result_check.label_image_1.show_text(text="BẮT ĐẦU KIỂM TRA")
        self.widget_result_check.label2.configure(text="---", bg="blue")
        self.widget_station_infor.show_body_numbers(d["body_numbers"])
        self.doimau()
        self.widget_result_check.listNGbox.delete(0, tk.END)

    def _h_button_ng(self, d):
        if self.selected_body_history is None:
            self.widget_button.set_cam_button_color(d["attr"], "violet", "red")

    def _h_stop_cycle(self, d):
        self.widget_result_check.label2.configure(text="ĐÃ DỪNG", bg="orange")

    def _h_stats_refresh(self, d):
        self.load_body_history()
        if (self.statistics_window is not None
                and self.statistics_window.winfo_exists()):
            self.search_statistics()

    def _show_error_message(self, title, text):
        print(title)
        print(text)
        # self.after(0, lambda: messagebox.showerror(title, text, parent=self))

    def _h_inspection_error(self, d):
        self._show_error_message("Lỗi kiểm tra", d.get("text", "Đã xảy ra lỗi khi kiểm tra"))

    def _h_plc_failed(self, d):
        self._show_error_message(
            "Lỗi ghi PLC",
            f"Không thể ghi {d['reg']}={d.get('value')} tới PLC "
            f"{SystemConfig.PLC_HOST}:{SystemConfig.PLC_PORT}.\nKiểm tra kết nối PLC.")

    def _h_cam_error(self, d):
        if self.camera_popup_showing:
            return
        self.camera_popup_showing = True
        reasons = d.get("reasons") or {}
        lines = []
        for c in d["cam_ids"]:
            r = reasons.get(c) or self.camera_last_error.get(c) or ""
            lines.append(f" • CAM {c}" + (f": {r}" if r else ""))
        msg = "LỖI CAMERA!"

        def _show(m=msg):
            try:
                messagebox.showerror("Lỗi camera", m, parent=self)
            finally:
                self.camera_popup_showing = False

        self.after(0, _show)

    def _h_plc_program_sent(self, d):
        prog = d.get("prog")
        try:
            self.widget_station_infor.label_4.config(
                text=f"{self.recipe.current_model_display}")

            print(f"D5={prog}")
        except Exception:
            pass

    def update_image(self, frame, roi_views):
        if self.selected_body_history is not None:
            return
        now = time.monotonic()
        if now - self._last_image_update_ts < SystemConfig.UI_IMAGE_UPDATE_INTERVAL:
            return
        self._last_image_update_ts = now
        self.widget_result_check.label_image_1.show_frame(frame, reset_view=False, roi_views=roi_views)
        if self.last_display_mode != "ng_grid":
            self.last_display_mode = "cam"

    @staticmethod
    def resize_image_max_width(image, max_width=1000):
        if image is None or image.size == 0:
            return None
        h, w = image.shape[:2]
        if w <= max_width:
            return image
        s = max_width / w
        return cv2.resize(image, (max_width, int(h * s)), interpolation=cv2.INTER_AREA)

    def doimau(self):
        self.widget_button._clear_cam_button_highlight()
        for bn in self.widget_button.buttons:
            self.widget_button.set_cam_button_color(bn, "green", "yellow")

    # ================= SAVE IMAGES (unified) =================
    def _save_image(self, cat, cam, frame, vitri, point=None,
                    suffix="", ext="jpg", barcode=None):
        if frame is None or frame.size == 0:
            return
        try:
            bc = barcode or self.current_barcode or self.databacode or "UNKNOWN"
            sp, now = self._get_dynamic_save_path(cat, cam, vitri, point)
            ts = now.strftime("%H%M%S_%f")[:-3]
            sbc = self._safe_filename_part(bc, 120)
            fn = f"{sbc}_{ts}{suffix}.{ext}"
            cv2.imwrite(os.path.join(sp, fn), frame)
        except Exception as e:
            print(f"WARN: Failed to save {cat} (CAM{cam}): {e}")

    def save_picture(self, c, f, v, b=None):
        self._save_image("save_all_file", c, f, v, barcode=b)

    def save_picture_ng(self, c, f, v, b=None):
        self._save_image("save_file_ng", c, f, v, barcode=b)

    def save_history_camera_image(self, frame, cam, vitri, barcode, history_dir, frame_no):
        image_dir = Path(history_dir) / f"cam{cam}" / f"vitri{vitri}"
        image_dir.mkdir(parents=True, exist_ok=True)
        image_name = f"{self._safe_filename_part(barcode, 120)}_{datetime.now():%H%M%S_%f}.jpg"
        image_path = image_dir / image_name
        if not cv2.imwrite(str(image_path), frame):
            raise IOError(
                f"Không lưu được ảnh NG: path={image_path}, barcode={barcode!r}, CAM={cam}, VT={vitri}")
        return {"frame_no": frame_no, "camera": cam, "vitri": vitri,
                "image_path": str(image_path.resolve())}

    def save_crop_all(self, c, f, v, p, n, pr, b=None):
        self._save_image("save_crop_all", c, f, v, point=p,
                         suffix=f"_{self._safe_filename_part(n)}_{pr:.4f}",
                         ext="png", barcode=b)

    def save_crop_ng(self, c, f, v, p, n, pr, b=None):
        self._save_image("save_crop_ng", c, f, v, point=p,
                         suffix=f"_{self._safe_filename_part(n)}_{pr:.4f}",
                         ext="png", barcode=b)

    # ================= CYCLE INIT =================
    def prepare_network_cycle(self, body_numbers, pm_mode=False, steps_snapshot=None):
        if not body_numbers or any(not isinstance(code, str) or not code.strip() for code in body_numbers):
            raise ValueError(
                f"Body No không hợp lệ: barcode={body_numbers!r}, recipe={self.recipe.current_model_display!r}, "
                f"PM={pm_mode}, cycle={self.cycle_id}")
        # Lưu các Body No theo thứ tự camera trong trường barcode hiện có.
        code = " | ".join(body_numbers)
        steps_snapshot = dict(steps_snapshot or self.recipe.plc_steps)
        expected = self._build_expected_points(steps_snapshot)
        if not expected:
            msg = (f"'{self.recipe.current_model_display}' "
                   "không có điểm kiểm tra.")
            print(f"ERROR: Recipe '{self.recipe.current_model_display}' "
                  "has no inspection points")
            with self.plc_lock:
                self.plc_cycle_aborted = True
                self.reset_plc_cycle_locked()
            self.ui_queue.put({"type": "inspection_error", "text": msg})
            self.ui_queue.put({"type": "final_result", "result": "CYCLE ERROR"})
            return False
        self.databacode = code
        self.current_barcode = code
        self.cycle_recipe_name = self.recipe.current_model_display
        self.cycle_pm_mode = pm_mode
        self.cycle_steps_snapshot = steps_snapshot
        self.cycle_step_delays = dict(self.recipe.plc_step_delays)
        self.cycle_expected_points = expected
        active_groups = {
            CameraKey.point_group_key(cam, vt)
            for pairs in steps_snapshot.values() for cam, vt in self._sanitize_pairs(pairs)
        }
        self.cycle_unit_configuration = UnitConfiguration(
            copy.deepcopy({key: self.recipe.points_cfg[key] for key in active_groups}),
            list(self.recipe.presence_points), self.cycle_recipe_name)
        with self.cycle_state_lock:
            self.cycle_completed_points.clear()
            self.cycle_point_errors.clear()
            self.cycle_point_results.clear()
            self.cycle_assessment = None
            self.cycle_render_frames.clear()
            self.cycle_counts_written = False
        self.current_check_no = None
        with self.cycle_state_lock:
            self.list_ng.clear()
            self.ng_cams.clear()
        with self.logical_frames_lock:
            self.logical_frames.clear()
            self.logical_roi_views.clear()
        with self.ng_frames_lock:
            self.history_ng_frames = []
            self.ng_frame_save_futures = []
        self.pointcheck = 0
        self.current_cam_name = None
        with self.allow_put_lock:
            self.allow_put = True
        with self.queue_put_lock:
            self.queue_put_count_step = 0
            self.queue_put_count_failed = 0
        with self.futures_lock:
            self.process_futures.clear()
        while not self.queue_xl_anh.empty():
            try:
                self.queue_xl_anh.get_nowait()
            except Exception:
                pass
        self.ui_queue.put({"type": "START CHECK", "body_numbers": body_numbers})
        print(f"CYCLE: recipe={self.cycle_recipe_name}, PM={pm_mode}, "
              f"steps={len(steps_snapshot)}, points={len(self.cycle_expected_points)}, barcode={code!r}")
        return True

    def _start_timer_ui(self):
        self.update_label()
        self.demtime()

    def demtime(self):
        if self.timer_active:
            return
        self.timer_active = True

        def _upd():
            if not self.timer_active:
                return
            with self.timer_lock:
                running = self.stop
                if running:
                    self.dem = time.monotonic() - self.timer_started_at
            self.update_label()
            if running:
                self.after(10, _upd)
            else:
                self.timer_active = False

        _upd()

    def update_label(self):
        try:
            self.widget_station_infor.label_6.configure(text=f"{self.dem:.2f} s".replace(".", ","))
        except Exception:
            pass

    # ================= STATISTICS =================
    @staticmethod
    def _get_shift_display_name(shift):
        return {"ca_ngay": "Ca ngày", "ca_dem": "Ca đêm",
                "ngoai_ca": "Ngoài ca"}.get(shift, shift or "")

    @staticmethod
    def _get_shift_internal_code(display_name):
        return {"Tất cả": "Tất cả", "Ca ngày": "ca_ngay",
                "Ca đêm": "ca_dem", "Ngoài ca": "ngoai_ca"}.get(
            display_name, display_name)

    def update_statistics_summary(self, rows):
        self.statistics_total_var.set(f"Tổng theo bộ lọc: {len(rows)}")
        self.statistics_ok_var.set(
            f"OK: {sum(row['final_status'] == 'OK' for row in rows)}")
        self.statistics_ng_var.set(
            f"NG: {sum(row['final_status'] == 'NG' for row in rows)}")

    def set_current_shift_statistics_filter(self):
        self.statistics_recipe_var.set("Tất cả")
        try:
            si = ShiftSchedule.get_shift_info()
            self.statistics_date_from_var.set(si["date"])
            self.statistics_date_to_var.set(si["date"])
            self.statistics_shift_var.set(self._get_shift_display_name(si["shift"]))
            self.statistics_filter_status_var.set("Tất cả")
            self.statistics_status_var.set("Đang hiển thị dữ liệu của ca hiện tại")
        except Exception:
            pass

    def open_statistics_window(self, event=None):
        if (self.statistics_window is not None
                and self.statistics_window.winfo_exists()):
            self.statistics_window.lift()
            self.statistics_window.focus_force()
            return "break"
        try:
            recipes = sorted(set(get_statistics_recipes()).union(self.recipe.available_models),
                             key=str.casefold)
        except RuntimeError as exc:
            messagebox.showerror("Lỗi đọc danh sách recipe", str(exc), parent=self)
            raise
        w = tk.Toplevel(self)
        w.title("Thống kê")
        w.geometry("1500x650")
        w.minsize(1100, 500)
        w.state("zoomed")
        w.protocol("WM_DELETE_WINDOW", self.close_statistics_window)
        self.statistics_window = w
        self.set_current_shift_statistics_filter()
        w.rowconfigure(1, weight=1)
        w.columnconfigure(0, weight=1)

        sf = tk.Frame(w, padx=10, pady=10)
        sf.grid(row=0, column=0, sticky=tk.EW)
        for c in (1, 3, 5):
            sf.columnconfigure(c, weight=1)
        tk.Label(sf, text="Body No:", font=("Arial", 12)).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 6))
        be = tk.Entry(sf, textvariable=self.statistics_barcode_var, font=("Arial", 12))
        be.grid(row=0, column=1, sticky=tk.EW, padx=(0, 12))
        be.bind("<Return>", self.search_statistics)
        try:
            recent = get_recent_production_dates()
        except Exception:
            recent = []
        tk.Label(sf, text="Từ ngày:", font=("Arial", 12)).grid(
            row=0, column=2, sticky=tk.W, padx=(0, 6))
        cbf = ttk.Combobox(sf, textvariable=self.statistics_date_from_var,
                           values=recent, state="readonly", width=14)
        cbf.grid(row=0, column=3, sticky=tk.W, padx=(0, 12))
        cbf.bind("<<ComboboxSelected>>", self.search_statistics)
        tk.Label(sf, text="Đến ngày:", font=("Arial", 12)).grid(
            row=0, column=4, sticky=tk.W, padx=(0, 6))
        cbt = ttk.Combobox(sf, textvariable=self.statistics_date_to_var,
                           values=recent, state="readonly", width=14)
        cbt.grid(row=0, column=5, sticky=tk.W, padx=(0, 12))
        cbt.bind("<<ComboboxSelected>>", self.search_statistics)
        tk.Label(sf, text="Ca:", font=("Arial", 12)).grid(
            row=1, column=0, sticky=tk.W, pady=(8, 0), padx=(0, 6))
        cbs = ttk.Combobox(sf, textvariable=self.statistics_shift_var,
                           values=("Tất cả", "Ca ngày", "Ca đêm"),
                           state="readonly", width=14)
        cbs.grid(row=1, column=1, sticky=tk.W, pady=(8, 0), padx=(0, 12))
        cbs.bind("<<ComboboxSelected>>", self.search_statistics)
        tk.Label(sf, text="Kết quả:", font=("Arial", 12)).grid(
            row=1, column=2, sticky=tk.W, pady=(8, 0), padx=(0, 6))
        cbst = ttk.Combobox(sf, textvariable=self.statistics_filter_status_var,
                            values=("Tất cả", "ĐANG CHỜ", "OK", "NG", "LỖI"),
                            state="readonly", width=14)
        cbst.grid(row=1, column=3, sticky=tk.W, pady=(8, 0), padx=(0, 12))
        cbst.bind("<<ComboboxSelected>>", self.search_statistics)
        tk.Label(sf, text="Recipe:", font=("Arial", 12)).grid(
            row=1, column=4, sticky=tk.W, pady=(8, 0), padx=(0, 6))
        cbr = ttk.Combobox(sf, textvariable=self.statistics_recipe_var,
                           values=("Tất cả", *recipes), state="readonly", width=20)
        cbr.grid(row=1, column=5, sticky=tk.EW, pady=(8, 0), padx=(0, 12))
        cbr.bind("<<ComboboxSelected>>", self.search_statistics)

        actions = tk.Frame(sf)
        actions.grid(row=1, column=6, sticky=tk.W, pady=(8, 0))
        tk.Button(actions, text="Lọc", width=10, command=self.search_statistics).pack(
            side=tk.LEFT, padx=(0, 8))
        tk.Button(actions, text="Xóa bộ lọc", width=10, command=self.clear_statistics_search).pack(
            side=tk.LEFT, padx=(0, 8))
        tk.Button(actions, text="Xuất .txt", width=10, command=self.export_statistics_txt).pack(
            side=tk.LEFT)

        content = tk.Frame(w)
        content.grid(row=1, column=0, sticky=tk.NSEW)
        content.grid_propagate(False)
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=4, uniform="statistics")
        content.columnconfigure(1, weight=2, uniform="statistics")

        tf = tk.Frame(content, padx=10)
        tf.grid(row=0, column=0, sticky=tk.NSEW)
        tf.rowconfigure(0, weight=1)
        tf.columnconfigure(0, weight=1)
        cols = ("stt", "barcode", "check_no", "shift", "final_status",
                "ng_points", "created_at", "updated_at")
        tree = ttk.Treeview(tf, columns=cols,
                            displaycolumns=("stt", "barcode", "final_status", "updated_at"),
                            show="headings", selectmode="browse", height=18)
        ys = ttk.Scrollbar(tf, orient=tk.VERTICAL, command=tree.yview)
        xs = ttk.Scrollbar(tf, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        heads = {"stt": "STT", "barcode": "Body No",
                 "check_no": "Lần kiểm tra", "shift": "Ca",
                 "final_status": "Kết quả", "ng_points": "Danh sách điểm NG",
                 "created_at": "Bắt đầu kiểm tra",
                 "updated_at": "Thời gian check"}
        wds = {"stt": 50, "barcode": 80, "check_no": 110, "shift": 85,
               "final_status": 125, "ng_points": 300, "created_at": 190, "updated_at": 80}
        for c in cols:
            tree.heading(c, text=heads[c])
            tree.column(c, width=wds[c], minwidth=50,
                        anchor=tk.CENTER if c == "stt" else tk.W,
                        stretch=c in ("barcode", "updated_at"))
        tree.grid(row=0, column=0, sticky=tk.NSEW)
        ys.grid(row=0, column=1, sticky=tk.NS)
        xs.grid(row=1, column=0, sticky=tk.EW)
        self.statistics_tree = tree
        if SystemConfig.ALLOW_DELETE_STATISTICS_RECORDS:
            self.statistics_menu = tk.Menu(tree, tearoff=0)
            self.statistics_menu.add_command(label="Xóa bản ghi")
            tree.bind("<Button-3>", self.show_statistics_menu)
        self.statistics_detail = StatisticsDetail(content)
        self.statistics_detail.grid(row=0, column=1, sticky=tk.NSEW)
        tree.bind("<<TreeviewSelect>>", self.show_statistics_detail)

        smf = tk.Frame(w, padx=10, pady=6)
        smf.grid(row=2, column=0, sticky=tk.EW)
        smf.columnconfigure(3, weight=1)
        tk.Label(smf, textvariable=self.statistics_total_var,
                 font=("Arial", 12, "bold")).grid(row=0, column=0, sticky=tk.W, padx=(0, 20))
        tk.Label(smf, textvariable=self.statistics_ok_var,
                 font=("Arial", 12, "bold"), fg="green").grid(
            row=0, column=1, sticky=tk.W, padx=(0, 20))
        tk.Label(smf, textvariable=self.statistics_ng_var,
                 font=("Arial", 12, "bold"), fg="red").grid(
            row=0, column=2, sticky=tk.W)
        tk.Label(w, textvariable=self.statistics_status_var, anchor=tk.W,
                 padx=10, pady=6).grid(row=3, column=0, sticky=tk.EW)
        self.search_statistics()
        return "break"

    def show_statistics_detail(self, event=None):
        selected = self.statistics_tree.selection()
        record = self.statistics_rows.get(selected[0]) if selected else None
        shift_name = self._get_shift_display_name(record["shift"]) if record else ""
        self.statistics_detail.show_record(record, shift_name)

    def show_statistics_menu(self, event):
        tree = self.statistics_tree
        row = tree.identify_row(event.y)
        if not row or tree.identify_region(event.x, event.y) != "cell":
            return "break"
        tree.selection_set(row)
        tree.focus(row)
        values = tree.item(row, "values")
        self.statistics_menu.entryconfigure(
            0, command=lambda: self.delete_statistics_record(
                int(row), values[1], int(values[2])))
        try:
            self.statistics_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.statistics_menu.grab_release()
        return "break"

    def delete_statistics_record(self, check_id, barcode, check_no):
        if not SystemConfig.ALLOW_DELETE_STATISTICS_RECORDS:
            raise PermissionError("Chức năng xóa bản ghi thống kê đang tắt (ALLOW_DELETE_STATISTICS_RECORDS=False)")
        if not messagebox.askyesno(
                "Xóa bản ghi",
                f"Xóa bản ghi Body No: {barcode}\nLần kiểm tra: {check_no}\n"
                "và dữ liệu NG của lần kiểm tra này?",
                parent=self.statistics_window):
            return
        try:
            delete_inspection_check(check_id, barcode, check_no)
        except Exception as exc:
            messagebox.showerror("Lỗi xóa bản ghi", str(exc),
                                 parent=self.statistics_window)
            raise
        self.search_statistics()
        self.load_body_history()

    def close_statistics_window(self):
        if (self.statistics_window is not None
                and self.statistics_window.winfo_exists()):
            self.statistics_window.destroy()
        self.statistics_window = None
        self.statistics_tree = None
        self.statistics_detail = None
        self.statistics_rows.clear()

    def clear_statistics_search(self):
        self.statistics_barcode_var.set("")
        self.set_current_shift_statistics_filter()
        self.search_statistics()

    def search_statistics(self, event=None):
        bc = self.statistics_barcode_var.get().strip()
        recipe = self.statistics_recipe_var.get()
        df = self.statistics_date_from_var.get().strip()
        dt = self.statistics_date_to_var.get().strip()
        sd = self.statistics_shift_var.get().strip()
        fsd = self.statistics_filter_status_var.get().strip()
        sh = self._get_shift_internal_code(sd)
        fst = {"Tất cả": "Tất cả", "ĐANG CHỜ": "PENDING", "LỖI": "ERROR"}.get(fsd, fsd)
        try:
            rows = query_statistics_checks(barcode=bc, date_from=df,
                                           date_to=dt, shift=sh,
                                           final_status=fst,
                                           recipe="" if recipe == "Tất cả" else recipe)
        except Exception as exc:
            messagebox.showerror("Lỗi tìm kiếm thống kê",
                                 f"Không thể tìm kiếm dữ liệu.\nChi tiết: {exc}",
                                 parent=self.statistics_window)
            return False
        if self.statistics_tree is None:
            return False
        selection = self.statistics_tree.selection()
        self.statistics_rows = {str(row["id"]): row for row in rows}
        for it in self.statistics_tree.get_children():
            self.statistics_tree.delete(it)
        for i, row in enumerate(rows, start=1):
            status = {"PENDING": "ĐANG CHỜ", "OK": "OK",
                      "NG": "NG", "ERROR": "LỖI"}.get(
                row.get("final_status", ""), row.get("final_status", ""))
            self.statistics_tree.insert("", tk.END, iid=str(row["id"]), values=(
                i, row.get("barcode", ""), row.get("check_no", ""),
                self._get_shift_display_name(row.get("shift", "")),
                status,
                row["ng_points"],
                row.get("created_at", "") or "",
                row.get("updated_at", "") or ""))
        if selection and selection[0] in self.statistics_rows:
            self.statistics_tree.selection_set(selection[0])
            self.statistics_tree.focus(selection[0])
            self.statistics_tree.see(selection[0])
        else:
            self.statistics_detail.show_record(None, "")
        self.update_statistics_summary(rows)
        self.statistics_status_var.set(
            f"Số dòng: {len(rows)} | {bc or 'Tất cả'} | Recipe={recipe} | "
            f"{df or '...'}       {dt or '...'} | {sd} | Kết quả={fsd}")
        return "break"

    @staticmethod
    def _statistics_text_width(text):
        return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
                   for char in text if not unicodedata.combining(char))

    def export_statistics_txt(self):
        output_path = filedialog.asksaveasfilename(
            parent=self.statistics_window, title="Xuất thống kê theo bộ lọc",
            defaultextension=".txt", filetypes=[("Text files", "*.txt")],
            initialfile=f"thong_ke_{datetime.now():%Y%m%d_%H%M%S}.txt")
        if not output_path:
            return
        if self.search_statistics() != "break":
            return
        tree = self.statistics_tree
        headers = ["Cập nhật kết quả" if column == "updated_at" else tree.heading(column, "text")
                   for column in tree["columns"]]
        table = [headers] + [tree.item(item, "values") for item in tree.get_children()]
        table = [[unicodedata.normalize("NFC", str(value)).replace("\t", " ")
                  .replace("\r", " ").replace("\n", " ") for value in row]
                 for row in table]
        widths = [max(self._statistics_text_width(row[column]) for row in table)
                  for column in range(len(headers))]
        separator = "-+-".join("-" * width for width in widths)
        lines = [self.statistics_status_var.get(),
                 " | ".join((self.statistics_total_var.get(),
                             self.statistics_ok_var.get(),
                             self.statistics_ng_var.get())), ""]
        for index, row in enumerate(table):
            lines.append(" | ".join(
                value + " " * (width - self._statistics_text_width(value))
                for value, width in zip(row, widths)))
            if index == 0:
                lines.append(separator)
        output_file = Path(output_path)
        try:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        except OSError as exc:
            messagebox.showerror("Lỗi xuất thống kê",
                                 f"Không thể ghi file {output_file}\nChi tiết: {exc}",
                                 parent=self.statistics_window)
            raise RuntimeError(f"Unable to export statistics: path={output_file}, error={exc}") from exc
        messagebox.showinfo("Xuất thống kê", f"Đã xuất file:\n{output_file}",
                            parent=self.statistics_window)

    # ================= KEYBOARD SHORTCUTS =================
    def switch_cam(self, event=None):
        if self.selected_body_history is not None:
            keys = sorted(self.body_history_frames)
            if not keys:
                return
            current = self.body_history_cam_name
            index = (keys.index(current) + 1) % len(keys) if current in keys else 0
            self.widget_button._show_cam(keys[index])
            return
        self.widget_result_check.listNGbox.selection_clear(0, tk.END)
        with self.cycle_state_lock:
            ng_cams = tuple(self.ng_cams)
        with self.logical_frames_lock:
            frame_keys = set(self.logical_frames)
        ng = sorted(frame_keys.intersection(ng_cams) if ng_cams else frame_keys)
        if not ng:
            return
        try:
            pos = ng.index(self.current_cam_name)
            npos = (pos + 1) % len(ng)
        except ValueError:
            npos = 0
        self.widget_button._show_cam(ng[npos])

    def handle_space_switch_cam(self, event=None):
        if event is not None and isinstance(event.widget, (tk.Entry, ttk.Entry)):
            return None
        self.switch_cam(event)
        return "break"

    def show_image_click_list(self, event=None):
        lb = (event.widget if (event is not None and hasattr(event, "widget"))
              else self.widget_result_check.listNGbox)
        si = lb.curselection()
        if not si:
            return "break"
        i = si[0]
        with self.cycle_state_lock:
            if i >= len(self.list_ng):
                return "break"
            ci = self.list_ng[i]
        if ci is None or ci.size == 0:
            return "break"
        title = lb.get(i)
        img = cv2.resize(ci, (224, 224), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        if self.crop_viewer is not None and self.crop_viewer.winfo_exists():
            self.crop_viewer.destroy()
        w = tk.Toplevel(self)
        w.title(f"Ảnh cắt NG: {title}")
        w.geometry("260x290")
        w.resizable(False, False)
        self.crop_viewer = w
        pi = Image.fromarray(rgb)
        itk = ImageTk.PhotoImage(pi)
        lbl = tk.Label(w, image=itk)
        lbl.image = itk
        lbl.pack(padx=4, pady=4)
        tk.Label(w, text=title, font=("Arial", 10, "bold")).pack(pady=(0, 6))
        return "break"

    def emergency_stop_cycle(self, event=None):
        self.fail_plc_cycle("ESC pressed by operator")
        return "break"

    def show_ng_grid_shortcut(self, event=None):
        self.widget_button.show_ng_frames()
        return "break"

    @staticmethod
    def get_local_ip():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
                connection.connect(("8.8.8.8", 80))
                return connection.getsockname()[0]
        except OSError as exc:
            print(f"Cannot determine local IP address: error={exc}")
            return None

    # ================= SHUTDOWN =================
    def on_closing(self):
        with self.camera_state_lock:
            if not self.camera_running:
                return
            self.camera_running = False
        print("Closing application...")
        self.title(f"{self.title()} - Đang đóng camera...")
        with self.frame_condition:
            self.frame_condition.notify_all()
        self._camera_stop_event.set()
        self.close_allcam()
        self.stop_plc_control()
        warn_at = time.monotonic() + SystemConfig.CAMERA_SHUTDOWN_WARN_SECONDS
        self._wait_for_camera_shutdown(warn_at)

    def _wait_for_camera_shutdown(self, warn_at):
        pending = [cam_id for cam_id, reader in self.camera_reader_threads.items()
                   if reader.is_alive()]
        if not pending:
            self._finish_shutdown()
            return
        if warn_at is not None and time.monotonic() >= warn_at:
            MAIN_LOG.warning("Camera shutdown still waiting for readers: cam_ids=%s", sorted(pending))
            warn_at = None
        self.after(100, self._wait_for_camera_shutdown, warn_at)

    def _finish_shutdown(self):
        if self.crop_viewer is not None and self.crop_viewer.winfo_exists():
            try:
                self.crop_viewer.destroy()
            except Exception:
                pass
        try:
            pending = self.pending_save_tasks
            if pending > 0:
                print(f"Waiting for {pending} save tasks...")
            done = threading.Event()

            def _wait():
                try:
                    self.executor_save.shutdown(wait=True)
                finally:
                    done.set()

            threading.Thread(target=_wait, daemon=True).start()
            if not done.wait(timeout=SystemConfig.SAVE_EXECUTOR_SHUTDOWN_TIMEOUT):
                print(f"WARN: Save tasks did not finish within {SystemConfig.SAVE_EXECUTOR_SHUTDOWN_TIMEOUT}s")
        except Exception as e:
            print(f"WARN: Save shutdown failed: {e}")
        for ex in (self.executor_capture, self.executor_barcode, self.executor, self.executor_ai):
            try:
                ex.shutdown(wait=False)
            except Exception:
                pass
        print("Application closed")
        self.destroy()


# ================= ENTRY POINT =================
if __name__ == "__main__":
    app_ui = MainApplication()

    app_ui.bind("<Control-Shift-p>", app_ui.toggle_pm_mode)
    app_ui.bind("<Control-Shift-P>", app_ui.toggle_pm_mode)
    app_ui.bind("<Control-o>", lambda e: app_ui.open_statistics_window())
    app_ui.bind("<Control-O>", lambda e: app_ui.open_statistics_window())
    app_ui.bind("<Control-n>", lambda e: app_ui.show_ng_grid_shortcut())
    app_ui.bind("<Control-N>", lambda e: app_ui.show_ng_grid_shortcut())
    app_ui.focus_force()
    app_ui.bind("<space>", app_ui.handle_space_switch_cam)
    app_ui.bind("<Escape>", lambda e: app_ui.emergency_stop_cycle())

    app_ui.protocol("WM_DELETE_WINDOW", app_ui.on_closing)
    app_ui.mainloop()
