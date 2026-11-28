import sqlite3
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path


DATABASE_PATH = Path(__file__).resolve().with_name("is1200_480.db")
STATION_NAME = "IS1200_480"
VALID_DB_STATUSES = {"PENDING", "OK", "NG", "ERROR"}
APPROVAL_OPERATORS = (
    "000000 - Admin",
)

APPROVAL_REASONS = (
    "NG ảo, LD đã kiểm tra OK",
    "NG ảo, SP đã kiểm tra OK",
    "NG ảo, QC đã kiểm tra OK",
    "Cụm đặt lệch, kiểm tra thực tế OK",
    "Lỗi sửa ngay, LD đã kiểm tra OK",
    "Lỗi sửa ngay, SP đã kiểm tra OK",
    "Lỗi sửa ngay, QC đã kiểm tra OK"
)


class ShiftSchedule:
    # Boundary theo yeu cau production; END la moc exclusive.
    DAY_START_HOUR = 7
    DAY_START_MINUTE = 30
    DAY_END_HOUR = 19
    DAY_END_MINUTE = 30
    NIGHT_START_HOUR = 19
    NIGHT_START_MINUTE = 30
    NIGHT_END_HOUR = 7
    NIGHT_END_MINUTE = 30

    @classmethod
    def get_shift_info(cls, now=None):
        """Xac dinh ca phu kin 24h, khong co ngoai_ca/khong co khoang trong."""
        now = now or datetime.now()
        current_time = now.time()
        day_start = dt_time(cls.DAY_START_HOUR, cls.DAY_START_MINUTE)
        night_start = dt_time(cls.NIGHT_START_HOUR, cls.NIGHT_START_MINUTE)

        if day_start <= current_time < night_start:
            production_date = now.date()
            shift = "ca_ngay"
        elif current_time >= night_start:
            production_date = now.date()
            shift = "ca_dem"
        else:
            # 00:00 -> 07:29:59 van thuoc ca dem bat dau tu ngay hom truoc.
            production_date = (now - timedelta(days=1)).date()
            shift = "ca_dem"

        return {
            "date": production_date.strftime("%Y-%m-%d"),
            "shift": shift,
        }


