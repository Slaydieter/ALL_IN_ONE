# -*- coding: utf-8 -*-
"""IS1200_480 ErrorArchive Manager - Industrial UI v5.

Quan ly inspection_ng_events / ErrorArchive:
- Loc theo barcode, ngay, PC, TAG, LEVEL.
- Gop cac TAG cung cycle + PC + CAM + VT thanh mot dong.
- Xuat TXT theo dung bo loc / thu tu dang hien thi.
- Xem anh va thong tin NG chi tiet.
- Hien thi nguoi / gio xac nhan NG.
- Xoa 1 hoac nhieu loi, xoa theo ngay, xoa theo thang.
- Tu dong giu 10 ngay lich gan nhat; du lieu cu bi xoa vao ngay thu 11.

Khong xoa inspection_checks, vi bang do la lich su thong ke OK/NG cua day chuyen.
"""
import calendar
import os
import threading
import tkinter as tk
from datetime import date, datetime
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from is480_statistics import (
    DATABASE_PATH,
    ERROR_ARCHIVE_RETENTION_DAYS,
    cleanup_ng_events_retention,
    delete_ng_events_by_date,
    delete_ng_events_by_ids,
    delete_ng_events_by_month,
    init_statistics_database,
    query_ng_events,
)


COLORS = {
    "navy": "#16324F",
    "navy2": "#244A6D",
    "blue": "#2F75B5",
    "blue_hover": "#3D8AC9",
    "steel": "#5F7487",
    "bg": "#E8EDF2",
    "panel": "#F7F9FB",
    "white": "#FFFFFF",
    "line": "#CAD4DE",
    "text": "#1E2A35",
    "muted": "#5D6B78",
    "red": "#B42318",
    "red_hover": "#D92D20",
    "orange": "#B54708",
    "green": "#067647",
    "green_bg": "#ECFDF3",
    "amber_bg": "#FFF6E5",
    "red_bg": "#FEF3F2",
    "table_alt": "#F1F5F9",
}


class CalendarPopup(tk.Toplevel):
    """Popup lich thuan tkinter, khong can cai tkcalendar."""

    WEEKDAYS = ("T2", "T3", "T4", "T5", "T6", "T7", "CN")

    def __init__(self, parent, initial_date, on_select, anchor_widget=None):
        super().__init__(parent)
        self.withdraw()
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.title("Chọn ngày")
        self.configure(bg=COLORS["bg"])

        self.on_select = on_select
        self.anchor_widget = anchor_widget
        self.year = initial_date.year
        self.month = initial_date.month
        self.selected_date = initial_date

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _e: self.destroy())

        self._build_ui()
        self._render_month()
        self._position_popup()

        self.deiconify()
        self.lift()
        self.focus_force()
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _build_ui(self):
        header = tk.Frame(self, bg=COLORS["navy"], padx=10, pady=8)
        header.pack(fill="x")

        nav_style = {
            "font": ("Segoe UI", 11, "bold"),
            "fg": "white",
            "bg": COLORS["navy"],
            "activeforeground": "white",
            "activebackground": COLORS["blue"],
            "bd": 0,
            "width": 3,
            "cursor": "hand2",
        }
        tk.Button(header, text="◀", command=self._prev_month, **nav_style).pack(side="left")
        self.month_label = tk.Label(
            header,
            text="",
            font=("Segoe UI", 12, "bold"),
            fg="white",
            bg=COLORS["navy"],
            width=18,
            anchor="center",
        )
        self.month_label.pack(side="left", padx=8)
        tk.Button(header, text="▶", command=self._next_month, **nav_style).pack(side="left")

        self.calendar_frame = tk.Frame(self, bg=COLORS["white"], padx=8, pady=7)
        self.calendar_frame.pack(fill="both", expand=True)

        # QUAN TRONG: padx/pady tuple chi dung cho pack/grid, khong truyen vao tk.Frame.
        # Day la fix cho TclError: bad screen distance "2 8".
        footer = tk.Frame(self, bg=COLORS["bg"], padx=8, pady=4)
        footer.pack(fill="x", pady=(2, 8))
        ttk.Button(footer, text="Hôm nay", command=self._choose_today).pack(side="left")
        ttk.Button(footer, text="Đóng", command=self.destroy).pack(side="right")

    def _render_month(self):
        for child in self.calendar_frame.winfo_children():
            child.destroy()

        self.month_label.configure(text=f"Tháng {self.month:02d} / {self.year}")
        for col, text in enumerate(self.WEEKDAYS):
            tk.Label(
                self.calendar_frame,
                text=text,
                font=("Segoe UI", 9, "bold"),
                bg="#D9E7F5",
                fg=COLORS["text"],
                width=4,
                height=1,
                bd=1,
                relief="solid",
            ).grid(row=0, column=col, padx=1, pady=1, sticky="nsew")

        cal = calendar.Calendar(firstweekday=calendar.MONDAY)
        weeks = cal.monthdayscalendar(self.year, self.month)
        today = date.today()
        for row_idx, week in enumerate(weeks, start=1):
            for col_idx, day_num in enumerate(week):
                if day_num == 0:
                    tk.Label(
                        self.calendar_frame, text="", bg="white", width=4, height=2
                    ).grid(row=row_idx, column=col_idx, padx=1, pady=1)
                    continue

                current = date(self.year, self.month, day_num)
                bg = "white"
                fg = COLORS["text"]
                if current == today:
                    bg = "#FFF2CC"
                if current == self.selected_date:
                    bg = COLORS["blue"]
                    fg = "white"

                tk.Button(
                    self.calendar_frame,
                    text=str(day_num),
                    font=("Segoe UI", 10, "bold" if current == self.selected_date else "normal"),
                    bg=bg,
                    fg=fg,
                    activebackground="#BDD7EE",
                    activeforeground=COLORS["text"],
                    width=4,
                    height=1,
                    bd=1,
                    relief="solid",
                    cursor="hand2",
                    command=lambda d=current: self._select_date(d),
                ).grid(row=row_idx, column=col_idx, padx=1, pady=1, sticky="nsew")

    def _prev_month(self):
        if self.month == 1:
            self.month = 12
            self.year -= 1
        else:
            self.month -= 1
        self._render_month()

    def _next_month(self):
        if self.month == 12:
            self.month = 1
            self.year += 1
        else:
            self.month += 1
        self._render_month()

    def _choose_today(self):
        self._select_date(date.today())

    def _select_date(self, selected):
        self.selected_date = selected
        self.on_select(selected.strftime("%Y-%m-%d"))
        self.destroy()

    def _position_popup(self):
        self.update_idletasks()
        if self.anchor_widget is not None and self.anchor_widget.winfo_exists():
            x = self.anchor_widget.winfo_rootx()
            y = self.anchor_widget.winfo_rooty() + self.anchor_widget.winfo_height() + 3
        else:
            parent = self.master.winfo_toplevel()
            x = parent.winfo_rootx() + 100
            y = parent.winfo_rooty() + 100

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        popup_w = self.winfo_reqwidth()
        popup_h = self.winfo_reqheight()
        x = max(0, min(x, screen_w - popup_w - 10))
        y = max(0, min(y, screen_h - popup_h - 50))
        self.geometry(f"+{x}+{y}")


