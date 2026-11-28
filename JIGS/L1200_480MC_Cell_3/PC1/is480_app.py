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


def _add_pc_menu(self):
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


def _attach_header(self):
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
    if self.is480_warning:
        self.is480_warning_bar.config(text='⚠ WARNING / HOLD — '+self.is480_warning)
        self.rowconfigure(0, minsize=34)
        self.is480_warning_bar.grid(row=0, column=0, sticky='ew')
    else:
        self.is480_warning_bar.grid_remove()
        self.rowconfigure(0, minsize=0)

def _sync_run_button(self):
    if getattr(self,'is480_pc_id',None)=='PC1' and hasattr(self,'is480_run_btn'):
        state=self.is480_mode_state or {}
        active=bool(state.get('pm_attempted')) and state.get('mode') != 'RUN'
        self.is480_run_btn.configure(state=('normal' if active else 'disabled'))


# Camera groups mirror real Main PC2/PC3 RUN buttons (CAM24 intentionally absent).
_IS480_VIEW_GROUPS = {
    'PC2': (('CAM12_VT1','CAM13_VT1','CAM14_VT1','CAM15_VT1',
             'CAM16_VT1','CAM17_VT1','CAM18_VT1'),
            ('CAM14_VT2','CAM15_VT2','CAM17_VT2','CAM18_VT2','CAM19_VT2')),
    'PC3': (('CAM21_VT1','CAM22_VT1','CAM23_VT1'),
            ('CAM8_VT2','CAM9_VT2','CAM10_VT2','CAM11_VT2',
             'CAM20_VT2','CAM21_VT2','CAM22_VT2','CAM23_VT2')),
}
_IS480_PM_HIDDEN = {
    'PC2': {'CAM18_VT1','CAM14_VT2','CAM15_VT2','CAM17_VT2'},
    'PC3': {'CAM21_VT1','CAM22_VT1','CAM23_VT2'},
}


def _prepare_cam_slots(self):
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


def _select_pc(self,pc):
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


def _show_global_ng(self):
    if self.is480_pc_id=='PC1':
        self._is480_render_global_ng()


def _toggle_ng(self):
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


def _scroll_body(self,*args):
    c=getattr(self,'is480_body_canvas',None)
    if c:
        c.yview(*args)
        self._is480_draw_body()


def _draw_body(self):
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


def _check_shift(self):
    from is480_statistics import ShiftSchedule, query_shift_bodies
    info=ShiftSchedule.get_shift_info()
    key=(info['date'],info['shift'])
    if self.is480_body_shift_key!=key:
        self.is480_body_shift_key=key
        self.is480_recent_bodies=query_shift_bodies()
        self._is480_draw_body()
    self.after(5000,self._is480_check_shift)


def _normalize_repair_barcode(raw):
    """Scanner rule from L1200_C6: use the first 10 printable characters."""
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='ignore')
    cleaned = ''.join(ch for ch in str(raw or '') if ch.isprintable() and not ch.isspace())
    return cleaned[:10] if len(cleaned) >= 10 else ''


def _repair_notice(self,msg,ok=False):
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


def _repair_barcode(self,event=None,raw=None):
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


def _on_repair_keypress(self,event):
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


def _repair_serial_reader(self):
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


def _repair_db_worker(self):
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


def _repair_done(self,event):
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


def _start_repair_scan(self):
    """Compatibility with old UI; external repair scanner is always ready."""
    if self.is480_pc_id == 'PC1':
        self._is480_repair_notice('Đầu đọc sửa HID/COM riêng sẵn sàng',ok=True)


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
        self.is480_remote_images.clear()  # legacy cache
        self.is480_pc1_last_img = None
        self.is480_remote_ng_paths.clear()
        self.is480_remote_view_verdict.clear()
        self.is480_remote_details_saved.clear()
    self._is480_vt_started={1:time.monotonic()}
    self.is480_ng_point_details=[]
    self.is480_final_cache=None
    self.widget_station_infor.label_5.config(text=barcode)
    self.bat_dau() # thuchien() runs on Tk thread and submits camera work to executor_ai