def format_db_timestamp(now=None):
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def get_shift_datetime_range(production_date, shift):
    try:
        base_date = datetime.strptime(production_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Ngay san xuat khong dung format YYYY-MM-DD: production_date={production_date!r}") from exc

    if shift == "ca_ngay":
        start_dt = datetime.combine(
            base_date,
            dt_time(ShiftSchedule.DAY_START_HOUR, ShiftSchedule.DAY_START_MINUTE),
        )
        end_exclusive = datetime.combine(
            base_date,
            dt_time(ShiftSchedule.NIGHT_START_HOUR, ShiftSchedule.NIGHT_START_MINUTE),
        )
    elif shift == "ca_dem":
        start_dt = datetime.combine(
            base_date,
            dt_time(ShiftSchedule.NIGHT_START_HOUR, ShiftSchedule.NIGHT_START_MINUTE),
        )
        end_exclusive = datetime.combine(
            base_date + timedelta(days=1),
            dt_time(ShiftSchedule.DAY_START_HOUR, ShiftSchedule.DAY_START_MINUTE),
        )
    else:
        raise ValueError(f"Ca loc khong hop le khi tinh khoang gio: shift={shift!r}")

    # DB timestamp chi luu den giay; de query <= end_dt khong an sang ca ke tiep.
    end_dt = end_exclusive - timedelta(seconds=1)
    return start_dt, end_dt


def get_db_connection():
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        raise RuntimeError(f"Khong mo duoc SQLite database: path={DATABASE_PATH}") from exc


def create_statistics_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS inspection_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            barcode TEXT NOT NULL,
            check_no INTEGER NOT NULL DEFAULT 1,
            station_name TEXT DEFAULT 'IS1200_480',
            shift TEXT,
            final_status TEXT NOT NULL DEFAULT 'PENDING',
            ng_confirmed_by TEXT DEFAULT '',
            ng_confirmed_at TEXT,
            approved_by TEXT DEFAULT '',
            approved_at TEXT,
            approved_reason TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_inspection_checks_barcode UNIQUE (barcode),
            CONSTRAINT ck_final_status CHECK (final_status IN ('PENDING', 'OK', 'NG', 'ERROR'))
        );

        CREATE INDEX IF NOT EXISTS idx_inspection_checks_barcode
        ON inspection_checks (barcode);

        CREATE INDEX IF NOT EXISTS idx_inspection_checks_created_shift_status
        ON inspection_checks (created_at, shift, final_status);

        CREATE INDEX IF NOT EXISTS idx_inspection_checks_final_status
        ON inspection_checks (final_status);

        CREATE TABLE IF NOT EXISTS inspection_ng_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id TEXT NOT NULL,
            barcode TEXT NOT NULL,
            check_no INTEGER NOT NULL DEFAULT 1,
            station_name TEXT DEFAULT 'IS1200_480',
            shift TEXT,
            pc TEXT NOT NULL,
            cam INTEGER NOT NULL,
            vitri INTEGER NOT NULL,
            point INTEGER NOT NULL,
            name TEXT DEFAULT '',
            tag TEXT DEFAULT '',
            level INTEGER NOT NULL DEFAULT 0,
            image_path TEXT DEFAULT '',
            ng_confirmed_by TEXT DEFAULT '',
            ng_confirmed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_inspection_ng_event UNIQUE (cycle_id, pc, cam, vitri, point)
        );

        CREATE INDEX IF NOT EXISTS idx_ng_events_barcode
        ON inspection_ng_events (barcode);

        CREATE INDEX IF NOT EXISTS idx_ng_events_created_at
        ON inspection_ng_events (created_at);

        CREATE INDEX IF NOT EXISTS idx_ng_events_tag_level
        ON inspection_ng_events (tag, level);

        CREATE INDEX IF NOT EXISTS idx_ng_events_pc_cam
        ON inspection_ng_events (pc, cam, vitri);
        """
    )

    # Migration cho DB da ton tai truoc khi co chuc nang quet the xac nhan NG.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(inspection_checks)").fetchall()}
    if "ng_confirmed_by" not in columns:
        conn.execute("ALTER TABLE inspection_checks ADD COLUMN ng_confirmed_by TEXT DEFAULT ''")
    if "ng_confirmed_at" not in columns:
        conn.execute("ALTER TABLE inspection_checks ADD COLUMN ng_confirmed_at TEXT")
    if 'repair_confirmed_at' not in columns:
        conn.execute('ALTER TABLE inspection_checks ADD COLUMN repair_confirmed_at TEXT')
    if 'repair_confirmed_by' not in columns:
        conn.execute("ALTER TABLE inspection_checks ADD COLUMN repair_confirmed_by TEXT DEFAULT ''")

    ng_columns = {row[1] for row in conn.execute("PRAGMA table_info(inspection_ng_events)").fetchall()}
    if "ng_confirmed_by" not in ng_columns:
        conn.execute("ALTER TABLE inspection_ng_events ADD COLUMN ng_confirmed_by TEXT DEFAULT ''")
    if "ng_confirmed_at" not in ng_columns:
        conn.execute("ALTER TABLE inspection_ng_events ADD COLUMN ng_confirmed_at TEXT")


    # Backfill du lieu cu neu inspection_checks hien tai van con dung barcode/check_no.
    conn.execute(
        """
        UPDATE inspection_ng_events
        SET
            ng_confirmed_by = COALESCE(
                NULLIF(ng_confirmed_by, ''),
                (
                    SELECT NULLIF(c.ng_confirmed_by, '')
                    FROM inspection_checks AS c
                    WHERE c.barcode = inspection_ng_events.barcode
                      AND c.check_no = inspection_ng_events.check_no
                )
            ),
            ng_confirmed_at = COALESCE(
                ng_confirmed_at,
                (
                    SELECT c.ng_confirmed_at
                    FROM inspection_checks AS c
                    WHERE c.barcode = inspection_ng_events.barcode
                      AND c.check_no = inspection_ng_events.check_no
                )
            )
        WHERE EXISTS (
            SELECT 1
            FROM inspection_checks AS c
            WHERE c.barcode = inspection_ng_events.barcode
              AND c.check_no = inspection_ng_events.check_no
              AND (NULLIF(c.ng_confirmed_by, '') IS NOT NULL OR c.ng_confirmed_at IS NOT NULL)
        )
        """
    )


def init_statistics_database():
    try:
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with get_db_connection() as conn:
            create_statistics_schema(conn)
            _ensure_is480_cycle_schema(conn)
    except Exception as exc:
        raise RuntimeError(f"Khoi tao database thong ke that bai: path={DATABASE_PATH}") from exc


def create_inspection_check(barcode):
    if not barcode:
        raise ValueError(f"Khong tao duoc luot check vi barcode rong: barcode={barcode!r}")

    now = datetime.now()
    now_text = format_db_timestamp(now)
    shift_info = ShiftSchedule.get_shift_info(now)

    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT COALESCE(check_no, 0) + 1 AS next_check_no
            FROM inspection_checks
            WHERE barcode = ?
            """,
            (barcode,),
        ).fetchone()
        check_no = int(row["next_check_no"]) if row is not None else 1
        conn.execute(
            """
            INSERT INTO inspection_checks (
                barcode,
                check_no,
                station_name,
                shift,
                final_status,
                ng_confirmed_by,
                ng_confirmed_at,
                approved_by,
                approved_at,
                approved_reason,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?)
            ON CONFLICT(barcode) DO UPDATE SET
                check_no = excluded.check_no,
                station_name = excluded.station_name,
                shift = excluded.shift,
                final_status = 'PENDING',
                ng_confirmed_by = '',
                ng_confirmed_at = NULL,
                repair_confirmed_by = '',
                repair_confirmed_at = NULL,
                approved_by = '',
                approved_at = NULL,
                approved_reason = '',
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (
                barcode,
                check_no,
                STATION_NAME,
                shift_info["shift"],
                "PENDING",
                "",
                "",
                "",
                now_text,
                now_text,
            ),
        )
        conn.commit()
        return check_no
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(
            f"Upsert luot check that bai: barcode={barcode!r}, check_no={locals().get('check_no')!r}, "
            f"path={DATABASE_PATH}"
        ) from exc
    finally:
        conn.close()


def update_final_status(barcode, final_status, check_no=None):
    if not barcode:
        raise ValueError(f"Thieu barcode khi update final_status: barcode={barcode!r}, check_no={check_no}")
    if final_status not in VALID_DB_STATUSES - {"PENDING"}:
        raise ValueError(
            f"final_status khong hop le: final_status={final_status!r}, "
            f"barcode={barcode!r}, check_no={check_no}"
        )

    now_text = format_db_timestamp()
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT check_no, final_status
            FROM inspection_checks
            WHERE barcode = ?
            """,
            (barcode,),
        ).fetchone()
        if row is None:
            raise LookupError(
                f"Khong tim thay row inspection_checks de update final_status: "
                f"barcode={barcode!r}, check_no={check_no}, final_status={final_status!r}, path={DATABASE_PATH}"
            )

        if check_no is not None and int(row['check_no']) != int(check_no):
            raise ValueError(f'check_no mismatch for {barcode}: expected={check_no}, got={row["check_no"]}')

        conn.execute(
            """
            UPDATE inspection_checks
            SET
                final_status = ?,
                updated_at = ?
            WHERE barcode = ?
            """,
            (
                final_status,
                now_text,
                barcode,
            ),
        )
        conn.commit()
        return final_status
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(
            f"Update final_status that bai: barcode={barcode!r}, "
            f"check_no={check_no}, final_status={final_status!r}, path={DATABASE_PATH}"
        ) from exc
    finally:
        conn.close()


