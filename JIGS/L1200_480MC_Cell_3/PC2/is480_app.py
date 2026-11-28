# -*- coding: utf-8 -*-
"""Integration of the shared IS480 coordinator with the three original Tk apps.

This module deliberately does NOT import camera/AI implementations. It retains
camera ID, ROI, ORB, model and PM topology from each existing main program.
"""
import base64
import json
import os
import queue
import re
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk
import traceback
from pathlib import Path
from datetime import datetime

from is480_runtime import Bridge, boot_marker, load_site_config, send_json


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


def setup(self):
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
    self.is480_warning = None
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
        # Scanner production is disabled until PLC requests barcode 0x10.
        scanner = getattr(self, 'initProcess', None)
        if scanner is not None and not scanner.send('scan_disable'):
            _log(self, 'SCANNER_IDLE', 'Chưa gửi được scan_disable COM3; kiểm tra kết nối')
        # mode recovery checks all three Windows boots before authorizing RUN
        if self.is480_mode_state.get('mode') == 'RUN':
            threading.Thread(target=self._is480_recover_mode, daemon=True).start()
    else:
        self.after(750, self._is480_ready_event)


def _refresh_screen(self):
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


def _warning(self,msg):
    self.is480_warning=str(msg)
    _log(self,'WARNING',self.is480_warning)
    if getattr(self,'is480_pc_id',None)=='PC1':
        if hasattr(self,'is480_warning_bar') and self.is480_warning_bar.winfo_exists():
            self.is480_warning_bar.config(text='⚠ WARNING / HOLD — '+self.is480_warning)
            self.rowconfigure(0, minsize=34)
            self.is480_warning_bar.grid(row=0, column=0, sticky='ew')
    else:
        # worker displays local warning on result window
        self.widget_result_check.label2.config(text='FAULT',bg='#F7C948')


def fault(self,reason):
    bridge=getattr(self,'is480_bridge',None)
    if bridge:bridge.fault(reason)
    else:self.ui_queue.put({'type':'is480_warning','message':reason})


def _current_thread_cycle(self):
    cid=getattr(self._is480_tls,'cid',None)
    return bool(cid and self.is480_bridge.machine.current(cid))


def _capture_checked(self,cid,*args):
    if not self.is480_bridge.machine.current(cid):return
    self._is480_tls.cid=cid
    try:return self.capture_and_process(*args)
    finally:self._is480_tls.cid=None


def _process_checked(self,cid,*args):
    if not self.is480_bridge.machine.current(cid):return
    self._is480_tls.cid=cid
    try:return self.process_image(*args)
    finally:self._is480_tls.cid=None


def _point_checked(self,cid,*args):
    if not self.is480_bridge.machine.current(cid):return None
    self._is480_tls.cid=cid
    try:return self.process_single_point(*args)
    finally:self._is480_tls.cid=None


def _stage_capture_valid(self,vt,expected):
    c=self.is480_bridge.machine.cycle
    if not c or not self.is480_bridge.machine.current(c.cid):
        return False
    actual=self.queue_put_count_vt1 if vt==1 else self.queue_put_count_vt2
    if actual != expected:
        self._is480_fault(f'CAMERA_CAPTURE_INCOMPLETE: {self.is480_pc_id} VT{vt}: {actual}/{expected} camera')
        return False
    return True


def _start_local(self,cid,barcode):
    if not self.is480_bridge.machine.current(cid):return
    self.databacode=barcode
    self.current_barcode=barcode
    self.gui_kqdocbarcode=False
    self.is480_raw_jpegs={}
    with self.is480_view_lock:
        self.is480_checked_jpegs.clear()
        if self.is480_pc_id=='PC1':self.is480_remote_view_jpegs.clear()
    if self.is480_pc_id=='PC1':
        self.is480_remote_images.clear()  # never display NG frames from an earlier BODY
    self._is480_vt_started={1:time.monotonic()}
    self.is480_ng_point_details=[]
    self.is480_final_cache=None
    self.widget_station_infor.label_5.config(text=barcode)
    self.bat_dau() # thuchien() runs on Tk thread and submits camera work to executor_ai


def _vt2_local(self,cid):
    if not self.is480_bridge.machine.current(cid):return
    self._is480_vt_started[2]=time.monotonic()
    self.executor_ai.submit(self.kiemtra_VT2)


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


def _is480_capture_checked_view(self, original, frame, cam, vt, toado, *args):
    """Mirror Main's fully annotated frame after real inference returns.

    Main.so_nut_an stores a reference to processed_frame for every point with
    NUTSO, so after process_image returns the same frame has all ROI overlays.
    Only a short-lived JPEG preview is retained (never inserted into production DB).
    """
    result = original(self, frame, cam, vt, toado, *args)
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
                                     is_ng=(label in getattr(self,'ng_cam_keys_pm',set())),
                                     image=base64.b64encode(blob).decode('ascii'))
    _log(self,'VIEW_SENT',f'{self.is480_pc_id} {len(items)} ảnh xem lại, cycle={cid}')