def _vt2_local(self,cid):
    if not self.is480_bridge.machine.current(cid):return
    self._is480_vt_started[2]=time.monotonic()
    self.executor_ai.submit(self.kiemtra_VT2)


def _store_ng_image(self,event):
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


def _image_event(self,event):
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


def _show_remote_image(self,pc,frame):
    # Keep compatible with callers already providing decoded BGR frames.
    self._is480_show_frame(frame)


def _show_frame(self,frame):
    import cv2
    from PIL import Image,ImageTk
    rgb=cv2.cvtColor(cv2.resize(frame,(1000,528),interpolation=cv2.INTER_AREA),cv2.COLOR_BGR2RGB)
    im=ImageTk.PhotoImage(Image.fromarray(rgb))
    self.widget_result_check.label_image_1.configure(image=im,text='')
    self.widget_result_check.label_image_1.is480img=im
    self.last_display_imgtk=im
    self.last_display_mode='cam'


def _store_view_image(self,msg):
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


def _archive_remote_details(self,msg):
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


def _show_checked_camera(self,pc,label):
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


def _view_image_event(self,event):
    if self.is480_pc_id != 'PC1':return
    pc=event.get('pc'); label=event.get('cam_vt')
    if self.is480_selected_pc == pc:
        self._is480_refresh_cam_button_colors()
    _log(self,'VIEW_RECEIVED',pc+' '+str(label))


def _refresh_cam_button_colors(self):
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


def _render_global_ng(self):
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


def _save_local_ng(self,cid,snapshot):
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


def _request_run(self,bypass=False):
    if self.is480_pc_id!='PC1':return
    if not bypass and not self.is480_mode_state.get('pm_attempted'):
        self._is480_warning('Chưa chạy PM; không thể chuyển RUN')
        return
    c=self.is480_bridge.machine.cycle
    if c and c.state not in ('DONE','ABORTED'):
        self._is480_warning('Phải kết thúc/hủy chu trình trước khi chuyển RUN')
        return
    threading.Thread(target=self._is480_run_worker,args=(bypass,),daemon=True).start()


def _run_worker(self,bypass=False):
    try:
        boots=self.is480_bridge.probe_peers()
        old=self.is480_mode_state.get('peers',{})
        if old and any(old.get(pc)!=boots[pc] for pc in ('PC2','PC3')) and not bypass:
            self.is480_mode_state.update({'pm_attempted':False,'mode':'PM','peers':boots})
            _save_mode(self,self.is480_mode_state)
            raise RuntimeError('Có PC đã reboot Windows: bắt buộc PM lại')
        for pc in ('PC2','PC3'):
            res=self.is480_bridge._request(pc,'SET_MODE_RUN')
            if not res.get('ok'):raise RuntimeError(f'{pc} RUN rejected: {res}')
        self.is480_mode_state.update({'mode':'RUN','pm_attempted':bool(self.is480_mode_state.get('pm_attempted')),'peers':boots,'boot':boot_marker(),
                                      'bypass':bool(bypass)})
        _save_mode(self,self.is480_mode_state)
        self.ui_queue.put({'type':'is480_mode_run'})
        _log(self,'PM_BYPASS_RUN' if bypass else 'MODE_RUN','both clients confirmed')
    except Exception as exc:
        self.ui_queue.put({'type':'is480_warning','message':f'Không thể chuyển RUN đồng bộ: {exc}'})


def _resync_pm(self):
    '''A reboot of ANY machine invalidates PM and RUN for all three PCs.'''
    failures=[]
    for pc in ('PC2','PC3'):
        try:
            result=self.is480_bridge._request(pc,'SET_MODE_PM')
            if not result.get('ok'):
                failures.append(f'{pc}: {result}')
        except Exception as exc:
            failures.append(f'{pc}: {exc}')
    if failures:
        self.ui_queue.put({'type':'is480_warning',
                           'message':'Chưa đồng bộ PM sau reboot: '+ '; '.join(failures)})