def confirm_ng_check(barcode, confirmed_by, check_no=None):
    """Ghi nguoi/thoi gian xac nhan cho luot check NG hien tai.

    Chi cap nhat 2 truong ng_confirmed_by va ng_confirmed_at.
    Khong doi final_status NG thanh OK.
    """
    barcode = str(barcode or "").strip()
    confirmed_by = str(confirmed_by or "").strip()
    if not barcode:
        raise ValueError(f"Thieu barcode khi xac nhan NG: barcode={barcode!r}, check_no={check_no}")
    if not confirmed_by:
        raise ValueError(f"Thieu ten nguoi xac nhan NG: barcode={barcode!r}, check_no={check_no}")

    confirmed_at = format_db_timestamp()
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT check_no, final_status, ng_confirmed_by, ng_confirmed_at
            FROM inspection_checks
            WHERE barcode = ?
            """,
            (barcode,),
        ).fetchone()
        if row is None:
            raise LookupError(
                f"Khong tim thay row de xac nhan NG: barcode={barcode!r}, check_no={check_no}, path={DATABASE_PATH}"
            )
        if check_no is not None and int(row["check_no"]) != int(check_no):
            raise ValueError(
                f"check_no khong khop khi xac nhan NG: barcode={barcode!r}, "
                f"db_check_no={row['check_no']}, expected={check_no}, path={DATABASE_PATH}"
            )
        if row["final_status"] != "NG":
            raise ValueError(
                f"Chi xac nhan the cho row NG: barcode={barcode!r}, check_no={row['check_no']}, "
                f"final_status={row['final_status']!r}, path={DATABASE_PATH}"
            )
        if str(row["ng_confirmed_by"] or "").strip() or row["ng_confirmed_at"]:
            raise ValueError(
                f"Row NG da duoc xac nhan truoc do: barcode={barcode!r}, check_no={row['check_no']}, "
                f"by={row['ng_confirmed_by']!r}, at={row['ng_confirmed_at']!r}"
            )

        conn.execute(
            """
            UPDATE inspection_checks
            SET
                ng_confirmed_by = ?,
                ng_confirmed_at = ?
            WHERE barcode = ?
            """,
            (confirmed_by, confirmed_at, barcode),
        )

        # Luu cung 2 thong tin vao tung NG event cua dung lan check.
        # Nhu vay ErrorArchive van giu dung nguoi xac nhan ke ca khi barcode
        # sau nay duoc check lai va row inspection_checks bi upsert.
        event_check_no = int(row["check_no"])
        conn.execute(
            """
            UPDATE inspection_ng_events
            SET
                ng_confirmed_by = ?,
                ng_confirmed_at = ?
            WHERE barcode = ?
              AND check_no = ?
            """,
            (confirmed_by, confirmed_at, barcode, event_check_no),
        )
        conn.commit()
        return confirmed_at
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(
            f"Xac nhan NG that bai: barcode={barcode!r}, check_no={check_no}, "
            f"confirmed_by={confirmed_by!r}, path={DATABASE_PATH}"
        ) from exc
    finally:
        conn.close()


def query_approvable_ng_checks(production_date="", shift="Tat ca"):
    production_date = str(production_date).strip()
    shift = str(shift).strip()
    if production_date:
        try:
            datetime.strptime(production_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"Ngay approve khong dung format YYYY-MM-DD: production_date={production_date!r}") from exc
    if shift not in {"Tat ca", "ca_ngay", "ca_dem"}:
        raise ValueError(f"Ca approve khong hop le: shift={shift!r}")

    where_clauses = ["final_status = 'NG'"]
    params = []
    if production_date and shift != "Tat ca":
        start_dt, end_dt = get_shift_datetime_range(production_date, shift)
        where_clauses.append("created_at >= ?")
        where_clauses.append("created_at <= ?")
        where_clauses.append("shift = ?")
        params.extend((format_db_timestamp(start_dt), format_db_timestamp(end_dt), shift))
    elif production_date:
        where_clauses.append("created_at >= ?")
        where_clauses.append("created_at <= ?")
        params.extend((f"{production_date} 00:00:00", f"{production_date} 23:59:59"))
    elif shift != "Tat ca":
        where_clauses.append("shift = ?")
        params.append(shift)

    where_sql = "WHERE " + " AND ".join(where_clauses)
    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    id,
                    barcode,
                    check_no,
                    shift,
                    final_status,
                    ng_confirmed_by,
                    ng_confirmed_at,
                    approved_by,
                    approved_at,
                    approved_reason,
                    created_at,
                    updated_at
                FROM inspection_checks
                {where_sql}
                ORDER BY updated_at DESC, created_at DESC, id DESC
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]
    except Exception as exc:
        raise RuntimeError(
            f"Doc danh sach NG can approve that bai: "
            f"production_date={production_date!r}, shift={shift!r}, path={DATABASE_PATH}"
        ) from exc


def approve_ng_check_to_ok(check_id, approved_by, approved_reason):
    if not approved_by:
        raise ValueError(f"Thieu Operator khi approve: id={check_id}, path={DATABASE_PATH}")
    if not approved_reason:
        raise ValueError(f"Thieu ly do approve: id={check_id}, approved_by={approved_by!r}, path={DATABASE_PATH}")

    approved_at = format_db_timestamp()
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, barcode, final_status
            FROM inspection_checks
            WHERE id = ?
            """,
            (check_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Khong tim thay row approve: id={check_id}, path={DATABASE_PATH}")
        if row["final_status"] != "NG":
            raise ValueError(
                f"Chi approve duoc row NG: id={check_id}, barcode={row['barcode']!r}, "
                f"final_status={row['final_status']!r}, path={DATABASE_PATH}"
            )

        conn.execute(
            """
            UPDATE inspection_checks
            SET
                final_status = 'OK',
                approved_by = ?,
                approved_at = ?,
                approved_reason = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (approved_by, approved_at, approved_reason, approved_at, check_id),
        )
        conn.commit()
        return approved_at
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(
            f"Approve NG thanh OK that bai: id={check_id}, "
            f"approved_by={approved_by!r}, reason={approved_reason!r}, path={DATABASE_PATH}"
        ) from exc
    finally:
        conn.close()


def query_statistics_checks(barcode="", date_from="", date_to="", shift="Tat ca", final_status="Tat ca"):
    barcode = str(barcode).strip()
    date_from = str(date_from).strip()
    date_to = str(date_to).strip()
    shift = str(shift).strip()
    final_status = str(final_status).strip()

    for label, value in (("date_from", date_from), ("date_to", date_to)):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError(
                    f"Ngay loc khong dung format YYYY-MM-DD: {label}={value!r}"
                ) from exc
    if date_from and date_to and date_from > date_to:
        raise ValueError(f"Khoang ngay khong hop le: date_from={date_from!r}, date_to={date_to!r}")

    if shift not in {"Tat ca", "ca_ngay", "ca_dem"}:
        raise ValueError(f"Ca loc khong hop le: shift={shift!r}")
    if final_status not in {"Tat ca", "PENDING", "OK", "NG", "ERROR"}:
        raise ValueError(f"Status loc khong hop le: final_status={final_status!r}")

    where_clauses = []
    params = []
    if barcode:
        where_clauses.append("barcode LIKE ?")
        params.append(f"%{barcode}%")
    if date_from:
        if shift != "Tat ca":
            start_dt, _end_dt = get_shift_datetime_range(date_from, shift)
            where_clauses.append("created_at >= ?")
            params.append(format_db_timestamp(start_dt))
        else:
            where_clauses.append("created_at >= ?")
            params.append(f"{date_from} 00:00:00")
    if date_to:
        if shift != "Tat ca":
            _start_dt, end_dt = get_shift_datetime_range(date_to, shift)
            where_clauses.append("created_at <= ?")
            params.append(format_db_timestamp(end_dt))
        else:
            where_clauses.append("created_at <= ?")
            params.append(f"{date_to} 23:59:59")
    if shift != "Tat ca":
        where_clauses.append("shift = ?")
        params.append(shift)
    if final_status != "Tat ca":
        where_clauses.append("final_status = ?")
        params.append(final_status)

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    barcode,
                    check_no,
                    shift,
                    final_status,
                    ng_confirmed_by,
                    ng_confirmed_at,
                    approved_by,
                    approved_at,
                    approved_reason,
                    created_at,
                    updated_at
                FROM inspection_checks
                {where_sql}
                ORDER BY created_at DESC, check_no DESC, id DESC
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]
    except Exception as exc:
        raise RuntimeError(
            f"Tim kiem thong ke that bai: barcode={barcode!r}, date_from={date_from!r}, "
            f"date_to={date_to!r}, shift={shift!r}, final_status={final_status!r}, path={DATABASE_PATH}"
        ) from exc


def get_recent_production_dates(days=10):
    today = datetime.now().date()
    return [""] + [
        (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(days)
    ]


def query_current_shift_summary():
    now = datetime.now()
    shift_info = ShiftSchedule.get_shift_info(now)
    production_date = shift_info["date"]
    shift = shift_info["shift"]
    start_dt, end_dt = get_shift_datetime_range(production_date, shift)

    start_text = format_db_timestamp(start_dt)
    end_text = format_db_timestamp(end_dt)
    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT final_status, COUNT(*) AS total
                FROM inspection_checks
                WHERE created_at >= ?
                  AND created_at <= ?
                  AND shift = ?
                  AND final_status IN ('OK', 'NG')
                GROUP BY final_status
                """,
                (start_text, end_text, shift),
            ).fetchall()
    except Exception as exc:
        raise RuntimeError(
            f"Doc so luong OK/NG ca hien tai that bai: "
            f"production_date={production_date!r}, shift={shift!r}, "
            f"start={start_text!r}, end={end_text!r}, path={DATABASE_PATH}"
        ) from exc

    totals = {"OK": 0, "NG": 0}
    for row in rows:
        totals[row["final_status"]] = int(row["total"])
    return production_date, shift, totals["OK"], totals["NG"]


# ---------------------------------------------------------------------------
# Chi tiet loi NG# ---------------------------------------------------------------------------
# Chi tiet loi NG / ErrorArchive
# ---------------------------------------------------------------------------

def insert_ng_events(events):
    """Ghi danh sach ROI NG cua mot/nhieu camera.

    Moi ROI NG la mot row. Nhieu ROI tren cung camera co the cung image_path
    (mot anh ErrorArchive full-size chi ve cac khung do NG).
    Ham idempotent theo (cycle_id, pc, cam, vitri, point).
    """
    events = list(events or [])
    if not events:
        return 0

    now_text = format_db_timestamp()
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        count = 0
        for event in events:
            cycle_id = str(event.get("cycle_id") or "").strip()
            barcode = str(event.get("barcode") or "").strip()
            pc = str(event.get("pc") or "").strip()
            if not cycle_id or not barcode or not pc:
                raise ValueError(
                    f"Thieu du lieu NG event: cycle_id={cycle_id!r}, barcode={barcode!r}, pc={pc!r}"
                )
            try:
                level = int(event.get("level", 0) or 0)
            except (TypeError, ValueError):
                level = 0
            check_no = int(event.get("check_no") or 1)
            confirmed_by = str(event.get("ng_confirmed_by") or "").strip()
            confirmed_at = event.get("ng_confirmed_at") or None
            if not confirmed_by and not confirmed_at:
                check_row = conn.execute(
                    """
                    SELECT ng_confirmed_by, ng_confirmed_at
                    FROM inspection_checks
                    WHERE barcode = ? AND check_no = ?
                    """,
                    (barcode, check_no),
                ).fetchone()
                if check_row is not None:
                    confirmed_by = str(check_row["ng_confirmed_by"] or "").strip()
                    confirmed_at = check_row["ng_confirmed_at"]

            created_at_text = str(event.get("created_at") or now_text)
            event_shift = str(event.get("shift") or "").strip()
            if not event_shift:
                try:
                    created_dt = datetime.strptime(created_at_text, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    created_dt = datetime.now()
                event_shift = ShiftSchedule.get_shift_info(created_dt)["shift"]

            conn.execute(
                """
                INSERT INTO inspection_ng_events (
                    cycle_id, barcode, check_no, station_name, shift,
                    pc, cam, vitri, point, name, tag, level, image_path,
                    ng_confirmed_by, ng_confirmed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id, pc, cam, vitri, point) DO UPDATE SET
                    barcode = excluded.barcode,
                    check_no = excluded.check_no,
                    station_name = excluded.station_name,
                    shift = excluded.shift,
                    name = excluded.name,
                    tag = excluded.tag,
                    level = excluded.level,
                    image_path = excluded.image_path,
                    ng_confirmed_by = CASE
                        WHEN excluded.ng_confirmed_by <> '' THEN excluded.ng_confirmed_by
                        ELSE inspection_ng_events.ng_confirmed_by
                    END,
                    ng_confirmed_at = COALESCE(excluded.ng_confirmed_at, inspection_ng_events.ng_confirmed_at),
                    created_at = excluded.created_at
                """,
                (
                    cycle_id,
                    barcode,
                    check_no,
                    str(event.get("station_name") or STATION_NAME),
                    event_shift,
                    pc,
                    int(event.get("cam") or 0),
                    int(event.get("vitri") or 0),
                    int(event.get("point") or 0),
                    str(event.get("name") or ""),
                    str(event.get("tag") or event.get("name") or ""),
                    level,
                    str(event.get("image_path") or ""),
                    confirmed_by,
                    confirmed_at,
                    created_at_text,
                ),
            )
            count += 1
        conn.commit()
        return count
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(
            f"Ghi inspection_ng_events that bai: count={len(events)}, path={DATABASE_PATH}"
        ) from exc
    finally:
        conn.close()


def query_ng_events(barcode="", date_from="", date_to="", pc="Tat ca", tag="", level="Tat ca"):
    """Tra ve danh sach loi NG de chuong trinh ErrorArchive Manager loc/xem."""
    barcode = str(barcode).strip()
    date_from = str(date_from).strip()
    date_to = str(date_to).strip()
    pc = str(pc).strip()
    tag = str(tag).strip()
    level_text = str(level).strip()

    for label, value in (("date_from", date_from), ("date_to", date_to)):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError(f"Ngay loc NG khong dung YYYY-MM-DD: {label}={value!r}") from exc
    if date_from and date_to and date_from > date_to:
        raise ValueError(f"Khoang ngay NG khong hop le: {date_from!r} > {date_to!r}")
    if pc not in {"Tat ca", "PC1", "PC2", "PC3"}:
        raise ValueError(f"PC loc NG khong hop le: {pc!r}")

    where = []
    params = []
    if barcode:
        where.append("barcode LIKE ?")
        params.append(f"%{barcode}%")
    if date_from:
        where.append("created_at >= ?")
        params.append(f"{date_from} 00:00:00")
    if date_to:
        where.append("created_at <= ?")
        params.append(f"{date_to} 23:59:59")
    if pc != "Tat ca":
        where.append("pc = ?")
        params.append(pc)
    if tag:
        where.append("tag LIKE ?")
        params.append(f"%{tag}%")
    if level_text and level_text != "Tat ca":
        try:
            level_value = int(level_text)
        except ValueError as exc:
            raise ValueError(f"LEVEL phai la so hoac 'Tat ca': level={level_text!r}") from exc
        where.append("level = ?")
        params.append(level_value)

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT id, cycle_id, barcode, check_no, shift, pc, cam, vitri,
                       point, name, tag, level, image_path,
                       ng_confirmed_by, ng_confirmed_at, created_at
                FROM inspection_ng_events
                {where_sql}
                ORDER BY created_at DESC, id DESC
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]
    except Exception as exc:
        raise RuntimeError(
            f"Doc inspection_ng_events that bai: barcode={barcode!r}, pc={pc!r}, "
            f"tag={tag!r}, level={level_text!r}, path={DATABASE_PATH}"
        ) from exc

# ---------------------------------------------------------------------------
# Xoa / retention ErrorArchive
# Chi xoa inspection_ng_events + file ErrorArchive khong con row tham chieu.
# KHONG xoa inspection_checks de giu lich su thong ke OK/NG cua day chuyen.
# ---------------------------------------------------------------------------

ERROR_ARCHIVE_RETENTION_DAYS = 10


def _normalize_event_ids(event_ids):
    ids = []
    seen = set()
    for value in event_ids or []:
        try:
            event_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"NG event id khong hop le: {value!r}") from exc
        if event_id <= 0:
            raise ValueError(f"NG event id phai > 0: {event_id}")
        if event_id not in seen:
            seen.add(event_id)
            ids.append(event_id)
    return ids


