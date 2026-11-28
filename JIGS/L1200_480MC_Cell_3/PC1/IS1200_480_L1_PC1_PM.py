import base64
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
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
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


# IS1200_480: coordinator/worker protocol. No YOLO or Tk import.
#
# Safety invariants:
# - Only PC1 opens PLC COM1. Worker messages NEVER contain PLC bytes.
# - All stage/results are keyed by one immutable cycle_id; stale/duplicate dropped.
# - Missing capture, inference error, peer timeout -> FAULT, never OK/NG PLC.
# - PLC 0x33 is issued after VT1 completion; VT2 has no PLC ACK.
# - Final 0x35/0x36 is issued after three valid AI results and both worker ACK_VT2 confirmations.
# - PM/RUN and Windows boot markers are separate from app restarts.

class CoalescedUIQueue:
    """Prevent full-resolution Tk preview frames accumulating in RAM.

    A user only needs the latest camera preview. Control/warning/result events
    retain regular FIFO order; preview frames share one replaceable slot.
    """
    def __init__(self):
        import queue
        self._events = queue.Queue()
        self._last_image = None
        self._lock = threading.Lock()

    def put(self, item, block=True, timeout=None):
        if isinstance(item,dict) and item.get('type') == 'image':
            with self._lock:self._last_image = item
            return
        self._events.put(item, block=block, timeout=timeout)

    def get_nowait(self):
        import queue
        try:return self._events.get_nowait()
        except queue.Empty:
            with self._lock:
                item=self._last_image
                self._last_image=None
            if item is None:raise queue.Empty
            return item

    def empty(self):
        with self._lock:has_image=self._last_image is not None
        return self._events.empty() and not has_image

    def qsize(self):
        with self._lock:has_image=self._last_image is not None
        return self._events.qsize() + int(has_image)


class BoundedExecutor(ThreadPoolExecutor):
    """Cap pending save tasks so hours of disk latency cannot exhaust RAM."""
    def __init__(self,max_workers=4,max_pending=320):
        super().__init__(max_workers=max_workers)
        self._slots=threading.BoundedSemaphore(max_pending)

    def submit(self,fn,*args,**kwargs):
        if not self._slots.acquire(timeout=10.0):
            raise RuntimeError('SAVE_BACKPRESSURE: save task queue full')
        try:
            future=super().submit(fn,*args,**kwargs)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _future:self._slots.release())
        return future


CTRL_PORT = 23456
EVENT_PORT = 23458
ABORT_PORT = 6101
MAX_BYTES = 5 * 1024 * 1024
DEFAULT_TOKEN = 'IS1200_480_CHANGE_ME'


