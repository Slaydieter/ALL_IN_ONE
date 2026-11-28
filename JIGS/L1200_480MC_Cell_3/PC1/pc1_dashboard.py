# -*- coding: utf-8 -*-
"""
pc3_dashboard.py — Chạy trên PC1 (máy chính có màn hình)

BẢN LIGHTWEIGHT:
  1. Trạng thái 3 mức cho PC1 và PC2:
       Đang tắt  /  Đang khởi động  /  Khởi động hoàn thành
  2. Nút "Tìm & Kết nối" — tự dò cổng Ethernet, tìm IP của PC1 và PC2
  3. Mở / Dừng / Tắt máy qua Agent tối giản, không tail log ứng dụng

ĐIỀU KIỆN CẦN:
  * PC2 và PC3 đã cài agent.py + auto-login + Task Scheduler (xem hướng dẫn agent.py)
  * Cả 3 PC nối chung một switch qua cổng Ethernet

NGUYÊN LÝ 3 TRẠNG THÁI — đây là phần dễ hiểu sai nhất:
  Ping/ARP thông KHÔNG chứng minh máy đã đăng nhập xong — máy đứng ở màn
  hình login vẫn có mạng bình thường. Chỉ AGENT trả lời được mới chứng
  minh: agent chỉ khởi động SAU KHI auto-login xong (Task Scheduler chạy
  agent lúc user đăng nhập). Nên:
      không phản hồi gì cả                 -> Đang tắt
      có phản hồi mạng, agent chưa trả lời -> Đang khởi động
      agent trả lời                        -> Khởi động hoàn thành

Mọi thao tác mạng chạy ở luồng nền; giao diện chỉ cập nhật ở luồng chính
qua Queue + root.after(), theo đúng nguyên tắc đã rút ra từ trước.
"""

import json
import os
import queue
import re
import socket
import struct
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "pc1_dashboard_config.json")

IS_WIN = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0
LEN_FMT, LEN_SIZE = "!I", 4
RE_IP = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
RE_MAC = re.compile(r"\b((?:[0-9a-fA-F]{2}[-:]){5}[0-9a-fA-F]{2})\b")

STATE_UNRESOLVED = "unresolved"   # chưa biết IP
STATE_OFF = "off"
STATE_BOOTING = "booting"
STATE_COMPLETE = "complete"
STATE_AGENT_OFFLINE = "agent_offline"   # máy có mạng nhưng Agent không trả lời

STATE_LABEL = {
    STATE_UNRESOLVED: "Chưa kết nối",
    STATE_OFF: "Đang tắt",
    STATE_BOOTING: "Đang khởi động",
    STATE_COMPLETE: "Khởi động hoàn thành",
    STATE_AGENT_OFFLINE: "Máy đã bật — Agent mất kết nối",
}
STATE_COLOR = {
    STATE_UNRESOLVED: "#9e9e9e",
    STATE_OFF: "#d32f2f",
    STATE_BOOTING: "#f9a825",
    STATE_COMPLETE: "#2e7d32",
    STATE_AGENT_OFFLINE: "#ef6c00",
}

DEFAULT_CFG = {
    "token": "IS1200_480_CHANGE_ME",
    "agent_port": 6100,
    "poll_seconds": 4.0,
    "boot_grace_seconds": 120.0,  # có mạng nhưng chưa có Agent: coi là đang boot tối đa 120s
    "event_port": 6200,            # nhận READY / ERROR rất nhẹ từ 3 chương trình
    "ready_timeout_seconds": 12.0, # quá thời gian này không có heartbeat -> chưa READY
    "subnet": None,          # tự dò lần đầu, sau đó nhớ lại
    "targets": [
        {"pc_id": "PC2", "match": "PC2", "app": "inspection",
         "program_label": "Mở chương trình PC2", "host": "10.255.128.197"},
        {"pc_id": "PC3", "match": "PC3", "app": "inspection",
         "program_label": "Mở chương trình PC3", "host": "10.255.129.53"},
    ],
}


# ════════════════════════════════════════════════ khung tin nhắn agent

def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        c = sock.recv(min(65536, n - len(buf)))
        if not c:
            raise ConnectionError("đóng kết nối")
        buf.extend(c)
    return bytes(buf)