def _delete_orphan_image_files(paths):
    deleted_files = 0
    missing_files = 0
    failed_files = []
    removed_dirs = 0

    for raw_path in sorted({str(p or "").strip() for p in paths if str(p or "").strip()}):
        try:
            if not Path(raw_path).exists():
                missing_files += 1
                continue
            Path(raw_path).unlink()
            deleted_files += 1

            # Don dep cac thu muc con rong, dung lai truoc/den thu muc ErrorArchive.
            parent = Path(raw_path).parent
            for _ in range(6):
                if not parent.exists() or not parent.is_dir():
                    break
                name = parent.name.lower()
                if name == "errorarchive":
                    break
                try:
                    parent.rmdir()
                    removed_dirs += 1
                except OSError:
                    break
                parent = parent.parent
        except Exception as exc:
            failed_files.append(f"{raw_path}: {exc}")

    return {
        "deleted_files": deleted_files,
        "missing_files": missing_files,
        "failed_files": failed_files,
        "removed_dirs": removed_dirs,
    }


def delete_ng_events_by_ids(event_ids):
    """Xoa cac NG event theo id va chi xoa anh neu khong con event nao dung anh do."""
    ids = _normalize_event_ids(event_ids)
    if not ids:
        return {
            "deleted_rows": 0,
            "deleted_files": 0,
            "missing_files": 0,
            "failed_files": [],
            "removed_dirs": 0,
        }

    placeholders = ",".join("?" for _ in ids)
    conn = get_db_connection()
    orphan_paths = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"SELECT id, image_path FROM inspection_ng_events WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        existing_ids = [int(row["id"]) for row in rows]
        candidate_paths = {str(row["image_path"] or "").strip() for row in rows if str(row["image_path"] or "").strip()}

        if existing_ids:
            existing_placeholders = ",".join("?" for _ in existing_ids)
            conn.execute(
                f"DELETE FROM inspection_ng_events WHERE id IN ({existing_placeholders})",
                existing_ids,
            )

        for path in candidate_paths:
            remaining = conn.execute(
                "SELECT 1 FROM inspection_ng_events WHERE image_path = ? LIMIT 1",
                (path,),
            ).fetchone()
            if remaining is None:
                orphan_paths.append(path)

        conn.commit()
        deleted_rows = len(existing_ids)
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(
            f"Xoa inspection_ng_events theo id that bai: ids={ids!r}, path={DATABASE_PATH}"
        ) from exc
    finally:
        conn.close()

    file_result = _delete_orphan_image_files(orphan_paths)
    return {"deleted_rows": deleted_rows, **file_result}


