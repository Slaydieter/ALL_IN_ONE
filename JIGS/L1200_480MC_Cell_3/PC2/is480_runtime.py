# -*- coding: utf-8 -*-
"""IS1200_480: coordinator/worker protocol. No YOLO or Tk import.

Safety invariants:
- Only PC1 opens PLC COM1. Worker messages NEVER contain PLC bytes.
- All stage/results are keyed by one immutable cycle_id; stale/duplicate dropped.
- Missing capture, inference error, peer timeout -> FAULT, never OK/NG PLC.
- PLC 0x33 is issued after VT1 completion; VT2 has no PLC ACK.
- Final 0x35/0x36 is issued after three valid AI results and both worker ACK_VT2 confirmations.
- PM/RUN and Windows boot markers are separate from app restarts.
"""
import base64
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

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
        self.last_fault = ''
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
            threading.Thread(target=self._watchdog, daemon=True).start()
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
            self.fault(f'{pc} Windows restarted (boot changed)', allow_idle=True)
            self.app.ui_queue.put({'type':'is480_remote_reboot','pc':pc})
        self.peer_boots[pc] = boot
        evt = msg.get('event')
        if evt == 'HEARTBEAT':
            cams=msg.get('camera_faults') or []
            if cams:
                self.fault(f'{pc} CAMERA DISCONNECTED: {cams}',allow_idle=True)
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
            self.app.ui_queue.put({'type':'is480_remote_details', 'cycle_id':cid,'pc':pc,'points':msg.get('points',[])})
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

    def probe_peers(self):
        boots = {}
        for pc in ('PC2','PC3'):
            res = self._request(pc, 'STATUS')
            if not res.get('ok') or res.get('pc') != pc:
                raise ConnectionError(f'{pc} unavailable: {res}')
            if res.get('camera_faults'):
                raise RuntimeError(f'{pc} camera lost: {res["camera_faults"]}')
            boots[pc] = res['boot']
            self.last_seen[pc] = time.monotonic()
            old = self.peer_boots.get(pc)
            if old is not None and old != res['boot']:
                self.fault(f'{pc} Windows rebooted', allow_idle=True)
            self.peer_boots[pc] = res['boot']
        return boots

    def begin_master(self, barcode, mode):
        self.probe_peers()
        cid = self.machine.start(barcode, mode)
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
            for pc in ('PC2','PC3'):
                try:
                    self._request(pc,'ACK_FINAL',cycle_id=c.cid)
                except Exception as exc:
                    self.app.ui_queue.put({'type':'is480_warning', 'message':f'{pc}: ACK_FINAL network issue: {exc}'})
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

    def fault(self, reason, cid=None, allow_idle=False):
        changed = self.machine.fail(reason, cid)
        # A failure while START_CYCLE is being negotiated has no active cycle
        # yet; it still MUST reach PC1 WARNING banner.
        if changed or ((allow_idle or self.machine.cycle is None) and str(reason)!=self.last_fault):
            self.last_fault = str(reason)
            self.app.ui_queue.put({'type':'is480_warning','message':self.last_fault})
        if changed and self.pc_id == 'PC1':
            def abort_peers():
                for pc in ('PC2','PC3'):
                    try:self._request(pc,'ABORT')
                    except Exception:pass
            threading.Thread(target=abort_peers,daemon=True).start()
        if changed and self.pc_id != 'PC1':
            try:
                self._out_event('FAULT', cycle_id=cid or self.machine.cycle.cid, reason=reason)
            except Exception as exc:
                print('[IS480] unable to report FAULT:', exc)
        return changed

    def _watchdog(self):
        while True:
            time.sleep(1)
            if time.monotonic()-self._last_health_poll>5:
                self._last_health_poll=time.monotonic()
                for pc in ('PC2','PC3'):
                    try:
                        response=self._request(pc,'STATUS')
                        if not response.get('ok'):
                            raise ConnectionError(response)
                        old=self.peer_boots.get(pc)
                        if old is not None and old!=response.get('boot'):
                            self.fault(f'{pc} Windows rebooted',allow_idle=True)
                            self.app.ui_queue.put({'type':'is480_remote_reboot','pc':pc})
                        self.peer_boots[pc]=response.get('boot')
                        self.last_seen[pc]=time.monotonic()
                        cams=response.get('camera_faults') or []
                        if cams:self.fault(f'{pc} disconnected cameras: {cams}',allow_idle=True)
                    except Exception as exc:
                        self.fault(f'{pc} unavailable: {exc}',allow_idle=True)
            c = self.machine.cycle
            if not c or c.state in ('FAULT','DONE','ABORTED'):
                continue
            if time.monotonic() - c.created > self._latency_timeout:
                self.fault(f'Cycle timeout {c.cid}: state={c.state}', cid=c.cid)
                continue
            for pc in ('PC2','PC3'):
                t = self.last_seen.get(pc, 0)
                if t and time.monotonic() - t > 14:
                    self.fault(f'{pc} heartbeat lost', cid=c.cid)
                    break

    def abort(self):
        # Synchronize with an in-flight Serial write; software-only ABORT
        # does not write any byte itself. Prevent pending retransmit races.
        if self.pc_id == 'PC1':
            with self.app.serial_lock:
                self.machine.abort()
                for event in list(self._repeat_stop.values()):event.set()
        else:
            self.machine.abort()
            for event in list(self._repeat_stop.values()):event.set()
        self._pending_local_final = None
        if self.pc_id == 'PC1':
            for pc in ('PC2','PC3'):
                try:
                    self._request(pc,'ABORT')
                except Exception as exc:
                    self.app.ui_queue.put({'type':'is480_warning','message':f'{pc} abort unavailable: {exc}'})
        self.app.ui_queue.put({'type':'is480_abort'})


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