def _recover_mode(self):
    try:
        boots=self.is480_bridge.probe_peers()
        saved=self.is480_mode_state.get('peers',{})
        if not saved or any(saved.get(pc)!=boots[pc] for pc in ('PC2','PC3')):
            self.is480_mode_state.update({'mode':'PM','pm_attempted':False,'peers':boots})
            _save_mode(self,self.is480_mode_state)
            for pc in ('PC2','PC3'):
                self.is480_bridge._request(pc,'SET_MODE_PM')
            self.ui_queue.put({'type':'is480_warning','message':'Một PC reboot: phải PM lại'})
            return
        for pc in ('PC2','PC3'):
            res=self.is480_bridge._request(pc,'SET_MODE_RUN')
            if not res.get('ok'):raise RuntimeError(f'{pc}: {res}')
        self.ui_queue.put({'type':'is480_mode_run'})
    except Exception as exc:
        self.ui_queue.put({'type':'is480_warning','message':f'Khôi phục RUN chưa thành công: {exc}'})


def _key5(self,event):
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


def _global_keypress(self,event):
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
                    self._is480_warning('DB COMMIT / WAIT CARD ERROR: '+str(exc))
            elif event['mode']=='PM' and c.mode=='PM':
                self.is480_mode_state['pm_attempted']=True
                self.is480_mode_state['peers']=dict(bridge.peer_boots)
                _save_mode(self,self.is480_mode_state)
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


def _is_ng_confirmation_waiting(self):
    """C6-style WAIT CARD status, not a production result by itself."""
    if self.is480_pc_id != 'PC1':
        return False
    with self.is480_ng_confirm_lock:
        return self.is480_ng_confirm_pending is not None


def _load_ng_confirm_user(self, card_id):
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


def _start_ng_confirmation_wait(self, barcode, check_no, cycle_id, restored=None):
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


def _handle_card_scan(self, card_id):
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


def _confirm_ng_card_worker(self, card_id):
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


def _accept_plc_start(self):
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
        if self.ser is None or not getattr(self.ser,'is_open',False):
            self._is480_fault('PLC 0x10: COM1 unavailable')
            return False
        self.gui_kqdocbarcode = False
        self.ui_queue.put({'type':'is480_reset_barcode'})
        self.is480_body_scan_active.set()
        try:
            with self.serial_lock:
                n = self.ser.write(b'\x30')
                if n is not None and n != 1:
                    raise IOError('Unable to write 0x30')
            threading.Thread(target=self.check_barcode, daemon=True,
                             name='IS480-PLC-BodyBarcode').start()
            return True
        except Exception:
            self.is480_body_scan_active.clear()
            raise


def read_master_serial(self):
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


def _begin_cycle(self,barcode):
    bridge=self.is480_bridge
    try:
        # gate = getattr(self,'is480_rfid_gate_active',None)
        # if gate is not None and gate.is_set():
        #     raise RuntimeError('Đang chờ xác nhận RFID/0x37 hoặc DB chưa chốt; không bắt đầu BODY mới')
        cid=bridge.begin_master(barcode,'PM' if self.PM else 'RUN')
        if not self.PM:
            from is480_statistics import create_inspection_check
            check_no=create_inspection_check(barcode)
        else:check_no=0
        bridge.machine.cycle.check_no=check_no
        self.current_barcode=barcode
        self.current_check_no=check_no
        self.ui_queue.put({'type':'is480_start','cycle_id':cid,'barcode':barcode})
    except Exception as exc:
        self._is480_fault('START_CYCLE: '+str(exc))


def _begin_vt2(self):
    try:
        cid=self.is480_bridge.start_vt2()
        self.ui_queue.put({'type':'is480_vt2','cycle_id':cid})
    except Exception as exc:
        self._is480_fault('CAPTURE_VT2: '+str(exc))