def _capture_fresh(self,cam,vt):
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


def _capture_raw(self,cam,vt,frame):
    """Keep compressed exact checked frame, not another frame grabbed later."""
    import cv2
    h,w=frame.shape[:2]
    ratio=min(1.0,1000/max(w,1),620/max(h,1))
    small=cv2.resize(frame,(int(w*ratio),int(h*ratio))) if ratio<1 else frame
    success, encoded=cv2.imencode('.jpg',small,[cv2.IMWRITE_JPEG_QUALITY,72])
    if success:self.is480_raw_jpegs[f'CAM{cam}_VT{vt}']=encoded.tobytes()


def _ng_point(self,cam,vt,point):
    detail={'cam':int(cam),'vitri':int(vt),'point':int(point['POINT']),
            'name':str(point.get('NAME','')),'tag':str(point.get('NAME','')),
            'level':int(point.get('LEVEL',0) or 0),
            'x':int(point.get('X',0) or 0),'y':int(point.get('Y',0) or 0),
            'size':int(point.get('SIZE',0) or 0)}
    self.is480_ng_point_details.append(detail)


def _send_ng_images(self,cid,snapshot):
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


def _finish_cycle(self):
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


def _reset_ui(self):
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


def _camera_health(self):
    if self.is480_pc_id!='PC1':return
    now=time.monotonic()
    bridge=self.is480_bridge
    for index in range(8):
        bad=getattr(self,f'cap{index}',None) is None
        if bad:
            since=self.is480_camera_bad_since.setdefault(index,now)
            if now-since>8:
                if bridge.machine.cycle and bridge.machine.cycle.state not in ('DONE','ABORTED'):
                    bridge.fault(f'PC1 CAM{index} disconnected')
                elif not self.is480_warning:
                    self._is480_warning(f'PC1 CAM{index} disconnected')
        else:self.is480_camera_bad_since.pop(index,None)
    self.after(2500,self._is480_camera_health)


def _ready_event(self):
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


def _ui_event(self,event):
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
    elif typ=='is480_remote_reboot':
        if self.is480_pc_id=='PC1':
            self.is480_mode_state.update({'mode':'PM','pm_attempted':False})
            _save_mode(self,self.is480_mode_state)
            self.show_pm()
            self._is480_sync_run_button()
            threading.Thread(target=self._is480_resync_pm,daemon=True).start()
    elif typ=='is480_warning':self._is480_warning(event.get('message','unknown fault'))
    elif typ=='is480_abort':self._is480_reset_ui()
    elif typ=='is480_check_final_ready':
        bridge.flush_pending_final()
        bridge._maybe_send_final(event['cycle_id'])
    return True


def install_controller(cls,pc):
    cls.is480_pc_id=pc
    functions={
        '_is480_setup':setup,
        '_is480_refresh_screen':_refresh_screen,
        '_is480_warning':_warning,
        '_is480_fault':fault,
        '_is480_current_thread_cycle':_current_thread_cycle,
        '_is480_capture_checked':_capture_checked,
        '_is480_process_checked':_process_checked,
        '_is480_point_checked':_point_checked,
        '_is480_stage_capture_valid':_stage_capture_valid,
        '_is480_start_local':_start_local,
        '_is480_vt2_local':_vt2_local,
        '_is480_capture_fresh':_capture_fresh,
        '_is480_capture_raw':_capture_raw,
        '_is480_ng_point':_ng_point,
        '_is480_send_ng_images':_send_ng_images,
        '_is480_send_view_images':_is480_send_view_images,
        '_is480_finish':_finish_cycle,
        '_is480_reset_ui':_reset_ui,
        '_is480_ready_event':_ready_event,
        '_is480_camera_health':_camera_health,
        '_is480_ui_event':_ui_event,
    }
    for name,fun in functions.items():setattr(cls,name,fun)
    # Existing Main builds may call shared UI hooks regardless of PC role.
    # The worker supplies harmless stubs instead of importing PC1-only modules.
    def _worker_unused_hook(self,*args,**kwargs):return None
    for name in ('_is480_add_pc_menu','_is480_attach_header',
                 '_is480_sync_run_button','_is480_select_pc',
                 '_is480_start_repair_scan','_is480_request_run'):
        if not hasattr(cls,name):setattr(cls,name,_worker_unused_hook)
    # Wrap original AI processing without touching camera/model/ORB implementations.
    if not getattr(cls,'_is480_view_wrapped',False):
        original=cls.process_image
        def process_with_view(self,frame,cam,vt,toado,*args):
            return _is480_capture_checked_view(self,original,frame,cam,vt,toado,*args)
        cls.process_image=process_with_view
        cls._is480_view_wrapped=True
    cls.build_ng_grid = staticmethod(_is480_square_ng_grid)
    return cls