def probe_agent(host, port, token, timeout=1.2):
    """Gửi lệnh 'status'. Trả về dict nếu agent trả lời, None nếu không."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            b = json.dumps({"cmd": "status", "token": token}).encode("utf-8")
            s.sendall(struct.pack(LEN_FMT, len(b)) + b)
            n = struct.unpack(LEN_FMT, recv_exact(s, LEN_SIZE))[0]
            return json.loads(recv_exact(s, n).decode("utf-8"))
    except Exception:
        return None


def send_agent_cmd(host, port, token, cmd, app, timeout=6.0):
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            b = json.dumps({"cmd": cmd, "token": token, "app": app}).encode("utf-8")
            s.sendall(struct.pack(LEN_FMT, len(b)) + b)
            n = struct.unpack(LEN_FMT, recv_exact(s, LEN_SIZE))[0]
            return json.loads(recv_exact(s, n).decode("utf-8"))
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def probe_network(host, timeout=0.6, ports=(445, 139, 135)):
    """
    Máy có tồn tại trên mạng hay không — KHÔNG cần agent trả lời.
    Bị từ chối kết nối (RST) vẫn tính là 'có tồn tại', vì máy phải sống
    thì mới có gì đó để từ chối kết nối.
    """
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((host, p))
            return True
        except ConnectionRefusedError:
            return True
        except OSError:
            continue
        finally:
            try:
                s.close()
            except OSError:
                pass
    return False


def classify(host, agent_result, network_up):
    if not host:
        return STATE_UNRESOLVED
    if agent_result is not None and agent_result.get("ok"):
        return STATE_COMPLETE
    if network_up:
        return STATE_BOOTING
    return STATE_OFF


# ════════════════════════════════════════════════ dò mạng (rút gọn từ discover.py)

def run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              errors="ignore", creationflags=CREATE_NO_WINDOW).stdout or ""
    except Exception:
        return ""


def list_adapters():
    res = []
    if IS_WIN:
        name = ""
        for raw in run(["ipconfig"]).splitlines():
            if raw.strip() and not raw[0].isspace():
                name = raw.strip().rstrip(":")
                continue
            if "IPv4" in raw:
                m = RE_IP.search(raw)
                if m:
                    res.append([name, m.group(1), None])
            elif "ask" in raw and res and res[-1][2] is None:
                m = RE_IP.search(raw)
                if m:
                    res[-1][2] = m.group(1)
    else:
        for line in run(["ip", "-4", "-o", "addr"]).splitlines():
            p = line.split()
            if len(p) >= 4 and p[2] == "inet":
                res.append([p[1], p[3].split("/")[0], None])
    return [tuple(r) for r in res if not r[1].startswith("127.")]


def default_gateways():
    gws = set()
    if IS_WIN:
        for raw in run(["ipconfig"]).splitlines():
            if "ateway" in raw:
                m = RE_IP.search(raw)
                if m and m.group(1) != "0.0.0.0":
                    gws.add(m.group(1))
    else:
        for line in run(["ip", "route"]).splitlines():
            if line.startswith("default via"):
                m = RE_IP.search(line)
                if m:
                    gws.add(m.group(1))
    return gws


def ping_once(ip, timeout_ms=500):
    cmd = (["ping", "-n", "1", "-w", str(timeout_ms), ip] if IS_WIN
           else ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip])
    return "TTL=" in run(cmd, timeout=6).upper()


def sweep(subnet, workers=48):
    import concurrent.futures as cf
    ips = ["%s.%d" % (subnet, i) for i in range(1, 255)]
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(ping_once, ips))


def arp_table(subnet):
    out = run(["arp", "-a"])
    found = {}
    for line in out.splitlines():
        mi, ii = RE_MAC.search(line), RE_IP.search(line)
        if not (mi and ii):
            continue
        ip, mac = ii.group(1), mi.group(1).upper().replace(":", "-")
        if not ip.startswith(subnet + "."):
            continue
        if mac == "FF-FF-FF-FF-FF-FF" or ip.endswith(".255"):
            continue
        found[ip] = mac
    return found


def netbios_name(ip):
    if IS_WIN:
        out = run(["nbtstat", "-A", ip], timeout=8)
        for line in out.splitlines():
            m = re.match(r"\s*(\S+)\s+<00>\s+UNIQUE", line)
            if m:
                return m.group(1)
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return ""


def scan_and_match(subnet, targets, exclude):
    """
    Quét mạng, khớp host tìm được với từng target theo tên máy.
    Trả về (assigned: {pc_id: (ip, mac, name)}, unmatched: [target,...],
            remaining: {ip: (mac, name)})
    """
    sweep(subnet)
    hosts = arp_table(subnet)
    gws = default_gateways()
    cand = {ip: mac for ip, mac in hosts.items()
            if ip not in exclude and ip not in gws}
    names = {ip: netbios_name(ip) for ip in cand}

    assigned = {}
    unmatched = []
    for t in targets:
        found_ip = None
        for ip in cand:
            if ip in [v[0] for v in assigned.values()]:
                continue
            if t["match"].lower() in (names.get(ip) or "").lower():
                found_ip = ip
                break
        if found_ip:
            assigned[t["pc_id"]] = (found_ip, cand[found_ip], names.get(found_ip, ""))
        else:
            unmatched.append(t)

    used_ips = {v[0] for v in assigned.values()}
    remaining = {ip: (mac, names.get(ip, "")) for ip, mac in cand.items()
                if ip not in used_ips}
    return assigned, unmatched, remaining


# ════════════════════════════════════════════════ hộp chọn thủ công

class PickerDialog(tk.Toplevel):
    """Khi không tự khớp được, để người dùng tự chọn host cho từng target."""

    def __init__(self, master, target, remaining, on_pick):
        super().__init__(master)
        self.title("Chọn máy cho %s" % target["pc_id"])
        self.geometry("420x320")
        self.on_pick = on_pick
        self.target = target
        self.remaining = remaining

        ttk.Label(self, text="Không tự nhận diện được %s.\nChọn đúng máy trong danh sách:"
                  % target["pc_id"], justify="left").pack(padx=12, pady=10, anchor="w")

        self.lb = tk.Listbox(self, font=("Consolas", 10))
        self.lb.pack(fill="both", expand=True, padx=12)
        for ip, (mac, name) in remaining.items():
            self.lb.insert("end", "%-16s %-20s %s" % (ip, name or "?", mac))

        row = ttk.Frame(self)
        row.pack(fill="x", padx=12, pady=10)
        ttk.Button(row, text="Bỏ qua", command=self.destroy).pack(side="right")
        ttk.Button(row, text="Chọn máy này", command=self._pick).pack(side="right", padx=6)

    def _pick(self):
        sel = self.lb.curselection()
        if not sel:
            return
        ip = list(self.remaining.keys())[sel[0]]
        self.on_pick(self.target, ip)
        self.destroy()


class AdapterDialog(tk.Toplevel):
    """Máy có nhiều card mạng — chọn đúng card nối tới PC1/PC2."""

    def __init__(self, master, adapters, on_pick):
        super().__init__(master)
        self.title("Chọn card mạng")
        self.geometry("480x260")
        self.on_pick = on_pick
        self.adapters = adapters
        ttk.Label(self, text="Máy có nhiều card mạng.\n"
                  "Chọn card NỐI VỚI PC1/PC2 (thường là Ethernet, không phải Wi-Fi):",
                  justify="left").pack(padx=12, pady=10, anchor="w")
        self.lb = tk.Listbox(self, font=("Consolas", 10))
        self.lb.pack(fill="both", expand=True, padx=12)
        for name, ip, mask in adapters:
            self.lb.insert("end", "%-30s %-15s %s" % (name[:30], ip, mask or ""))
        self.lb.selection_set(0)
        ttk.Button(self, text="Chọn", command=self._pick).pack(pady=10)

    def _pick(self):
        sel = self.lb.curselection()
        idx = sel[0] if sel else 0
        self.on_pick(self.adapters[idx])
        self.destroy()


# ════════════════════════════════════════════════ 1 khối trạng thái PC

class TargetPanel(ttk.LabelFrame):
    def __init__(self, master, target, on_open, on_stop, on_shutdown, on_abort):
        super().__init__(master, text=" %s " % target["pc_id"])
        self.target = target
        self.on_open = on_open
        self.on_stop = on_stop
        self.on_shutdown = on_shutdown
        self.on_abort = on_abort

        row = ttk.Frame(self)
        row.pack(fill="x", padx=10, pady=(10, 4))
        self.dot = tk.Canvas(row, width=16, height=16, highlightthickness=0)
        self.dot.pack(side="left")
        self._oval = self.dot.create_oval(
            3, 3, 14, 14, fill=STATE_COLOR[STATE_UNRESOLVED], outline="")
        self.lbl_state = ttk.Label(
            row, text=STATE_LABEL[STATE_UNRESOLVED], font=("Segoe UI", 12, "bold"))
        self.lbl_state.pack(side="left", padx=8)

        self.lbl_ip = ttk.Label(
            self, text='Chưa có IP — bấm "Tìm & Kết nối"', foreground="#666")
        self.lbl_ip.pack(anchor="w", padx=10)

        self.lbl_app = ttk.Label(self, text="", foreground="#666")
        self.lbl_app.pack(anchor="w", padx=10, pady=(2, 4))

        # 3 nút cùng một hàng.
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=10, pady=(8, 10))
        for c in range(3):
            buttons.columnconfigure(c, weight=1)

        self.btn_open = ttk.Button(
            buttons, text="▶ Mở chương trình",
            command=lambda: self.on_open(self.target))
        self.btn_open.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        self.btn_stop = ttk.Button(
            buttons, text="■ Dừng chương trình",
            command=lambda: self.on_stop(self.target))
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=4)

        self.btn_shutdown = ttk.Button(
            buttons, text="⏻ Tắt máy",
            command=lambda: self.on_shutdown(self.target))
        self.btn_shutdown.grid(row=0, column=2, sticky="ew", padx=(4, 0))

        # Separate from STOP PROGRAM: does not stop Python and does not send PLC signals.
        self.btn_abort = ttk.Button(self, text="⚠ DỪNG CHECK (giữ chương trình chạy)",
                                    command=lambda: self.on_abort(self.target))
        self.btn_abort.pack(fill="x", padx=10, pady=(0, 10))
        self.btn_abort.state(["disabled"])

        self.btn_open.state(["disabled"])
        self.btn_stop.state(["disabled"])
        self.btn_shutdown.state(["disabled"])

    def set_host(self, host, name=""):
        self.target["host"] = host
        self.lbl_ip.config(text=("%s  (%s)" % (host, name)) if name else host)

    def render(self, state, agent_result, app_ready=False):
        self.dot.itemconfig(self._oval, fill=STATE_COLOR[state])
        self.lbl_state.config(text=STATE_LABEL[state])

        self.btn_open.state(["disabled"])
        self.btn_stop.state(["disabled"])
        self.btn_shutdown.state(["disabled"])
        self.btn_abort.state(["disabled"])

        if state == STATE_COMPLETE:
            self.btn_shutdown.state(["!disabled"])
            apps = (agent_result or {}).get("apps") or []
            want = self.target.get("app", "inspection")
            app = next((a for a in apps if a.get("name") == want),
                       apps[0] if apps else None)
            running = bool(app and app.get("running"))

            if running:
                self.btn_abort.state(["!disabled"])
                self.btn_stop.state(["!disabled"])
                up = app.get("uptime", 0)
                if app_ready:
                    self.lbl_app.config(
                        text="✅ SẴN SÀNG — PID %s — %dh%02dm" %
                             (app.get("pid"), up // 3600, (up % 3600) // 60),
                        foreground="#2e7d32")
                else:
                    self.lbl_app.config(
                        text="⏳ Đang khởi động / tải model... — PID %s" % app.get("pid"),
                        foreground="#ef6c00")
            else:
                if app_ready:
                    self.btn_abort.state(["!disabled"])
                    # UDP READY is independent of Agent/PID discovery.
                    # Do not offer Start if an app is already sending a live heartbeat.
                    self.lbl_app.config(
                        text="✅ SẴN SÀNG — tín hiệu trực tiếp (Agent chưa đồng bộ PID)",
                        foreground="#2e7d32")
                else:
                    self.btn_open.state(["!disabled"])
                    self.lbl_app.config(text="Chương trình chưa chạy", foreground="#666")
        else:
            if app_ready:
                self.lbl_app.config(
                    text="✅ SẴN SÀNG — tín hiệu trực tiếp từ chương trình",
                    foreground="#2e7d32")
                return
            if state == STATE_BOOTING:
                msg = "Windows có mạng — đang chờ Agent khởi động..."
            elif state == STATE_AGENT_OFFLINE:
                msg = "Windows đang bật nhưng Agent không phản hồi — không thể gửi lệnh"
            elif state == STATE_OFF:
                msg = "Máy không phản hồi mạng"
            else:
                msg = ""
            self.lbl_app.config(text=msg, foreground="#666")


# ════════════════════════════════════════════════ cửa sổ chính

class Dashboard(tk.Tk):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.title("IS1200_480 — PC1 MASTER: điều khiển PC2 / PC3")
        self.geometry("710x730")
        self.minsize(600, 520)

        self.q = queue.Queue()
        self.panels = {}
        self._scanning = False

        # State machine mạng/Agent. Dùng để phân biệt "đang boot" với
        # "Windows đã lên nhưng Agent đã chết/mất kết nối".
        self._state_lock = threading.Lock()
        self._machine_state = {}      # pc_id -> state gần nhất đã xác định
        self._booting_since = {}      # pc_id -> thời điểm đầu tiên thấy mạng nhưng chưa thấy Agent

        # Mỗi vòng poll có sequence riêng. Kết quả poll cũ về trễ sẽ bị bỏ qua,
        # tránh trạng thái mới bị ghi đè bởi dữ liệu cũ.
        self._poll_seq = {}
        self._applied_poll_seq = {}
        for _t in cfg["targets"]:
            _pc = _t["pc_id"]
            self._machine_state[_pc] = STATE_UNRESOLVED
            self._poll_seq[_pc] = 0
            self._applied_poll_seq[_pc] = 0

        # READY heartbeat từ 3 chương trình chính (PC1 / PC2 / PC3).
        # Không dùng log; mỗi app chỉ gửi 1 gói UDP nhỏ mỗi 5 giây.
        self._app_ready_last_seen = {"PC1": 0.0, "PC2": 0.0, "PC3": 0.0}
        self._ready_flags = {"PC1": False, "PC2": False, "PC3": False}
        self._seen_event_ids = {}
        self._last_panel_render = {}
        self._all_ready_notified = False

        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=(12, 0))
        ttk.Label(top, text="Bảng điều khiển", font=("Segoe UI", 14, "bold")).pack(side="left")
        self.btn_scan = ttk.Button(top, text="Tìm & Kết nối", command=self.on_discover)
        self.btn_scan.pack(side="right")
        ttk.Button(top, text="Làm mới", command=self.poll_now).pack(side="right", padx=6)

        self.ready_banner = tk.Label(
            self,
            text="⏳ ĐANG CHỜ PC1 / PC2 / PC3 SẴN SÀNG",
            font=("Segoe UI", 12, "bold"),
            fg="black",
            bg="#f9a825",
            padx=10,
            pady=8
        )
        self.ready_banner.pack(fill="x", padx=12, pady=(8, 0))

        for t in cfg["targets"]:
            p = TargetPanel(self, t, self.on_open_program, self.on_stop_program, self.on_shutdown_machine, self.on_abort_check)
            p.pack(fill="x", padx=12, pady=8)
            if t.get("host"):
                p.set_host(t["host"])
            self.panels[t["pc_id"]] = p

        # ── Nhật ký: 1 tab "Hệ thống" (dò mạng, chọn card...) + 1 tab riêng
        #    cho mỗi PC (sự kiện bấm nút + nội dung inspection.log trực tiếp) ──
        ttk.Label(self, text="Nhật ký").pack(anchor="w", padx=12)
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(2, 12))

        self.log_widgets = {}   # pc_id hoặc "system" -> tk.Text
        self._add_log_tab("system", "Hệ thống")
        for t in cfg["targets"]:
            self._add_log_tab(t["pc_id"], t["pc_id"])

        self.protocol("WM_DELETE_WINDOW", self.destroy)

        # Listener riêng cho READY / ERROR. Không tail log, không ảnh hưởng inference.
        threading.Thread(target=self._app_event_listener, daemon=True).start()

        self.after(100, self.drain_queue)
        self.after(300, self.poll_now)
        self.after(1000, self._refresh_ready_status)
        self.after(int(cfg.get("poll_seconds", 4.0) * 1000), self.poll_loop)

    # ---------- READY / ERROR event channel ----------

    def _app_event_listener(self):
        port = int(self.cfg.get("event_port", 6200))
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            while True:
                raw, addr = sock.recvfrom(8192)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    if isinstance(payload, dict) and payload.get('token') == self.cfg['token']:
                        self.q.put(("app_event", payload, addr))
                except Exception:
                    pass
        except Exception as e:
            self.q.put(("event_listener_error", str(e)))
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _is_app_ready(self, pc_id):
        timeout = float(self.cfg.get("ready_timeout_seconds", 12.0))
        last = float(self._app_ready_last_seen.get(pc_id, 0.0) or 0.0)
        return last > 0 and (time.time() - last) <= timeout

    def _refresh_ready_status(self):
        # Tính READY dựa vào heartbeat mới nhất.
        ready = {pc: self._is_app_ready(pc) for pc in ("PC1", "PC2", "PC3")}
        self._ready_flags.update(ready)

        # Vẽ lại PC1/PC2 để phân biệt "process đã chạy" và "app thực sự READY".
        for pc_id in ("PC2", "PC3"):
            if pc_id in self._last_panel_render:
                state, res = self._last_panel_render[pc_id]
                self.panels[pc_id].render(state, res, ready.get(pc_id, False))

        ready_names = [pc for pc in ("PC1", "PC2", "PC3") if ready[pc]]
        wait_names = [pc for pc in ("PC1", "PC2", "PC3") if not ready[pc]]

        if len(ready_names) == 3:
            self.ready_banner.config(
                text="✅ CẢ 3 CHƯƠNG TRÌNH PC1 + PC2 + PC3 ĐÃ SẴN SÀNG HOẠT ĐỘNG",
                bg="#2e7d32",
                fg="white"
            )
            if not self._all_ready_notified:
                self._all_ready_notified = True
                self.write_log("✅ Cả 3 chương trình PC1 / PC2 / PC3 đã READY.")
                messagebox.showinfo(
                    "HỆ THỐNG SẴN SÀNG",
                    "PC1, PC2 và PC3 đã load xong toàn bộ model/camera/toạ độ và UI đã sẵn sàng."
                )
        else:
            text = "⏳ READY: %s    |    ĐANG CHỜ: %s" % (
                ", ".join(ready_names) if ready_names else "chưa có",
                ", ".join(wait_names) if wait_names else "không"
            )
            self.ready_banner.config(text=text, bg="#f9a825", fg="black")
            self._all_ready_notified = False

        # Dọn event id cũ để dictionary không tăng mãi.
        cutoff = time.time() - 60.0
        self._seen_event_ids = {
            k: v for k, v in self._seen_event_ids.items() if v >= cutoff
        }

        self.after(1000, self._refresh_ready_status)

    # ---------- nhật ký ----------
    def _add_log_tab(self, key, title):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=title)
        txt = tk.Text(frame, wrap="word", state="disabled", font=("Consolas", 9))
        sb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log_widgets[key] = txt

    def _append_log(self, key, text, max_lines=800):
        w = self.log_widgets.get(key)
        if w is None:
            return
        w.config(state="normal")
        w.insert("end", text)
        # Giới hạn số dòng để không phình bộ nhớ khi chạy nhiều ngày liên tục.
        n = int(w.index("end-1c").split(".")[0])
        if n > max_lines:
            w.delete("1.0", "%d.0" % (n - max_lines))
        w.see("end")
        w.config(state="disabled")

    def write_log(self, text):
        """Sự kiện chung (dò mạng, chọn card...) -> tab Hệ thống."""
        self._append_log("system", "%s  %s\n" % (time.strftime("%H:%M:%S"), text))

    def write_pc_log(self, pc_id, text):
        """Sự kiện riêng của 1 PC (bấm nút, kết quả) -> tab của PC đó."""
        self._append_log(pc_id, "%s  » %s\n" % (time.strftime("%H:%M:%S"), text))

    # ---------- poll trạng thái (luồng nền) ----------
    def _poll_worker(self, target, poll_seq):
        host = target.get("host")
        pc_id = target["pc_id"]

        if not host:
            with self._state_lock:
                self._machine_state[pc_id] = STATE_UNRESOLVED
                self._booting_since.pop(pc_id, None)
            self.q.put(("state", pc_id, STATE_UNRESOLVED, None, poll_seq))
            return

        # 1) Agent trả lời -> máy đã login và Agent đang sống.
        res = probe_agent(host, self.cfg["agent_port"], self.cfg["token"])
        if res and res.get("ok"):
            with self._state_lock:
                self._machine_state[pc_id] = STATE_COMPLETE
                self._booting_since.pop(pc_id, None)
            self.q.put(("state", pc_id, STATE_COMPLETE, res, poll_seq))
            return

        # 2) Agent không trả lời. Kiểm tra bản thân Windows/máy còn sống trên mạng không.
        up = probe_network(host)
        now = time.time()

        with self._state_lock:
            prev = self._machine_state.get(pc_id, STATE_UNRESOLVED)

            if not up:
                # Máy thực sự biến mất khỏi mạng -> OFF. Reset chu kỳ boot để lần
                # bật máy tiếp theo được phép hiện "Đang khởi động".
                state = STATE_OFF
                self._booting_since.pop(pc_id, None)

            elif prev in (STATE_COMPLETE, STATE_AGENT_OFFLINE):
                # Máy vừa từng COMPLETE mà Agent đột nhiên biến mất trong khi mạng
                # vẫn sống: đây KHÔNG phải booting, mà là Agent đã chết/mất kết nối.
                state = STATE_AGENT_OFFLINE
                self._booting_since.pop(pc_id, None)

            else:
                # Máy mới xuất hiện trên mạng nhưng Agent chưa lên. Cho Agent một
                # khoảng grace để Task Scheduler/auto-login khởi động. Quá thời gian
                # này thì kết luận Windows đã lên nhưng Agent có vấn đề.
                since = self._booting_since.setdefault(pc_id, now)
                grace = float(self.cfg.get("boot_grace_seconds", 120.0))
                state = (STATE_BOOTING if now - since < grace
                         else STATE_AGENT_OFFLINE)

            self._machine_state[pc_id] = state

        self.q.put(("state", pc_id, state, res, poll_seq))


    def poll_now(self):
        for t in self.cfg["targets"]:
            pc_id = t["pc_id"]
            with self._state_lock:
                self._poll_seq[pc_id] = self._poll_seq.get(pc_id, 0) + 1
                seq = self._poll_seq[pc_id]
            threading.Thread(target=self._poll_worker, args=(t, seq), daemon=True).start()

    def poll_loop(self):
        self.poll_now()
        self.after(int(self.cfg.get("poll_seconds", 4.0) * 1000), self.poll_loop)

    # ---------- mở chương trình ----------
    def _open_worker(self, target):
        res = send_agent_cmd(target["host"], self.cfg["agent_port"], self.cfg["token"],
                             "start", target.get("app", "inspection"))
        self.q.put(("open_result", target["pc_id"], res))

    def on_open_program(self, target):
        if not target.get("host"):
            return
        pc_id = target["pc_id"]
        self._app_ready_last_seen[pc_id] = 0.0
        self._ready_flags[pc_id] = False
        self._all_ready_notified = False
        self.write_pc_log(pc_id, "→ mở chương trình")
        threading.Thread(target=self._open_worker, args=(target,), daemon=True).start()

    # ---------- dừng chương trình ----------
    def _stop_worker(self, target):
        res = send_agent_cmd(target["host"], self.cfg["agent_port"], self.cfg["token"],
                             "stop", target.get("app", "inspection"))
        self.q.put(("stop_result", target["pc_id"], res))

    def on_stop_program(self, target):
        if not target.get("host"):
            return
        pc_id = target["pc_id"]
        if not messagebox.askyesno(
                "Xác nhận dừng chương trình",
                "Dừng chương trình trên %s?" % pc_id):
            return
        self._app_ready_last_seen[pc_id] = 0.0
        self._ready_flags[pc_id] = False
        self._all_ready_notified = False
        self.write_pc_log(pc_id, "→ dừng chương trình")
        threading.Thread(target=self._stop_worker, args=(target,), daemon=True).start()

    # ---------- hủy vòng kiểm tra đang treo, KHÔNG gửi PLC ----------
    def _abort_check_worker(self, target):
        res = send_agent_cmd(target['host'], self.cfg['agent_port'], self.cfg['token'],
                             'abort_check', target.get('app', 'inspection'), timeout=9.0)
        self.q.put(('abort_check_result', target['pc_id'], res))

    def on_abort_check(self, target):
        if not target.get('host'):
            return
        pc_id = target['pc_id']
        if not messagebox.askyesno(
                'Xác nhận DỪNG CHECK',
                f'Hủy chu trình kiểm tra đang dở trên {pc_id}?\n\n'
                'Ứng dụng, camera và model vẫn chạy.\n'
                'KHÔNG có tín hiệu nào gửi tới PLC.\n'
                'BODY đang hủy sẽ không được tính OK/NG.'):
            return
        self.panels[pc_id].btn_abort.state(['disabled'])
        self.write_pc_log(pc_id, '→ DỪNG CHECK (chỉ reset phần mềm; không gửi PLC)')
        threading.Thread(target=self._abort_check_worker, args=(dict(target),), daemon=True).start()

    # ---------- tắt máy ----------
    def _shutdown_worker(self, target):
        res = send_agent_cmd(target["host"], self.cfg["agent_port"], self.cfg["token"],
                             "shutdown", target.get("app", "inspection"))
        self.q.put(("shutdown_result", target["pc_id"], res))

    def on_shutdown_machine(self, target):
        if not target.get("host"):
            return
        pc_id = target["pc_id"]
        if not messagebox.askyesno(
                "Xác nhận tắt máy",
                "Tắt máy %s?\n\n"
                "Chương trình đang chạy sẽ được đóng trước,\n"
                "sau đó máy sẽ tự tắt sau 10 giây." % pc_id):
            return
        self._app_ready_last_seen[pc_id] = 0.0
        self._ready_flags[pc_id] = False
        self._all_ready_notified = False
        self.write_pc_log(pc_id, "→ yêu cầu tắt máy (đóng app + shutdown sau 10s)")
        threading.Thread(target=self._shutdown_worker, args=(target,), daemon=True).start()

    # ---------- tìm & kết nối ----------
    def on_discover(self):
        if self._scanning:
            return
        self._scanning = True
        self.btn_scan.state(["disabled"])
        self.write_log("Bắt đầu dò mạng...")

        subnet = self.cfg.get("subnet")
        if subnet:
            threading.Thread(target=self._discover_worker, args=(subnet,), daemon=True).start()
        else:
            ads = list_adapters()
            if not ads:
                self.write_log("Không tìm thấy card mạng nào.")
                self._scanning = False
                self.btn_scan.state(["!disabled"])
                return
            if len(ads) == 1:
                self._on_adapter_picked(ads[0])
            else:
                AdapterDialog(self, ads, self._on_adapter_picked)

    def _on_adapter_picked(self, adapter):
        name, ip, mask = adapter
        subnet = ip.rsplit(".", 1)[0]
        self.cfg["subnet"] = subnet
        save_cfg(self.cfg)
        self.write_log("Dùng card: %s (%s) — dải %s.1-254" % (name, ip, subnet))
        threading.Thread(target=self._discover_worker, args=(subnet,), daemon=True).start()

    def _discover_worker(self, subnet):
        exclude = {ip for _, ip, _ in list_adapters()}
        try:
            assigned, unmatched, remaining = scan_and_match(
                subnet, self.cfg["targets"], exclude)
        except Exception as e:
            self.q.put(("discover_error", str(e)))
            return
        self.q.put(("discover_done", assigned, unmatched, remaining))

    def _apply_assignment(self, pc_id, ip, name=""):
        for t in self.cfg["targets"]:
            if t["pc_id"] == pc_id:
                t["host"] = ip
                self.panels[pc_id].set_host(ip, name)
        save_cfg(self.cfg)
        self.write_log("%s -> %s%s" % (pc_id, ip, "  (%s)" % name if name else ""))

    def _manual_pick(self, target, ip):
        self._apply_assignment(target["pc_id"], ip)
        self.poll_now()

    # ---------- luồng chính: lấy kết quả ra khỏi queue ----------
    def drain_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]

                if kind == "state":
                    _, pc_id, state, res, poll_seq = item

                    # Nếu một poll cũ trả về sau poll mới, bỏ kết quả cũ.
                    if poll_seq < self._applied_poll_seq.get(pc_id, 0):
                        continue
                    self._applied_poll_seq[pc_id] = poll_seq
                    self._last_panel_render[pc_id] = (state, res)

                    # Nếu Agent xác nhận app không chạy thì READY cũ không còn giá trị.
                    if state == STATE_COMPLETE:
                        apps = (res or {}).get("apps") or []
                        want = next(
                            (t.get("app", "inspection") for t in self.cfg["targets"]
                             if t["pc_id"] == pc_id),
                            "inspection"
                        )
                        app_info = next(
                            (a for a in apps if a.get("name") == want),
                            apps[0] if apps else None
                        )
                        if not (app_info and app_info.get("running")):
                            self._app_ready_last_seen[pc_id] = 0.0
                            self._ready_flags[pc_id] = False

                    self.panels[pc_id].render(
                        state, res, self._is_app_ready(pc_id)
                    )

                elif kind == "app_event":
                    _, payload, addr = item
                    pc_id = str(payload.get("pc_id") or "").upper()
                    event_type = str(payload.get("type") or "").lower()

                    if pc_id not in ("PC1", "PC2", "PC3"):
                        continue

                    if event_type == "ready":
                        was_ready = self._is_app_ready(pc_id)
                        self._app_ready_last_seen[pc_id] = time.time()
                        self._ready_flags[pc_id] = True
                        if not was_ready:
                            self.write_log("%s: chương trình đã READY." % pc_id)

                    elif event_type == "error":
                        event_id = str(payload.get("event_id") or "")
                        if event_id and event_id in self._seen_event_ids:
                            continue
                        if event_id:
                            self._seen_event_ids[event_id] = time.time()

                        title = payload.get("title") or ("%s - LỖI" % pc_id)
                        message = payload.get("message") or "Có lỗi từ chương trình."
                        code = payload.get("code") or "ERROR"

                        if pc_id in self.log_widgets:
                            self.write_pc_log(pc_id, "⚠ %s: %s" % (code, message.replace("\n", " | ")))
                        else:
                            self.write_log("%s ⚠ %s: %s" % (pc_id, code, message.replace("\n", " | ")))

                        messagebox.showerror(title, message)

                elif kind == "event_listener_error":
                    self.write_log("Lỗi listener READY/ERROR: %s" % item[1])

                elif kind == "open_result":
                    _, pc_id, res = item
                    ok = res.get("ok")
                    self.write_pc_log(pc_id, "← %s" % (
                        "đã bật" if ok else res.get("msg", "lỗi")))
                    # Poll lại bằng cơ chế sequence chuẩn để không tạo kết quả cũ
                    # ghi đè kết quả mới.
                    self.poll_now()

                elif kind == "stop_result":
                    _, pc_id, res = item
                    ok = res.get("ok")
                    self.write_pc_log(pc_id, "← %s" % (
                        "đã dừng" if ok else res.get("msg", "lỗi")))
                    self.poll_now()

                elif kind == 'abort_check_result':
                    _, pc_id, res = item
                    if res.get('ok'):
                        self.write_pc_log(pc_id, '✓ ' + str(res.get('msg') or 'Đã hủy check'))
                    else:
                        self.write_pc_log(pc_id, '⚠ DỪNG CHECK thất bại: ' + str(res.get('msg') or 'Không có ACK'))
                        messagebox.showwarning('DỪNG CHECK chưa thành công',
                                               f'{pc_id}: {res.get("msg", "Ứng dụng không phản hồi")}')
                    self.poll_now()

                elif kind == "shutdown_result":
                    _, pc_id, res = item
                    ok = res.get("ok")
                    self.write_pc_log(pc_id, "← %s" % (
                        res.get("msg") or ("đã gửi lệnh tắt máy" if ok else "lỗi không rõ")))
                    # Poll lại bằng cơ chế sequence chuẩn để không tạo kết quả cũ
                    # ghi đè kết quả mới.
                    self.poll_now()

                elif kind == "discover_error":
                    self.write_log("Lỗi dò mạng: %s" % item[1])
                    self._scanning = False
                    self.btn_scan.state(["!disabled"])

                elif kind == "discover_done":
                    _, assigned, unmatched, remaining = item
                    for pc_id, (ip, mac, name) in assigned.items():
                        self._apply_assignment(pc_id, ip, name)
                    if not assigned and not remaining:
                        self.write_log("Không tìm thấy máy nào khác ngoài chính PC3.")
                    for t in unmatched:
                        if remaining:
                            PickerDialog(self, t, remaining, self._manual_pick)
                        else:
                            self.write_log("Không tìm thấy %s trên mạng." % t["pc_id"])
                    self._scanning = False
                    self.btn_scan.state(["!disabled"])
                    self.write_log("Dò mạng xong.")
                    self.poll_now()
        except queue.Empty:
            pass
        self.after(150, self.drain_queue)


def load_cfg():
    if not os.path.exists(CFG_PATH):
        save_cfg(DEFAULT_CFG)
        return json.loads(json.dumps(DEFAULT_CFG))
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULT_CFG.items():
        cfg.setdefault(k, v)
    return cfg


def save_cfg(cfg):
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    Dashboard(load_cfg()).mainloop()
