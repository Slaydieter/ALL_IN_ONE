# -*- coding: utf-8 -*-
"""
agent_lightweight.py — Chạy trên PC1 và PC2 (mỗi máy 1 bản, khác nhau ở agent_config.json)

Nhiệm vụ:
  * Tự bật app inspection khi máy auto-login  (mục "autostart" trong config)
  * Mở socket server tối giản, nhận start/stop/restart/status/shutdown từ PC3

Chạy thử bằng tay:
    python agent.py
Lần đầu chạy sẽ tự sinh agent_config.json — sửa file đó rồi chạy lại.

QUAN TRỌNG — vì sao KHÔNG chạy agent dạng Windows Service:
    Service chạy ở Session 0, tách biệt hoàn toàn với desktop người dùng.
    App Tkinter bật từ đó sẽ CHẠY NHƯNG KHÔNG HIỆN GIAO DIỆN.
    Bắt buộc dùng Task Scheduler với tuỳ chọn "Run only when user is logged on".
"""

import ctypes
from ctypes import wintypes
import getpass
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

IS_WIN = os.name == "nt"
BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "agent_config.json")
STATE_PATH = os.path.join(BASE, "agent_state.json")
LOG_PATH = os.path.join(BASE, "agent.log")

LEN_FMT = "!I"
LEN_SIZE = 4
MAX_MSG = 1024 * 1024

CREATE_NEW_PROCESS_GROUP = 0x00000200
HIGH_PRIORITY_CLASS = 0x00000080
CREATE_NO_WINDOW = 0x08000000

DEFAULT_CFG = {
    "pc_id": "PC2",
    "host": "0.0.0.0",
    "port": 6100,
    "token": "IS1200_480_CHANGE_ME",
    "startup_delay_sec": 20,
    "apps": {
        "inspection": {
            "label": "IS1200_480 PC2",
            "cmd": ["C:\\Python311\\pythonw.exe", "D:\\IS1200_480\\IS1200_480_L1_PC2_PM.py"],
            "cwd": "D:\\IS1200_480",
            "autostart": False,
            "hide_console": True,
            "discard_output": True
        }
    }
}


AGENT_START = time.time()


# ------------------------------------------------- thông tin phiên đăng nhập

def sys_info():
    """
    Báo cho PC1 biết agent đang chạy trong phiên nào.

    session = 0  -> agent đang chạy như Windows Service (Session 0).
                    App Tkinter bật từ đây SẼ KHÔNG HIỆN GIAO DIỆN.
    session >= 1 -> phiên desktop thật, auto-login đã chạy xong. Đúng.
    """
    info = {
        "user": getpass.getuser(),
        "agent_started": AGENT_START,
        "session": None,
        "interactive": None,
        "boot_time": None,
    }
    try:
        if IS_WIN:
            import ctypes
            from ctypes import wintypes
            sid = wintypes.DWORD()
            ok = ctypes.windll.kernel32.ProcessIdToSessionId(
                wintypes.DWORD(os.getpid()), ctypes.byref(sid))
            if ok:
                info["session"] = int(sid.value)
                info["interactive"] = sid.value != 0
            ticks = ctypes.windll.kernel32.GetTickCount64()   # ms từ lúc boot
            info["boot_time"] = time.time() - ticks / 1000.0
        else:
            with open("/proc/uptime") as f:
                info["boot_time"] = time.time() - float(f.read().split()[0])
            info["session"] = os.getsid(os.getpid())
            info["interactive"] = True
    except Exception as e:
        info["error"] = str(e)
    return info


# ---------------------------------------------------------------- log

def setup_log():
    log = logging.getLogger("agent")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=2 * 1024 * 1024,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    try:                                  # chạy qua pythonw thì không có stdout
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    except Exception:
        pass
    return log


log = setup_log()


# ---------------------------------------------------------------- khung tin

def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        c = sock.recv(min(65536, n - len(buf)))
        if not c:
            raise ConnectionError("Đối phương đóng kết nối")
        buf.extend(c)
    return bytes(buf)


def send_msg(sock, obj):
    b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sock.sendall(struct.pack(LEN_FMT, len(b)) + b)


def recv_msg(sock):
    n = struct.unpack(LEN_FMT, recv_exact(sock, LEN_SIZE))[0]
    if n > MAX_MSG:
        raise ValueError("Message quá lớn")
    return json.loads(recv_exact(sock, n).decode("utf-8"))


# ---------------------------------------------------------------- tiến trình