def _event_ids_for_range(start_text, end_text_exclusive):
    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT id
                FROM inspection_ng_events
                WHERE created_at >= ? AND created_at < ?
                ORDER BY id
                """,
                (start_text, end_text_exclusive),
            ).fetchall()
        return [int(row["id"]) for row in rows]
    except Exception as exc:
        raise RuntimeError(
            f"Tim NG event de xoa that bai: start={start_text!r}, end={end_text_exclusive!r}, path={DATABASE_PATH}"
        ) from exc


def delete_ng_events_by_date(date_text):
    date_text = str(date_text or "").strip()
    try:
        selected = datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Ngay xoa phai dung YYYY-MM-DD: {date_text!r}") from exc
    next_day = selected + timedelta(days=1)
    ids = _event_ids_for_range(
        f"{selected:%Y-%m-%d} 00:00:00",
        f"{next_day:%Y-%m-%d} 00:00:00",
    )
    result = delete_ng_events_by_ids(ids)
    result["scope"] = f"date:{selected:%Y-%m-%d}"
    return result


def delete_ng_events_by_month(month_text):
    month_text = str(month_text or "").strip()
    try:
        first = datetime.strptime(month_text, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise ValueError(f"Thang xoa phai dung YYYY-MM: {month_text!r}") from exc
    if first.month == 12:
        next_month = first.replace(year=first.year + 1, month=1)
    else:
        next_month = first.replace(month=first.month + 1)
    ids = _event_ids_for_range(
        f"{first:%Y-%m-%d} 00:00:00",
        f"{next_month:%Y-%m-%d} 00:00:00",
    )
    result = delete_ng_events_by_ids(ids)
    result["scope"] = f"month:{first:%Y-%m}"
    return result


def cleanup_ng_events_retention(retention_days=ERROR_ARCHIVE_RETENTION_DAYS, now=None):
    """Giu 10 ngay lich gan nhat (tinh ca hom nay), xoa vao ngay thu 11.

    Vi du retention_days=10: hom nay + 9 ngay truoc duoc giu; moi event
    truoc 00:00 cua ngay (hom nay - 9 ngay) se bi xoa.
    """
    try:
        retention_days = int(retention_days)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"retention_days khong hop le: {retention_days!r}") from exc
    if retention_days < 1:
        raise ValueError(f"retention_days phai >= 1: {retention_days}")

    today = (now or datetime.now()).date()
    oldest_kept_date = today - timedelta(days=retention_days - 1)
    cutoff_text = f"{oldest_kept_date:%Y-%m-%d} 00:00:00"

    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                "SELECT id FROM inspection_ng_events WHERE created_at < ? ORDER BY id",
                (cutoff_text,),
            ).fetchall()
        ids = [int(row["id"]) for row in rows]
    except Exception as exc:
        raise RuntimeError(
            f"Tim NG event qua han that bai: cutoff={cutoff_text!r}, path={DATABASE_PATH}"
        ) from exc

    result = delete_ng_events_by_ids(ids)
    result["retention_days"] = retention_days
    result["oldest_kept_date"] = oldest_kept_date.strftime("%Y-%m-%d")
    result["cutoff"] = cutoff_text
    return result




# IS1200_480 extensions ----------------------------------------------------
def _ensure_is480_cycle_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS is480_cycle_history (
       cycle_id TEXT PRIMARY KEY, barcode TEXT NOT NULL, check_no INTEGER NOT NULL,
       final_status TEXT NOT NULL CHECK(final_status IN ('OK','NG')),
       completed_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_is480_cycle_barcode ON is480_cycle_history(barcode)")


def finalize_cycle(cycle_id, barcode, check_no, status):
    """Immutable final history, idempotent on duplicated ACK; never mutate verdict."""
    if not cycle_id or not barcode or status not in ('OK','NG'):
        raise ValueError('Invalid final cycle data')
    with get_db_connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _ensure_is480_cycle_schema(conn)
        row=conn.execute('SELECT barcode, check_no, final_status FROM is480_cycle_history WHERE cycle_id=?',
                         (cycle_id,)).fetchone()
        if row:
            if (row['barcode'],row['check_no'],row['final_status'])!=(barcode,int(check_no),status):
                raise ValueError('Conflicting duplicate final ACK')
            return False
        conn.execute('INSERT INTO is480_cycle_history(cycle_id,barcode,check_no,final_status,completed_at) VALUES(?,?,?,?,?)',
                     (cycle_id,barcode,int(check_no),status,format_db_timestamp()))
        return True


def query_shift_bodies(now=None):
    info=ShiftSchedule.get_shift_info(now)
    start,end=get_shift_datetime_range(info['date'],info['shift'])
    start_text=start.strftime('%Y-%m-%d %H:%M:%S')
    end_text=(end+timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')
    with get_db_connection() as conn:
        rows=conn.execute("""SELECT barcode, final_status, repair_confirmed_at
            FROM inspection_checks
            WHERE created_at >= ? AND created_at < ? AND final_status IN ('OK','NG')
            ORDER BY created_at DESC, id DESC""",(start_text,end_text)).fetchall()
    return [dict(row) for row in rows]


def mark_repaired(barcode, repaired_by='BARCODE_SCANNER', now=None):
    """Mark an NG BODY repaired within current shift; preserve original NG verdict."""
    barcode=str(barcode or '').strip()
    if not barcode:raise ValueError('empty repaired barcode')
    info=ShiftSchedule.get_shift_info(now)
    start,end=get_shift_datetime_range(info['date'],info['shift'])
    with get_db_connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT final_status, created_at, repair_confirmed_at FROM inspection_checks WHERE barcode=?',
                         (barcode,)).fetchone()
        if row is None:raise LookupError('BODY barcode not found')
        if row['final_status']!='NG':raise ValueError('BODY is not NG')
        created=datetime.strptime(row['created_at'],'%Y-%m-%d %H:%M:%S')
        if not start<=created<end+timedelta(seconds=1):
            raise ValueError('BODY belongs to another shift')
        if row['repair_confirmed_at']:return False
        conn.execute('UPDATE inspection_checks SET repair_confirmed_at=?, repair_confirmed_by=?, updated_at=? WHERE barcode=?',
                     (format_db_timestamp(now),repaired_by,format_db_timestamp(now),barcode))
    return True


def commit_complete_cycle(cycle_id, barcode, check_no, status):
    """Atomic/idempotent update of the current BODY and immutable cycle history.

    Returns True for a newly recorded cycle, False for a duplicate ACK. A stale
    check_no or conflicting verdict cannot overwrite the latest BODY record.
    """
    barcode=str(barcode or '').strip()
    if not cycle_id or not barcode or status not in ('OK','NG'):
        raise ValueError('Invalid cycle/barcode/verdict')
    if not isinstance(check_no,int) or check_no<=0:
        raise ValueError('Invalid check_no')
    with get_db_connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _ensure_is480_cycle_schema(conn)
        old=conn.execute('SELECT barcode,check_no,final_status FROM is480_cycle_history WHERE cycle_id=?',
                         (cycle_id,)).fetchone()
        if old:
            if (old['barcode'],int(old['check_no']),old['final_status']) != (barcode,check_no,status):
                raise ValueError('Conflicting ACK for completed cycle')
            return False
        item=conn.execute('SELECT check_no,final_status FROM inspection_checks WHERE barcode=?',(barcode,)).fetchone()
        if item is None or int(item['check_no'])!=check_no:
            raise ValueError(f'Missing or superseded barcode/check_no: {barcode}/{check_no}')
        if item['final_status'] not in ('PENDING',status):
            raise ValueError(f'Conflicting final status for {barcode}: {item["final_status"]} vs {status}')
        conn.execute('UPDATE inspection_checks SET final_status=?,updated_at=? WHERE barcode=? AND check_no=?',
                     (status,format_db_timestamp(),barcode,check_no))
        conn.execute('INSERT INTO is480_cycle_history(cycle_id,barcode,check_no,final_status,completed_at) VALUES (?,?,?,?,?)',
                     (cycle_id,barcode,check_no,status,format_db_timestamp()))
        return True