def install_controller(cls,pc):
    cls.is480_pc_id=pc
    functions={
      '_is480_setup':setup,'_is480_refresh_screen':_refresh_screen,'_is480_attach_header':_attach_header,
      '_is480_add_pc_menu':_add_pc_menu,'_is480_sync_run_button':_sync_run_button,
      '_is480_select_pc':_select_pc,'_is480_prepare_cam_slots':_prepare_cam_slots,
      '_is480_refresh_cam_button_colors':_refresh_cam_button_colors,
      '_is480_show_checked_camera':_show_checked_camera,
      '_is480_show_global_ng':_show_global_ng,'_is480_render_global_ng':_render_global_ng,
      '_is480_show_frame':_show_frame,'_is480_view_image_event':_view_image_event,
      '_is480_store_view_image':_store_view_image,
      '_is480_archive_remote_details':_archive_remote_details,
      '_is480_toggle_ng':_toggle_ng,
      '_is480_scroll_body':_scroll_body,'_is480_draw_body':_draw_body,'_is480_check_shift':_check_shift,
      '_is480_repair_barcode':_repair_barcode,'_is480_repair_notice':_repair_notice,
      '_is480_on_repair_keypress':_on_repair_keypress,
      '_is480_repair_serial_reader':_repair_serial_reader,
      '_is480_repair_db_worker':_repair_db_worker,
      '_is480_repair_done':_repair_done,
      '_is480_start_repair_scan':_start_repair_scan,'_is480_warning':_warning,'_is480_fault':fault,
      '_is480_current_thread_cycle':_current_thread_cycle,
      '_is480_capture_checked':_capture_checked, '_is480_process_checked':_process_checked,
      '_is480_point_checked':_point_checked,
      '_is480_stage_capture_valid':_stage_capture_valid,'_is480_start_local':_start_local,
      '_is480_vt2_local':_vt2_local,'_is480_store_ng_image':_store_ng_image,'_is480_image_event':_image_event,
      '_is480_show_remote_image':_show_remote_image,'_is480_send_ng_images':_send_ng_images,
      '_is480_capture_fresh':_capture_fresh,'_is480_capture_raw':_capture_raw,'_is480_ng_point':_ng_point,'_is480_save_local_ng':_save_local_ng,
      '_is480_finish':_finish_cycle,'_is480_reset_ui':_reset_ui,
      '_is480_request_run':_request_run,'_is480_run_worker':_run_worker,
      '_is480_recover_mode':_recover_mode,'_is480_resync_pm':_resync_pm,'_is480_key5':_key5,'_is480_global_keypress':_global_keypress,'_is480_ready_event':_ready_event,
      '_is480_camera_health':_camera_health,
      '_is480_ui_event':_ui_event,'_is480_begin_cycle':_begin_cycle,'_is480_begin_vt2':_begin_vt2,
      '_is480_accept_plc_start':_accept_plc_start,
      '_is480_load_ng_confirm_user':_load_ng_confirm_user,
      '_is480_is_ng_confirmation_waiting':_is_ng_confirmation_waiting,
      '_is480_start_ng_confirmation_wait':_start_ng_confirmation_wait,
      '_is480_handle_card_scan':_handle_card_scan,
      '_is480_confirm_ng_card_worker':_confirm_ng_card_worker}
    functions['_is480_send_view_images'] = _is480_send_view_images
    for name,fun in functions.items():setattr(cls,name,fun)
    # Wrap original AI processing without touching camera/model/ORB implementations.
    if not getattr(cls,'_is480_view_wrapped',False):
        original=cls.process_image
        def process_with_view(self,frame,cam,vt,toado,*args):
            return _is480_capture_checked_view(self,original,frame,cam,vt,toado,*args)
        cls.process_image=process_with_view
        cls._is480_view_wrapped=True
    cls.build_ng_grid = staticmethod(_is480_square_ng_grid)
    if pc=='PC1':cls.read_serial=read_master_serial
    return cls