class DatePicker(ttk.Frame):
    """O ngay readonly + nut lich. Gia tri YYYY-MM-DD."""

    def __init__(self, parent, variable, width=12):
        super().__init__(parent, style="Panel.TFrame")
        self.variable = variable
        self.entry = ttk.Entry(
            self, textvariable=variable, width=width, state="readonly", justify="center"
        )
        self.entry.pack(side=tk.LEFT, fill="x", expand=True)
        self.button = ttk.Button(self, text="Lịch", width=5, command=self.open_calendar)
        self.button.pack(side=tk.LEFT, padx=(4, 0))
        self.entry.bind("<Button-1>", lambda _e: self.open_calendar())

    def open_calendar(self):
        value = self.variable.get().strip()
        try:
            initial = datetime.strptime(value, "%Y-%m-%d").date() if value else date.today()
        except ValueError:
            initial = date.today()
        CalendarPopup(self, initial, self.variable.set, anchor_widget=self)


class DeleteDateDialog(tk.Toplevel):
    def __init__(self, parent, on_confirm):
        super().__init__(parent)
        self.title("Xóa lỗi theo ngày")
        self.transient(parent)
        self.resizable(False, False)
        self.configure(bg=COLORS["bg"])
        self.on_confirm = on_confirm
        self.date_var = tk.StringVar(value=date.today().strftime("%Y-%m-%d"))

        box = tk.Frame(self, bg=COLORS["white"], padx=18, pady=16, bd=1, relief="solid")
        box.pack(padx=14, pady=14, fill="both", expand=True)
        tk.Label(
            box, text="XÓA ERROR ARCHIVE THEO NGÀY", font=("Segoe UI", 13, "bold"),
            fg=COLORS["navy"], bg=COLORS["white"]
        ).pack(anchor="w")
        tk.Label(
            box,
            text="Chỉ xóa dữ liệu lỗi chi tiết và ảnh ErrorArchive.\nThống kê OK/NG chính vẫn được giữ.",
            font=("Segoe UI", 9), fg=COLORS["muted"], bg=COLORS["white"], justify="left"
        ).pack(anchor="w", pady=(4, 12))
        picker = DatePicker(box, self.date_var, width=16)
        picker.pack(fill="x")

        actions = tk.Frame(box, bg=COLORS["white"])
        actions.pack(fill="x", pady=(14, 0))
        ttk.Button(actions, text="Hủy", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(actions, text="Tiếp tục", command=self._confirm).pack(side="right")
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.focus_force()

    def _confirm(self):
        value = self.date_var.get().strip()
        self.destroy()
        self.on_confirm(value)


class DeleteMonthDialog(tk.Toplevel):
    def __init__(self, parent, on_confirm):
        super().__init__(parent)
        self.title("Xóa lỗi theo tháng")
        self.transient(parent)
        self.resizable(False, False)
        self.configure(bg=COLORS["bg"])
        self.on_confirm = on_confirm
        now = date.today()
        self.month_var = tk.StringVar(value=f"{now.month:02d}")
        self.year_var = tk.StringVar(value=str(now.year))

        box = tk.Frame(self, bg=COLORS["white"], padx=18, pady=16, bd=1, relief="solid")
        box.pack(padx=14, pady=14, fill="both", expand=True)
        tk.Label(
            box, text="XÓA ERROR ARCHIVE THEO THÁNG", font=("Segoe UI", 13, "bold"),
            fg=COLORS["navy"], bg=COLORS["white"]
        ).grid(row=0, column=0, columnspan=4, sticky="w")
        tk.Label(box, text="Tháng", bg=COLORS["white"], fg=COLORS["text"]).grid(
            row=1, column=0, sticky="w", pady=(14, 4)
        )
        ttk.Combobox(
            box, textvariable=self.month_var,
            values=tuple(f"{m:02d}" for m in range(1, 13)), state="readonly", width=8
        ).grid(row=2, column=0, sticky="ew", padx=(0, 10))
        tk.Label(box, text="Năm", bg=COLORS["white"], fg=COLORS["text"]).grid(
            row=1, column=1, sticky="w", pady=(14, 4)
        )
        ttk.Combobox(
            box, textvariable=self.year_var,
            values=tuple(str(y) for y in range(now.year - 5, now.year + 2)),
            state="readonly", width=10
        ).grid(row=2, column=1, sticky="ew")

        actions = tk.Frame(box, bg=COLORS["white"])
        actions.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(16, 0))
        ttk.Button(actions, text="Hủy", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(actions, text="Tiếp tục", command=self._confirm).pack(side="right")
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.focus_force()

    def _confirm(self):
        value = f"{self.year_var.get()}-{self.month_var.get()}"
        self.destroy()
        self.on_confirm(value)


class ErrorArchiveManager(tk.Tk):
    RETENTION_CHECK_MS = 6 * 60 * 60 * 1000

    def __init__(self):
        super().__init__()
        self.title("IS1200_480 - Error Archive Manager")
        self.geometry("1600x900")
        try:
            self.state("zoomed")
        except tk.TclError:
            pass
        self.configure(bg=COLORS["bg"])
        self.minsize(1180, 700)

        self.rows_by_iid = {}
        self.filtered_groups = []
        self.preview_imgtk = None
        self.busy = False
        self._preview_after = None

        # Filter state. Search gom Barcode / TAG / NAME / nguoi xac nhan de thanh loc gon hon.
        self.search_var = tk.StringVar()
        self.date_from_var = tk.StringVar()
        self.date_to_var = tk.StringVar()
        self.pc_var = tk.StringVar(value="Tất cả")
        self.level_var = tk.StringVar(value="Tất cả")
        self.confirm_filter_var = tk.StringVar(value="Tất cả")

        # Header / status state.
        self.total_var = tk.StringVar(value="0")
        self.selected_var = tk.StringVar(value="0")
        self.status_var = tk.StringVar(value=f"DB: {DATABASE_PATH}")
        self.retention_var = tk.StringVar(
            value=f"Tự động xóa ngày thứ {ERROR_ARCHIVE_RETENTION_DAYS + 1}"
        )

        # Detail state.
        self.detail_barcode_var = tk.StringVar(value="--")
        self.detail_location_var = tk.StringVar(value="--")
        self.detail_tag_var = tk.StringVar(value="--")
        self.detail_level_var = tk.StringVar(value="--")
        self.detail_time_var = tk.StringVar(value="--")
        self.detail_confirm_var = tk.StringVar(value="Chưa xác nhận")
        self.detail_confirm_time_var = tk.StringVar(value="--")
        self.detail_path_var = tk.StringVar(value="")

        self._configure_styles()
        init_statistics_database()
        self._build_ui()
        self.refresh()
        self.after(600, self._run_retention_cleanup_silent)

    def _configure_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("Toolbar.TFrame", background="#EFF4F8")
        style.configure(
            "TLabel", font=("Segoe UI", 10),
            background=COLORS["panel"], foreground=COLORS["text"]
        )
        style.configure(
            "Toolbar.TLabel", font=("Segoe UI", 9, "bold"),
            background="#EFF4F8", foreground=COLORS["text"]
        )
        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=(10, 6))
        style.configure(
            "Compact.TButton", font=("Segoe UI", 9, "bold"), padding=(9, 5)
        )
        style.configure(
            "Treeview", font=("Segoe UI", 10), rowheight=34,
            background="white", fieldbackground="white", foreground=COLORS["text"],
            bordercolor=COLORS["line"], lightcolor=COLORS["line"], darkcolor=COLORS["line"]
        )
        style.configure(
            "Treeview.Heading", font=("Segoe UI", 10, "bold"),
            background=COLORS["navy2"], foreground="white", relief="flat", padding=(7, 8)
        )
        style.map(
            "Treeview",
            background=[("selected", "#1976D2")],
            foreground=[("selected", "white")],
        )
        style.map("Treeview.Heading", background=[("active", COLORS["blue"])])
        style.configure(
            "TCombobox", padding=4, arrowsize=14,
        )
        style.configure("TEntry", padding=4)

    def _build_ui(self):
        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)
        self._build_header()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

    def _build_header(self):
        header = tk.Frame(self, bg=COLORS["navy"], padx=16, pady=9)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        title_box = tk.Frame(header, bg=COLORS["navy"])
        title_box.grid(row=0, column=0, sticky="w")
        tk.Label(
            title_box, text="ERROR ARCHIVE MANAGER",
            font=("Segoe UI", 20, "bold"), fg="#F5A623", bg=COLORS["navy"]
        ).pack(side="left")
        tk.Label(
            title_box, text="   |   IS1200_480 FACTORY ERROR TRACKING",
            font=("Segoe UI", 11, "bold"), fg="#D6E3EF", bg=COLORS["navy"]
        ).pack(side="left", pady=(5, 0))

        tk.Label(
            header,
            text="Danh sách lỗi là trung tâm • Chọn dòng để xem ảnh ngay • Dữ liệu lưu trữ 10 ngày",
            font=("Segoe UI", 9), fg="#BCD0E2", bg=COLORS["navy"]
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        retention = tk.Label(
            header, textvariable=self.retention_var,
            font=("Segoe UI", 9, "bold"), fg="#176B46", bg="#E8FFF4",
            padx=12, pady=6, bd=1, relief="solid"
        )
        retention.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))

    def _build_toolbar(self):
        toolbar = tk.Frame(self, bg="#EFF4F8", bd=1, relief="solid", padx=10, pady=8)
        toolbar.grid(row=1, column=0, sticky="ew", padx=8, pady=(7, 5))
        for c in range(18):
            toolbar.columnconfigure(c, weight=0)
        toolbar.columnconfigure(6, weight=1)

        # Dates
        ttk.Label(toolbar, text="Từ ngày", style="Toolbar.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 4)
        )
        DatePicker(toolbar, self.date_from_var, width=11).grid(
            row=1, column=0, sticky="ew", padx=(0, 8)
        )

        ttk.Label(toolbar, text="Đến ngày", style="Toolbar.TLabel").grid(
            row=0, column=1, sticky="w", padx=(0, 4)
        )
        DatePicker(toolbar, self.date_to_var, width=11).grid(
            row=1, column=1, sticky="ew", padx=(0, 10)
        )

        # Search - central filter input
        ttk.Label(toolbar, text="Barcode / TAG / Người xác nhận", style="Toolbar.TLabel").grid(
            row=0, column=2, columnspan=5, sticky="w", padx=(0, 4)
        )
        search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=32)
        search_entry.grid(row=1, column=2, columnspan=5, sticky="ew", padx=(0, 10))
        search_entry.bind("<Return>", lambda _e: self.refresh())

        ttk.Label(toolbar, text="PC", style="Toolbar.TLabel").grid(
            row=0, column=7, sticky="w", padx=(0, 4)
        )
        ttk.Combobox(
            toolbar, textvariable=self.pc_var,
            values=("Tất cả", "PC1", "PC2", "PC3"), state="readonly", width=8
        ).grid(row=1, column=7, sticky="ew", padx=(0, 8))

        ttk.Label(toolbar, text="LEVEL", style="Toolbar.TLabel").grid(
            row=0, column=8, sticky="w", padx=(0, 4)
        )
        ttk.Combobox(
            toolbar, textvariable=self.level_var,
            values=("Tất cả", "0", "1", "2", "3", "4", "5"), state="readonly", width=8
        ).grid(row=1, column=8, sticky="ew", padx=(0, 8))

        ttk.Label(toolbar, text="Xác nhận", style="Toolbar.TLabel").grid(
            row=0, column=9, sticky="w", padx=(0, 4)
        )
        ttk.Combobox(
            toolbar, textvariable=self.confirm_filter_var,
            values=("Tất cả", "Đã xác nhận", "Chưa xác nhận"), state="readonly", width=13
        ).grid(row=1, column=9, sticky="ew", padx=(0, 10))

        # Main actions
        self.filter_btn = tk.Button(
            toolbar, text="LỌC", command=self.refresh,
            font=("Segoe UI", 10, "bold"), fg="white", bg="#0067C5",
            activeforeground="white", activebackground="#1684E8",
            bd=0, padx=18, pady=7, cursor="hand2"
        )
        self.filter_btn.grid(row=0, column=10, rowspan=2, padx=(0, 7), sticky="ns")

        ttk.Button(
            toolbar, text="LÀM MỚI", command=self._reset_and_refresh, style="Compact.TButton"
        ).grid(row=0, column=11, rowspan=2, padx=(0, 7), sticky="ns")

        self.export_btn = tk.Button(
            toolbar, text="XUẤT TXT", command=self.export_filtered_txt,
            font=("Segoe UI", 10, "bold"), fg="white", bg=COLORS["green"],
            activeforeground="white", activebackground="#08875D",
            bd=0, padx=14, pady=7, cursor="hand2"
        )
        self.export_btn.grid(row=0, column=12, rowspan=2, padx=(0, 7), sticky="ns")

        self.delete_menu_btn = tk.Menubutton(
            toolbar, text="XÓA  ▼",
            font=("Segoe UI", 10, "bold"), fg="white", bg=COLORS["red"],
            activeforeground="white", activebackground=COLORS["red_hover"],
            bd=0, padx=14, pady=7, cursor="hand2", relief="flat"
        )
        self.delete_menu_btn.grid(row=0, column=13, rowspan=2, padx=(0, 8), sticky="ns")
        delete_menu = tk.Menu(self.delete_menu_btn, tearoff=0, font=("Segoe UI", 10))
        delete_menu.add_command(label="Xóa lỗi đã chọn", command=self.delete_selected)
        delete_menu.add_separator()
        delete_menu.add_command(
            label="Xóa theo ngày...", command=lambda: DeleteDateDialog(self, self.delete_by_date)
        )
        delete_menu.add_command(
            label="Xóa theo tháng...", command=lambda: DeleteMonthDialog(self, self.delete_by_month)
        )
        self.delete_menu_btn.configure(menu=delete_menu)


    def _build_body(self):
        holder = tk.Frame(self, bg=COLORS["bg"], padx=8, pady=2)
        holder.grid(row=2, column=0, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)

        body = ttk.Panedwindow(holder, orient=tk.HORIZONTAL)
        body.grid(row=0, column=0, sticky="nsew")

        # Table duoc uu tien dien tich lon hon preview.
        left = tk.Frame(body, bg=COLORS["white"], bd=1, relief="solid")
        right = tk.Frame(body, bg=COLORS["white"], bd=1, relief="solid")
        body.add(left, weight=7)
        body.add(right, weight=3)

        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)

        # ===== LEFT / TABLE =====
        table_header = tk.Frame(left, bg=COLORS["navy2"], padx=12, pady=8)
        table_header.grid(row=0, column=0, sticky="ew")
        table_header.columnconfigure(0, weight=1)
        tk.Label(
            table_header, text="DANH SÁCH LỖI",
            font=("Segoe UI", 12, "bold"), fg="white", bg=COLORS["navy2"]
        ).grid(row=0, column=0, sticky="w")
        self.table_count_label = tk.Label(
            table_header, text="0 bản ghi", font=("Segoe UI", 9, "bold"),
            fg="#EAF4FF", bg="#3A6285", padx=10, pady=3
        )
        self.table_count_label.grid(row=0, column=1, sticky="e", padx=(8, 6))
        tk.Button(
            table_header, text="Chọn tất cả", command=self.select_all_visible,
            font=("Segoe UI", 8, "bold"), fg="white", bg="#3A6285",
            activeforeground="white", activebackground=COLORS["blue"],
            bd=0, padx=8, pady=3, cursor="hand2"
        ).grid(row=0, column=2, sticky="e", padx=(0, 4))
        tk.Button(
            table_header, text="Bỏ chọn", command=self.clear_selection,
            font=("Segoe UI", 8, "bold"), fg="white", bg="#3A6285",
            activeforeground="white", activebackground=COLORS["blue"],
            bd=0, padx=8, pady=3, cursor="hand2"
        ).grid(row=0, column=3, sticky="e")

        table_frame = tk.Frame(left, bg="white")
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        columns = (
            "created_at", "barcode", "check_no", "pc", "cam_vt", "tag", "level",
            "ng_confirmed_by", "ng_confirmed_at", "repair_status"
        )
        self.tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="extended"
        )
        headings = {
            "created_at": "Thời gian lỗi",
            "barcode": "Barcode",
            "check_no": "Check",
            "pc": "PC",
            "cam_vt": "CAM / VT",
            "tag": "Vị trí lỗi (TAG)",
            "level": "LEVEL",
            "ng_confirmed_by": "Người xác nhận lỗi",
            "ng_confirmed_at": "Giờ xác nhận",
            "repair_status": "XÁC NHẬN SỬA",
        }
        widths = {
            "created_at": 155,
            "barcode": 170,
            "check_no": 58,
            "pc": 52,
            "cam_vt": 105,
            "tag": 210,
            "level": 78,
            "ng_confirmed_by": 165,
            "ng_confirmed_at": 155,
            "repair_status": 145,
        }
        left_anchor = {"barcode", "tag", "ng_confirmed_by"}
        for col in columns:
            self.tree.heading(col, text=headings[col], command=lambda c=col: self._sort_tree(c, False))
            self.tree.column(
                col, width=widths[col], minwidth=45,
                anchor="w" if col in left_anchor else "center", stretch=True
            )

        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.tag_configure("alt", background="#F4F7FA")
        self.tree.tag_configure("pending", foreground="#A15C00")
        self.tree.tag_configure("confirmed", foreground=COLORS["text"])
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Delete>", lambda _e: self.delete_selected())
        self.tree.bind("<Return>", lambda _e: self.on_select())

        # ===== RIGHT / AUTO PREVIEW =====
        preview_header = tk.Frame(right, bg=COLORS["navy2"], padx=12, pady=8)
        preview_header.grid(row=0, column=0, sticky="ew")
        preview_header.columnconfigure(0, weight=1)
        tk.Label(
            preview_header, text="HÌNH ẢNH LỖI",
            font=("Segoe UI", 12, "bold"), fg="white", bg=COLORS["navy2"]
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            preview_header, text="Chọn một dòng để hiển thị ngay",
            font=("Segoe UI", 8), fg="#D9E7F2", bg=COLORS["navy2"]
        ).grid(row=1, column=0, sticky="w", pady=(1, 0))

        quick = tk.Frame(right, bg="#F6F9FC", padx=10, pady=8)
        quick.grid(row=1, column=0, sticky="ew")
        quick.columnconfigure(0, weight=2)
        quick.columnconfigure(1, weight=1)
        quick.columnconfigure(2, weight=1)
        quick.columnconfigure(3, weight=2)
        self._mini_info(quick, 0, "BARCODE", self.detail_barcode_var)
        self._mini_info(quick, 1, "TAG", self.detail_tag_var)
        self._mini_info(quick, 2, "LEVEL", self.detail_level_var)
        self._mini_info(quick, 3, "NGƯỜI XÁC NHẬN", self.detail_confirm_var)

        preview_wrap = tk.Frame(right, bg="#101820", bd=0)
        preview_wrap.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 7))
        preview_wrap.rowconfigure(0, weight=1)
        preview_wrap.columnconfigure(0, weight=1)
        self.preview = tk.Label(
            preview_wrap,
            text="CHỌN MỘT LỖI TRONG BẢNG\nĐỂ XEM ẢNH",
            bg="#101820", fg="#B9C6D2", font=("Segoe UI", 13, "bold"),
            justify="center"
        )
        self.preview.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        self.preview.bind("<Configure>", self._on_preview_resize)
        self.preview.bind("<Double-Button-1>", lambda _e: self.open_selected_image())

        detail = tk.Frame(right, bg="#F6F9FC", bd=1, relief="solid", padx=10, pady=8)
        detail.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))
        detail.columnconfigure(1, weight=1)
        detail.columnconfigure(3, weight=1)
        self._detail_row(detail, 0, "Thời gian lỗi", self.detail_time_var, "Vị trí", self.detail_location_var)
        self._detail_row(detail, 1, "Giờ xác nhận", self.detail_confirm_time_var, "Đường dẫn", self.detail_path_var)

        detail_actions = tk.Frame(detail, bg="#F6F9FC")
        detail_actions.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(7, 0))
        tk.Label(
            detail_actions, text="Nhấp đúp vào ảnh để mở file gốc",
            font=("Segoe UI", 8), fg=COLORS["muted"], bg="#F6F9FC"
        ).pack(side="left")
        ttk.Button(
            detail_actions, text="MỞ THƯ MỤC", command=self.open_selected_folder, style="Compact.TButton"
        ).pack(side="right")

    def _mini_info(self, parent, column, title, variable):
        box = tk.Frame(parent, bg="#F6F9FC", padx=5)
        box.grid(row=0, column=column, sticky="nsew")
        tk.Label(
            box, text=title, font=("Segoe UI", 8, "bold"), fg=COLORS["muted"], bg="#F6F9FC"
        ).pack(anchor="w")
        tk.Label(
            box, textvariable=variable, font=("Segoe UI", 10, "bold"),
            fg=COLORS["navy"], bg="#F6F9FC", anchor="w"
        ).pack(anchor="w", fill="x")

    def _detail_row(self, parent, row, label1, var1, label2, var2):
        tk.Label(
            parent, text=label1 + ":", font=("Segoe UI", 8, "bold"),
            fg=COLORS["muted"], bg="#F6F9FC"
        ).grid(row=row, column=0, sticky="nw", padx=(0, 5), pady=2)
        tk.Label(
            parent, textvariable=var1, font=("Segoe UI", 9),
            fg=COLORS["text"], bg="#F6F9FC", anchor="w", justify="left",
            wraplength=220
        ).grid(row=row, column=1, sticky="ew", padx=(0, 12), pady=2)
        tk.Label(
            parent, text=label2 + ":", font=("Segoe UI", 8, "bold"),
            fg=COLORS["muted"], bg="#F6F9FC"
        ).grid(row=row, column=2, sticky="nw", padx=(0, 5), pady=2)
        tk.Label(
            parent, textvariable=var2, font=("Segoe UI", 9),
            fg=COLORS["text"], bg="#F6F9FC", anchor="w", justify="left",
            wraplength=260
        ).grid(row=row, column=3, sticky="ew", pady=2)

    def _build_statusbar(self):
        bar = tk.Frame(self, bg=COLORS["navy"], padx=12, pady=6)
        bar.grid(row=3, column=0, sticky="ew")
        bar.columnconfigure(3, weight=1)

        tk.Label(
            bar, text="Tổng bản ghi:", bg=COLORS["navy"], fg="#BFD2E3",
            font=("Segoe UI", 9)
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            bar, textvariable=self.total_var, bg=COLORS["navy"], fg="white",
            font=("Segoe UI", 9, "bold"), padx=5
        ).grid(row=0, column=1, sticky="w")
        tk.Label(
            bar, textvariable=self.selected_var, bg=COLORS["navy"], fg="#FFD580",
            font=("Segoe UI", 9, "bold"), padx=10
        ).grid(row=0, column=2, sticky="w")
        tk.Label(
            bar, textvariable=self.status_var, anchor="w", bg=COLORS["navy"], fg="#DCE7F1",
            font=("Segoe UI", 8)
        ).grid(row=0, column=3, sticky="ew")
        self.busy_label = tk.Label(
            bar, text="SẴN SÀNG", bg=COLORS["green"], fg="white", padx=12, pady=2,
            font=("Segoe UI", 9, "bold")
        )
        self.busy_label.grid(row=0, column=4, sticky="e")

    def _set_busy(self, busy, text=None):
        self.busy = bool(busy)
        if busy:
            self.busy_label.configure(text=text or "ĐANG XỬ LÝ...", bg=COLORS["orange"])
        else:
            self.busy_label.configure(text=text or "SẴN SÀNG", bg=COLORS["green"])

    def _reset_and_refresh(self):
        self.search_var.set("")
        self.date_from_var.set("")
        self.date_to_var.set("")
        self.pc_var.set("Tất cả")
        self.level_var.set("Tất cả")
        self.confirm_filter_var.set("Tất cả")
        self.refresh()

    # Tuong thich voi ten function cu neu co noi goi lai.
    clear_filters = _reset_and_refresh

    @staticmethod
    def _unique_text(values):
        result = []
        seen = set()
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result

    @staticmethod
    def _format_cam_vt(cam, vitri):
        return f"CAM{cam}_VT{vitri}"

    def _group_rows(self, rows):
        """Gop cac NG event cung cycle + PC + CAM + VT thanh mot dong hien thi.

        DB van giu tung TAG/POINT rieng de bao toan lich su chi tiet. UI chi gom o
        lop hien thi, vi vay xoa mot dong gom se xoa tat ca event_id cua nhom do.
        """
        buckets = {}
        for row in rows:
            key = (
                str(row.get("cycle_id") or ""),
                str(row.get("barcode") or ""),
                int(row.get("check_no") or 0),
                str(row.get("pc") or ""),
                int(row.get("cam") or 0),
                int(row.get("vitri") or 0),
            )
            buckets.setdefault(key, []).append(row)

        groups = []
        for key, members in buckets.items():
            # Sort theo POINT de TAG hien thi on dinh: D1, D2, D3...
            members = sorted(
                members,
                key=lambda r: (int(r.get("point") or 0), int(r.get("id") or 0)),
            )
            first = members[0]
            event_ids = [int(r["id"]) for r in members]
            tags = self._unique_text(
                (r.get("tag") or r.get("name") or f"D{r.get('point')}") for r in members
            )
            names = self._unique_text(r.get("name") for r in members)
            levels = sorted({int(r.get("level") or 0) for r in members})
            points = sorted({int(r.get("point") or 0) for r in members})
            created_values = [str(r.get("created_at") or "").strip() for r in members]
            created_values = [v for v in created_values if v]
            confirm_values = [str(r.get("ng_confirmed_at") or "").strip() for r in members]
            confirm_values = [v for v in confirm_values if v]
            confirmed_by_values = self._unique_text(r.get("ng_confirmed_by") for r in members)
            image_paths = self._unique_text(r.get("image_path") for r in members)

            cam = int(first.get("cam") or 0)
            vitri = int(first.get("vitri") or 0)
            group = dict(first)
            group.update({
                "event_ids": event_ids,
                "event_count": len(event_ids),
                "tag": ", ".join(tags) if tags else "--",
                "tags": tags,
                "names": names,
                "levels": levels,
                "level": ", ".join(str(v) for v in levels) if levels else "0",
                "points": points,
                "point_text": ", ".join(f"D{p}" for p in points),
                "created_at": min(created_values) if created_values else "",
                "ng_confirmed_by": ", ".join(confirmed_by_values),
                "ng_confirmed_at": min(confirm_values) if confirm_values else "",
                "repair_status": "ĐÃ SỬA" if any(r.get("repair_status") == "ĐÃ SỬA" for r in members) else "CHƯA SỬA",
                "image_path": image_paths[0] if image_paths else "",
                "cam_vt": self._format_cam_vt(cam, vitri),
            })
            groups.append(group)

        groups.sort(
            key=lambda g: (str(g.get("created_at") or ""), max(g.get("event_ids") or [0])),
            reverse=True,
        )
        return groups

    def _apply_local_filters(self, groups):
        search = self.search_var.get().strip().casefold()
        confirm_filter = self.confirm_filter_var.get().strip()
        level_filter = self.level_var.get().strip()
        filtered = []
        for group in groups:
            confirmed = bool(
                str(group.get("ng_confirmed_by") or "").strip() or group.get("ng_confirmed_at")
            )
            if confirm_filter == "Đã xác nhận" and not confirmed:
                continue
            if confirm_filter == "Chưa xác nhận" and confirmed:
                continue
            if level_filter and level_filter != "Tất cả":
                try:
                    wanted_level = int(level_filter)
                except ValueError:
                    wanted_level = None
                if wanted_level is not None and wanted_level not in set(group.get("levels") or []):
                    continue
            if search:
                haystack = " ".join([
                    str(group.get("barcode") or ""),
                    str(group.get("tag") or ""),
                    " ".join(group.get("names") or []),
                    str(group.get("ng_confirmed_by") or ""),
                    str(group.get("cam_vt") or ""),
                    str(group.get("pc") or ""),
                ]).casefold()
                if search not in haystack:
                    continue
            filtered.append(group)
        return filtered

    def refresh(self):
        if self.busy:
            return
        try:
            # LEVEL/TAG/search duoc loc SAU khi group de mot nhom CAM/VT khi match
            # van hien day du tat ca TAG cua camera do.
            rows = query_ng_events(
                barcode="",
                date_from=self.date_from_var.get(),
                date_to=self.date_to_var.get(),
                pc=("Tat ca" if self.pc_var.get() == "Tất cả" else self.pc_var.get()),
                tag="",
                level="Tat ca",
            )
            groups = self._group_rows(rows)
            groups = self._apply_local_filters(groups)
        except Exception as exc:
            messagebox.showerror("Lỗi đọc DB", str(exc), parent=self)
            return

        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.rows_by_iid.clear()
        self.filtered_groups = list(groups)

        total_event_count = 0
        for index, group in enumerate(groups):
            iid = f"group_{index}"
            self.rows_by_iid[iid] = group
            total_event_count += int(group.get("event_count") or 0)
            is_confirmed = bool(
                str(group.get("ng_confirmed_by") or "").strip() or group.get("ng_confirmed_at")
            )
            row_tags = []
            if index % 2:
                row_tags.append("alt")
            row_tags.append("confirmed" if is_confirmed else "pending")
            self.tree.insert(
                "", tk.END, iid=iid, tags=tuple(row_tags),
                values=(
                    group.get("created_at", ""),
                    group.get("barcode", ""),
                    group.get("check_no", ""),
                    group.get("pc", ""),
                    group.get("cam_vt", ""),
                    group.get("tag", ""),
                    group.get("level", "0"),
                    group.get("ng_confirmed_by", "") or "Chưa xác nhận",
                    self._format_confirm_time(group.get("ng_confirmed_at", "")),
                    group.get("repair_status", "CHƯA SỬA"),
                ),
            )

        total = len(groups)
        self.total_var.set(str(total))
        self.selected_var.set("Đã chọn: 0 nhóm")
        self.table_count_label.configure(text=f"{total} CAM/VT • {total_event_count} TAG")
        self.status_var.set(f"DB: {DATABASE_PATH}")
        self._clear_detail()

    @staticmethod
    def _format_confirm_time(value):
        text = str(value or "").strip()
        if not text:
            return "--"
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%d/%m/%Y %H:%M:%S")
        except ValueError:
            return text

    def _sort_tree(self, column, reverse):
        items = [(self.tree.set(iid, column), iid) for iid in self.tree.get_children("")]

        def key(item):
            value = item[0]
            try:
                return (0, float(value))
            except (TypeError, ValueError):
                return (1, str(value).casefold())

        items.sort(key=key, reverse=reverse)
        for index, (_value, iid) in enumerate(items):
            self.tree.move(iid, "", index)
        self.tree.heading(column, command=lambda c=column: self._sort_tree(c, not reverse))

    def select_all_visible(self):
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children)
            if children:
                self.tree.focus(children[0])
            self.on_select()

    def clear_selection(self):
        selected = self.tree.selection()
        if selected:
            self.tree.selection_remove(selected)
        self.selected_var.set("Đã chọn: 0 nhóm")
        self._clear_detail()

    def _selected_rows(self):
        return [
            self.rows_by_iid[iid]
            for iid in self.tree.selection()
            if iid in self.rows_by_iid
        ]

    def _selected_row(self):
        focus = self.tree.focus()
        if focus in self.rows_by_iid:
            return self.rows_by_iid[focus]
        rows = self._selected_rows()
        return rows[0] if rows else None

    def on_select(self, _event=None):
        rows = self._selected_rows()
        self.selected_var.set(f"Đã chọn: {len(rows)} nhóm")
        row = self._selected_row()
        if not row:
            self._clear_detail()
            return

        confirmed_by = str(row.get("ng_confirmed_by") or "").strip() or "Chưa xác nhận"
        confirmed_at = str(row.get("ng_confirmed_at") or "").strip() or "--"
        tag = row.get("tag") or row.get("name") or "--"

        self.detail_barcode_var.set(str(row.get("barcode") or "--"))
        points_text = str(row.get("point_text") or "").strip() or "--"
        self.detail_location_var.set(
            f"{row.get('pc') or '--'} | {row.get('cam_vt') or self._format_cam_vt(row.get('cam'), row.get('vitri'))} | {points_text}"
        )
        self.detail_tag_var.set(str(tag))
        self.detail_level_var.set(str(row.get("level", 0)))
        self.detail_time_var.set(str(row.get("created_at") or "--"))
        self.detail_confirm_var.set(confirmed_by)
        self.detail_confirm_time_var.set(self._format_confirm_time(confirmed_at))
        self.detail_path_var.set(str(row.get("image_path") or "(chưa có)"))

        # Yeu cau UI: click dong loi la hien anh ngay, khong can bam Mo anh.
        self.show_image(row.get("image_path") or "")

    def _clear_detail(self):
        self.detail_barcode_var.set("--")
        self.detail_location_var.set("--")
        self.detail_tag_var.set("--")
        self.detail_level_var.set("--")
        self.detail_time_var.set("--")
        self.detail_confirm_var.set("Chưa xác nhận")
        self.detail_confirm_time_var.set("--")
        self.detail_path_var.set("")
        if hasattr(self, "preview"):
            self.preview.configure(image="", text="CHỌN MỘT LỖI TRONG BẢNG\nĐỂ XEM ẢNH")
        self.preview_imgtk = None

    def _on_preview_resize(self, _event=None):
        row = self._selected_row()
        if not row or not row.get("image_path"):
            return
        if self._preview_after:
            try:
                self.after_cancel(self._preview_after)
            except tk.TclError:
                pass
        self._preview_after = self.after(
            180, lambda: self.show_image(row.get("image_path") or "")
        )

    def show_image(self, path):
        if not path or not os.path.exists(path):
            self.preview.configure(image="", text="KHÔNG TÌM THẤY ẢNH ERROR ARCHIVE")
            self.preview_imgtk = None
            return
        try:
            with Image.open(path) as source:
                img = source.convert("RGB")
            max_w = max(self.preview.winfo_width() - 14, 340)
            max_h = max(self.preview.winfo_height() - 14, 280)
            img.thumbnail((max_w, max_h), Image.LANCZOS)
            self.preview_imgtk = ImageTk.PhotoImage(img)
            self.preview.configure(image=self.preview_imgtk, text="")
        except Exception as exc:
            self.preview.configure(image="", text=f"LỖI MỞ ẢNH:\n{exc}")
            self.preview_imgtk = None

    def open_selected_image(self):
        row = self._selected_row()
        path = (row or {}).get("image_path") or ""
        if not path or not os.path.exists(path):
            messagebox.showwarning("ErrorArchive", "Không tìm thấy file ảnh.", parent=self)
            return
        try:
            os.startfile(path)
        except AttributeError:
            messagebox.showinfo("ErrorArchive", f"Ảnh: {path}", parent=self)
        except Exception as exc:
            messagebox.showerror("ErrorArchive", str(exc), parent=self)

    def open_selected_folder(self):
        row = self._selected_row()
        path = (row or {}).get("image_path") or ""
        folder = os.path.dirname(path) if path else ""
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning("ErrorArchive", "Không tìm thấy thư mục ảnh.", parent=self)
            return
        try:
            os.startfile(folder)
        except AttributeError:
            messagebox.showinfo("ErrorArchive", f"Thư mục: {folder}", parent=self)
        except Exception as exc:
            messagebox.showerror("ErrorArchive", str(exc), parent=self)

    def _run_background(self, label, func, on_success):
        if self.busy:
            messagebox.showinfo(
                "Đang xử lý", "Vui lòng chờ thao tác hiện tại hoàn tất.", parent=self
            )
            return
        self._set_busy(True, label)

        def worker():
            try:
                result = func()
            except Exception as exc:
                self.after(0, lambda e=exc: self._background_failed(e))
                return
            self.after(0, lambda r=result: self._background_done(r, on_success))

        threading.Thread(target=worker, daemon=True).start()

    def _background_failed(self, exc):
        self._set_busy(False)
        messagebox.showerror("Thao tác thất bại", str(exc), parent=self)

    def _background_done(self, result, on_success):
        self._set_busy(False)
        on_success(result)

    @staticmethod
    def _result_text(result):
        lines = [
            f"Đã xóa {result.get('deleted_rows', 0)} dòng lỗi.",
            f"Đã xóa {result.get('deleted_files', 0)} file ảnh không còn được sử dụng.",
        ]
        if result.get("missing_files"):
            lines.append(f"Có {result.get('missing_files')} file ảnh đã không còn trên ổ đĩa.")
        failed = result.get("failed_files") or []
        if failed:
            lines.append(f"Có {len(failed)} file không xóa được; DB vẫn đã xóa thành công.")
        return "\n".join(lines)

    @staticmethod
    def _txt_value(value):
        return str(value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()

    def _visible_groups_in_tree_order(self):
        result = []
        for iid in self.tree.get_children(""):
            row = self.rows_by_iid.get(iid)
            if row is not None:
                result.append(row)
        return result

    def export_filtered_txt(self):
        """Xuat dung du lieu dang hien thi sau bo loc, moi CAM/VT la mot dong."""
        groups = self._visible_groups_in_tree_order()
        if not groups:
            messagebox.showwarning(
                "Không có dữ liệu",
                "Bộ lọc hiện tại không có dữ liệu để xuất.",
                parent=self,
            )
            return

        default_name = f"ErrorArchive_{datetime.now():%Y-%m-%d_%H%M%S}.txt"
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Xuất ErrorArchive theo bộ lọc hiện tại",
            defaultextension=".txt",
            initialfile=default_name,
            filetypes=(("Text file", "*.txt"), ("Tất cả file", "*.*")),
        )
        if not path:
            return

        headers = (
            "Barcode",
            "Giờ lỗi",
            "Giờ xác nhận",
            "Người xác nhận",
            "CAM_VT",
            "Vị trí lỗi (TAG)",
            "XÁC NHẬN SỬA",
        )
        try:
            # utf-8-sig de Notepad/Excel Windows nhan dung tieng Viet.
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write("\t".join(headers) + "\n")
                for group in groups:
                    values = (
                        group.get("barcode", ""),
                        group.get("created_at", ""),
                        group.get("ng_confirmed_at", ""),
                        group.get("ng_confirmed_by", "") or "Chưa xác nhận",
                        group.get("cam_vt", ""),
                        group.get("tag", ""),
                        group.get("repair_status", "CHƯA SỬA"),
                    )
                    f.write("\t".join(self._txt_value(v) for v in values) + "\n")
        except Exception as exc:
            messagebox.showerror(
                "Xuất file thất bại",
                f"Không ghi được file TXT:\n{exc}",
                parent=self,
            )
            return

        messagebox.showinfo(
            "Xuất file thành công",
            f"Đã xuất {len(groups)} dòng CAM/VT theo bộ lọc hiện tại.\n\n{path}",
            parent=self,
        )

    def delete_selected(self):
        rows = self._selected_rows()
        if not rows:
            messagebox.showwarning(
                "Chưa chọn lỗi",
                "Chọn một hoặc nhiều nhóm CAM/VT trước khi xóa.\nCó thể dùng Ctrl/Shift để chọn nhiều dòng.",
                parent=self,
            )
            return
        ids = []
        for row in rows:
            ids.extend(row.get("event_ids") or [])
        ids = sorted(set(int(v) for v in ids))
        unique_images = len({row.get("image_path") for row in rows if row.get("image_path")})
        total_tags = sum(int(row.get("event_count") or 0) for row in rows)
        if not messagebox.askyesno(
            "Xác nhận xóa",
            f"Bạn đang chọn {len(rows)} nhóm CAM/VT, gồm {total_tags} TAG lỗi "
            f"({unique_images} ảnh liên quan).\n\n"
            "Xóa một dòng gộp sẽ xóa toàn bộ TAG thuộc CAM/VT của dòng đó.\n"
            "Chỉ file ảnh không còn lỗi nào khác tham chiếu mới bị xóa.\n"
            "Thống kê OK/NG chính không bị xóa.\n\nTiếp tục?",
            icon="warning",
            parent=self,
        ):
            return
        self._run_background(
            "ĐANG XÓA...",
            lambda: delete_ng_events_by_ids(ids),
            self._after_manual_delete,
        )

    def delete_by_date(self, date_text):
        try:
            datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError:
            messagebox.showerror(
                "Ngày không hợp lệ", f"Ngày phải theo YYYY-MM-DD: {date_text!r}", parent=self
            )
            return
        if not messagebox.askyesno(
            "Xóa theo ngày",
            f"Xóa toàn bộ lỗi ErrorArchive của ngày {date_text}?\n\n"
            "Thao tác này có thể xóa nhiều dòng và nhiều ảnh.\n"
            "Thống kê OK/NG chính vẫn được giữ.",
            icon="warning",
            parent=self,
        ):
            return
        self._run_background(
            "ĐANG XÓA THEO NGÀY...",
            lambda: delete_ng_events_by_date(date_text),
            self._after_manual_delete,
        )

    def delete_by_month(self, month_text):
        try:
            datetime.strptime(month_text, "%Y-%m")
        except ValueError:
            messagebox.showerror(
                "Tháng không hợp lệ", f"Tháng phải theo YYYY-MM: {month_text!r}", parent=self
            )
            return
        if not messagebox.askyesno(
            "Xóa theo tháng",
            f"Xóa TOÀN BỘ lỗi ErrorArchive của tháng {month_text}?\n\n"
            "Đây là thao tác xóa hàng loạt. Thống kê OK/NG chính vẫn được giữ.",
            icon="warning",
            parent=self,
        ):
            return
        self._run_background(
            "ĐANG XÓA THEO THÁNG...",
            lambda: delete_ng_events_by_month(month_text),
            self._after_manual_delete,
        )

    def _after_manual_delete(self, result):
        self.refresh()
        messagebox.showinfo("Đã xóa", self._result_text(result), parent=self)

    def _run_retention_cleanup_silent(self):
        # Luon dat lich lan tiep theo truoc. Neu DB/o dia loi tam thoi thi lan sau van tiep tuc.
        self.after(self.RETENTION_CHECK_MS, self._run_retention_cleanup_silent)
        if self.busy:
            return

        def done(result):
            deleted = int(result.get("deleted_rows", 0))
            files = int(result.get("deleted_files", 0))
            oldest = result.get("oldest_kept_date", "")
            self.retention_var.set(
                f"Giữ {ERROR_ARCHIVE_RETENTION_DAYS} ngày | Cũ nhất: {oldest or '--'}"
            )
            if deleted:
                self.refresh()
                self.status_var.set(
                    f"Tự dọn retention: đã xóa {deleted} lỗi / {files} ảnh quá hạn | DB: {DATABASE_PATH}"
                )

        self._run_background(
            "KIỂM TRA LƯU TRỮ...",
            lambda: cleanup_ng_events_retention(ERROR_ARCHIVE_RETENTION_DAYS),
            done,
        )


if __name__ == "__main__":
    ErrorArchiveManager().mainloop()