def boot_marker():
    """Best-effort Windows boot time, stable over restarts of inspection app.

    Windows Fast Startup / hibernation may preserve the kernel session: production
    deployment must DISABLE Fast Startup if a physical power cycle must force PM.
    """
    try:
        import psutil
        t = psutil.boot_time()
    except ImportError:
        if os.name == 'nt':
            import ctypes
            uptime_sec = ctypes.windll.kernel32.GetTickCount64() / 1000.0
        else:
            with open('/proc/uptime', encoding='ascii') as f:
                uptime_sec = float(f.read().split()[0])
        t = time.time() - uptime_sec
    # psutil exposes a stable kernel boot timestamp. Fallback from uptime
    # can drift slightly; 10s buckets avoid false resets on app restart.
    return int(t) if 'psutil' in locals() else int(t // 10)


def send_json(host, port, obj, timeout=4.0):
    raw = (json.dumps(obj, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf8')
    if len(raw) > MAX_BYTES:
        raise ValueError('message exceeds limit')
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(raw)
        return recv_json(s)


def recv_json(conn):
    buf = bytearray()
    while len(buf) < MAX_BYTES:
        data = conn.recv(min(65536, MAX_BYTES - len(buf)))
        if not data:
            break
        buf.extend(data)
        if b'\n' in data:
            break
    if not buf or b'\n' not in buf:
        raise ValueError('invalid or oversize JSON line')
    obj = json.loads(buf.split(b'\n', 1)[0].decode('utf8'))
    if not isinstance(obj, dict):
        raise ValueError('message is not object')
    return obj


def serve_loop(host, port, handler, label):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, int(port)))
    server.listen(24)
    server.settimeout(1)
    print(f'[IS480] {label} on {host}:{port}')
    try:
        while True:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            def task(c=conn, a=addr):
                with c:
                    c.settimeout(12)
                    try:
                        answer = handler(recv_json(c), a)
                        raw = (json.dumps(answer or {'ok': True}, ensure_ascii=False) + '\n').encode('utf8')
                        c.sendall(raw)
                    except Exception as exc:
                        try:
                            c.sendall((json.dumps({'ok': False, 'error': str(exc)}) + '\n').encode('utf8'))
                        except OSError:
                            pass
            threading.Thread(target=task, daemon=True).start()
    finally:
        server.close()


@dataclass
class Cycle:
    cid: str
    barcode: str
    mode: str
    created: float = field(default_factory=time.monotonic)
    stages: dict = field(default_factory=lambda: {1: {}, 2: {}, 'FINAL': {}})
    state: str = 'VT1'
    fault: str = ''
    verdict: str = ''
    check_no: int = 0
    ack_final: bool = False
    worker_boots: dict = field(default_factory=dict)


class CycleMachine:
    """Thread-safe state machine; only one active inspection cycle."""
    NODES = ('PC1', 'PC2', 'PC3')

    def __init__(self):
        self.lock = threading.RLock()
        self.cycle = None
        self.archived = set()

    def start(self, barcode, mode, cid=None):
        barcode = str(barcode or '').strip()
        if not barcode:
            raise ValueError('empty barcode')
        with self.lock:
            if self.cycle and self.cycle.state not in ('DONE', 'ABORTED'):
                raise RuntimeError('previous cycle is still active')
            cid = cid or f'IS480-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:9]}'
            self.cycle = Cycle(cid, barcode, mode)
            return cid

    def current(self, cid):
        with self.lock:
            return self.cycle is not None and self.cycle.cid == cid and self.cycle.state not in ('FAULT','DONE','ABORTED')

    def fail(self, reason, cid=None):
        with self.lock:
            c = self.cycle
            if c is None or (cid and c.cid != cid) or c.state in ('DONE', 'ABORTED'):
                return False
            if c.state != 'FAULT':
                c.fault = str(reason)
                c.state = 'FAULT'
                return True
            return False

    def record(self, cid, pc, stage, ok=True, verdict=None):
        with self.lock:
            c = self.cycle
            if not c or c.cid != cid or c.state in ('FAULT','DONE','ABORTED') or pc not in self.NODES:
                return False
            if stage not in (1, 2, 'FINAL'):
                return False
            wanted_state = {1: 'VT1', 2: 'VT2', 'FINAL': 'FINAL'}[stage]
            if c.state != wanted_state:
                return False
            if not ok:
                self.fail(f'{pc}: {stage} failed', cid)
                return False
            if stage == 'FINAL' and verdict not in ('OK','NG','PM_OK','PM_NG'):
                self.fail(f'{pc}: invalid final {verdict}', cid)
                return False
            c.stages[stage].setdefault(pc, verdict if stage == 'FINAL' else True)
            return len(c.stages[stage]) == 3

    def staged(self, stage):
        with self.lock:
            c = self.cycle
            return bool(c and len(c.stages[stage]) == 3)

    def move(self, expected, next_state):
        with self.lock:
            c = self.cycle
            if not c or c.state != expected:
                return False
            c.state = next_state
            return True

    def verdict(self):
        with self.lock:
            c = self.cycle
            if not c or c.state != 'FINAL' or len(c.stages['FINAL']) != 3:
                return None
            vals = tuple(c.stages['FINAL'].values())
            ng = any('NG' in v for v in vals)
            return ('PM_NG' if ng else 'PM_OK') if c.mode == 'PM' else ('NG' if ng else 'OK')

    def abort(self):
        with self.lock:
            if self.cycle and self.cycle.state != 'DONE':
                self.cycle.state = 'ABORTED'
                return True
            return False


class Bridge:
    """Network bridge; native Main_Application owns camera, AI and Tk UI.

    UI events passed exclusively via ui_queue. Worker links are command/ack and
    asynchronous completion via PC1's event server.
    """
    def __init__(self, app, pc_id, peers=None, token=DEFAULT_TOKEN):
        self.app = app
        self.pc_id = pc_id
        self.peers = peers or {}
        self.token = token
        self.machine = CycleMachine()
        self.master_ip = None
        self.local_boot = boot_marker()
        self.peer_boots = {}
        self.ready = True
        self.last_seen = {}
        self.workers_ready = threading.Event()
        self.mode_lock = threading.Lock()
        self.mode_state_lock = threading.Lock()
        self.pending_aborts = set()
        self._last_started = set()
        self._latency_timeout = 150.0
        self._repeat_lock = threading.Lock()
        self._repeat_stop = {}
        self._stage_sent = set()
        self._final_sent = set()
        # True only after BOTH workers confirm internal TCP ACK_VT2.
        self._vt2_release_cid = None
        self._last_health_poll = 0.0
        self._last_health_note = ''
        self._pending_local_final = None
        if pc_id == 'PC1':
            threading.Thread(target=serve_loop, args=('0.0.0.0', EVENT_PORT, self._on_event, 'master events'), daemon=True).start()
        else:
            threading.Thread(target=serve_loop, args=('0.0.0.0', CTRL_PORT, self._on_command, 'worker commands'), daemon=True).start()
            threading.Thread(target=serve_loop, args=('127.0.0.1', ABORT_PORT, self._on_abort, 'abort listener'), daemon=True).start()
            threading.Thread(target=self._worker_heartbeat, daemon=True).start()

    def _auth(self, msg):
        if msg.get('token') != self.token:
            raise PermissionError('invalid coordinator token')

    def _request(self, pc, command, **args):
        host = self.peers[pc]
        return send_json(host, CTRL_PORT, {'token': self.token, 'cmd': command, **args}, timeout=8)

    def _out_event(self, name, **kwargs):
        if self.pc_id == 'PC1':
            return
        if not self.master_ip:
            raise ConnectionError('no PC1 peer address learned yet')
        msg = {'token': self.token, 'event': name, 'pc': self.pc_id, 'boot': self.local_boot, **kwargs}
        response = send_json(self.master_ip, EVENT_PORT, msg, timeout=5)
        if not response.get('ok'):
            raise RuntimeError(response)

    def camera_faults(self):
        """Check only cameras physically configured by the *running* Main.

        Hard-coded PC3 CAM24 in v1.2 kept reporting an unplugged camera even
        after CAM24 had been intentionally removed from the PC3 Main program.
        This inventory is read after the original Main has mapped/initialized
        its cameras, so configured-but-missing cameras STILL fault safely.
        """
        import re
        cam_ref = getattr(self.app, 'CAM_LOCATION_REF', None)
        if not isinstance(cam_ref, dict) or not cam_ref:
            # Fail closed: losing the camera inventory is not equivalent to
            # all cameras being operational.
            return ['CAMERA_CONFIG_MISSING']
        ids = set()
        for key in cam_ref:
            match = re.fullmatch(r'cam(\d+)', str(key), flags=re.IGNORECASE)
            if match:
                ids.add(int(match.group(1)))
        if not ids:
            return ['CAMERA_CONFIG_INVALID']
        return [f'CAM{idx}' for idx in sorted(ids)
                if getattr(self.app, f'cap{idx}', None) is None]

    def _worker_heartbeat(self):
        while True:
            time.sleep(4)
            if self.master_ip:
                try:
                    self._out_event('HEARTBEAT', mode='PM' if self.app.PM else 'RUN',
                                    camera_faults=self.camera_faults())
                except Exception:
                    pass

    def _on_command(self, msg, addr):
        self._auth(msg)
        cmd = msg.get('cmd')
        if cmd == 'STATUS':
            self.master_ip = addr[0]
            return {'ok': True, 'pc': self.pc_id, 'boot': self.local_boot,
                    'camera_faults':self.camera_faults(), 'mode': 'PM' if self.app.PM else 'RUN'}
        if cmd == 'SET_MODE_PM':
            if self.machine.cycle and self.machine.cycle.state not in ('DONE','ABORTED','FAULT'):
                return {'ok': False, 'error': 'cycle active'}
            self.master_ip = addr[0]
            ready=threading.Event()
            self.app.ui_queue.put({'type': 'is480_mode_pm', 'applied':ready})
            if not ready.wait(5):return {'ok':False,'error':'Tk mode PM update timeout'}
            return {'ok': True, 'pc': self.pc_id, 'boot': self.local_boot}
        if cmd == 'SET_MODE_RUN':
            if self.machine.cycle and self.machine.cycle.state not in ('DONE','ABORTED','FAULT'):
                return {'ok': False, 'error': 'cycle active'}
            self.master_ip = addr[0]
            ready=threading.Event()
            self.app.ui_queue.put({'type': 'is480_mode_run', 'applied':ready})
            if not ready.wait(5):return {'ok':False,'error':'Tk mode RUN update timeout'}
            return {'ok': True, 'pc': self.pc_id, 'boot': self.local_boot}
        if cmd == 'ABORT':
            self.app.ui_queue.put({'type': 'is480_abort'})
            self.machine.abort()
            return {'ok': True, 'pc': self.pc_id}
        cid = str(msg.get('cycle_id') or '')
        if cmd == 'CAPTURE_VT1':
            if self.camera_faults():
                return {'ok': False, 'error': 'camera disconnected: '+str(self.camera_faults())}
            if not cid or not str(msg.get('barcode') or '').strip():
                return {'ok': False, 'error': 'missing cycle/barcode'}
            if self.machine.cycle and self.machine.cycle.cid == cid:
                return {'ok': True, 'duplicate': True}
            if self.machine.cycle and self.machine.cycle.state not in ('DONE','FAULT','ABORTED'):
                return {'ok': False, 'error': 'busy'}
            if str(msg.get('mode')) != ('PM' if self.app.PM else 'RUN'):
                return {'ok': False, 'error': 'mode mismatch'}
            self.master_ip = addr[0]
            self.machine.start(msg['barcode'], msg['mode'], cid=cid)
            self.app.ui_queue.put({'type': 'is480_start', 'cycle_id': cid, 'barcode': msg['barcode']})
            return {'ok': True, 'pc': self.pc_id, 'cycle_id': cid, 'boot': self.local_boot}
        if cmd == 'CAPTURE_VT2':
            if self.camera_faults():
                return {'ok':False,'error':'camera disconnected: '+str(self.camera_faults())}
            if not self.machine.current(cid) or self.machine.cycle.state != 'VT1_ACK':
                return {'ok': False, 'error': 'VT1 not acknowledged'}
            self.machine.move('VT1_ACK', 'VT2')
            self.app.ui_queue.put({'type': 'is480_vt2', 'cycle_id': cid})
            return {'ok': True, 'pc': self.pc_id, 'cycle_id': cid}
        if cmd == 'ACK_VT1':
            c = self.machine.cycle
            if c and c.cid == cid and c.state in ('VT1','VT1_ACK'):
                if c.state == 'VT1':
                    self.machine.move('VT1','VT1_ACK')
                return {'ok': True}
            return {'ok': False, 'error':'wrong stage for VT1 ACK'}
        if cmd == 'ACK_VT2':
            c = self.machine.cycle
            if c and c.cid == cid and c.state in ('VT2','FINAL'):
                if c.state == 'VT2':
                    self.machine.move('VT2','FINAL')
                    self.flush_pending_final()
                return {'ok': True}
            return {'ok': False, 'error':'wrong stage for VT2 ACK'}
        if cmd == 'ACK_FINAL':
            c = self.machine.cycle
            if c and c.cid == cid and c.state in ('FINAL','DONE'):
                c.state = 'DONE'
                return {'ok': True}
            return {'ok': False, 'error':'wrong stage for FINAL ACK'}
        return {'ok': False, 'error': 'unknown command'}

    def _on_abort(self, msg, addr):
        if msg.get('cmd') != 'abort_check':
            return {'ok': False, 'pc_id': self.pc_id, 'msg': 'unknown command'}
        self.machine.abort()
        self.app.ui_queue.put({'type': 'is480_abort'})
        return {'ok': True, 'pc_id': self.pc_id, 'aborted': True, 'msg': 'Software cycle aborted, PLC untouched'}

    def _on_event(self, msg, addr):
        self._auth(msg)
        pc = msg.get('pc')
        if pc not in self.peers or addr[0] != self.peers[pc]:
            return {'ok': False, 'error': 'unknown sender/host'}
        boot = msg.get('boot')
        self.last_seen[pc] = time.monotonic()
        old_boot = self.peer_boots.get(pc)
        if old_boot is not None and old_boot != boot:
            self.workers_ready.clear()
            self.fault(f'{pc} Windows rebooted', transient=True)
            return {'ok': False, 'error': 'worker boot changed; waiting for STATUS'}
        evt = msg.get('event')
        if evt == 'HEARTBEAT':
            cams = msg.get('camera_faults') or []
            if cams:
                self.workers_ready.clear()
                self.warning(f'{pc}:camera', f'{pc} mất camera: {cams}')
                self.fault(f'{pc} mất camera: {cams}', transient=True)
            return {'ok': True}
        cid = str(msg.get('cycle_id') or '')
        if evt == 'FAULT':
            self.fault(f'{pc} {msg.get("reason")}', cid=cid)
            return {'ok': True}
        if not self.machine.current(cid):
            return {'ok': False, 'error': 'stale cycle'}
        if evt == 'VT_DONE':
            self.stage_done(pc, int(msg['vt']), cid)
        elif evt == 'CYCLE_DONE':
            self.final_done(pc, str(msg.get('result')), cid)
        elif evt == 'NG_DETAILS':
            try:
                self.app._is480_archive_remote_details(msg)
            except Exception as exc:
                self.fault(f'{pc} NG details ErrorArchive failed: {exc}',cid=cid)
                return {'ok':False,'error':'NG details ErrorArchive failed: '+str(exc)}
        elif evt == 'VIEW_IMAGE':
            try:
                self.app._is480_store_view_image(msg)
            except Exception as exc:
                return {'ok':False,'error':'Preview image rejected: '+str(exc)}
            self.app.ui_queue.put({'type':'is480_view_image','pc':pc,
                'cam_vt':msg.get('cam_vt'),'cycle_id':cid})
        elif evt == 'NG_IMAGE':
            # An ACK must mean the JPEG is on PC1 disk; not merely queued in RAM.
            try:
                saved=self.app._is480_store_ng_image(msg)
            except Exception as exc:
                self.fault(f'{pc} NG image could not be archived: {exc}',cid=cid)
                return {'ok':False,'error':f'NG image archive failed: {exc}'}
            self.app.ui_queue.put({'type':'is480_remote_image', 'pc':pc, 'cycle_id':cid,
                                   'cam_vt':msg.get('cam_vt'),'kind':msg.get('kind'), 'path':saved})
        return {'ok': True}

    def warning(self, source, message=None):
        self.app.ui_queue.put({'type': 'is480_warning', 'source': source, 'message': message})

    def poll_workers(self):
        statuses = {}
        for pc in ('PC2', 'PC3'):
            try:
                status = self._request(pc, 'STATUS')
                if (not status.get('ok') or status.get('pc') != pc
                        or status.get('boot') is None or status.get('mode') not in ('PM', 'RUN')
                        or not isinstance(status.get('camera_faults'), list)):
                    raise ConnectionError('STATUS không hợp lệ')
            except Exception as exc:
                reason = f'Mất kết nối {pc}: {exc}'
                self.workers_ready.clear()
                self.warning(f'{pc}:connection', reason)
                self.fault(reason, transient=True)
                continue
            self.last_seen[pc] = time.monotonic()
            self.warning(f'{pc}:connection')
            if status['camera_faults']:
                reason = f"{pc} mất camera: {status['camera_faults']}"
                self.workers_ready.clear()
                self.warning(f'{pc}:camera', reason)
                self.fault(reason, transient=True)
            else:
                self.warning(f'{pc}:camera')
            statuses[pc] = status
        return statuses

    def begin_master(self, barcode, mode):
        if not self.workers_ready.is_set():
            raise ConnectionError('Hai máy phụ chưa sẵn sàng')
        statuses = self.poll_workers()
        if (len(statuses) != 2 or any(
                st['camera_faults'] or st['mode'] != mode
                or st['boot'] != self.peer_boots.get(pc) for pc, st in statuses.items())):
            self.workers_ready.clear()
            raise ConnectionError('Trạng thái máy phụ đã thay đổi; chờ đồng bộ lại')
        cid = self.machine.start(barcode, mode)
        self.machine.cycle.worker_boots = dict(self.peer_boots)
        for pc in ('PC2', 'PC3'):
            res = self._request(pc, 'CAPTURE_VT1', cycle_id=cid, barcode=barcode, mode=mode)
            if not res.get('ok') or res.get('cycle_id') != cid:
                self.fault(f'{pc} VT1 command rejected: {res}', cid=cid)
                raise RuntimeError(f'{pc} refused VT1: {res}')
        return cid

    def start_vt2(self):
        with self.machine.lock:
            c = self.machine.cycle
            if not c or c.state != 'VT1_ACK':
                raise RuntimeError('VT1 has not been ACKed by PLC')
            cid = c.cid
        self.machine.move('VT1_ACK','VT2')
        for pc in ('PC2','PC3'):
            res = self._request(pc, 'CAPTURE_VT2', cycle_id=cid)
            if not res.get('ok'):
                self.fault(f'{pc} VT2 rejected: {res}', cid=cid)
                raise RuntimeError(f'{pc} VT2 rejected')
        return cid

    def stage_done(self, pc, vt, cid):
        if self.machine.record(cid, pc, vt):
            key = (cid, vt)
            with self._repeat_lock:
                if key in self._stage_sent:
                    return
                self._stage_sent.add(key)
            if self.pc_id == 'PC1':
                if vt == 1:
                    self._repeat_serial(0x33, 'VT1', cid)
                elif vt == 2:
                    # Do NOT block master TCP event listener while waiting for
                    # ACK_VT2 from workers (worker may synchronously report AI).
                    threading.Thread(target=self._release_vt2_without_plc,
                                     args=(cid,), daemon=True,
                                     name='IS480-vt2-final-release').start()
            else:
                self._out_event('VT_DONE', cycle_id=cid, vt=vt)
        elif self.pc_id != 'PC1' and self.machine.current(cid):
            # worker has only its own local stage, it may transmit immediately.
            self._out_event('VT_DONE', cycle_id=cid, vt=vt)

    def local_stage_done(self, vt):
        c = self.machine.cycle
        if c and self.machine.current(c.cid):
            self.stage_done(self.pc_id, vt, c.cid)

    def _release_vt2_without_plc(self, cid):
        """VT2_DONE x3 -> TCP ACK_VT2 x2 -> release 3-PC AI verdict.

        No PLC 0x34 write and no PLC 0x14 wait. A rejection is a fault,
        never a reason to send an assumed NG or OK.
        """
        if self.pc_id != 'PC1' or not self.machine.current(cid):
            return
        if not self.machine.staged(2):
            return
        if not self.machine.move('VT2', 'FINAL'):
            return
        for pc in ('PC2', 'PC3'):
            if not self.machine.current(cid):
                return
            try:
                response = self._request(pc, 'ACK_VT2', cycle_id=cid)
                if not response.get('ok'):
                    raise RuntimeError(f'ACK_VT2 rejected: {response}')
            except Exception as exc:
                self.fault(f'{pc} ACK_VT2 TCP failure: {exc}', cid=cid)
                return
        if not self.machine.current(cid):
            return
        with self._repeat_lock:
            self._vt2_release_cid = cid
        self.flush_pending_final()
        self._maybe_send_final(cid)
        self.app.ui_queue.put({'type': 'is480_check_final_ready',
                               'cycle_id': cid})

    def _maybe_send_final(self, cid):
        """Only master may send final after TCP barrier + three AI verdicts."""
        if self.pc_id != 'PC1':
            return
        with self.machine.lock:
            c = self.machine.cycle
            if c is None or c.cid != cid or c.state != 'FINAL':
                return
            result = self.machine.verdict()
            if result is None:
                return
            with self._repeat_lock:
                if self._vt2_release_cid != cid or cid in self._final_sent:
                    return
                self._final_sent.add(cid)
            c.verdict = result
        self._repeat_serial(0x35 if result in ('OK', 'PM_OK') else 0x36,
                            'FINAL', cid)

    def final_done(self, pc, verdict, cid):
        if self.machine.record(cid, pc, 'FINAL', verdict=verdict):
            if self.pc_id == 'PC1':
                self._maybe_send_final(cid)
            else:
                self._out_event('CYCLE_DONE', cycle_id=cid, result=verdict)
        elif self.pc_id != 'PC1' and self.machine.current(cid):
            self._out_event('CYCLE_DONE', cycle_id=cid, result=verdict)

    def local_final_done(self, verdict):
        c = self.machine.cycle
        if c and self.machine.current(c.cid):
            if c.state != 'FINAL':
                self._pending_local_final = (c.cid, verdict)
                return
            self.final_done(self.pc_id, verdict, c.cid)

    def flush_pending_final(self):
        pending = self._pending_local_final
        if pending and self.machine.current(pending[0]) and self.machine.cycle.state == 'FINAL':
            self._pending_local_final = None
            self.final_done(self.pc_id, pending[1], pending[0])

    def _repeat_serial(self, value, step, cid):
        if self.pc_id != 'PC1' or not self.machine.current(cid):
            return
        key = (cid,step)
        stop = threading.Event()
        with self._repeat_lock:
            if key in self._repeat_stop:
                return
            self._repeat_stop[key] = stop
        def work():
            t0 = time.monotonic()
            while not stop.is_set() and self.machine.current(cid):
                if time.monotonic() - t0 > 30:
                    self.fault(f'PLC missing ACK for {step} ({value:#04x})', cid=cid)
                    break
                try:
                    if self.app.ser is None:
                        raise ConnectionError('PLC COM1 not open')
                    with self.app.serial_lock:
                        # Abort acquires this same lock before invalidating the
                        # cycle, so no stale completion byte can be written
                        # AFTER DỪNG CHECK finishes its local state transition.
                        if stop.is_set() or not self.machine.current(cid):
                            break
                        self.app.ser.write(bytes([value]))
                except Exception as exc:
                    self.fault(f'PLC write {value:#04x}: {exc}', cid=cid)
                    break
                stop.wait(0.3)
        threading.Thread(target=work, daemon=True).start()

    def ack(self, value):
        stage = {0x12:'VT1',0x15:'FINAL'}.get(value)
        if not stage:
            return False
        c = self.machine.cycle
        if not c or c.state == 'FAULT':
            return False
        # Idempotent PLC ACKs; duplicated ACK must not turn a healthy cycle into HOLD.
        if stage == 'VT1' and c.state in ('VT1_ACK','VT2','FINAL','DONE'):
            return True
        if stage == 'FINAL' and c.state == 'DONE':
            return True
        key = (c.cid,stage)
        with self._repeat_lock:
            stop = self._repeat_stop.get(key)
            if not stop:
                return False
            stop.set()
        if stage == 'VT1':
            if not self.machine.staged(1) or not self.machine.move('VT1','VT1_ACK'):
                return False
            for pc in ('PC2','PC3'):
                try:
                    self._request(pc, 'ACK_VT1', cycle_id=c.cid)
                except Exception as exc:
                    self.fault(f'{pc} ACK_VT1 network failure: {exc}', cid=c.cid)
            return True
        if stage == 'FINAL':
            if not self.machine.staged('FINAL') or not self.machine.move('FINAL','DONE'):
                return False
            c.ack_final = True
            # Serial thread may receive PLC 0x10 before Tk handles DB commit.
            # Lock production admission synchronously at FINAL ACK, not in Tk.
            if self.pc_id == 'PC1' and c.mode == 'RUN':
                gate = getattr(self.app, 'is480_rfid_gate_active', None)
                if gate is not None:
                    gate.set()
            for pc in ('PC2', 'PC3'):
                try:
                    if not self._request(pc, 'ACK_FINAL', cycle_id=c.cid).get('ok'):
                        raise RuntimeError('máy phụ từ chối ACK_FINAL')
                except Exception as exc:
                    self.workers_ready.clear()
                    with self.mode_lock:
                        # PLC has finalized this Body. Discard a stale worker
                        # cycle before admitting another Body.
                        self.pending_aborts.add(pc)
                        self.warning(f'{pc}:abort', f'{pc} chưa chốt chu trình qua TCP: {exc}')
            self.app.ui_queue.put({'type':'is480_commit', 'cycle_id':c.cid, 'verdict':c.verdict,
                                   'barcode':c.barcode,'check_no':c.check_no, 'mode':c.mode})
            with self._repeat_lock:
                self._repeat_stop = {k:v for k,v in self._repeat_stop.items() if k[0] != c.cid}
                self._stage_sent = {k for k in self._stage_sent if k[0] != c.cid}
                self._final_sent.discard(c.cid)
                if self._vt2_release_cid == c.cid:
                    self._vt2_release_cid = None
            return True
        return False

    def fault(self, reason, cid=None, transient=False):
        changed = self.machine.fail(reason, cid)
        if changed:
            self.workers_ready.clear()
            self.warning('cycle', f'{reason} — dùng DỪNG CHECK để hủy Body đang lỗi')
        elif not transient and self.machine.cycle is None:
            self.warning('operation', str(reason))
        if changed and self.pc_id == 'PC1':
            failed_cid = self.machine.cycle.cid

            def abort_peers():
                with self.mode_lock:
                    cycle = self.machine.cycle
                    if cycle.cid == failed_cid and cycle.state == 'FAULT':
                        self.pending_aborts.update(self.peers)
                        self.retry_peer_aborts(self.peers)
            threading.Thread(target=abort_peers, daemon=True).start()
        if changed and self.pc_id != 'PC1':
            try:
                self._out_event('FAULT', cycle_id=cid or self.machine.cycle.cid, reason=reason)
            except Exception as exc:
                print('[IS480] unable to report FAULT:', exc)
        return changed

    def _watchdog(self):
        while self.app.camera_running:
            time.sleep(1)
            if time.monotonic() - self._last_health_poll > 5:
                self._last_health_poll = time.monotonic()
                self.app._is480_sync_workers()
            c = self.machine.cycle
            if not c or c.state in ('FAULT', 'DONE', 'ABORTED'):
                continue
            if time.monotonic() - c.created > self._latency_timeout:
                self.fault(f'Cycle timeout {c.cid}: state={c.state}', cid=c.cid)
                continue
            for pc in ('PC2', 'PC3'):
                t = self.last_seen.get(pc, 0)
                if t and time.monotonic() - t > 14:
                    self.fault(f'{pc} heartbeat lost', cid=c.cid)
                    break

    def retry_peer_aborts(self, online):
        for pc in sorted(self.pending_aborts.intersection(online)):
            try:
                if not self._request(pc, 'ABORT').get('ok'):
                    raise RuntimeError('máy phụ từ chối ABORT')
            except Exception as exc:
                self.warning(f'{pc}:abort', f'{pc} chưa xác nhận hủy chu trình: {exc}')
            else:
                self.pending_aborts.remove(pc)
                self.warning(f'{pc}:abort')

    def abort(self):
        # Serialize cancellation with mode changes and admission of a new cycle.
        with self.mode_lock:
            self.workers_ready.clear()
            with self.app.serial_lock:
                self.machine.abort()
                for event in list(self._repeat_stop.values()):
                    event.set()
            self._pending_local_final = None
            self.warning('cycle')
            self.app.ui_queue.put({'type': 'is480_abort'})
            self.pending_aborts.update(self.peers)
            self.retry_peer_aborts(self.peers)


def load_site_config(path, pc_id):
    """Generate local JSON with explicit worker IPs; user must verify before production."""
    path = Path(path)
    if not path.exists():
        cfg = {'pc_id': pc_id, 'token': DEFAULT_TOKEN,
               'PC1_host': 'SET_PC1_IP_HERE',
               'PC2_host':'10.255.128.197', 'PC3_host':'10.255.129.53',
               'note':'VERIFY real host IPs and change token before production'}
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf8')
    with path.open(encoding='utf8') as f:
        cfg = json.load(f)
    if cfg.get('pc_id') != pc_id:
        raise RuntimeError(f'Config pc_id mismatch {cfg.get("pc_id")} != {pc_id}')
    return cfg


# Application helpers

def _log(app, event, message):
    try:
        p = Path(__file__).resolve().with_name('is480_operation.log')
        with p.open('a', encoding='utf8') as f:
            f.write(f'{datetime.now():%Y-%m-%d %H:%M:%S} [{app.is480_pc_id}] {event}: {message}\n')
    except OSError:
        pass
    print(f'[IS480][{event}] {message}')


def _state_path(app):
    return Path(__file__).resolve().with_name('is480_mode_state.json')


def _load_mode(app):
    try:
        state = json.loads(_state_path(app).read_text(encoding='utf8'))
    except (OSError, ValueError):
        state = {}
    current_boot = boot_marker()
    if state.get('boot') != current_boot:
        state = {'boot':current_boot,'pm_attempted':False,'mode':'PM','peers':{}}
        _save_mode(app, state)
    return state


def _save_mode(app, state):
    path = _state_path(app)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf8')
    os.replace(temp, path)


def _normalize_repair_barcode(raw):
    """Scanner rule from L1200_C6: use the first 10 printable characters."""
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='ignore')
    cleaned = ''.join(ch for ch in str(raw or '') if ch.isprintable() and not ch.isspace())
    return cleaned[:10] if len(cleaned) >= 10 else ''