def pid_alive(pid):
    """Kiểm tra PID mà không spawn tasklist.exe mỗi lần poll."""
    if not pid:
        return False

    if IS_WIN:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class AppManager:
    """Quản lý các app được cấu hình trong agent_config.json"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.state = self._load_state()      # {app_name: {"pid":.., "since":..}}

    # --- lưu PID ra file để agent restart vẫn biết app đang chạy ---
    def _load_state(self):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            return {}
        return {k: v for k, v in st.items() if pid_alive(v.get("pid"))}

    def _save_state(self):
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("Không ghi được state: %s", e)

    def is_running(self, name):
        info = self.state.get(name)
        return bool(info) and pid_alive(info.get("pid"))


    def start(self, name):
        with self.lock:
            app = self.cfg["apps"].get(name)
            if not app:
                return {"ok": False, "msg": "Không có app tên '%s'" % name}

            if self.is_running(name):
                return {"ok": False, "msg": "App đang chạy rồi",
                        "pid": self.state[name]["pid"]}

            cmd = list(app["cmd"])
            if not cmd:
                return {"ok": False, "msg": "Cấu hình cmd đang rỗng"}

            # Chấp nhận cả đường dẫn tuyệt đối lẫn lệnh nằm trên PATH.
            # Ví dụ hiện tại:
            #   cmd.exe /c <python.exe> <script.py>
            exe = cmd[0] if os.path.isabs(cmd[0]) else (shutil.which(cmd[0]) or cmd[0])
            if not os.path.exists(exe):
                return {"ok": False,
                        "msg": "Không thấy chương trình: %s" % cmd[0]}
            cmd[0] = exe

            # Nếu launcher là cmd.exe thì /c, /k, /d, /s... là SWITCH,
            # không phải đường dẫn. Bản cũ dùng os.path.isabs('/c') nên trên
            # Windows có thể hiểu nhầm /c là path và báo "Không thấy file: /c".
            is_cmd_launcher = os.path.basename(exe).lower() in ("cmd.exe", "cmd")

            # Kiểm tra các argument thực sự có dạng đường dẫn Windows.
            # Vẫn hỗ trợ cấu hình cũ chạy trực tiếp python.exe script.py.
            for extra in cmd[1:]:
                if not isinstance(extra, str) or not extra:
                    continue

                if is_cmd_launcher and extra.startswith("/") \
                        and "\\" not in extra and ":" not in extra:
                    continue

                looks_like_path = (
                    "\\" in extra
                    or (len(extra) >= 2 and extra[1] == ":")
                )

                if looks_like_path and not os.path.exists(extra):
                    return {"ok": False, "msg": "Không thấy file: %s" % extra}

            # cwd RẤT quan trọng: app Tkinter/YOLO thường dùng đường dẫn tương đối
            # cho model, config, icon... Ưu tiên cwd trong JSON.
            cwd = app.get("cwd")

            # Nếu config không khai báo cwd, ưu tiên lấy thư mục của script/bat/cmd.
            if not cwd:
                for extra in reversed(cmd[1:]):
                    if not isinstance(extra, str):
                        continue
                    low = extra.lower()
                    if low.endswith((".py", ".bat", ".cmd")) and os.path.isabs(extra):
                        cwd = os.path.dirname(extra)
                        break

            # Cuối cùng mới fallback về thư mục của executable chính.
            if not cwd:
                cwd = os.path.dirname(exe)

            if cwd and not os.path.isdir(cwd):
                return {"ok": False, "msg": "Không thấy thư mục cwd: %s" % cwd}

            flags = 0
            if IS_WIN:
                # Ép process inspection chạy ở mức HIGH.
                # Agent vẫn giữ priority mặc định/Normal.
                flags = CREATE_NEW_PROCESS_GROUP | HIGH_PRIORITY_CLASS
                if app.get("hide_console", True):
                    flags |= CREATE_NO_WINDOW

            # LIGHTWEIGHT: tuyệt đối không ghi stdout/stderr của app vào file.
            # Nếu app print rất nhiều trong lúc OpenVINO/YOLO xử lý, ghi log xuống
            # ổ đĩa có thể tạo I/O không cần thiết. Mặc định đẩy output vào NUL.
            try:
                popen_kwargs = dict(cwd=cwd or None)
                if app.get("discard_output", True):
                    popen_kwargs["stdout"] = subprocess.DEVNULL
                    popen_kwargs["stderr"] = subprocess.DEVNULL
                if IS_WIN:
                    popen_kwargs["creationflags"] = flags
                else:
                    popen_kwargs["start_new_session"] = True
                p = subprocess.Popen(cmd, **popen_kwargs)
            except Exception as e:
                log.exception("Bật app thất bại")
                return {"ok": False, "msg": "Lỗi khi bật: %s" % e}

            time.sleep(0.4)  # chỉ chờ ngắn xem process có chết ngay không
            if p.poll() is not None:
                return {"ok": False,
                        "msg": "App thoát ngay, exit code %s" % p.returncode}

            self.state[name] = {"pid": p.pid, "since": time.time()}
            self._save_state()
            log.info("Đã bật '%s' pid=%d", name, p.pid)
            return {"ok": True, "pid": p.pid}

    def abort_check(self, name):
        """Software-only abort through local inspection listener; never kill app or touch PLC."""
        app = self.cfg.get('apps', {}).get(name)
        if app is None:
            return {'ok': False, 'msg': f'Unknown application: {name}'}
        port = int(app.get('abort_port', 6101))
        if not 1 <= port <= 65535:
            return {'ok': False, 'msg': 'Invalid local abort port'}
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=2.0) as conn:
                conn.settimeout(6.0)
                conn.sendall(b'{"cmd":"abort_check"}\n')
                payload = bytearray()
                while len(payload) < 4096 and b'\n' not in payload:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    payload.extend(chunk)
                result = json.loads(payload.split(b'\n', 1)[0].decode('utf-8'))
                if not isinstance(result, dict) or result.get('pc_id') != self.cfg.get('pc_id'):
                    return {'ok': False, 'msg': 'Wrong PC or invalid inspection reply'}
                return {'ok': bool(result.get('ok')), 'msg': str(result.get('msg', '')),
                        'aborted': bool(result.get('aborted'))}
        except Exception as exc:
            log.warning('abort_check local listener unavailable: %s', exc)
            return {'ok': False, 'msg': f'Inspection app not responding at localhost:{port}: {exc}'}

    def stop(self, name):
        with self.lock:
            if not self.is_running(name):
                self.state.pop(name, None)
                self._save_state()
                return {"ok": True, "msg": "App vốn không chạy"}

            pid = self.state[name]["pid"]
            try:
                if IS_WIN:
                    # /T kill cả cây con — app YOLO hay sinh process phụ
                    subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                                   capture_output=True, timeout=10,
                                   creationflags=CREATE_NO_WINDOW)
                    for _ in range(10):
                        if not pid_alive(pid):
                            break
                        time.sleep(0.5)
                    if pid_alive(pid):
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                       capture_output=True, timeout=10,
                                       creationflags=CREATE_NO_WINDOW)
                else:
                    os.kill(pid, 15)
                    for _ in range(10):
                        if not pid_alive(pid):
                            break
                        time.sleep(0.3)
                    if pid_alive(pid):
                        os.kill(pid, 9)
            except Exception as e:
                return {"ok": False, "msg": "Lỗi khi tắt: %s" % e}

            self.state.pop(name, None)
            self._save_state()
            log.info("Đã tắt '%s' pid=%d", name, pid)
            return {"ok": True}

    def status(self):
        out = []
        for name, app in self.cfg["apps"].items():
            running = self.is_running(name)
            info = self.state.get(name, {})
            out.append({
                "name": name,
                "label": app.get("label", name),
                "running": running,
                "pid": info.get("pid") if running else None,
                "uptime": int(time.time() - info["since"]) if running and
                          info.get("since") else 0,
            })
        return out


# ---------------------------------------------------------------- server

class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.apps = AppManager(cfg)
        self.stop_flag = threading.Event()

    def autostart(self):
        delay = int(self.cfg.get("startup_delay_sec", 0))
        if delay:
            log.info("Chờ %ds cho mạng/camera sẵn sàng...", delay)
            time.sleep(delay)
        for name, app in self.cfg["apps"].items():
            if app.get("autostart"):
                r = self.apps.start(name)
                log.info("Autostart '%s': %s", name, r)

    def handle(self, req):
        if req.get("token") != self.cfg.get("token"):
            return {"ok": False, "msg": "Sai token"}

        cmd = (req.get("cmd") or "").lower()
        name = req.get("app", "inspection")

        if cmd == "ping":
            return {"ok": True, "pc_id": self.cfg["pc_id"],
                    "time": time.strftime("%H:%M:%S"), "sys": sys_info()}
        if cmd == "status":
            return {"ok": True, "pc_id": self.cfg["pc_id"],
                    "apps": self.apps.status(), "sys": sys_info()}
        if cmd == "start":
            return self.apps.start(name)
        if cmd == "stop":
            return self.apps.stop(name)
        if cmd == "abort_check":
            return self.apps.abort_check(name)
        if cmd == "restart":
            self.apps.stop(name)
            time.sleep(1.5)
            return self.apps.start(name)
        if cmd == "shutdown":
            return self.shutdown_machine(name, int(req.get("delay_sec", 10)))
        return {"ok": False, "msg": "Lệnh lạ: %r" % cmd}

    def shutdown_machine(self, name, delay_sec=10):
        """Đóng app (dùng lại stop() -- đã kill nhẹ rồi ép kill nếu cần,
        tối đa ~5s) rồi gọi shutdown hệ điều hành sau delay_sec giây."""
        stop_res = self.apps.stop(name)
        try:
            if IS_WIN:
                subprocess.run(["shutdown", "/s", "/t", str(delay_sec)],
                               capture_output=True, timeout=10,
                               creationflags=CREATE_NO_WINDOW)
            else:
                subprocess.run(["shutdown", "-h", "+%d" % max(1, delay_sec // 60)],
                               capture_output=True, timeout=10)
        except Exception as e:
            log.exception("Loi khi goi lenh shutdown he thong")
            return {"ok": False,
                    "msg": "Da dong app nhung loi khi goi shutdown: %s" % e,
                    "stop_result": stop_res}
        log.info("Da goi shutdown he thong (tre %ds) sau khi dong app '%s'",
                 delay_sec, name)
        return {"ok": True,
                "msg": "Da dong app, may se tat sau %ds" % delay_sec,
                "stop_result": stop_res}


    def client_loop(self, conn, addr):
        conn.settimeout(30.0)
        try:
            while not self.stop_flag.is_set():
                req = recv_msg(conn)
                try:
                    res = self.handle(req)
                except Exception as e:
                    log.exception("Lỗi xử lý lệnh")
                    res = {"ok": False, "msg": str(e)}
                send_msg(conn, res)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            conn.close()

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.cfg.get("host", "0.0.0.0"), int(self.cfg["port"])))
        srv.listen(8)
        srv.settimeout(1.0)
        log.info("Agent %s nghe ở cổng %d", self.cfg["pc_id"], self.cfg["port"])
        try:
            while not self.stop_flag.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self.client_loop,
                                 args=(conn, addr), daemon=True).start()
        except KeyboardInterrupt:
            log.info("Dừng agent (Ctrl+C)")
            raise
        finally:
            self.stop_flag.set()
            srv.close()


def load_cfg():
    if not os.path.exists(CFG_PATH):
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CFG, f, ensure_ascii=False, indent=2)
        print("Đã tạo mẫu %s — hãy sửa lại rồi chạy lại." % CFG_PATH)
        sys.exit(0)
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _agent_already_running(cfg, timeout=1.0):
    """True nếu đã có ai đó (agent cũ, hoặc chính agent 'At log on' vừa
    bật) đang lắng nghe ở cổng này. Dùng để nếu Task Scheduler có thêm
    trigger 'Repeat task every N phút' (khuyến nghị thêm, xem docstring
    đầu file) thì mỗi lần trigger bắn ra chỉ là no-op vô hại khi agent
    vẫn đang sống, không tạo ra 2 agent cùng bind 1 port."""
    try:
        with socket.create_connection(("127.0.0.1", int(cfg["port"])), timeout=timeout):
            return True
    except OSError:
        return False


def run_agent_once(cfg):
    """Chạy trọn 1 vòng đời agent. Trả về khi serve() kết thúc -- do
    KeyboardInterrupt (re-raise, xem serve()) hoặc do exception khác."""
    ag = Agent(cfg)
    threading.Thread(target=ag.autostart, daemon=True).start()
    ag.serve()


def main():
    cfg = load_cfg()

    if _agent_already_running(cfg):
        log.info("Da co agent khac dang chay o cong %d -> thoat, khong chay trung.",
                 cfg["port"])
        return

    # Vòng ngoài: serve() kết thúc vì lý do NGOÀI DỰ KIẾN (lỗi bind cổng
    # do process cũ còn giữ, exception chưa lường trước...) -> tự khởi
    # động lại sau vài giây, KHÔNG phải chờ tới lần đăng nhập kế tiếp mới
    # sống lại (đây chính là nguyên nhân dashboard PC3 kẹt màu vàng "Đang
    # khởi động" dù máy đã lên xong từ lâu -- agent chết và không ai dựng
    # lại nó cho tới lần "At log on" kế tiếp).
    while True:
        try:
            run_agent_once(cfg)
            log.warning("serve() ket thuc binh thuong (hiem khi xay ra) -> khoi dong lai")
        except KeyboardInterrupt:
            log.info("Da dung agent theo yeu cau (Ctrl+C)")
            break
        except Exception:
            log.exception("Agent crash ngoai du kien -> khoi dong lai sau 5s")
        time.sleep(5)


if __name__ == "__main__":
    main()