def _is480_square_ng_grid(frames, width=1000, height=528):
    """NG tiles nearly square: 4=2x2, 5=3x2, 9=3x3; no cropping."""
    import cv2
    import numpy as np
    import math
    items = sorted(((str(k), v) for k, v in frames.items() if v is not None),
                   key=lambda kv: kv[0])
    if not items:
        return None
    cols = int(math.ceil(math.sqrt(len(items))))
    rows = (len(items) + cols - 1) // cols
    cell_w, cell_h = width // cols, height // rows
    result = np.full((height, width, 3), 28, dtype=np.uint8)
    for i, (title, frame) in enumerate(items):
        y0, x0 = divmod(i, cols)
        x1, y1 = x0 * cell_w, y0 * cell_h
        x2 = width if x0 == cols-1 else x1 + cell_w
        y2 = height if y0 == rows-1 else y1 + cell_h
        span_w, span_h = x2-x1, y2-y1
        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            continue
        scale = min((span_w-8)/w, (span_h-8)/h)
        tw, th = max(1,int(w*scale)),max(1,int(h*scale))
        sample = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
        xo, yo = x1+(span_w-tw)//2, y1+(span_h-th)//2
        result[yo:yo+th, xo:xo+tw] = sample
        cv2.rectangle(result, (x1+1,y1+1), (x2-2,y2-2),(0,0,255),3)
        label_scale = min(0.67, max(0.37, span_w / 470.0))
        font = cv2.FONT_HERSHEY_SIMPLEX
        (lw,lh),_ = cv2.getTextSize(title,font,label_scale,2)
        cv2.rectangle(result,(x1+5,y1+5),(min(x2-4,x1+lw+16),y1+lh+17),(15,15,15),-1)
        cv2.putText(result,title,(x1+10,y1+lh+11),font,label_scale,(255,255,255),2,cv2.LINE_AA)
    return result


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
    is480_pc_id = 'PC1'

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
        self.ui_queue = CoalescedUIQueue()
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
        self.executor_save = BoundedExecutor(max_workers=4,max_pending=320)
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

    def _process_image(self, frame, cam, vitri, toado, anhgoc, roi_large, *models):
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
            self.is480_bridge.warning('barcode')
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
                if scanner.send('scan_disable'):
                    self.is480_bridge.warning('barcode_disable')
                else:
                    self.is480_bridge.warning('barcode_disable', 'Không gửi được scan_disable COM3')
            except Exception as exc:
                self.is480_bridge.warning('barcode_disable', f'COM3 scan_disable lỗi: {exc}')

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
        self.is480_bridge.warning('barcode', 'Không đọc được barcode thật từ COM3; chờ xử lý')

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

    # Inspection controller

    def _is480_setup(self):
        pc = self.is480_pc_id
        cfg = load_site_config(Path(__file__).resolve().with_name('is480_config.json'), pc)
        peer_hosts = {'PC2': cfg['PC2_host'], 'PC3':cfg['PC3_host']} if pc == 'PC1' else None
        self.is480_bridge = Bridge(self, pc, peers=peer_hosts, token=cfg['token'])
        self.is480_mode_state = _load_mode(self) if pc == 'PC1' else None
        self.is480_vt1_ready = False
        self.is480_vt2_ready = False
        self.is480_final_cache = None
        self.is480_cycle_started = False
        self.is480_raw_jpegs = {}
        self._is480_vt_started = {}
        self.is480_ng_point_details = []
        self.is480_selected_pc = 'PC1'
        self.is480_remote_images = {}
        self.is480_view_lock = threading.RLock()
        self.is480_checked_jpegs = {}  # latest cycle; JPEG thumbnails only
        self.is480_remote_view_jpegs = {}  # PC1: {(pc,CAM_VT): JPEG}
        self.is480_warnings = {}
        self.is480_remote_ng_paths = {} # {(pc,CAM_VT): PROC JPG path}
        self.is480_remote_view_verdict = {} # PM and RUN camera flags
        self.is480_remote_details_lock = threading.RLock()
        self.is480_remote_details_saved = set()
        self.is480_cam_slots = []
        self.is480_visible_cam_slots = []
        self.is480_last_ng_count = 0
        self._is480_tls = threading.local()
        self.is480_recent_bodies = []
        self.is480_body_shift_key = None
        self.is480_current_scan = ''
        if pc == 'PC1':
            self.is480_scan_lock = threading.Lock()
            self.is480_body_scan_request = threading.Event()
            self.is480_repair_scan_stop = threading.Event()
            self.is480_repair_reader_thread = None
            self.is480_repair_last_scan = ('', 0.0)
            # L1200_C6: repair scanner is a SEPARATE USB HID or COM port.
            # Never consume/enable production COM3 from the background.
            self.is480_repair_com = str(getattr(self, 'BODY_REPAIR_SCANNER_PORT', '') or '').strip().upper()
            self.is480_repair_baud = int(getattr(self, 'BODY_REPAIR_SCANNER_BAUD', 9600))
            self.is480_repair_queue = queue.Queue(maxsize=32)
            self.is480_repair_pending = set()
            self.is480_repair_key_buffer = ''
            self.is480_repair_first_key_at = 0.0
            self.is480_repair_last_key_at = 0.0
            self._is480_555_pending = None
            self.is480_body_scan_active = threading.Event()
            # A RUN result is locked from PLC 0x15 until DB settles; NG remains
            # locked until verified COM4 UID + durable DB record + COM1 0x37.
            self.is480_rfid_gate_active = threading.Event()
            self.is480_ng_confirm_lock = threading.RLock()
            self.is480_rfid_37_written = False
            self.is480_deferred_plc_start = False
            self.is480_ng_confirm_pending = None  # dict(cycle_id,barcode,check_no,...)
            self.is480_ng_confirm_in_progress = False
            self.is480_ng_confirm_last_card = None
            self.is480_ng_confirm_last_card_at = 0.0
        self.is480_last_5 = []
        self._is480_refresh_screen()
        if pc == 'PC1':
            self.after(500, self._is480_check_shift)
            self.after(750, self._is480_ready_event)
            self.is480_camera_bad_since={}
            self.after(2500, self._is480_camera_health)
            # Default: separate USB HID barcode reader like C6 PC3. It emits
            # rapid keyboard events into the focused PC1 Tk window.
            self.bind_all('<KeyPress>', self._is480_on_repair_keypress, add='+')
            if self.is480_repair_com:
                if self.is480_repair_com in ('COM1', 'COM3', 'COM4'):
                    self._is480_repair_notice(
                        'Cổng sửa trùng PLC/BODY/RFID: ' + self.is480_repair_com)
                    _log(self, 'REPAIR_PORT_INVALID', self.is480_repair_com)
                else:
                    self.is480_repair_reader_thread = threading.Thread(
                        target=self._is480_repair_serial_reader, daemon=True,
                        name='IS480_RepairBarcodeDedicatedCOM')
                    self.is480_repair_reader_thread.start()
            threading.Thread(target=self._is480_repair_db_worker, daemon=True,
                             name='IS480_RepairDBWorker').start()
            # Resume any RUN_NG that was not successfully released by 0x37.
            # Pending RFID is stored in SQLite, never only in Tk/volatile memory.
            try:
                from is480_statistics import get_pending_ng_rfid
                pending = get_pending_ng_rfid()
                if pending:
                    self._is480_start_ng_confirmation_wait(
                        pending['barcode'], pending['check_no'], pending['cycle_id'],
                        restored=pending)
                    _log(self, 'RFID_RESTORED',
                         f"WAIT CARD sau khoi dong: {pending['barcode']}/{pending['check_no']}")
            except Exception as exc:
                self.is480_rfid_gate_active.set()
                self.ui_queue.put({'type':'is480_rfid_notice',
                                   'message':'LỖI DB RFID KHI KHỞI ĐỘNG: '+str(exc)})
                _log(self,'RFID_RECOVER_ERROR',str(exc))
            # Scanner production is disabled until PLC requests barcode 0x10.
            scanner = getattr(self, 'initProcess', None)
            if scanner is not None and not scanner.send('scan_disable'):
                _log(self, 'SCANNER_IDLE', 'Chưa gửi được scan_disable COM3; kiểm tra kết nối')
            if self.is480_mode_state.get('pm_required'):
                self._is480_warning('Máy phụ đã reboot Windows — cần chạy PM lại', 'pm_required')
            threading.Thread(target=self.is480_bridge._watchdog, daemon=True).start()
        else:
            self.after(750, self._is480_ready_event)

    def _is480_refresh_screen(self):
        if not hasattr(self, 'widget_result_check'):
            return
        wc = self.widget_result_check
        wc.columnconfigure(3, minsize=145, weight=0)
        wc.label2.config(font=('Arial',26,'bold'))
        if self.is480_pc_id == 'PC1':
            # Reserve a dedicated row above the statistics; no WARNING overlay
            # hiding Unit/Body/Qty as happened in the factory screenshot.
            self.rowconfigure(0, minsize=0, weight=0)
            self.rowconfigure(1, minsize=100, weight=1)
            self.rowconfigure(2, minsize=500, weight=6)
            self.rowconfigure(3, minsize=100, weight=1)
            self.widget_station_infor.grid_configure(row=1)
            self.widget_result_check.grid_configure(row=2)
            self.widget_buttom.grid_configure(row=3)
            if self.PM:
                wc.label3.config(text='PM: KHÔNG CẦN RFID', bg='#E8EDF4',
                                 font=('Arial',10,'bold'), wraplength=140)
            else:
                wc.label3.config(text='RFID: '+str(getattr(self,'latest_approval_operator','') or '--'),
                                 bg='yellow',font=('Arial',10,'bold'),wraplength=140)
            # The old NG point Listbox remains available via its own toggle.
            self.is480_list_visible = 'body'
            wc.listNGbox.grid_remove()
            wc.scroll.grid_remove()
            main = tk.Frame(wc, bg='white')
            main.grid(row=3,column=3,sticky='nsew')
            self.is480_body_frame = main
            tk.Button(main,text='BODY / NG POINT',font=('Arial',8),command=self._is480_toggle_ng).pack(fill='x')
            cv = tk.Canvas(main,bg='#FFFFFF',highlightthickness=0,width=145,height=220)
            cv.pack(side='left',fill='both',expand=True)
            scr = tk.Scrollbar(main,orient='vertical',command=lambda *a:self._is480_scroll_body(*a))
            scr.pack(side='right',fill='y')
            cv.configure(yscrollcommand=scr.set)
            cv.bind('<Configure>', lambda _event: self._is480_draw_body())
            cv.bind('<MouseWheel>', lambda ev:self._is480_scroll_body('scroll',int(-ev.delta/120),'units'))
            self.is480_body_canvas=cv
            self.is480_body_scroll=scr
            scan = tk.Entry(main,font=('Arial',9))
            scan.pack(side='bottom',fill='x')
            scan.insert(0,'')
            scan.bind('<Return>',self._is480_repair_barcode)
            self.is480_repair_entry=scan
            # C6: independent USB HID or dedicated COM reader. COM3 is PLC-only.
            # This Entry remains available for operator manual repair input.
            scan.configure(font=('Consolas',9))
            self.is480_repair_notice=tk.Label(main,text='',bg='#FFFFFF',fg='#B91C1C',
                                             font=('Arial',8),wraplength=140,anchor='w')
            self.is480_repair_notice.pack(side='bottom',fill='x')
            self._is480_draw_body()
            self._is480_attach_header()
        else:
            wc.label3.config(font=('Arial',11,'bold'),wraplength=140)

    def _is480_add_pc_menu(self):
        """Place PC1/PC2/PC3 next to Home and Mode in the real Tk menu bar."""
        if self.is480_pc_id != 'PC1':
            return
        menu_name = str(self.cget('menu') or '')
        if not menu_name:
            return
        menu = self.nametowidget(menu_name)
        # Each mode creates a fresh Tk Menu; never add duplicate commands.
        for pc in ('PC1', 'PC2', 'PC3'):
            menu.add_command(label=pc, command=lambda p=pc: self._is480_select_pc(p))

    def _is480_attach_header(self):
        """Keep the stats readable; move RUN and STOP beside XEM ẢNH NG."""
        if self.is480_pc_id != 'PC1':
            return
        bottom = self.widget_buttom
        # PC1 RUN and PM both have CAM5_VT2 in column 4, leaving 5,6,7 for actions.
        bottom.btn_xem_ng.grid_configure(row=1, column=5, columnspan=1,
                                        sticky='nsew', padx=3, pady=3)
        self.is480_run_btn = tk.Button(
            bottom, text='CHUYỂN RUN', bg='#69B6DA', fg='white',
            activebackground='#48A2D1', activeforeground='white',
            disabledforeground='#E6F4FC', font=('Arial',11,'bold'),
            state='disabled', command=self._is480_request_run)
        self.is480_run_btn.grid(row=1, column=6, sticky='nsew', padx=3, pady=3)
        self.is480_abort_btn = tk.Button(
            bottom, text='DỪNG CHECK', bg='#B91C1C', fg='white',
            activebackground='#991B1B', activeforeground='white',
            font=('Arial',11,'bold'),
            command=lambda: threading.Thread(
                target=self.is480_bridge.abort, daemon=True).start())
        self.is480_abort_btn.grid(row=1, column=7, sticky='nsew', padx=3, pady=3)
        self._is480_sync_run_button()
        bottom.btn_xem_ng.configure(command=self._is480_show_global_ng)
        self._is480_prepare_cam_slots()
        if self.is480_selected_pc!='PC1':
            self._is480_select_pc(self.is480_selected_pc)
        if not hasattr(self,'is480_warning_bar') or not self.is480_warning_bar.winfo_exists():
            self.is480_warning_bar = tk.Label(
                self, text='', bg='#FFD166', fg='#6B2100',
                font=('Arial',12,'bold'), anchor='w', padx=10)
        self._is480_render_warnings()

    def _is480_sync_run_button(self):
        if getattr(self,'is480_pc_id',None)=='PC1' and hasattr(self,'is480_run_btn'):
            state=self.is480_mode_state or {}
            active=bool(state.get('pm_attempted')) and state.get('mode') != 'RUN'
            self.is480_run_btn.configure(state=('normal' if active else 'disabled'))

    def _is480_prepare_cam_slots(self):
        if self.is480_pc_id!='PC1':return
        bottom=self.widget_buttom
        if getattr(self,'is480_cam_slot_owner',None) is bottom:return
        slots=[]
        for button in bottom.winfo_children():
            if not isinstance(button,tk.Button):continue
            label=str(button.cget('text'))
            m=re.fullmatch(r'CAM\d+_VT[12]',label)
            if not m:continue
            cfg=button.grid_info() if button.winfo_manager()=='grid' else None
            row=int(cfg['row']) if cfg else (1 if label.endswith('VT2') else 0)
            col=int(cfg['column']) if cfg else 0
            slots.append({'button':button,'label':label,'command':button.cget('command'),
                          'row':row,'col':col,'bg':button.cget('bg'),'fg':button.cget('fg'),
                          'was_visible':bool(cfg)})
        slots.sort(key=lambda x:(x['row'],x['col'],x['label']))
        self.is480_cam_slots=slots
        self.is480_cam_slot_owner=bottom
        self.is480_visible_cam_slots=[]

    def _is480_select_pc(self,pc):
        if self.is480_pc_id!='PC1' or pc not in ('PC1','PC2','PC3'):return
        self._is480_prepare_cam_slots()
        bottom=self.widget_buttom
        old=self.is480_selected_pc
        if old=='PC1' and pc!='PC1':
            self.is480_pc1_last_img=getattr(self,'last_display_imgtk',None)
            for slot in self.is480_cam_slots:
                button=slot['button']
                slot['bg'],slot['fg']=button.cget('bg'),button.cget('fg')
        # Clear any previous native camera highlight before reusing buttons.
        clear=getattr(bottom,'_clear_cam_button_highlight',None)
        if callable(clear):clear()
        self.is480_selected_pc=pc
        if pc=='PC1':
            for slot in self.is480_cam_slots:
                button=slot['button']
                button.config(text=slot['label'],command=slot['command'],
                              bg=slot['bg'],fg=slot['fg'])
                button.grid(row=slot['row'],column=slot['col'],
                            sticky='nsew',padx=3,pady=3)
                if self.PM and slot['label'] in (
                        'CAM0_VT1','CAM2_VT1','CAM1_VT2','CAM3_VT2','CAM5_VT1'):
                    button.grid_remove()
            if hasattr(self,'is480_run_btn'):
                bottom.btn_xem_ng.grid(row=1,column=5,columnspan=1,sticky='nsew',padx=3,pady=3)
                self.is480_run_btn.grid(row=1,column=6,sticky='nsew',padx=3,pady=3)
                self.is480_abort_btn.grid(row=1,column=7,sticky='nsew',padx=3,pady=3)
            self.is480_visible_cam_slots=[]
            # Home shows the aggregate NG grid if present, never a stale preview
            # last displayed while PC2/PC3 was selected.
            if self.is480_remote_ng_paths or getattr(self,'ng_frames',{}):
                self._is480_render_global_ng()
            elif getattr(self,'is480_pc1_last_img',None) is not None:
                self.widget_result_check.label_image_1.config(image=self.is480_pc1_last_img,text='')
            else:
                self.widget_result_check.label_image_1.config(image='',text='PC1: CHƯA CÓ ẢNH')
            return
        for slot in self.is480_cam_slots:slot['button'].grid_remove()
        groups=_IS480_VIEW_GROUPS[pc]
        hidden=_IS480_PM_HIDDEN[pc] if self.PM else set()
        visible=[]
        # Allocate native 13 camera buttons; remote PC2: 12, remote PC3: 11.
        available=iter(self.is480_cam_slots)
        for row,labels in enumerate(groups):
            col=0
            for label in labels:
                if label in hidden:continue
                try:slot=next(available)
                except StopIteration:
                    self._is480_warning('Không đủ nút camera PC1 để hiển thị '+pc)
                    break
                b=slot['button']
                b.config(text=label,command=lambda p=pc,k=label:self._is480_show_checked_camera(p,k))
                b.grid(row=row,column=col,sticky='nsew',padx=3,pady=3)
                visible.append((b,label))
                col+=1
        self.is480_visible_cam_slots=visible
        # PC3 uses eight VT2 buttons: action controls move to the third row.
        if hasattr(self,'is480_run_btn'):
            bottom.btn_xem_ng.grid(row=2,column=5,columnspan=1,sticky='nsew',padx=3,pady=3)
            self.is480_run_btn.grid(row=2,column=6,sticky='nsew',padx=3,pady=3)
            self.is480_abort_btn.grid(row=2,column=7,sticky='nsew',padx=3,pady=3)
        self._is480_refresh_cam_button_colors()
        first=next((label for b,label in visible if (pc,label) in self.is480_remote_view_jpegs),None)
        if first:self._is480_show_checked_camera(pc,first)
        else:self.widget_result_check.label_image_1.configure(
            image='',text=pc+': CHƯA CÓ ẢNH ĐÃ CHECK')

    def _is480_show_global_ng(self):
        if self.is480_pc_id=='PC1':
            self._is480_render_global_ng()

    def _is480_toggle_ng(self):
        if self.is480_list_visible=='body':
            self.is480_list_visible='ng'
            self.is480_body_frame.grid_remove()
            self.widget_result_check.listNGbox.grid(row=3,column=3,sticky='nsew')
            self.widget_result_check.scroll.grid(row=3,column=3,sticky='nse')
        else:
            self.is480_list_visible='body'
            self.widget_result_check.listNGbox.grid_remove()
            self.widget_result_check.scroll.grid_remove()
            self.is480_body_frame.grid(row=3,column=3,sticky='nsew')

    def _is480_scroll_body(self,*args):
        c=getattr(self,'is480_body_canvas',None)
        if c:
            c.yview(*args)
            self._is480_draw_body()

    def _is480_draw_body(self):
        c=getattr(self,'is480_body_canvas',None)
        if c is None or not c.winfo_exists():return
        items=self.is480_recent_bodies
        row_h=26
        h=max(c.winfo_height(),100)
        total_h=max(len(items)*row_h,h)
        c.config(scrollregion=(0,0,145,total_h))
        first=int(c.canvasy(0)//row_h)
        c.delete('body')
        for i in range(max(0,first-1),min(len(items),first+int(h/row_h)+3)):
            row=items[i]; barcode=str(row['barcode'])
            ok=row['final_status']=='OK' or bool(row['repair_confirmed_at'])
            color='#18803C' if ok else '#D32323'
            x,y=71,(i*row_h)+13
            # stroked glyphs (3px), NOT rectangular border
            for dx in (-3,0,3):
                for dy in (-3,0,3):
                    if dx or dy:
                        c.create_text(x+dx,y+dy,text=barcode,font=('Consolas',11,'bold'),fill=color,tags='body')
            c.create_text(x,y,text=barcode,font=('Consolas',11,'bold'),fill='white',tags='body')

    def _is480_check_shift(self):
        from is480_statistics import ShiftSchedule, query_shift_bodies
        info=ShiftSchedule.get_shift_info()
        key=(info['date'],info['shift'])
        if self.is480_body_shift_key!=key:
            self.is480_body_shift_key=key
            self.is480_recent_bodies=query_shift_bodies()
            self._is480_draw_body()
        self.after(5000,self._is480_check_shift)

    def _is480_repair_notice(self,msg,ok=False):
        """Short repair-scanner feedback, not a production HOLD/FAULT warning."""
        notice=getattr(self,'is480_repair_notice',None)
        if notice is None or not notice.winfo_exists():
            _log(self,'REPAIR_NOTICE',msg)
            return
        notice.config(text=msg,fg=('#18803C' if ok else '#B91C1C'))
        ident=getattr(self,'is480_repair_notice_id',0)+1
        self.is480_repair_notice_id=ident
        def clear_if_current():
            if (getattr(self,'is480_repair_notice_id',0)==ident
                    and notice.winfo_exists()):
                notice.config(text='')
        notice.after(5000,clear_if_current)

    def _is480_repair_barcode(self,event=None,raw=None):
        """C6-style repair admission: current shift, red BODY only, no HOLD."""
        if self.is480_pc_id != 'PC1':
            return 'break'
        if raw is None:
            raw = self.is480_repair_entry.get()
        if hasattr(self, 'is480_repair_entry'):
            self.is480_repair_entry.delete(0, 'end')
        barcode = _normalize_repair_barcode(raw)
        if not barcode:
            self._is480_repair_notice('BARCODE SỬA phải có ít nhất 10 ký tự')
            return 'break'
        try:
            from is480_statistics import query_shift_bodies
            rows = query_shift_bodies()
            matched = next((r for r in rows if str(r['barcode']) == barcode), None)
            if not matched:
                self._is480_repair_notice(f'{barcode}: Không có BODY trong ca hiện tại')
            elif matched['final_status'] == 'OK' or matched.get('repair_confirmed_at'):
                self._is480_repair_notice(barcode + ': Đã xanh', ok=True)
            elif matched['final_status'] == 'NG':
                check_no = int(matched['check_no'])
                key = (barcode, check_no)
                if key not in self.is480_repair_pending:
                    try:
                        self.is480_repair_queue.put_nowait(key)
                        self.is480_repair_pending.add(key)
                    except queue.Full:
                        self._is480_repair_notice('Đầu đọc sửa đang bận, hãy quét lại')
                else:
                    self._is480_repair_notice(barcode + ': Đang xử lý', ok=True)
            else:
                self._is480_repair_notice(barcode + ': Chưa có kết quả tổng')
        except Exception as exc:
            self._is480_repair_notice(f'Lỗi xác nhận sửa {barcode}: {exc}')
            _log(self, 'REPAIR_DB_ERROR', str(exc))
        return 'break'

    def _is480_on_repair_keypress(self,event):
        """C6 USB-HID wedge: first 10 characters, rapid scan, independent of PLC.

        Tk only receives keyboard events when PC1 app/window has focus. Manual
        entry fields are excluded (their Return handler handles typed barcodes).
        """
        if self.is480_pc_id != 'PC1' or self.is480_repair_com:
            return
        if isinstance(event.widget, (tk.Entry, tk.Text, tk.Spinbox, ttk.Entry, ttk.Combobox)):
            return
        now = time.monotonic()
        if now - self.is480_repair_last_key_at > 0.4:
            self.is480_repair_key_buffer = ''
            self.is480_repair_first_key_at = now
        self.is480_repair_last_key_at = now
        key = str(getattr(event, 'char', '') or '')
        if key in ('\r','\n') or str(event.keysym) in ('Return','KP_Enter'):
            candidate = self.is480_repair_key_buffer
            if len(candidate) >= 10 and now - self.is480_repair_first_key_at <= 1.8:
                self.ui_queue.put({'type':'is480_repair_scanned', 'barcode':candidate[:10]})
            self.is480_repair_key_buffer = ''
        elif key and len(key) == 1 and 32 <= ord(key) < 127:
            if not self.is480_repair_key_buffer:
                self.is480_repair_first_key_at = now
            self.is480_repair_key_buffer += key
            if len(self.is480_repair_key_buffer) == 10:
                if now - self.is480_repair_first_key_at <= 1.8:
                    self.ui_queue.put({'type':'is480_repair_scanned',
                                       'barcode': self.is480_repair_key_buffer})
                self.is480_repair_key_buffer = ''
        elif str(event.keysym) not in ('Shift_L','Shift_R','Control_L',
                                       'Control_R','Alt_L','Alt_R','Caps_Lock'):
            self.is480_repair_key_buffer = ''

    def _is480_repair_serial_reader(self):
        """Optional independent repair COM. NEVER open COM1, COM3 or COM4."""
        import serial
        warning_at = 0.0
        while not self.is480_repair_scan_stop.is_set():
            try:
                with serial.Serial(self.is480_repair_com, self.is480_repair_baud,
                                   timeout=0.2) as reader:
                    raw = bytearray()
                    last_byte_at = time.monotonic()
                    while not self.is480_repair_scan_stop.is_set():
                        data = reader.read(1)
                        if not data:
                            continue
                        now = time.monotonic()
                        if now - last_byte_at > 0.5:
                            raw.clear()
                        last_byte_at = now
                        if data in (b'\r', b'\n'):
                            if raw:
                                self.ui_queue.put({'type':'is480_repair_scanned',
                                                   'barcode':raw.decode('ascii', errors='ignore')})
                            raw.clear()
                        elif 32 <= data[0] < 127:
                            if len(raw) < 10:
                                raw.extend(data)
                            if len(raw) == 10:
                                self.ui_queue.put({'type':'is480_repair_scanned',
                                                   'barcode':raw.decode('ascii', errors='ignore')})
                                raw.clear()
                        else:
                            raw.clear()
            except Exception as exc:
                now = time.monotonic()
                if now - warning_at >= 60:
                    warning_at = now
                    self.ui_queue.put({'type':'is480_repair_notice_event',
                        'message':f'Đầu đọc sửa {self.is480_repair_com}: {exc}'})
                self.is480_repair_scan_stop.wait(3)

    def _is480_repair_db_worker(self):
        """One background DB writer; per-check_no guard prevents stale scan write."""
        while not self.is480_repair_scan_stop.is_set():
            try:
                barcode, check_no = self.is480_repair_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                from is480_statistics import mark_repaired
                updated = mark_repaired(barcode, check_no=check_no)
                self.ui_queue.put({'type':'is480_repair_done','barcode':barcode,
                                   'check_no':check_no,'updated':bool(updated)})
            except Exception as exc:
                self.ui_queue.put({'type':'is480_repair_done','barcode':barcode,
                                   'check_no':check_no,'error':str(exc)})
            finally:
                self.is480_repair_queue.task_done()

    def _is480_repair_done(self,event):
        barcode = str(event.get('barcode') or '')
        key = (barcode, int(event.get('check_no') or 0))
        self.is480_repair_pending.discard(key)
        if event.get('error'):
            self._is480_repair_notice(f'{barcode}: {event["error"]}')
        else:
            self._is480_repair_notice(
                barcode + (': ĐÃ SỬA' if event.get('updated') else ': Đã xanh'),ok=True)
        try:
            from is480_statistics import query_shift_bodies
            self.is480_recent_bodies = query_shift_bodies()
            self._is480_draw_body()
        except Exception as exc:
            _log(self, 'REPAIR_REFRESH_ERROR', str(exc))

    def _is480_start_repair_scan(self):
        """Compatibility with old UI; external repair scanner is always ready."""
        if self.is480_pc_id == 'PC1':
            self._is480_repair_notice('Đầu đọc sửa HID/COM riêng sẵn sàng',ok=True)

    def _is480_render_warnings(self):
        bar = getattr(self, 'is480_warning_bar', None)
        if bar is None or not bar.winfo_exists():
            return
        if self.is480_warnings:
            bar.config(text='⚠ WARNING / HOLD — ' + ' | '.join(self.is480_warnings.values()),
                       wraplength=max(600, self.winfo_width() - 25))
            self.rowconfigure(0, minsize=34)
            bar.grid(row=0, column=0, sticky='ew')
        else:
            bar.grid_remove()
            self.rowconfigure(0, minsize=0)

    def _is480_warning(self, message, source='operation'):
        if message is None:
            if source not in self.is480_warnings:
                return
            del self.is480_warnings[source]
        else:
            message = str(message)
            if self.is480_warnings.get(source) == message:
                return
            self.is480_warnings[source] = message
            _log(self, 'WARNING', message)
        self._is480_render_warnings()

    def _is480_fault(self,reason):
        bridge=getattr(self,'is480_bridge',None)
        if bridge:bridge.fault(reason)
        else:self.ui_queue.put({'type':'is480_warning','message':reason})

    def _is480_current_thread_cycle(self):
        cid=getattr(self._is480_tls,'cid',None)
        return bool(cid and self.is480_bridge.machine.current(cid))

    def _is480_capture_checked(self,cid,*args):
        if not self.is480_bridge.machine.current(cid):return
        self._is480_tls.cid=cid
        try:return self.capture_and_process(*args)
        finally:self._is480_tls.cid=None

    def _is480_process_checked(self,cid,*args):
        if not self.is480_bridge.machine.current(cid):return
        self._is480_tls.cid=cid
        try:return self.process_image(*args)
        finally:self._is480_tls.cid=None

    def _is480_point_checked(self,cid,*args):
        if not self.is480_bridge.machine.current(cid):return None
        self._is480_tls.cid=cid
        try:return self.process_single_point(*args)
        finally:self._is480_tls.cid=None

    def _is480_stage_capture_valid(self,vt,expected):
        c=self.is480_bridge.machine.cycle
        if not c or not self.is480_bridge.machine.current(c.cid):
            return False
        actual=self.queue_put_count_vt1 if vt==1 else self.queue_put_count_vt2
        if actual != expected:
            self._is480_fault(f'CAMERA_CAPTURE_INCOMPLETE: {self.is480_pc_id} VT{vt}: {actual}/{expected} camera')
            return False
        return True

    def _is480_start_local(self,cid,barcode):
        if not self.is480_bridge.machine.current(cid):return
        self.databacode=barcode
        self.current_barcode=barcode
        self.gui_kqdocbarcode=False
        self.is480_raw_jpegs={}
        with self.is480_view_lock:
            self.is480_checked_jpegs.clear()
            if self.is480_pc_id=='PC1':self.is480_remote_view_jpegs.clear()
        if self.is480_pc_id=='PC1':
            self.is480_remote_images.clear()  # legacy cache
            self.is480_pc1_last_img = None
            self.is480_remote_ng_paths.clear()
            self.is480_remote_view_verdict.clear()
            self.is480_remote_details_saved.clear()
        self._is480_vt_started={1:time.monotonic()}
        self.is480_ng_point_details=[]
        self.is480_final_cache=None
        self.widget_station_infor.label_5.config(text=barcode)
        self.bat_dau()

    def _is480_vt2_local(self,cid):
        if not self.is480_bridge.machine.current(cid):return
        self._is480_vt_started[2]=time.monotonic()
        self.executor_ai.submit(self.kiemtra_VT2)

    def _is480_store_ng_image(self,event):
        """Durably save a worker NG JPEG BEFORE responding ACK over TCP.

        Network receiver thread only performs file I/O; it never touches Tkinter.
        A successful worker send therefore means PC1 has the JPEG on disk, even if
        the PC1 UI event queue is temporarily blocked or the next BODY begins.
        """
        if self.is480_pc_id!='PC1':raise RuntimeError('Only PC1 archives remote NG images')
        import base64
        c=self.is480_bridge.machine.cycle
        if not c or c.cid!=str(event.get('cycle_id')) or c.state in ('FAULT','ABORTED'):
            raise ValueError('stale NG image cycle')
        pc=str(event.get('pc') or '')
        cam_vt=str(event.get('cam_vt') or '')
        kind=str(event.get('kind') or '')
        if pc not in ('PC2','PC3') or not re.fullmatch(r'CAM\d+_VT[12]',cam_vt) or kind not in ('RAW','PROC'):
            raise ValueError('invalid NG image metadata')
        raw=base64.b64decode(str(event.get('image') or ''),validate=True)
        if not raw or len(raw)>3_000_000 or not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):
            raise ValueError('invalid NG JPEG bytes')
        path=Path(__file__).resolve().with_name('is480_ng_images')/c.cid/pc
        path.mkdir(parents=True,exist_ok=True)
        target=path/(cam_vt+'_'+kind+'.jpg')
        temp=path/(cam_vt+'_'+kind+'.uploading')
        temp.write_bytes(raw)
        os.replace(temp,target)
        return str(target)

    def _is480_image_event(self,event):
        if self.is480_pc_id != 'PC1':return
        c=self.is480_bridge.machine.cycle
        if not c or c.cid != event.get('cycle_id'):return
        if event.get('kind')!='PROC':return
        pc=event.get('pc');label=str(event.get('cam_vt',''))
        file=Path(event.get('path',''))
        if not file.is_file():
            self._is480_fault('Ảnh PROC không tồn tại: '+str(file))
            return
        self.is480_remote_ng_paths[(pc,label)]=str(file)
        # This is ONLY a UI update; NG detail rows are written atomically by the
        # event server on NG_DETAILS, and only from genuine NG POINT metadata.
        if self.is480_selected_pc == pc:
            self._is480_refresh_cam_button_colors()
        elif self.is480_selected_pc == 'PC1' and self.is480_final_cache in ('NG','PM_NG'):
            self._is480_render_global_ng()

    def _is480_show_remote_image(self,pc,frame):
        # Keep compatible with callers already providing decoded BGR frames.
        self._is480_show_frame(frame)

    def _is480_show_frame(self,frame):
        import cv2
        from PIL import Image,ImageTk
        rgb=cv2.cvtColor(cv2.resize(frame,(1000,528),interpolation=cv2.INTER_AREA),cv2.COLOR_BGR2RGB)
        im=ImageTk.PhotoImage(Image.fromarray(rgb))
        self.widget_result_check.label_image_1.configure(image=im,text='')
        self.widget_result_check.label_image_1.is480img=im
        self.last_display_imgtk=im
        self.last_display_mode='cam'

    def _is480_store_view_image(self,msg):
        """Network thread: retain bounded per-cam JPEG previews, never production DB."""
        import base64
        pc=str(msg.get('pc',''));label=str(msg.get('cam_vt',''))
        c=self.is480_bridge.machine.cycle
        if not c or c.cid!=str(msg.get('cycle_id')) or pc not in ('PC2','PC3'):
            raise ValueError('cycle/PC không hợp lệ cho VIEW_IMAGE')
        if not re.fullmatch(r'CAM\\d+_VT[12]',label):
            raise ValueError('VIEW_IMAGE camera không hợp lệ')
        blob=base64.b64decode(str(msg.get('image','')),validate=True)
        if not (10<len(blob)<1_500_000 and blob.startswith(b'\\xff\\xd8') and blob.endswith(b'\\xff\\xd9')):
            raise ValueError('VIEW_IMAGE JPEG không hợp lệ')
        with self.is480_view_lock:
            self.is480_remote_view_jpegs[(pc,label)]=blob
            self.is480_remote_view_verdict[(pc,label)]=bool(msg.get('is_ng'))
        return True

    def _is480_archive_remote_details(self,msg):
        """Event receiver: verify NG JPEG already on disk, commit point rows before ACK.

        Prevent old phantom point=0 row and image_path referring to missing JPG.
        """
        c=self.is480_bridge.machine.cycle
        if not c or c.cid != str(msg.get('cycle_id')):
            raise ValueError('NG_DETAILS stale cycle')
        pc=str(msg.get('pc',''))
        if pc not in ('PC2','PC3'):raise ValueError('NG_DETAILS invalid PC')
        points=msg.get('points',[])
        if not isinstance(points,list) or len(points)>1000:
            raise ValueError('NG_DETAILS invalid list')
        with self.is480_remote_details_lock:
            key=(c.cid,pc)
            if key in self.is480_remote_details_saved:return
            base=Path(__file__).resolve().with_name('is480_ng_images')/c.cid/pc
            events=[]
            for raw in points:
                if not isinstance(raw,dict):raise ValueError('NG_DETAILS malformed point')
                cam=int(raw['cam']);vt=int(raw['vitri']);num=int(raw['point'])
                label=f'CAM{cam}_VT{vt}'
                proc=base/(label+'_PROC.jpg')
                original=base/(label+'_RAW.jpg')
                if not proc.is_file() or not original.is_file():
                    raise IOError('ErrorArchive chưa nhận đủ RAW/PROC cho '+pc+' '+label)
                entry=dict(raw,cycle_id=c.cid,barcode=c.barcode,
                           check_no=c.check_no,pc=pc,image_path=str(proc))
                events.append(entry)
            if c.mode=='RUN':
                # If any camera was transmitted as NG, it must have POINT detail.
                if not events and any(base.glob('*_PROC.jpg')):
                    raise ValueError('Có ảnh NG nhưng thiếu danh sách POINT NG: '+pc)
                if events:
                    from is480_statistics import insert_ng_events
                    insert_ng_events(events)
            self.is480_remote_details_saved.add(key)
            _log(self,'NG_DETAILS_ARCHIVED',f'{pc}: {len(events)} POINT, cycle={c.cid}')

    def _is480_show_checked_camera(self,pc,label):
        """PC1 screen: click CAMxx_VTy for a processed OK or NG image from that PC."""
        import cv2,numpy as np
        if pc=='PC1':
            with self.is480_view_lock:blob=self.is480_checked_jpegs.get(label)
        else:
            with self.is480_view_lock:blob=self.is480_remote_view_jpegs.get((pc,label))
        if not blob:
            self.widget_result_check.label_image_1.configure(image='',text=pc+' '+label+': CHƯA CÓ ẢNH CHECK')
            return False
        frame=cv2.imdecode(np.frombuffer(blob,dtype=np.uint8),cv2.IMREAD_COLOR)
        if frame is None:
            self._is480_warning('JPEG xem ảnh lỗi: '+pc+' '+label)
            return False
        self._is480_show_frame(frame)
        return True

    def _is480_view_image_event(self,event):
        if self.is480_pc_id != 'PC1':return
        pc=event.get('pc'); label=event.get('cam_vt')
        if self.is480_selected_pc == pc:
            self._is480_refresh_cam_button_colors()
        _log(self,'VIEW_RECEIVED',pc+' '+str(label))

    def _is480_refresh_cam_button_colors(self):
        if self.is480_pc_id!='PC1' or not hasattr(self,'is480_cam_slots'):return
        pc=self.is480_selected_pc
        ng_keys=set(getattr(self,'is480_remote_ng_paths',{}))
        for button,label in getattr(self,'is480_visible_cam_slots',[]):
            if pc=='PC1':continue
            if (pc,label) in ng_keys or self.is480_remote_view_verdict.get((pc,label),False):
                button.config(bg='#BC2626',fg='white')
            elif (pc,label) in self.is480_remote_view_jpegs:
                button.config(bg='#18803C',fg='white')
            else:
                button.config(bg='#4B5563',fg='white')

    def _is480_render_global_ng(self):
        """Show all RUN NG PROC images of PC1,PC2,PC3 in a near-square grid."""
        if self.is480_pc_id!='PC1':return
        import cv2,numpy as np
        c=self.is480_bridge.machine.cycle
        if c is None:return
        frames={}
        with self.ng_frames_lock:
            own=dict(self.ng_frames)
        own_ng=set(getattr(self,'ng_cam_keys_pm',set()))
        for label,frame in own.items():
            if not self.PM or label in own_ng:
                frames['PC1 '+label]=frame
        for (pc,label),filename in list(self.is480_remote_ng_paths.items()):
            if not Path(filename).is_file():continue
            try:
                raw=np.frombuffer(Path(filename).read_bytes(),dtype=np.uint8)
                frame=cv2.imdecode(raw,cv2.IMREAD_COLOR)
                if frame is not None:frames[pc+' '+label]=frame
            except OSError as exc:
                self._is480_warning('Lỗi đọc JPEG NG '+str(exc))
        self.is480_last_ng_count=len(frames)
        if not frames:
            self.widget_result_check.label_image_1.configure(
                image='',text='CHƯA CÓ ẢNH NG CỦA BA PC')
            return
        mosaic=_is480_square_ng_grid(frames)
        if mosaic is not None:self._is480_show_frame(mosaic)

    def process_image(self, frame, cam, vt, toado, *args):
        """Mirror Main's fully annotated frame after real inference returns.

        Main.so_nut_an stores a reference to processed_frame for every point with
        NUTSO, so after process_image returns the same frame has all ROI overlays.
        Only a short-lived JPEG preview is retained (never inserted into production DB).
        """
        result = self._process_image(frame, cam, vt, toado, *args)
        bridge = getattr(self,'is480_bridge',None)
        cycle = getattr(bridge.machine,'cycle',None) if bridge else None
        if not cycle or not bridge.machine.current(cycle.cid):
            return result
        label = 'CAM%d_VT%d' % (int(cam),int(vt))
        selected = None
        for point in toado:
            try:
                ix=int(point.get('NUTSO',-1))
                if 0 <= ix < len(self.so_nut_an):
                    value=self.so_nut_an[ix]
                    if hasattr(value,'shape') and getattr(value,'size',0):
                        selected=value
                        break
            except (TypeError,ValueError,IndexError,AttributeError):
                continue
        if selected is None:
            with self.ng_frames_lock:
                selected=self.ng_frames.get(label)
        if selected is None:
            # Avoid confusing an unannotated capture with an AI image.
            _log(self,'VIEW_NOT_AVAILABLE',label+' không có frame processed/NUTSO')
            return result
        import cv2
        h,w=selected.shape[:2]
        scale=min(1.0,960.0/max(w,1),540.0/max(h,1))
        scaled=cv2.resize(selected,(max(1,int(w*scale)),max(1,int(h*scale))),
                          interpolation=cv2.INTER_AREA) if scale<1 else selected
        ok,blob=cv2.imencode('.jpg',scaled,[cv2.IMWRITE_JPEG_QUALITY,70])
        if not ok:raise RuntimeError('Không mã hóa được ảnh xem lại '+label)
        with self.is480_view_lock:
            self.is480_checked_jpegs[label]=blob.tobytes()
        return result

    def _is480_send_view_images(self,cid):
        if self.is480_pc_id == 'PC1':return
        import base64
        with self.is480_view_lock:
            items=sorted(self.is480_checked_jpegs.items())
        for label,blob in items:
            if not self.is480_bridge.machine.current(cid):return
            # Preview only; it must not become NG in database or ErrorArchive.
            self.is480_bridge._out_event('VIEW_IMAGE',cycle_id=cid,cam_vt=label,
                                         image=base64.b64encode(blob).decode('ascii'))
        _log(self,'VIEW_SENT',f'{self.is480_pc_id} {len(items)} ảnh xem lại, cycle={cid}')

    def _is480_capture_fresh(self,cam,vt):
        """Demand a frame newer than VT trigger; settle after first camera exposure."""
        triggered=self._is480_vt_started.get(vt,time.monotonic())
        deadline=time.monotonic()+2.8
        while time.monotonic()<deadline:
            if not self._is480_current_thread_cycle():return False
            if time.monotonic()>=triggered+0.45:
                with self.frame_lock:
                    timestamp=getattr(self,'is480_frame_times',{}).get(cam,0)
                if timestamp>=triggered+0.20:
                    return True
            time.sleep(0.04)
        return False

    def _is480_capture_raw(self,cam,vt,frame):
        """Keep compressed exact checked frame, not another frame grabbed later."""
        import cv2
        h,w=frame.shape[:2]
        ratio=min(1.0,1000/max(w,1),620/max(h,1))
        small=cv2.resize(frame,(int(w*ratio),int(h*ratio))) if ratio<1 else frame
        success, encoded=cv2.imencode('.jpg',small,[cv2.IMWRITE_JPEG_QUALITY,72])
        if success:self.is480_raw_jpegs[f'CAM{cam}_VT{vt}']=encoded.tobytes()

    def _is480_ng_point(self,cam,vt,point):
        detail={'cam':int(cam),'vitri':int(vt),'point':int(point['POINT']),
                'name':str(point.get('NAME','')),'tag':str(point.get('NAME','')),
                'level':int(point.get('LEVEL',0) or 0),
                'x':int(point.get('X',0) or 0),'y':int(point.get('Y',0) or 0),
                'size':int(point.get('SIZE',0) or 0)}
        self.is480_ng_point_details.append(detail)

    def _is480_send_ng_images(self,cid,snapshot):
        if self.is480_pc_id=='PC1':return
        import cv2
        for label,frame in snapshot.items():
            if not self.is480_bridge.machine.current(cid):return
            if not label.startswith('CAM'):continue
            try:
                h,w=frame.shape[:2]
                ratio=min(1,1000/max(w,1),620/max(h,1))
                resized=cv2.resize(frame,(max(1,int(w*ratio)),max(1,int(h*ratio)))) if ratio<1 else frame
                okay,encoded=cv2.imencode('.jpg',resized,[cv2.IMWRITE_JPEG_QUALITY,75])
                if not okay:raise RuntimeError('JPEG encode fail')
                if label not in self.is480_raw_jpegs:
                    raise RuntimeError('Thiếu ảnh RAW camera NG: '+label)
                self.is480_bridge._out_event('NG_IMAGE',cycle_id=cid,cam_vt=label,kind='RAW',
                    image=base64.b64encode(self.is480_raw_jpegs[label]).decode('ascii'))
                self.is480_bridge._out_event('NG_IMAGE',cycle_id=cid,cam_vt=label,kind='PROC',
                    image=base64.b64encode(encoded.tobytes()).decode('ascii'))
            except Exception as exc:
                self._is480_fault(f'IMAGE_SEND_FAILED {label}: {exc}')
                return
        try:
            self.is480_bridge._out_event('NG_DETAILS',cycle_id=cid,points=list(self.is480_ng_point_details))
        except Exception as exc:
            self._is480_fault(f'NG_DETAILS_SEND_FAILED: {exc}')

    def _is480_save_local_ng(self,cid,snapshot):
        import cv2
        from is480_statistics import insert_ng_events
        c=self.is480_bridge.machine.cycle
        if not c:return
        base=Path(__file__).resolve().with_name('is480_ng_images')/cid/'PC1'
        base.mkdir(parents=True,exist_ok=True)
        for label,frame in snapshot.items():
            safe=re.sub('[^A-Za-z0-9_-]','',label)
            filename=base/(safe+'_PROC.jpg')
            if not cv2.imwrite(str(filename),frame):
                raise RuntimeError(f'Could not save local NG image {label}')
            if label in self.is480_raw_jpegs:
                (base/(safe+'_RAW.jpg')).write_bytes(self.is480_raw_jpegs[label])
        events=[]
        for item in self.is480_ng_point_details:
            label=f"CAM{item['cam']}_VT{item['vitri']}"
            entry=dict(item,cycle_id=cid,barcode=c.barcode,check_no=c.check_no,pc='PC1',
                       image_path=str(base/(label+'_PROC.jpg')))
            events.append(entry)
        if events:insert_ng_events(events)

    def _is480_finish(self):
        from concurrent.futures import as_completed
        bridge=self.is480_bridge
        c=bridge.machine.cycle
        if not c or not bridge.machine.current(c.cid):return
        cid=c.cid
        try:
            with self.futures_lock:futures=list(self.process_futures)
            for f in as_completed(futures):f.result()
        except Exception as exc:
            self._is480_fault(f'AI_PROCESS_FAILED: {exc}')
            return
        if not bridge.machine.current(cid):return
        result='NG' if self.list_ng else 'OK'
        self.is480_final_cache=('PM_'+result) if self.PM else result
        self.step_cam=0
        self.stop=False
        if hasattr(self,'initProcess'):
            self.initProcess.barcode=False
        self.gui_kq=False
        self.mocam=True
        with self.ng_frames_lock:snapshot=dict(self.ng_frames)
        if self.PM:
            img=self.build_all_grid(snapshot)
        else:
            img=self.build_ng_grid(snapshot) if self.list_ng else None
        if img is not None:self.ui_queue.put({'type':'ng_grid','frame':img})
        self.ui_queue.put({'type':'final_result','result':self.is480_final_cache})
        self.process_futures.clear()
        if result=='NG' and not self.PM:
            try:
                if self.is480_pc_id!='PC1':
                    # Deliver images BEFORE CYCLE_DONE, no arbitrary 2s race.
                    self._is480_send_ng_images(cid,snapshot)
                else:self._is480_save_local_ng(cid,snapshot)
            except Exception as exc:
                self._is480_fault('NG_ARCHIVE_FAILED: '+str(exc))
                return
        if bridge.machine.current(cid) and self.is480_pc_id!='PC1':
            try:
                # All cameras (OK + NG + PM), sent BEFORE CYCLE_DONE.
                self._is480_send_view_images(cid)
            except Exception as exc:
                # Previews are noncritical; archived NG images above remain strict.
                _log(self,'VIEW_SEND_FAILED',str(exc))
                self.ui_queue.put({'type':'is480_warning',
                    'message':'Ảnh xem lại PC phụ chưa gửi đủ: '+str(exc)})
        if bridge.machine.current(cid):
            bridge.local_final_done(self.is480_final_cache)

    def _is480_reset_ui(self):
        # No serial write. Old AI tasks are invalidated by cycle_id/generation.
        self.stop=False
        self.start=False
        self.gui_kq=False
        self.guithVT1=False
        self.guithVT2=False
        self.gui_kqdocbarcode=False
        self.step_cam=0
        self.mocam=False
        self.is480_final_cache=None
        self.is480_raw_jpegs={}
        with self.is480_view_lock:
            self.is480_checked_jpegs.clear()
            self.is480_remote_view_jpegs.clear()
        self.is480_remote_ng_paths.clear()
        self.is480_remote_view_verdict.clear()
        self.is480_remote_details_saved.clear()
        self.is480_bridge._pending_local_final=None
        with self.allow_put_lock:self.allow_put=False
        while True:
            try:self.queue_xl_anh.get_nowait()
            except queue.Empty:break
        with self.futures_lock:
            for future in self.process_futures:
                future.cancel()
            self.process_futures.clear()
        self.ng_frames={}
        self.list_ng.clear()
        self.widget_result_check.label2.configure(text='HOLD',bg='#FFD166')

    def _is480_request_run(self,bypass=False):
        if self.is480_pc_id!='PC1':return
        if not bypass and not self.is480_mode_state.get('pm_attempted'):
            self._is480_warning('Chưa chạy PM; không thể chuyển RUN', 'run_request')
            return
        c=self.is480_bridge.machine.cycle
        if c and c.state not in ('DONE','ABORTED'):
            self._is480_warning('Phải kết thúc/hủy chu trình trước khi chuyển RUN', 'run_request')
            return
        threading.Thread(target=self._is480_run_worker,args=(bypass,),daemon=True).start()

    def _is480_run_worker(self, bypass=False):
        self._is480_sync_workers(request_run=True, bypass=bypass)

    def _is480_sync_workers(self, request_run=False, bypass=False):
        bridge = self.is480_bridge
        with bridge.mode_lock:
            try:
                self._is480_check_workers(request_run, bypass)
            except Exception as exc:
                bridge.workers_ready.clear()
                bridge.warning('mode', f'Chưa đồng bộ PM/RUN: {exc}')

    def _is480_check_workers(self, request_run, bypass):
        bridge = self.is480_bridge
        statuses = bridge.poll_workers()
        with bridge.mode_state_lock:
            state = self.is480_mode_state
            boots = {pc: status['boot'] for pc, status in statuses.items()}
            saved_boots = state.get('peers', {})
            rebooted = any(
                (pc in bridge.peer_boots and bridge.peer_boots[pc] != boot)
                or (pc in saved_boots and saved_boots[pc] != boot)
                or (state.get('mode') == 'RUN' and pc not in saved_boots)
                for pc, boot in boots.items()
            )
            if rebooted:
                bridge.workers_ready.clear()
                bridge.fault('Máy phụ đã reboot Windows', transient=True)
                updated = {**state, 'mode': 'PM', 'pm_attempted': False, 'pm_required': True,
                           'peers': {**saved_boots, **boots}}
                _save_mode(self, updated)
                state.update(updated)
                bridge.warning('pm_required', 'Máy phụ đã reboot Windows — cần chạy PM lại')
            bridge.peer_boots.update(boots)
        bridge.retry_peer_aborts(statuses)

        online = len(statuses) == 2 and not any(st['camera_faults'] for st in statuses.values())
        if not online or bridge.pending_aborts or bridge.camera_faults():
            bridge.workers_ready.clear()
            if request_run:
                bridge.warning('mode', 'Chưa thể chuyển RUN: hệ thống chưa sẵn sàng')
            return

        if state.get('pm_required'):
            request_run = False
        desired = 'RUN' if request_run else state.get('mode', 'PM')
        local_mode = 'PM' if self.PM else 'RUN'
        mismatch = [pc for pc, status in statuses.items() if status['mode'] != desired]
        cycle = bridge.machine.cycle
        if cycle and cycle.state not in ('DONE', 'ABORTED'):
            if any(st['mode'] != cycle.mode for st in statuses.values()):
                bridge.fault('Máy phụ lệch chế độ trong khi đang kiểm tra', transient=True)
            if cycle.state == 'FAULT':
                bridge.workers_ready.clear()
            return

        if request_run and not (bypass or state.get('pm_attempted')):
            bridge.warning('run_request', 'Chưa chạy PM; không thể chuyển RUN')
            return
        if mismatch or local_mode != desired or request_run:
            bridge.workers_ready.clear()
            if self.is480_rfid_gate_active.is_set() or self.is480_body_scan_active.is_set():
                bridge.warning('mode', f'Đang chờ kết thúc đọc barcode/xác nhận RFID để đồng bộ {desired}')
                return
            for pc in mismatch:
                ack = bridge._request(pc, 'SET_MODE_' + desired)
                if not ack.get('ok') or ack.get('pc') != pc or ack.get('boot') != boots[pc]:
                    raise RuntimeError(f'{pc} chưa xác nhận {desired}: {ack}')
            # Persist authorized RUN only after both workers confirm the mode.
            if request_run:
                with bridge.mode_state_lock:
                    updated = {**state, 'mode': 'RUN', 'peers': boots, 'boot': boot_marker(),
                               'bypass': bool(bypass)}
                    _save_mode(self, updated)
                    state.update(updated)
            if local_mode != desired:
                applied = threading.Event()
                self.ui_queue.put({'type': f'is480_mode_{desired.lower()}', 'applied': applied})
                if not applied.wait(5):
                    raise TimeoutError(f'PC1 chưa xác nhận giao diện {desired}')
        if request_run:
            bridge.warning('run_request')
        bridge.warning('mode')
        bridge.warning('start_cycle')
        bridge.workers_ready.set()

    def _is480_key5(self,event):
        if self.is480_pc_id != 'PC1' or not self.PM:
            return
        if isinstance(event.widget,(tk.Entry,tk.Text,tk.Spinbox,ttk.Entry,ttk.Combobox)):
            return
        # A keyboard-wedge barcode can START with '555'; wait for 300ms of
        # inactivity before triggering the bypass. An arriving 4th key cancels it.
        now = time.monotonic()
        self.is480_last_5 = [t for t in self.is480_last_5 if now-t < 2.3]+[now]
        if len(self.is480_last_5) == 3:
            def confirm():
                self._is480_555_pending = None
                if self.PM and len(self.is480_last_5) == 3:
                    self.is480_last_5=[]
                    _log(self,'PM_BYPASS_555','555 confirmed; coordinated direct RUN requested')
                    self._is480_request_run(bypass=True)
            self._is480_555_pending = self.after(300,confirm)

    def _is480_global_keypress(self,event):
        """555 => direct RUN (A); never mistake the prefix of a HID scan."""
        if self.is480_pc_id != 'PC1':
            return
        key = str(getattr(event,'keysym',''))
        digit = key in ('5','KP_5') or str(getattr(event,'char',''))=='5'
        if self._is480_555_pending is not None:
            if key not in ('Shift_L','Shift_R','Num_Lock'):
                self.after_cancel(self._is480_555_pending)
                self._is480_555_pending = None
                self.is480_last_5=[]
            return
        if digit:
            self._is480_key5(event)
        elif self.is480_last_5 and key not in ('Shift_L','Shift_R','Num_Lock'):
            self.is480_last_5=[]

    def _is480_camera_health(self):
        now = time.monotonic()
        bridge = self.is480_bridge
        for index in range(8):
            source = f'PC1:camera:{index}'
            if getattr(self, f'cap{index}', None) is None:
                since = self.is480_camera_bad_since.setdefault(index, now)
                if now - since > 8:
                    reason = f'PC1 mất kết nối CAM{index}'
                    self._is480_warning(reason, source)
                    bridge.fault(reason, transient=True)
            else:
                self.is480_camera_bad_since.pop(index, None)
                self._is480_warning(None, source)
        self.after(2500, self._is480_camera_health)

    def _is480_ready_event(self):
        # Direct UDP heartbeat is independent from PC1 inspection program/network bridge.
        cfg=load_site_config(Path(__file__).resolve().with_name('is480_config.json'),self.is480_pc_id)
        try:
            if self.is480_pc_id!='PC1':
                host=self.is480_bridge.master_ip or cfg.get('PC1_host')
            else:host='127.0.0.1'
            if host:
                payload={'token':cfg['token'],'pc_id':self.is480_pc_id,'type':'ready','event':'ready','code':'APP_READY','message':'ready',
                         'event_id':f'{self.is480_pc_id}-{int(time.time()//5)}'}
                s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
                with s:s.sendto(json.dumps(payload).encode('utf8'),(host,6200))
        except Exception:pass
        self.after(5000,self._is480_ready_event)

    def _is480_ui_event(self,event):
        typ=event.get('type')
        if not isinstance(typ,str) or not typ.startswith('is480_'):return False
        bridge=self.is480_bridge
        if typ=='is480_reset_barcode':
            self.databacode=None
            self.current_barcode=None
            self.widget_result_check.label2.config(text='SCAN',bg='#DBEAFE')
        elif typ=='is480_start':
            self._is480_start_local(event['cycle_id'],event['barcode'])
        elif typ=='is480_vt2':
            self._is480_vt2_local(event['cycle_id'])
        elif typ=='is480_mode_run':
            self.show_run()
            if event.get('applied'):event['applied'].set()
            if self.is480_pc_id=='PC1':self._is480_sync_run_button()
        elif typ=='is480_mode_pm':
            self.show_pm()
            if event.get('applied'):event['applied'].set()
            if self.is480_pc_id=='PC1':self._is480_sync_run_button()
        elif typ == 'is480_warning':
            self._is480_warning(event.get('message'), event.get('source', 'operation'))
        elif typ == 'is480_abort':
            self._is480_reset_ui()
            self._is480_warning(None, 'run_request')
        elif typ=='is480_commit':
            c=bridge.machine.cycle
            if c and c.cid==event['cycle_id'] and c.ack_final:
                if 'NG' in event['verdict'] and c.mode=='RUN' and self.is480_selected_pc=='PC1':
                    self._is480_render_global_ng()
                self.widget_result_check.label2.config(
                    text=event['verdict'],bg=('#D62424' if 'NG' in event['verdict'] else '#278C3E'))
                # Fail closed: a PM result can NEVER be committed as a RUN check.
                if event['mode']=='RUN' and c.mode=='RUN':
                    try:
                        from is480_statistics import commit_complete_cycle
                        inserted=commit_complete_cycle(event['cycle_id'],event['barcode'],
                                                        event['check_no'],event['verdict'])
                        if inserted:
                            if event['verdict']=='OK':self.qty_ok+=1
                            else:self.qty_ng+=1
                            self.qty_all+=1
                            self.ui_queue.put({'type':'counter','ok':self.qty_ok,'ng':self.qty_ng,'all':self.qty_all})
                            try:
                                self.ghifile(self.qty_ok,self.qty_ng,self.qty_all)
                            except Exception as exc:
                                # Counters backup cannot bypass the durable NG RFID gate.
                                _log(self,'COUNTER_FILE_ERROR',str(exc))
                        self.is480_body_shift_key=None
                        if event['verdict']=='NG':
                            # SQLite stores durable WAIT CARD in same transaction as RUN_NG.
                            self.start_ng_confirmation_wait(event['barcode'],event['check_no'],event['cycle_id'])
                        else:
                            # RUN_OK has no RFID; 0x15 already ACKed, DB is committed.
                            self.is480_rfid_gate_active.clear()
                    except Exception as exc:
                        # Fail closed: never clear gate if the DB cannot commit.
                        self._is480_warning('DB COMMIT / WAIT CARD ERROR: ' + str(exc), 'db')
                elif event['mode']=='PM' and c.mode=='PM':
                    with bridge.mode_state_lock:
                        if c.worker_boots != bridge.peer_boots:
                            self._is480_warning('Máy phụ đã reboot Windows — cần chạy PM lại', 'pm_required')
                            return True
                        updated = {**self.is480_mode_state, 'pm_attempted': True,
                                   'peers': dict(bridge.peer_boots)}
                        updated.pop('pm_required', None)
                        _save_mode(self, updated)
                        self.is480_mode_state.update(updated)
                        self.is480_mode_state.pop('pm_required', None)
                    self._is480_warning(None, 'pm_required')
                    self._is480_warning(None, 'run_request')
                    self._is480_sync_run_button()
                    self.widget_result_check.label3.config(
                        text=event['verdict']+' - CÓ THỂ CHUYỂN RUN',
                        bg=('#FFD166' if 'NG' in event['verdict'] else '#B9E9C9'))
                    _log(self,'PM_ATTEMPTED',event['verdict']+' - RUN unlocked, no RFID/DB/counters')
                else:
                    self._is480_warning('Bất nhất chế độ khi chốt kết quả: '+str(event['mode']))
        elif typ=='is480_rfid_card':
            self.handle_card_scan(event.get('card_id'))
        elif typ=='is480_rfid_wait':
            # Close historical F1 window; it must not bypass automatic RUN_NG RFID.
            if getattr(self,'approval_window',None) is not None:
                self.close_approval_window()
            self.widget_result_check.label2.config(text='NG', bg='#D62424')
            self.widget_result_check.label3.config(
                text='NG - VUI LÒNG QUẸT THẺ COM4', bg='#FFD166')
            _log(self,'RFID_WAIT_UI',
                 f"{event.get('barcode')} / {event.get('check_no')}")
        elif typ=='is480_rfid_notice':
            message = str(event.get('message') or '')
            if self._is_ng_confirmation_waiting():
                self.widget_result_check.label3.config(
                    text='CHỜ RFID - '+message, bg='#FFD166')
            _log(self,'RFID_NOTICE',message)
        elif typ=='is480_rfid_success':
            name = str(event.get('name') or '')
            self.latest_approval_operator = name
            self.widget_result_check.label3.config(
                text='RFID OK: '+name+' - ĐÃ GỬI 0x37', bg='#B9E9C9')
            _log(self,'RFID_SUCCESS',
                 f"{event.get('barcode')}/{event.get('check_no')} by {name}")
        elif typ=='is480_repair_scanned':
            self._is480_repair_barcode(raw=event.get('barcode'))
        elif typ=='is480_repair_done':
            self._is480_repair_done(event)
        elif typ=='is480_repair_notice_event':
            self._is480_repair_notice(event.get('message','Đầu đọc sửa lỗi'))
        elif typ=='is480_remote_image':self._is480_image_event(event)
        elif typ=='is480_view_image':self._is480_view_image_event(event)
        elif typ=='is480_remote_details':
            # Backwards-compatible event type; network receiver now commits before ACK.
            pass
        elif typ=='is480_check_final_ready':
            bridge.flush_pending_final()
            bridge._maybe_send_final(event['cycle_id'])
        return True

    def _is480_is_ng_confirmation_waiting(self):
        """C6-style WAIT CARD status, not a production result by itself."""
        if self.is480_pc_id != 'PC1':
            return False
        with self.is480_ng_confirm_lock:
            return self.is480_ng_confirm_pending is not None

    def _is480_load_ng_confirm_user(self, card_id):
        """UID authorization using exactly the C6 PC3 JSON users/card_id/name schema."""
        card_id = self.clean_approval_card_data(card_id)
        if not card_id:
            return None
        path = getattr(self, 'NG_CONFIRM_USER_FILE', r'E:\database\L1200.json')
        try:
            with open(path, 'r', encoding='utf8') as inp:
                data = json.load(inp)
        except Exception as exc:
            raise RuntimeError(f'Không đọc được danh sách RFID {path}: {exc}') from exc
        if not isinstance(data, dict) or not isinstance(data.get('users'), list):
            raise ValueError('Danh sách RFID phải là JSON object chứa users: [...]')
        for user in data['users']:
            if not isinstance(user, dict):
                continue
            stored = self.clean_approval_card_data(user.get('card_id', ''))
            if stored and stored == card_id:
                name = str(user.get('name') or '').strip()
                if not name:
                    raise ValueError('UID có trong danh sách nhưng thiếu tên nhân viên')
                return user
        return None

    def _is480_start_ng_confirmation_wait(self, barcode, check_no, cycle_id, restored=None):
        """Called only after RUN_NG / PLC 0x15 / successful production DB commit."""
        if self.is480_pc_id != 'PC1':
            return False
        barcode = str(barcode or '').strip()
        check_no = int(check_no)
        if not barcode or check_no <= 0 or not cycle_id:
            raise ValueError('Dữ liệu WAIT CARD không hợp lệ')
        with self.is480_ng_confirm_lock:
            pending = self.is480_ng_confirm_pending
            if pending:
                if (pending['cycle_id'],pending['barcode'],int(pending['check_no'])) == (cycle_id,barcode,check_no):
                    return False
                raise RuntimeError('Đang WAIT CARD một BODY khác: '+str(pending))
            self.is480_ng_confirm_pending = restored or {
                'cycle_id':cycle_id,'barcode':barcode,'check_no':check_no,
                'card_id':None,'confirmed_by':None,'confirmed_at':None}
            self.is480_ng_confirm_in_progress = False
            self.is480_ng_confirm_last_card = None
            self.is480_ng_confirm_last_card_at = 0.0
            self.is480_rfid_gate_active.set()
            self.is480_rfid_37_written = False
            self.is480_deferred_plc_start = False
        self.ui_queue.put({'type':'is480_rfid_wait',
                           'barcode':barcode,'check_no':check_no})
        if getattr(self,'approval_card_error',''):
            self.ui_queue.put({'type':'is480_rfid_notice',
                               'message':self.approval_card_error})
        _log(self,'WAIT_CARD',f'RUN_NG {barcode} #{check_no}, chờ COM4')
        return True

    def _is480_handle_card_scan(self, card_id):
        """RFID incoming: ignore outside durable WAIT CARD; debounce duplicate UID."""
        if self.is480_pc_id != 'PC1':
            return
        uid = self.clean_approval_card_data(card_id)
        if not uid:
            return
        with self.is480_ng_confirm_lock:
            if self.is480_ng_confirm_pending is None or self.is480_ng_confirm_in_progress:
                return
            now = time.monotonic()
            if (uid == self.is480_ng_confirm_last_card
                    and now - self.is480_ng_confirm_last_card_at
                    < float(getattr(self,'NG_CONFIRM_CARD_DEBOUNCE_SEC',1.0))):
                return
            self.is480_ng_confirm_last_card = uid
            self.is480_ng_confirm_last_card_at = now
            self.is480_ng_confirm_in_progress = True
        try:
            self.executor.submit(self._confirm_ng_card_worker, uid)
        except Exception as exc:
            with self.is480_ng_confirm_lock:
                self.is480_ng_confirm_in_progress = False
            self.ui_queue.put({'type':'is480_rfid_notice',
                               'message':'Không chạy được worker xác nhận RFID: '+str(exc)})

    def _is480_confirm_ng_card_worker(self, card_id):
        """C6 flow on PC1: UID validate -> atomic DB -> COM1 0x37 -> unlock.

        A failed COM write keeps the same UID in SQLite so scanning that card
        again retries the signal without writing NG confirmation twice.
        """
        if self.is480_pc_id != 'PC1':
            return
        try:
            from is480_statistics import confirm_ng_rfid_release, mark_ng_rfid_37_sent
            with self.is480_ng_confirm_lock:
                record = dict(self.is480_ng_confirm_pending or {})
            if not record:
                return
            cycle_id = record['cycle_id']
            barcode = record['barcode']
            check_no = int(record['check_no'])
            saved_uid = record.get('card_id')
            if saved_uid:
                if saved_uid != card_id:
                    raise PermissionError('DB đã xác nhận UID khác. Quẹt lại đúng thẻ trước đó.')
                name = record.get('confirmed_by')
                if not name:
                    raise RuntimeError('DB đã có UID nhưng thiếu tên xác nhận')
            else:
                user = self._load_ng_confirm_user(card_id)
                if user is None:
                    self.ui_queue.put({'type':'is480_rfid_notice',
                                       'message':'THẺ KHÔNG HỢP LỆ. Hãy quẹt lại!'})
                    with self.is480_ng_confirm_lock:
                        self.is480_ng_confirm_last_card = None
                    return
                name = str(user['name']).strip()
            state = confirm_ng_rfid_release(cycle_id,barcode,check_no,card_id,name)
            with self.is480_ng_confirm_lock:
                if self.is480_ng_confirm_pending is not None and self.is480_ng_confirm_pending['cycle_id'] == cycle_id:
                    self.is480_ng_confirm_pending.update(state)
            # Only the PLC master PC1 COM1 may issue 0x37. No fabricated PLC ACK.
            with self.serial_lock:
                ser = getattr(self,'ser',None)
                if ser is None or not getattr(ser,'is_open',False):
                    raise ConnectionError('COM1 PLC mất kết nối; chưa gửi được 0x37')
                # Hold the same lock as serial's 0x10 admission: a lightning-fast
                # PLC restart cannot slip between write(0x37) and deferral flag.
                with self.is480_ng_confirm_lock:
                    written = ser.write(b'\x37')
                    if written != 1:
                        raise IOError('COM1 không ghi đủ 1 byte 0x37')
                    self.is480_rfid_37_written = True
                ser.flush()
            # If this DB write fails AFTER serial, keep WAIT CARD and warn: the
            # real PLC may already have received 0x37 (no ACK protocol available).
            mark_ng_rfid_37_sent(cycle_id, card_id)
            deferred_start = False
            with self.is480_ng_confirm_lock:
                if (self.is480_ng_confirm_pending is not None
                        and self.is480_ng_confirm_pending['cycle_id'] == cycle_id):
                    self.is480_ng_confirm_pending = None
                    self.is480_ng_confirm_in_progress = False
                    self.is480_rfid_gate_active.clear()
                    deferred_start = self.is480_deferred_plc_start
                    self.is480_deferred_plc_start = False
                    self.is480_rfid_37_written = False
            # PLC could send 0x10 immediately after processing 0x37. Replay that
            # one start only AFTER the DB release marker has been written.
            if deferred_start:
                try:
                    self._is480_accept_plc_start()
                except Exception as exc:
                    self._is480_fault('PLC 0x10 sau RFID: '+str(exc))
            self.ui_queue.put({'type':'is480_rfid_success',
                               'name':name,'barcode':barcode,'check_no':check_no,
                               'confirmed_at':state['confirmed_at']})
            _log(self,'RFID_0X37',f'UID hợp lệ, DB đã ghi, đã phát 0x37: {barcode}/{check_no} người xác nhận {name}')
        except Exception as exc:
            self.ui_queue.put({'type':'is480_rfid_notice',
                               'message':'RFID chưa hoàn tất / KHÔNG mở BODY: '+str(exc)})
            _log(self,'RFID_CONFIRM_ERROR',str(exc))
        finally:
            with self.is480_ng_confirm_lock:
                self.is480_ng_confirm_in_progress = False

    def _is480_accept_plc_start(self):
        """Admit exactly one PLC 0x10; reused for immediately deferred post-0x37.

        Never ack an unadmitted BODY, and never begin scanner when RFID gate is on.
        """
        with self.is480_scan_lock:
            if self.is480_rfid_gate_active.is_set():
                return False
            if self.is480_body_scan_active.is_set():
                _log(self,'PLC_DUP','0x10 ignored: COM3 BODY already scanning')
                return False
            c = self.is480_bridge.machine.cycle
            if c and c.state not in ('DONE','ABORTED'):
                self._is480_fault('PLC START while previous cycle active')
                return False
            if not self.is480_bridge.workers_ready.is_set():
                self.is480_bridge.warning('start_cycle', 'Chưa nhận Body: đang chờ PC2/PC3 sẵn sàng')
                return False
            if self.ser is None or not getattr(self.ser,'is_open',False):
                self.is480_bridge.warning('plc', 'PLC 0x10: COM1 chưa sẵn sàng')
                return False
            self.gui_kqdocbarcode = False
            self.ui_queue.put({'type':'is480_reset_barcode'})
            self.is480_body_scan_active.set()
            try:
                with self.serial_lock:
                    n = self.ser.write(b'\x30')
                    if n is not None and n != 1:
                        raise IOError('Unable to write 0x30')
                self.is480_bridge.warning('plc')
                threading.Thread(target=self.check_barcode, daemon=True,
                                 name='IS480-PLC-BodyBarcode').start()
                return True
            except Exception:
                self.is480_body_scan_active.clear()
                raise

    def read_serial(self):
        """Only PC1 talks PLC. 0x16/0x17/0x18 are intentionally not handled."""
        if self.ser is None:
            self._is480_fault('PLC COM1 unavailable')
            return
        _log(self,'PLC','Starting one-Serial master reader')
        while getattr(self,'camera_running',True):
            try:
                data=self.ser.read(1)
                if not data:continue
                code=data[0]
                bridge=self.is480_bridge
                if code==0x10:
                    with self.is480_ng_confirm_lock:
                        if self.is480_rfid_gate_active.is_set():
                            if self.is480_rfid_37_written:
                                self.is480_deferred_plc_start = True
                                _log(self,'PLC_0X10_DEFER','PLC 0x10 đến ngay sau 0x37; sẽ xử lý sau DB')
                            else:
                                _log(self,'PLC_BLOCK','0x10 bị chặn: đang chờ RFID/0x37 hoặc DB')
                            continue
                    self._is480_accept_plc_start()
                elif code==0x11:
                    if self.is480_rfid_gate_active.is_set():
                        _log(self,'PLC_BLOCK','0x11 bị chặn: chưa hoàn tất RFID RUN_NG')
                        continue
                    active=bridge.machine.cycle
                    if active and active.state not in ('DONE','ABORTED','FAULT'):
                        _log(self,'PLC_DUP','duplicate 0x11 ignored')
                        continue
                    if not bridge.workers_ready.is_set():
                        bridge.warning('start_cycle', 'Chưa bắt đầu VT1: đang chờ PC2/PC3 sẵn sàng')
                        continue
                    if not self.gui_kqdocbarcode or not str(self.databacode or '').strip():
                        self._is480_fault('PLC 0x11: barcode missing/not acknowledged')
                        continue
                    barcode=str(self.databacode).strip()
                    self.gui_kqdocbarcode=False
                    threading.Thread(target=self._is480_begin_cycle,args=(barcode,),daemon=True).start()
                elif code==0x12:
                    if not bridge.ack(0x12):self._is480_fault('Unexpected PLC 0x12')
                elif code==0x13:
                    active=bridge.machine.cycle
                    if active and active.state in ('VT2','FINAL','DONE'):
                        _log(self,'PLC_DUP','duplicate 0x13 ignored')
                        continue
                    threading.Thread(target=self._is480_begin_vt2,daemon=True).start()
                elif code==0x14:
                    _log(self,'PLC_OLD_BYTE','ignored retired VT2 PLC ACK 0x14')
                elif code==0x15:
                    if not bridge.ack(0x15):self._is480_fault('Unexpected PLC 0x15')
                elif code==0x40:
                    self.initProcess.close()
                    self.ui_queue.put({'type':'barcodeng'})
                elif code==0x41:
                    # Legacy barcode retry cannot bypass WAIT CARD.
                    if self.is480_rfid_gate_active.is_set():
                        _log(self,'PLC_BLOCK','0x41 bị chặn: WAIT CARD')
                        continue
                    if not bridge.workers_ready.is_set():
                        bridge.warning('start_cycle', 'Chưa đọc lại Body: đang chờ PC2/PC3 sẵn sàng')
                        continue
                    if not self.is480_body_scan_active.is_set():
                        from ReadBarcode import ScanHome
                        self.initProcess=ScanHome('COM3')
                        self.is480_body_scan_active.set()
                        threading.Thread(target=self.check_barcode,daemon=True).start()
                elif code in (0x16,0x17,0x18):
                    _log(self,'PLC_OLD_BYTE',f'ignored deprecated byte {code:#04x}')
                else:_log(self,'PLC_UNKNOWN',f'{code:#04x}')
            except Exception as exc:
                self._is480_fault('PLC communication error: '+str(exc))
                time.sleep(0.3)

    def _is480_begin_cycle(self, barcode):
        bridge = self.is480_bridge
        with bridge.mode_lock:
            if self.is480_rfid_gate_active.is_set():
                return
            try:
                cid = bridge.begin_master(barcode, 'PM' if self.PM else 'RUN')
                if not self.PM:
                    check_no = create_inspection_check(barcode)
                else:
                    check_no = 0
                bridge.machine.cycle.check_no = check_no
                self.current_barcode = barcode
                self.current_check_no = check_no
                self.ui_queue.put({'type': 'is480_start', 'cycle_id': cid, 'barcode': barcode})
            except Exception as exc:
                reason = 'START_CYCLE: ' + str(exc)
                if bridge.machine.cycle and bridge.machine.cycle.state not in ('DONE', 'ABORTED'):
                    bridge.fault(reason)
                else:
                    bridge.warning('start_cycle', reason)

    def _is480_begin_vt2(self):
        try:
            cid=self.is480_bridge.start_vt2()
            self.ui_queue.put({'type':'is480_vt2','cycle_id':cid})
        except Exception as exc:
            self._is480_fault('CAPTURE_VT2: '+str(exc))

    build_ng_grid = staticmethod(_is480_square_ng_grid)

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
