from __future__ import annotations

import hmac
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("CALENDAR_DATA_DIR", ROOT / "data"))
LOG_DIR = Path(os.getenv("CALENDAR_LOG_DIR", ROOT / "logs"))
UPLOAD_DIR = DATA_DIR / "uploads"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "calendar.db"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_CATEGORIES = {"SR", "CM", "회의", "휴가", "기타"}
CATEGORY_COLORS = {"SR":"#3478D4","CM":"#8B5CC7","회의":"#159B83","휴가":"#E49A32","기타":"#8491A5"}
ADMIN_TABLES = {"users","events","todos","event_comments","activity_logs","attachments"}
ALLOWED_ENVIRONMENTS = {"Dev", "Stg", "Prd"}

logger = logging.getLogger("company_calendar")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        LOG_DIR / "activity.log", maxBytes=5 * 1024 *  1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(file_handler)

app = FastAPI(title="사내 공용 캘린더", version="0.4.0")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            ip TEXT NOT NULL,
            color TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            start_at TEXT NOT NULL,
            end_at TEXT,
            all_day INTEGER NOT NULL DEFAULT 0,
            owner_id TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'shared',
            category TEXT NOT NULL DEFAULT '기타',
            environment TEXT,
            assignee TEXT,
            ticket_no TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT,
            deleted_by TEXT,
            FOREIGN KEY(owner_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            due_date TEXT,
            priority TEXT NOT NULL DEFAULT 'normal',
            completed INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'todo',
            assignee_id TEXT,
            visibility TEXT NOT NULL DEFAULT 'personal',
            owner_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            deleted_at TEXT,
            deleted_by TEXT,
            FOREIGN KEY(owner_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size INTEGER NOT NULL,
            uploaded_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE,
            FOREIGN KEY(uploaded_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS event_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_event_comments_event ON event_comments(event_id, created_at);
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            ip TEXT NOT NULL,
            user_id TEXT,
            action TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_at);
        CREATE INDEX IF NOT EXISTS idx_events_end ON events(end_at);
        CREATE INDEX IF NOT EXISTS idx_todos_due ON todos(due_date);
        CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_logs(created_at);
        """)
        # 기존 MVP DB를 보존하면서 새 필드를 추가하는 간단한 마이그레이션
        event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
        for name, sql_type, default in [
            ("category", "TEXT", "'기타'"),
            ("environment", "TEXT", "NULL"),
            ("assignee", "TEXT", "NULL"),
            ("ticket_no", "TEXT", "NULL"),
        ]:
            if name not in event_columns:
                conn.execute(f"ALTER TABLE events ADD COLUMN {name} {sql_type} DEFAULT {default}")
        todo_columns = {row["name"] for row in conn.execute("PRAGMA table_info(todos)")}
        if "status" not in todo_columns:
            conn.execute("ALTER TABLE todos ADD COLUMN status TEXT NOT NULL DEFAULT 'todo'")
        if "assignee_id" not in todo_columns:
            conn.execute("ALTER TABLE todos ADD COLUMN assignee_id TEXT")
        conn.execute("UPDATE todos SET assignee_id=owner_id WHERE assignee_id IS NULL")
        conn.execute("UPDATE todos SET status='done' WHERE completed=1")
        for table in ("events", "todos"):
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "deleted_at" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN deleted_at TEXT")
            if "deleted_by" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN deleted_by TEXT")
        conn.execute("UPDATE todos SET status='todo' WHERE completed=0 AND status='done'")


init_db()


class IdentifyIn(BaseModel):
    user_id: str = Field(min_length=8, max_length=80)
    display_name: str | None = Field(default=None, min_length=1, max_length=30)


class UserIn(BaseModel):
    user_id: str = Field(min_length=8, max_length=80)
    display_name: str = Field(min_length=1, max_length=30)


class RenameIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=30)


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=5000)
    start_at: str
    end_at: str | None = None
    all_day: bool = False
    category: str = "기타"
    environment: str | None = None
    assignee: str | None = Field(default=None, max_length=60)
    ticket_no: str | None = Field(default=None, max_length=80)


class TodoIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    due_date: str | None = None
    priority: str = "normal"
    visibility: str = "personal"


class ParseIn(BaseModel):
    text: str = Field(min_length=1, max_length=300)


class EventCommentIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class TodoMoveIn(BaseModel):
    status: str = Field(min_length=1, max_length=20)
    owner_id: str = Field(min_length=8, max_length=80)


def client_ip(request: Request) -> str:
    # 프록시 헤더는 신뢰하지 않고 TCP 연결 상대 IP를 사용합니다.
    return request.client.host if request.client else "unknown"


def audit(request: Request, user_id: str | None, action: str, **details: Any) -> None:
    ip = client_ip(request)
    now = datetime.now().isoformat(timespec="seconds")
    actor = user_id or "-"
    try:
        with db() as conn:
            if user_id:
                row = conn.execute("SELECT display_name FROM users WHERE id=?", (user_id,)).fetchone()
                if row:
                    actor = row["display_name"]
            safe_details = json.dumps(details, ensure_ascii=False, default=str)
            logger.info("IP=%s | USER=%s | ACTION=%s | DETAILS=%s", ip, actor, action, safe_details)
            conn.execute(
                "INSERT INTO activity_logs(created_at,ip,user_id,action,details) VALUES(?,?,?,?,?)",
                (now, ip, user_id, action, safe_details),
            )
    except sqlite3.Error:
        logger.exception("활동 로그 DB 저장 실패: IP=%s ACTION=%s", ip, action)


def get_user(request: Request, user_id: str | None) -> sqlite3.Row:
    if not user_id:
        raise HTTPException(401, "사용자 식별 정보가 없습니다. 페이지를 새로고침해 주세요.")
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(401, "등록되지 않은 사용자입니다. 페이지를 새로고침해 주세요.")
        conn.execute(
            "UPDATE users SET ip=?, last_seen=? WHERE id=?",
            (client_ip(request), datetime.now().isoformat(timespec="seconds"), user_id),
        )
        return row


def normalize_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"날짜 또는 시간 형식이 올바르지 않습니다: {value}") from exc
    if parsed.tzinfo is not None:
        raise HTTPException(422, "현재는 사내 표준 시간대의 로컬 날짜/시간을 입력해 주세요.")
    return parsed


def validate_event(payload: EventIn) -> tuple[datetime, datetime | None]:
    start = normalize_datetime(payload.start_at)
    end = normalize_datetime(payload.end_at) if payload.end_at else None
    if end and end < start:
        raise HTTPException(422, "종료 일시는 시작 일시보다 빠를 수 없습니다.")
    if payload.category not in ALLOWED_CATEGORIES:
        raise HTTPException(422, "일정 유형이 올바르지 않습니다.")
    if payload.category in {"SR", "CM"}:
        if payload.environment not in ALLOWED_ENVIRONMENTS:
            raise HTTPException(422, "SR 및 CM 일정은 Dev, Stg, Prd 환경을 선택해야 합니다.")
    elif payload.environment not in (None, "", *ALLOWED_ENVIRONMENTS):
        raise HTTPException(422, "환경 값이 올바르지 않습니다.")
    return start, end


def parse_period_hour(hour: int, period: str | None, *, korean_hour: bool = True) -> int:
    if period in ("오후", "저녁", "밤") and 1 <= hour <= 11:
        return hour + 12
    if period in ("오전", "아침") and hour == 12:
        return 0
    # 명시적인 오전/오후가 없으면 한국어 일정 입력에서 1~11시는 오후로 해석
    if period is None and korean_hour and 1 <= hour <= 11:
        return hour + 12
    return hour


def parse_natural(text: str, now: datetime | None = None) -> dict[str, Any]:
    """외부 API 없이 동작하는 한국어 날짜/시간 규칙 파서."""
    now = now or datetime.now()
    raw = text.strip()
    compact = re.sub(r"\s+", "", raw)
    target = now.date()
    range_end_date: date | None = None
    date_detected = False

    relative = [("글피", 3), ("모레", 2), ("내일", 1), ("오늘", 0)]
    for token, offset in relative:
        if token in compact:
            target = now.date() + timedelta(days=offset)
            date_detected = True
            break

    if not date_detected:
        weekdays = {"월요일": 0, "화요일": 1, "수요일": 2, "목요일": 3,
                    "금요일": 4, "토요일": 5, "일요일": 6,
                    "월욜": 0, "화욜": 1, "수욜": 2, "목욜": 3, "금욜": 4}
        found = next(((label, wd) for label, wd in weekdays.items() if label in compact), None)
        if found:
            _, weekday = found
            monday = now.date() - timedelta(days=now.weekday())
            if "다음주" in compact:
                target = monday + timedelta(days=7 + weekday)
            elif "이번주" in compact:
                target = monday + timedelta(days=weekday)
            else:
                delta = (weekday - now.weekday()) % 7
                target = now.date() + timedelta(days=delta)
            date_detected = True
        else:
            # 11월 23일부터 25일까지 / 23일~25일 등 날짜 범위
            range_match = re.search(
                r"(?:(\d{1,2})월)?(\d{1,2})일(?:부터|~|-)(?:(\d{1,2})월)?(\d{1,2})일(?:까지)?",
                compact,
            )
            if range_match:
                first_month = int(range_match.group(1) or now.month)
                first_day = int(range_match.group(2))
                last_month = int(range_match.group(3) or first_month)
                last_day = int(range_match.group(4))
                year = now.year
                if last_month < first_month:
                    year += 1
                try:
                    target = date(year, first_month, first_day)
                    range_end_date = date(year, last_month, last_day)
                except ValueError as exc:
                    raise HTTPException(422, "날짜 범위가 올바르지 않습니다.") from exc
                if range_end_date < target:
                    raise HTTPException(422, "종료 날짜는 시작 날짜보다 빠를 수 없습니다.")
                date_detected = True
            else:
                range_end_date = None
                month_match = re.search(r"(?:(\d{4})년)?(\d{1,2})월(\d{1,2})일", compact)
                if month_match:
                    year = int(month_match.group(1) or now.year)
                    try:
                        target = date(year, int(month_match.group(2)), int(month_match.group(3)))
                    except ValueError as exc:
                        raise HTTPException(422, "날짜가 올바르지 않습니다.") from exc
                    date_detected = True
                else:
                    month_day = re.search(r"(다음달|이번달)(\d{1,2})일", compact)
                    if month_day:
                        month = now.month + (1 if month_day.group(1) == "다음달" else 0)
                        year = now.year
                        if month > 12:
                            month, year = 1, year + 1
                        try:
                            target = date(year, month, int(month_day.group(2)))
                        except ValueError as exc:
                            raise HTTPException(422, "날짜가 올바르지 않습니다.") from exc
                        date_detected = True
                    else:
                        short_date = re.search(r"(\d{1,2})[./-](\d{1,2})(?:일)?", compact)
                        if short_date:
                            try:
                                target = date(now.year, int(short_date.group(1)), int(short_date.group(2)))
                            except ValueError as exc:
                                raise HTTPException(422, "날짜가 올바르지 않습니다.") from exc
                            date_detected = True

    # 시작 시간: 오후 2시, 2시, 14:00, 2시 반
    time_pattern = re.compile(
        r"(?P<period>오전|오후|저녁|아침|밤)?(?P<hour>\d{1,2})(?P<unit>시|:)(?:(?P<minute>\d{1,2})분?|(?P<half>반))?"
    )
    start_match = time_pattern.search(compact)
    start_time: time | None = None
    start_period: str | None = None
    if start_match:
        start_period = start_match.group("period")
        hour = parse_period_hour(int(start_match.group("hour")), start_period,
                                 korean_hour=start_match.group("unit") == "시")
        minute = 30 if start_match.group("half") else int(start_match.group("minute") or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise HTTPException(422, "시간이 올바르지 않습니다.")
        start_time = time(hour, minute)

    # 시간 범위 종료. 종료 시 오전/오후 생략 시 시작 시간의 오전/오후를 상속합니다.
    end_time: time | None = None
    if start_match:
        tail = compact[start_match.end():]
        range_start = re.search(r"(?:부터|~)(오전|오후|저녁|아침|밤)?(\d{1,2})(시|:)(?:(\d{1,2})분?|반)?(?:까지)?", tail)
        if range_start:
            end_period = range_start.group(1) or start_period
            end_hour_raw = int(range_start.group(2))
            end_hour = parse_period_hour(end_hour_raw, end_period,
                                         korean_hour=range_start.group(3) == "시")
            end_minute = 30 if "반" in range_start.group(0) else int(range_start.group(4) or 0)
            if 0 <= end_hour <= 23 and 0 <= end_minute <= 59:
                end_time = time(end_hour, end_minute)

    # 자연어 시간/날짜 부분만 제목에서 제거
    title = raw
    remove_patterns = [
        r"(?:(?:\d{1,2}\s*월\s*)?\d{1,2}\s*일)\s*(?:부터|~|-)\s*(?:\d{1,2}\s*월\s*)?\d{1,2}\s*일(?:까지)?",
        r"(?:다음\s*달|이번\s*달)\s*\d{1,2}일",
        r"다음\s*주|이번\s*주|다음\s*달|이번\s*달|오늘|내일|모레|글피",
        r"(?:월|화|수|목|금|토|일)(?:요일|욜)",
        r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일|\d{1,2}월\s*\d{1,2}일",
        r"\d{1,2}[./-]\d{1,2}(?:일)?",
        r"(?:오전|오후|저녁|아침|밤)?\s*\d{1,2}\s*(?:시|:)\s*(?:\d{1,2}\s*분?|반)?",
        r"(?:부터|~)\s*(?:오전|오후|저녁|아침|밤)?\s*\d{1,2}\s*(?:시|:)\s*(?:\d{1,2}\s*분?|반)?\s*(?:까지)?",
        r"부터|까지",
    ]
    for pattern in remove_patterns:
        title = re.sub(pattern, " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip(" ,.-") or raw

    start_at = datetime.combine(target, start_time or time(9, 0))
    if range_end_date:
        end_at = datetime.combine(range_end_date, end_time or start_time or time(23, 59))
    else:
        end_at = datetime.combine(target, end_time) if end_time else None
    if end_at and end_at <= start_at:
        end_at += timedelta(days=1)
    return {
        "title": title,
        "date": target.isoformat(),
        "start_at": start_at.isoformat(timespec="minutes"),
        "end_at": end_at.isoformat(timespec="minutes") if end_at else None,
        "all_day": start_time is None,
        "date_detected": date_detected,
        "time_detected": start_time is not None,
    }


def attachment_rows(conn: sqlite3.Connection, event_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id,original_name,content_type,size,created_at FROM attachments WHERE event_id=? ORDER BY id",
        (event_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def event_query_select() -> str:
    return """SELECT e.*,u.display_name,u.color FROM events e
              JOIN users u ON u.id=e.owner_id"""


def event_to_dict(row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
    result = dict(row)
    result["color"] = CATEGORY_COLORS.get(result.get("category") or "기타", CATEGORY_COLORS["기타"])
    result["attachments"] = attachment_rows(conn, row["id"])
    return result


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return HTML


@app.get("/kanban", response_class=HTMLResponse)
def kanban_page() -> str:
    return KANBAN_HTML


@app.get("/admin", response_class=HTMLResponse)
def admin_page() -> str:
    return ADMIN_HTML



def require_admin(x_admin_token: str | None) -> None:
    expected = os.getenv("CALENDAR_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(503, "서버 환경변수 CALENDAR_ADMIN_TOKEN이 설정되지 않았습니다.")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(403, "관리자 인증에 실패했습니다.")


@app.get("/api/admin/summary")
def admin_summary(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    with db() as conn:
        result = {
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM events WHERE deleted_at IS NULL").fetchone()[0],
            "deleted_events": conn.execute("SELECT COUNT(*) FROM events WHERE deleted_at IS NOT NULL").fetchone()[0],
            "todos": conn.execute("SELECT COUNT(*) FROM todos WHERE deleted_at IS NULL").fetchone()[0],
            "deleted_todos": conn.execute("SELECT COUNT(*) FROM todos WHERE deleted_at IS NOT NULL").fetchone()[0],
            "comments": conn.execute("SELECT COUNT(*) FROM event_comments").fetchone()[0],
            "logs": conn.execute("SELECT COUNT(*) FROM activity_logs").fetchone()[0],
        }
    return result


@app.get("/api/admin/records")
def admin_records(table: str, limit: int = 100, offset: int = 0,
                  x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    if table not in ADMIN_TABLES:
        raise HTTPException(400, "조회할 수 없는 테이블입니다.")
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with db() as conn:
        total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid DESC LIMIT ? OFFSET ?',
                            (limit, offset)).fetchall()
        columns = [row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')]
    return {"table": table, "columns": columns, "total": total,
            "limit": limit, "offset": offset, "records": [dict(row) for row in rows]}


@app.post("/api/admin/restore/{table}/{record_id}")
def admin_restore(table: str, record_id: int, request: Request,
                  x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    if table not in ("events", "todos"):
        raise HTTPException(400, "일정 또는 Todo만 복구할 수 있습니다.")
    with db() as conn:
        row = conn.execute(f'SELECT id,deleted_at FROM "{table}" WHERE id=?', (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, "기록을 찾을 수 없습니다.")
        if not row["deleted_at"]:
            raise HTTPException(409, "이미 활성 상태인 기록입니다.")
        conn.execute(f'UPDATE "{table}" SET deleted_at=NULL,deleted_by=NULL WHERE id=?', (record_id,))
    audit(request, None, "ADMIN_RESTORE", table=table, record_id=record_id)
    return {"ok": True}


@app.post("/api/users/identify")
def identify_user(payload: IdentifyIn, request: Request):
    ip = client_ip(request)
    now = datetime.now().isoformat(timespec="seconds")
    palette = ["#2563eb", "#059669", "#d97706", "#9333ea", "#db2777", "#0891b2", "#475569", "#0f766e"]

    with db() as conn:
        existing = conn.execute("SELECT * FROM users WHERE id=?", (payload.user_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET ip=?,last_seen=? WHERE id=?",
                (ip, now, payload.user_id),
            )
            result = {
                "id": existing["id"],
                "display_name": existing["display_name"],
                "color": existing["color"],
                "needs_name": False,
            }
        else:
            # 브라우저 저장소가 초기화된 경우 같은 IP에 사용자가 한 명뿐이면 기존 사용자를 재사용합니다.
            same_ip = conn.execute(
                "SELECT * FROM users WHERE ip=? ORDER BY last_seen DESC",
                (ip,),
            ).fetchall()

            if len(same_ip) == 1:
                row = same_ip[0]
                conn.execute(
                    "UPDATE users SET last_seen=? WHERE id=?",
                    (now, row["id"]),
                )
                result = {
                    "id": row["id"],
                    "display_name": row["display_name"],
                    "color": row["color"],
                    "needs_name": False,
                }
            elif payload.display_name and payload.display_name.strip():
                # 저장된 이름이 있지만 UUID가 DB에 없는 경우 반드시 신규 사용자를 DB에 생성합니다.
                # 이전 버전은 needs_name=False만 반환하고 INSERT를 누락해 이후 API가 401을 반환했습니다.
                count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                color = palette[count % len(palette)]
                display_name = payload.display_name.strip()
                conn.execute(
                    "INSERT INTO users(id,display_name,ip,color,created_at,last_seen) "
                    "VALUES(?,?,?,?,?,?)",
                    (payload.user_id, display_name, ip, color, now, now),
                )
                result = {
                    "id": payload.user_id,
                    "display_name": display_name,
                    "color": color,
                    "needs_name": False,
                }
            else:
                # 사용자 이름이 없으면 화면에서 입력받은 뒤 /api/users에서 등록합니다.
                result = {
                    "id": payload.user_id,
                    "display_name": "",
                    "color": palette[0],
                    "needs_name": True,
                }

    audit(
        request,
        result["id"] if not result["needs_name"] else None,
        "USER_IDENTIFY",
        display_name=result["display_name"],
        reused=not result["needs_name"] and result["id"] != payload.user_id,
        needs_name=result["needs_name"],
    )
    return result


@app.post("/api/users")
def register_user(payload: UserIn, request: Request):
    now = datetime.now().isoformat(timespec="seconds")
    palette = ["#2563eb", "#059669", "#d97706", "#9333ea", "#db2777", "#0891b2", "#475569", "#0f766e"]
    with db() as conn:
        existing = conn.execute("SELECT * FROM users WHERE id=?", (payload.user_id,)).fetchone()
        if existing:
            conn.execute("UPDATE users SET display_name=?,ip=?,last_seen=? WHERE id=?",
                         (payload.display_name, client_ip(request), now, payload.user_id))
            color = existing["color"]
        else:
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            color = palette[count % len(palette)]
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)",
                         (payload.user_id, payload.display_name, client_ip(request), color, now, now))
    audit(request, payload.user_id, "USER_REGISTER", display_name=payload.display_name)
    return {"id": payload.user_id, "display_name": payload.display_name, "color": color}


@app.patch("/api/users/me")
def rename_user(payload: RenameIn, request: Request,
                x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    with db() as conn:
        conn.execute("UPDATE users SET display_name=? WHERE id=?", (payload.display_name.strip(), user["id"]))
    audit(request, user["id"], "USER_RENAME", old_name=user["display_name"], new_name=payload.display_name.strip())
    return {"id": user["id"], "display_name": payload.display_name.strip(), "color": user["color"]}


@app.get("/api/parse")
def parse_help():
    return {"examples": ["내일 오후 3시 회의", "다음 주 수요일 2시부터 3시까지 회의", "11월 10일 오후 2시 반 미팅"]}


@app.post("/api/parse")
def parse_text(payload: ParseIn, request: Request,
               x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    result = parse_natural(payload.text)
    audit(request, user["id"], "NATURAL_LANGUAGE_PARSE", text=payload.text,
          parsed_start=result["start_at"], parsed_end=result["end_at"])
    return result


@app.get("/api/events")
def list_events(start: str, end: str, request: Request,
               x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    # 종료일을 포함해 날짜 단위로 겹치는 일정을 조회합니다.
    start_date = normalize_datetime(start).date().isoformat() if "T" in start else date.fromisoformat(start).isoformat()
    end_date = normalize_datetime(end).date().isoformat() if "T" in end else date.fromisoformat(end).isoformat()
    with db() as conn:
        rows = conn.execute(event_query_select() + """
            WHERE e.deleted_at IS NULL AND date(e.start_at) <= date(?) AND date(COALESCE(e.end_at,e.start_at)) >= date(?)
            ORDER BY e.all_day DESC,e.start_at,e.id
        """, (end_date, start_date)).fetchall()
        result = [event_to_dict(row, conn) for row in rows]
    audit(request, user["id"], "CALENDAR_VIEW", start=start_date, end=end_date, result_count=len(result))
    return result


@app.get("/api/events/search")
def search_events(request: Request, q: str = "", category: str = "", environment: str = "",
                  from_date: str = "", to_date: str = "",
                  x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    clauses: list[str] = []
    params: list[Any] = []
    if q.strip():
        like = f"%{q.strip()}%"
        clauses.append("(e.title LIKE ? OR e.description LIKE ? OR COALESCE(e.assignee,'') LIKE ? OR COALESCE(e.ticket_no,'') LIKE ? OR COALESCE(e.environment,'') LIKE ? OR e.category LIKE ? OR u.display_name LIKE ?)")
        params.extend([like, like, like, like, like, like, like])
    if category:
        clauses.append("e.category=?")
        params.append(category)
    if environment:
        clauses.append("e.environment=?")
        params.append(environment)
    if from_date:
        date.fromisoformat(from_date)
        clauses.append("date(COALESCE(e.end_at,e.start_at)) >= date(?)")
        params.append(from_date)
    if to_date:
        date.fromisoformat(to_date)
        clauses.append("date(e.start_at) <= date(?)")
        params.append(to_date)
    clauses.insert(0, "e.deleted_at IS NULL")
    where = " WHERE " + " AND ".join(clauses)
    with db() as conn:
        rows = conn.execute(event_query_select() + where + " ORDER BY e.start_at DESC LIMIT 250", params).fetchall()
        result = [event_to_dict(row, conn) for row in rows]
    audit(request, user["id"], "EVENT_SEARCH", query=q, category=category, environment=environment,
          result_count=len(result))
    return result


@app.post("/api/events")
def create_event(payload: EventIn, request: Request,
                 x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    start, end = validate_event(payload)
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        cur = conn.execute("""INSERT INTO events
            (title,description,start_at,end_at,all_day,owner_id,category,environment,assignee,ticket_no,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (payload.title.strip(), payload.description, start.isoformat(timespec="minutes"),
             end.isoformat(timespec="minutes") if end else None, int(payload.all_day), user["id"],
             payload.category, payload.environment or None, payload.assignee or None,
             payload.ticket_no or None, now, now))
        event_id = cur.lastrowid
    audit(request, user["id"], "EVENT_CREATE", event_id=event_id, title=payload.title,
          category=payload.category, environment=payload.environment, start_at=payload.start_at,
          end_at=payload.end_at)
    return {"id": event_id}


@app.put("/api/events/{event_id}")
def update_event(event_id: int, payload: EventIn, request: Request,
                 x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    start, end = validate_event(payload)
    with db() as conn:
        row = conn.execute("SELECT owner_id FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise HTTPException(404, "일정을 찾을 수 없습니다.")
        if row["owner_id"] != user["id"]:
            raise HTTPException(403, "본인이 등록한 일정만 수정할 수 있습니다.")
        conn.execute("""UPDATE events SET title=?,description=?,start_at=?,end_at=?,all_day=?,
            category=?,environment=?,assignee=?,ticket_no=?,updated_at=? WHERE id=?""",
            (payload.title.strip(), payload.description, start.isoformat(timespec="minutes"),
             end.isoformat(timespec="minutes") if end else None, int(payload.all_day),
             payload.category, payload.environment or None, payload.assignee or None,
             payload.ticket_no or None, datetime.now().isoformat(timespec="seconds"), event_id))
    audit(request, user["id"], "EVENT_UPDATE", event_id=event_id, title=payload.title,
          category=payload.category, start_at=payload.start_at, end_at=payload.end_at)
    return {"ok": True}


@app.delete("/api/events/{event_id}")
def delete_event(event_id: int, request: Request,
                 x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    with db() as conn:
        row = conn.execute("SELECT owner_id,title,deleted_at FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise HTTPException(404, "일정을 찾을 수 없습니다.")
        if row["owner_id"] != user["id"]:
            raise HTTPException(403, "본인이 등록한 일정만 삭제할 수 있습니다.")
        if row["deleted_at"]:
            raise HTTPException(404, "이미 삭제된 일정입니다.")
        conn.execute("UPDATE events SET deleted_at=?,deleted_by=? WHERE id=?",
                     (datetime.now().isoformat(timespec="seconds"), user["id"], event_id))
    audit(request, user["id"], "EVENT_DELETE", event_id=event_id, title=row["title"])
    return {"ok": True}


@app.post("/api/events/{event_id}/attachments")
async def upload_attachments(event_id: int, request: Request,
                             files: list[UploadFile] = File(...),
                             x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    with db() as conn:
        event = conn.execute("SELECT owner_id FROM events WHERE id=?", (event_id,)).fetchone()
        if not event:
            raise HTTPException(404, "일정을 찾을 수 없습니다.")
        if event["owner_id"] != user["id"]:
            raise HTTPException(403, "일정을 등록한 사용자만 첨부파일을 추가할 수 있습니다.")
    saved: list[dict[str, Any]] = []
    for upload in files:
        content = await upload.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"파일당 최대 용량은 {MAX_UPLOAD_BYTES // (1024*1024)}MB입니다.")
        original = Path((upload.filename or "첨부파일").replace("\\", "/")).name[:240]
        stored = f"{uuid.uuid4().hex}{Path(original).suffix[:12]}"
        (UPLOAD_DIR / stored).write_bytes(content)
        item = (event_id, original, stored, upload.content_type or "application/octet-stream",
                len(content), user["id"], datetime.now().isoformat(timespec="seconds"))
        with db() as conn:
            cur = conn.execute("""INSERT INTO attachments
                (event_id,original_name,stored_name,content_type,size,uploaded_by,created_at)
                VALUES(?,?,?,?,?,?,?)""", item)
            saved.append({"id": cur.lastrowid, "name": original, "size": len(content)})
        await upload.close()
    audit(request, user["id"], "ATTACHMENT_UPLOAD", event_id=event_id, files=saved)
    return {"uploaded": saved}


@app.get("/api/attachments/{attachment_id}")
def download_attachment(attachment_id: int, request: Request,
                        x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    with db() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    if not row:
        raise HTTPException(404, "첨부파일을 찾을 수 없습니다.")
    file_path = UPLOAD_DIR / row["stored_name"]
    if not file_path.is_file():
        raise HTTPException(410, "첨부파일이 서버에서 삭제되었습니다.")
    audit(request, user["id"], "ATTACHMENT_DOWNLOAD", attachment_id=attachment_id,
          original_name=row["original_name"])
    return FileResponse(file_path, media_type=row["content_type"], filename=row["original_name"])


@app.delete("/api/attachments/{attachment_id}")
def delete_attachment(attachment_id: int, request: Request,
                      x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    with db() as conn:
        row = conn.execute("""SELECT a.*,e.owner_id FROM attachments a
                          JOIN events e ON e.id=a.event_id WHERE a.id=?""", (attachment_id,)).fetchone()
        if not row:
            raise HTTPException(404, "첨부파일을 찾을 수 없습니다.")
        if row["owner_id"] != user["id"]:
            raise HTTPException(403, "일정 등록자만 첨부파일을 삭제할 수 있습니다.")
        conn.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
    (UPLOAD_DIR / row["stored_name"]).unlink(missing_ok=True)
    audit(request, user["id"], "ATTACHMENT_DELETE", attachment_id=attachment_id,
          original_name=row["original_name"])
    return {"ok": True}


@app.get("/api/events/{event_id}/comments")
def list_event_comments(event_id: int, request: Request,
                        x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    with db() as conn:
        if not conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone():
            raise HTTPException(404, "일정을 찾을 수 없습니다.")
        rows = conn.execute("""SELECT c.id,c.event_id,c.user_id,c.content,c.created_at,c.updated_at,
                                      u.display_name
                               FROM event_comments c JOIN users u ON u.id=c.user_id
                               WHERE c.event_id=? ORDER BY c.created_at,c.id""", (event_id,)).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/events/{event_id}/comments")
def create_event_comment(event_id: int, payload: EventCommentIn, request: Request,
                         x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    content = payload.content.strip()
    if not content:
        raise HTTPException(422, "댓글 내용을 입력하세요.")
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone():
            raise HTTPException(404, "일정을 찾을 수 없습니다.")
        cur = conn.execute("""INSERT INTO event_comments(event_id,user_id,content,created_at,updated_at)
                              VALUES(?,?,?,?,?)""", (event_id,user["id"],content,now,now))
        comment_id = cur.lastrowid
    audit(request, user["id"], "EVENT_COMMENT_CREATE", event_id=event_id, comment_id=comment_id)
    return {"id": comment_id}


@app.patch("/api/event-comments/{comment_id}")
def update_event_comment(comment_id: int, payload: EventCommentIn, request: Request,
                         x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    content = payload.content.strip()
    if not content:
        raise HTTPException(422, "댓글 내용을 입력하세요.")
    with db() as conn:
        row = conn.execute("SELECT user_id,event_id FROM event_comments WHERE id=?", (comment_id,)).fetchone()
        if not row:
            raise HTTPException(404, "댓글을 찾을 수 없습니다.")
        if row["user_id"] != user["id"]:
            raise HTTPException(403, "본인이 작성한 댓글만 수정할 수 있습니다.")
        conn.execute("UPDATE event_comments SET content=?,updated_at=? WHERE id=?",
                     (content,datetime.now().isoformat(timespec="seconds"),comment_id))
    audit(request, user["id"], "EVENT_COMMENT_UPDATE", event_id=row["event_id"], comment_id=comment_id)
    return {"ok": True}


@app.delete("/api/event-comments/{comment_id}")
def delete_event_comment(comment_id: int, request: Request,
                         x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    with db() as conn:
        row = conn.execute("SELECT user_id,event_id FROM event_comments WHERE id=?", (comment_id,)).fetchone()
        if not row:
            raise HTTPException(404, "댓글을 찾을 수 없습니다.")
        if row["user_id"] != user["id"]:
            raise HTTPException(403, "본인이 작성한 댓글만 삭제할 수 있습니다.")
        conn.execute("DELETE FROM event_comments WHERE id=?", (comment_id,))
    audit(request, user["id"], "EVENT_COMMENT_DELETE", event_id=row["event_id"], comment_id=comment_id)
    return {"ok": True}


@app.get("/api/todos")
def list_todos(request: Request, x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    with db() as conn:
        rows = conn.execute("""SELECT t.*,u.display_name,u.color FROM todos t
            JOIN users u ON u.id=t.owner_id
            WHERE t.deleted_at IS NULL AND (t.visibility='shared' OR t.owner_id=?)
            ORDER BY t.completed,t.due_date,t.id DESC""", (user["id"],)).fetchall()
    audit(request, user["id"], "TODO_VIEW", result_count=len(rows))
    return [dict(row) for row in rows]


@app.post("/api/todos")
def create_todo(payload: TodoIn, request: Request,
                x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    if payload.priority not in ("low", "normal", "high"):
        raise HTTPException(422, "우선순위 값이 올바르지 않습니다.")
    if payload.visibility not in ("personal", "shared"):
        raise HTTPException(422, "공개 범위가 올바르지 않습니다.")
    if payload.due_date:
        date.fromisoformat(payload.due_date)
    with db() as conn:
        cur = conn.execute("""INSERT INTO todos(title,due_date,priority,visibility,owner_id,created_at)
            VALUES(?,?,?,?,?,?)""",
            (payload.title.strip(), payload.due_date, payload.priority, payload.visibility,
             user["id"], datetime.now().isoformat(timespec="seconds")))
        todo_id = cur.lastrowid
    audit(request, user["id"], "TODO_CREATE", todo_id=todo_id, title=payload.title,
          visibility=payload.visibility, due_date=payload.due_date)
    return {"id": todo_id}


@app.patch("/api/todos/{todo_id}")
def toggle_todo(todo_id: int, request: Request,
                x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    with db() as conn:
        row = conn.execute("SELECT completed,owner_id,visibility,title,deleted_at FROM todos WHERE id=?", (todo_id,)).fetchone()
        if not row or row["deleted_at"]:
            raise HTTPException(404, "Todo를 찾을 수 없습니다.")
        if row["owner_id"] != user["id"] and row["visibility"] != "shared":
            raise HTTPException(403, "개인 Todo는 등록자만 완료 상태를 변경할 수 있습니다.")
        completed = 0 if row["completed"] else 1
        conn.execute("UPDATE todos SET completed=?,status=? WHERE id=?",
                     (completed, "done" if completed else "todo", todo_id))
    audit(request, user["id"], "TODO_COMPLETE" if completed else "TODO_REOPEN",
          todo_id=todo_id, title=row["title"])
    return {"ok": True, "completed": bool(completed)}


@app.get("/api/kanban")
def get_kanban_data(request: Request, x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    with db() as conn:
        users = conn.execute("SELECT id,display_name FROM users ORDER BY display_name COLLATE NOCASE,id").fetchall()
        todos = conn.execute("""SELECT t.id,t.title,t.due_date,t.priority,t.completed,t.status,
                                       t.owner_id,COALESCE(t.assignee_id,t.owner_id) AS assignee_id,
                                       t.created_at,u.display_name
                                FROM todos t JOIN users u ON u.id=COALESCE(t.assignee_id,t.owner_id)
                                WHERE t.deleted_at IS NULL AND t.visibility='shared'
                                ORDER BY t.due_date IS NULL,t.due_date,t.created_at,t.id""").fetchall()
    audit(request, user["id"], "KANBAN_VIEW", result_count=len(todos))
    return {"users":[dict(row) for row in users],"todos":[dict(row) for row in todos]}


@app.patch("/api/kanban/todos/{todo_id}")
def move_kanban_todo(todo_id: int, payload: TodoMoveIn, request: Request,
                     x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    if payload.status not in ("todo", "in_progress", "blocked", "done", "trash"):
        raise HTTPException(422, "칸반 단계가 올바르지 않습니다.")
    with db() as conn:
        row = conn.execute("SELECT title,visibility,deleted_at,completed FROM todos WHERE id=?", (todo_id,)).fetchone()
        if not row or row["deleted_at"]:
            raise HTTPException(404, "Todo를 찾을 수 없습니다.")
        if row["visibility"] != "shared":
            raise HTTPException(403, "공용 Todo만 칸반 보드에서 이동할 수 있습니다.")
        if not conn.execute("SELECT 1 FROM users WHERE id=?", (payload.owner_id,)).fetchone():
            raise HTTPException(404, "담당 사용자를 찾을 수 없습니다.")
        if payload.status == "trash":
            deleted_at = datetime.now().isoformat(timespec="seconds")
            conn.execute("UPDATE todos SET deleted_at=?,deleted_by=? WHERE id=?",
                         (deleted_at,user["id"],todo_id))
            completed = bool(row["completed"])
        else:
            completed = 1 if payload.status == "done" else 0
            conn.execute("UPDATE todos SET assignee_id=?,status=?,completed=? WHERE id=?",
                         (payload.owner_id,payload.status,completed,todo_id))
    audit(request, user["id"], "TODO_KANBAN_MOVE", todo_id=todo_id, title=row["title"],
          owner_id=payload.owner_id, status=payload.status)
    return {"ok": True, "completed": bool(completed)}


@app.delete("/api/todos/{todo_id}")
def delete_todo(todo_id: int, request: Request,
                x_user_id: str | None = Header(default=None)):
    user = get_user(request, x_user_id)
    with db() as conn:
        row = conn.execute("SELECT owner_id,title,deleted_at FROM todos WHERE id=?", (todo_id,)).fetchone()
        if not row or row["deleted_at"]:
            raise HTTPException(404, "Todo를 찾을 수 없습니다.")
        if row["owner_id"] != user["id"]:
            raise HTTPException(403, "본인이 등록한 Todo만 삭제할 수 있습니다.")
        conn.execute("UPDATE todos SET deleted_at=?,deleted_by=? WHERE id=?",
                     (datetime.now().isoformat(timespec="seconds"), user["id"], todo_id))
    audit(request, user["id"], "TODO_DELETE", todo_id=todo_id, title=row["title"])
    return {"ok": True}



HTML = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>사내 공용 캘린더</title>
<style>
:root{font-family:Inter,"Pretendard","Noto Sans KR","Malgun Gothic",system-ui,sans-serif;color:#172033;background:#f3f6fb;font-synthesis:none;text-rendering:optimizeLegibility;--primary:#3659d9;--primary-dark:#2848bd;--line:#e6eaf2;--muted:#758096;--surface:#fff;--soft:#f7f9fc;--shadow:0 12px 32px rgba(23,35,65,.07)}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 0 0,#edf2ff 0,transparent 32%),#f4f6fa}button,input,select,textarea{font:inherit}button{border:1px solid #dce2ed;background:#fff;color:#344054;border-radius:10px;padding:9px 13px;cursor:pointer;transition:.15s ease}button:hover{border-color:#aebce2;background:#f8faff}button:disabled{opacity:.55;cursor:not-allowed}.primary{background:var(--primary);color:#fff;border-color:var(--primary)}.primary:hover{background:var(--primary-dark);color:#fff}.quiet{border-color:transparent;background:transparent}.danger{color:#c03945;border-color:#f1d5d8;background:#fff8f8}.shell{max-width:1600px;margin:auto;padding:26px 30px 40px}.topbar{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:22px}.brand{display:flex;align-items:center;gap:13px}.brandmark{width:42px;height:42px;border-radius:13px;background:linear-gradient(145deg,#4669ee,#2944b6);display:grid;place-items:center;color:#fff;font-size:20px;box-shadow:0 8px 18px #3659d933}.brand h1{margin:0;font-size:21px;letter-spacing:-.6px}.brand p{margin:3px 0 0;color:var(--muted);font-size:12px}.top-actions{display:flex;gap:9px;align-items:center}.user-chip{display:flex;gap:8px;align-items:center;border:1px solid var(--line);background:#fff;border-radius:999px;padding:7px 12px;font-size:13px;color:#4b5568}.dot{width:9px;height:9px;border-radius:50%;display:inline-block}.page-grid{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:18px;align-items:start}.panel{background:var(--surface);border:1px solid #e7ebf3;border-radius:17px;box-shadow:var(--shadow);min-width:0}.calendar-panel{padding:20px}.side-panel{padding:18px;position:sticky;top:16px}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}.toolbar h2{margin:0;font-size:19px;letter-spacing:-.5px}.toolbar-left,.toolbar-right{display:flex;align-items:center;gap:7px}.icon-btn{font-size:17px;width:38px;height:38px;padding:0}.view-switch{display:flex;padding:3px;border-radius:11px;background:#f0f3f9;gap:2px}.view-switch button{border:0;background:transparent;padding:7px 12px;font-size:12px;color:#667085}.view-switch button.active{background:#fff;color:var(--primary);box-shadow:0 1px 4px #172b4d18}.calendar{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));border-left:1px solid var(--line);border-top:1px solid var(--line);overflow:hidden;border-radius:10px}.weekday{padding:11px 5px;text-align:center;background:#f8faff;border-right:1px solid var(--line);border-bottom:1px solid var(--line);font-size:11px;font-weight:700;letter-spacing:.3px;color:#778197}.day{height:132px;min-width:0;padding:7px 7px 6px;border-right:1px solid var(--line);border-bottom:1px solid var(--line);overflow:hidden;cursor:pointer;background:#fff;transition:background .12s}.day:hover{background:#f9fbff}.day.other{background:#fbfcfe}.day.today{background:#f5f7ff}.day-head{height:25px;display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}.num{width:25px;height:25px;display:grid;place-items:center;border-radius:50%;font-size:12px;color:#45516a;font-weight:600}.today .num{background:var(--primary);color:#fff}.day-events{display:flex;flex-direction:column;gap:3px;overflow:hidden}.event-chip{min-width:0;display:flex;align-items:center;gap:4px;max-width:100%;padding:3px 5px;border-radius:5px;color:#fff;font-size:10px;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-align:left;border:0}.event-chip:hover{filter:brightness(.96);color:#fff}.event-chip .kind{font-weight:800;opacity:.9}.event-chip.continues-left{border-top-left-radius:0;border-bottom-left-radius:0;margin-left:-7px;padding-left:10px}.event-chip.continues-right{border-top-right-radius:0;border-bottom-right-radius:0;margin-right:-7px}.more-link{border:0;background:transparent;color:var(--primary);font-size:10px;text-align:left;padding:2px 4px;font-weight:700}.more-link:hover{background:#eef2ff}.calendar-hint{font-size:11px;color:#8791a4;margin:11px 2px 0}.side-section+.side-section{border-top:1px solid #edf0f5;margin-top:18px;padding-top:17px}.section-title{display:flex;align-items:center;justify-content:space-between;margin:0 0 11px;font-size:14px;font-weight:750}.side-label{color:#97a0b1;font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase}.field{margin:10px 0}.field label{display:block;font-size:11px;color:#667085;margin:0 0 5px;font-weight:650}input,select,textarea{width:100%;border:1px solid #dce2ed;border-radius:10px;background:#fff;color:#273247;padding:10px 11px;outline:none;transition:border .15s,box-shadow .15s}input:focus,select:focus,textarea:focus{border-color:#8da2ed;box-shadow:0 0 0 3px #3659d91a}textarea{resize:vertical}input::placeholder,textarea::placeholder{color:#a6afbf}.full{width:100%}.inline{display:flex;gap:8px}.inline>*{min-width:0;flex:1}.natural-help{margin:7px 1px 0;font-size:10px;line-height:1.5;color:#8993a6}.parse-result{display:none;padding:10px;border-radius:10px;background:#f2f5ff;color:#3e529e;font-size:11px;line-height:1.6;margin-top:9px}.todo-list{margin-top:10px}.todo-item{display:flex;gap:8px;align-items:flex-start;padding:9px 0;border-bottom:1px solid #f0f2f6;font-size:12px}.todo-item:last-child{border-bottom:0}.todo-item input{width:15px;height:15px;margin:1px 0 0;accent-color:var(--primary)}.todo-main{flex:1;min-width:0}.todo-title{display:block;line-height:1.45;overflow-wrap:anywhere}.todo-item.done .todo-title{text-decoration:line-through;color:#a4acba}.todo-meta{font-size:10px;color:#929bad;margin-top:3px}.todo-delete{padding:2px 6px;border:0;color:#9aa3b3;background:transparent}.todo-filter{font-size:10px;padding:5px 7px;border-radius:8px}.modal{position:fixed;inset:0;display:none;align-items:center;justify-content:center;padding:18px;background:rgba(16,24,40,.48);backdrop-filter:blur(3px);z-index:20}.dialog{width:min(650px,100%);max-height:92vh;overflow:auto;background:#fff;border-radius:18px;padding:22px;box-shadow:0 24px 70px #15213b35}.dialog.wide{width:min(850px,100%)}.dialog-head{display:flex;justify-content:space-between;align-items:center;gap:14px;margin-bottom:14px}.day-dialog-actions{display:flex;align-items:center;justify-content:flex-end;gap:12px;flex:none}.day-dialog-actions .close{flex:0 0 34px}.dialog-head h2{font-size:18px;margin:0;letter-spacing:-.4px}.close{border:0;background:#f3f5f9;border-radius:9px;width:32px;height:32px;padding:0;font-size:18px}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}.form-grid .span2{grid-column:1/-1}.description{min-height:142px}.dialog-actions{display:flex;justify-content:flex-end;align-items:center;gap:8px;margin-top:16px;padding-top:14px;border-top:1px solid #edf0f5}.muted{color:var(--muted)}.hidden{display:none!important}.event-list{display:flex;flex-direction:column;gap:9px}.event-row{display:flex;gap:12px;align-items:flex-start;border:1px solid #e8ecf3;border-radius:12px;padding:12px}.event-color{width:4px;min-height:46px;border-radius:4px;flex:none}.event-row-main{flex:1;min-width:0}.event-row-title{font-size:13px;font-weight:750;overflow-wrap:anywhere}.event-row-meta{font-size:10px;color:#828da1;margin-top:5px;line-height:1.5}.pill{display:inline-flex;align-items:center;border-radius:999px;padding:3px 7px;background:#f0f3fa;color:#59657b;font-size:10px;font-weight:700;margin-right:4px}.attachment-list{display:flex;flex-wrap:wrap;gap:7px;margin-top:8px}.attachment{display:flex;align-items:center;gap:6px;background:#f5f7fb;border:1px solid #e8ecf3;padding:6px 8px;border-radius:8px;font-size:10px}.attachment a{color:#3659d9;text-decoration:none}.search-bar{display:flex;align-items:center;gap:7px;width:min(360px,38vw);min-width:250px;flex:0 1 360px;flex-wrap:nowrap;white-space:nowrap}.search-bar input{padding:9px 12px;min-width:0;flex:1 1 auto;width:0}.search-bar button{flex:0 0 auto;min-width:62px;white-space:nowrap}.top-actions{min-width:0}.search-filters{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:8px;margin-bottom:13px}.week-scroll{overflow:auto;border:1px solid var(--line);border-radius:10px}.week-grid{display:grid;grid-template-columns:64px repeat(7,minmax(112px,1fr));min-width:850px}.week-corner,.week-head,.week-label,.week-cell{border-right:1px solid var(--line);border-bottom:1px solid var(--line);min-width:0}.week-head{background:#f8faff;padding:10px 4px;text-align:center;font-size:11px;font-weight:750;color:#5d6980}.week-head.today{color:var(--primary);background:#f0f3ff}.week-label{display:flex;align-items:flex-start;justify-content:center;padding:10px 3px;background:#fafbfd;color:#8b95a7;font-size:10px;font-weight:650}.week-cell{min-height:62px;height:auto;padding:4px;overflow:visible;background:#fff}.week-cell:hover{background:#fafbff}.week-cell.all-day{min-height:76px;height:auto;background:#fbfcff}.week-chip{display:block;width:100%;margin:0 0 3px;text-align:left;border:0;border-radius:5px;padding:4px 5px;color:white;font-size:10px;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.week-more{border:0;background:transparent;color:var(--primary);padding:2px;font-size:10px}.pill.cat-SR{background:#e8f1ff;color:#245da8}.pill.cat-CM{background:#f1eaff;color:#7042a4}.pill.cat-회의{background:#e3f5ef;color:#137761}.pill.cat-휴가{background:#fff2df;color:#a96513}.pill.cat-기타{background:#edf0f4;color:#5d6878}.settings-note{font-size:11px;line-height:1.6;color:#8791a4}.nav-link{display:inline-flex;align-items:center;padding:9px 12px;border:1px solid #dce2ed;border-radius:10px;color:#344054;text-decoration:none;font-size:12px}.event-comments{display:flex;flex-direction:column;gap:8px;margin:8px 0}.event-comment{border:1px solid #e8ecf3;border-radius:10px;padding:9px 11px}.event-comment-head{display:flex;justify-content:space-between;gap:8px;font-size:11px;color:#778197}.event-comment-body{font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere;margin-top:6px}.event-comment-actions{display:flex;gap:4px}.event-comment-actions button{font-size:10px;padding:3px 7px}.comment-compose{display:flex;align-items:flex-end;gap:8px}.comment-compose textarea{flex:1;min-height:54px}.comment-compose button{white-space:nowrap}.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#202b43;color:white;border-radius:10px;padding:10px 16px;font-size:12px;z-index:50;box-shadow:var(--shadow);display:none}
@media(max-width:1120px){.page-grid{grid-template-columns:minmax(0,1fr) 290px}.shell{padding:20px}}
@media(max-width:900px){.page-grid{grid-template-columns:1fr}.side-panel{position:static}.calendar-panel{padding:12px}.day{height:105px;padding:5px}.event-chip{font-size:9px}.topbar{align-items:flex-start;flex-wrap:wrap}.search-bar{width:min(350px,60vw)}}
@media(max-width:600px){.shell{padding:12px}.topbar{gap:12px}.top-actions{width:100%;justify-content:space-between;flex-wrap:wrap}.search-bar{order:3;width:100%;min-width:0;flex:1 1 100%}.toolbar{flex-wrap:wrap}.toolbar h2{order:3;width:100%;text-align:center}.day{height:88px;padding:3px}.num{width:22px;height:22px}.event-chip{padding:2px 3px;font-size:8px}.weekday{font-size:10px}.form-grid{grid-template-columns:1fr}.form-grid .span2{grid-column:auto}.search-filters{grid-template-columns:1fr 1fr}.search-filters input:first-child{grid-column:1/-1}}
</style></head><body>
<div class="shell">
<header class="topbar">
 <div class="brand"><div class="brandmark">▦</div><div><h1>Team Calendar</h1><p>팀 일정과 업무를 한곳에서 관리하세요</p></div></div>
 <div class="top-actions">
  <div class="search-bar"><input id="searchInput" placeholder="일정, 담당자, 작업번호 검색…" aria-label="일정 검색"><button id="searchBtn" class="primary">검색</button></div>
  <div class="user-chip"><span class="dot" id="userDot" style="background:#3659d9"></span><span id="who">사용자 확인 중</span></div>
  <a class="nav-link" href="/kanban">팀 칸반</a><a class="nav-link" href="/admin">관리자</a><button id="settingsBtn">⚙ 설정</button>
 </div>
</header>
<div class="page-grid">
 <main class="panel calendar-panel">
  <div class="toolbar"><div class="toolbar-left"><button class="icon-btn" id="prev" aria-label="이전">‹</button><button id="today">오늘</button><button class="icon-btn" id="next" aria-label="다음">›</button></div><h2 id="periodTitle"></h2><div class="toolbar-right"><div class="view-switch"><button id="monthView" class="active">월간</button><button id="weekView">주간</button></div><button id="addEvent" class="primary">＋ 일정</button></div></div>
  <div id="calendar" class="calendar"></div>
  <p class="calendar-hint">날짜를 누르면 해당 날짜 상세 일정이 열립니다. 빈 날짜를 두 번 누르거나 '+ 일정'으로 일정을 등록할 수 있습니다.</p>
 </main>
 <aside class="panel side-panel">
  <section class="side-section"><div class="side-label">QUICK ADD</div><h3 class="section-title" style="margin-top:5px">자연어로 일정 등록</h3><div class="field"><input id="naturalInput" placeholder="예: 다음 주 수요일 2시부터 3시 회의"></div><button class="primary full" id="parseBtn">일정 분석하기</button><div class="parse-result" id="parseResult"></div><p class="natural-help">오전/오후를 생략한 1~11시는 오후로 해석합니다. 분석 결과 확인 후 저장하세요.</p></section>
  <section class="side-section"><div class="side-label">MY TASKS</div><div class="section-title" style="margin-top:5px"><span>Todo</span><select id="todoFilter" class="todo-filter" style="width:auto"><option value="all">전체</option><option value="open">진행 중</option><option value="done">완료</option></select></div><div class="field"><input id="todoTitle" placeholder="새로운 할 일을 입력하세요"></div><div class="inline"><input id="todoDue" type="date" aria-label="마감일"><select id="todoScope" aria-label="공개 범위"><option value="personal">개인</option><option value="shared">공용</option></select></div><div class="field"><select id="todoPriority"><option value="normal">보통 우선순위</option><option value="high">높은 우선순위</option><option value="low">낮은 우선순위</option></select></div><button id="todoAdd" class="full">Todo 추가</button><div id="todoList" class="todo-list"></div><p class="natural-help">완료한 Todo는 자동 삭제되지 않습니다. 필요할 때 휴지통으로 직접 삭제할 수 있습니다.</p></section>
 </aside>
</div></div>

<div class="modal" id="eventModal"><div class="dialog"><div class="dialog-head"><h2 id="eventDialogTitle">일정 등록</h2><button class="close" data-close="eventModal">×</button></div>
 <div class="form-grid">
  <div class="field span2"><label>일정 제목 *</label><input id="eventTitle" maxlength="120" placeholder="일정 제목을 입력하세요"></div>
  <div class="field"><label>일정 유형 *</label><select id="eventCategory"><option>SR</option><option>CM</option><option>회의</option><option>휴가</option><option selected>기타</option></select></div>
  <div class="field hidden" id="environmentField"><label>환경 * (SR / CM 필수)</label><select id="eventEnvironment"><option value="">환경 선택</option><option>Dev</option><option>Stg</option><option>Prd</option></select></div>
  <div class="span2 sr-cm-fields hidden" id="srCmFields"><div class="form-grid">
   <div class="field"><label>담당자 (선택)</label><input id="eventAssignee" maxlength="60" placeholder="담당자 이름"></div>
   <div class="field"><label>작업번호 (선택)</label><input id="eventTicket" maxlength="80" placeholder="예: SR-2026-001"></div>
  </div></div>
  <div class="span2"><div class="form-grid date-time-grid">
   <div class="field"><label>시작 일시</label><input id="eventStart" type="datetime-local"></div>
   <div class="field"><label>종료 일시</label><input id="eventEnd" type="datetime-local"></div>
  </div></div>
  <div class="field span2"><label><input id="eventAllDay" type="checkbox" style="width:auto;vertical-align:middle;margin-right:5px">종일 일정</label></div>
  <div class="field span2"><label>설명 / 작업 내용</label><textarea id="eventDesc" class="description" rows="6" placeholder="일정 상세 내용, 작업 계획, 참고 사항 등을 입력하세요."></textarea></div>
  <div class="field span2 hidden" id="eventCommentsSection"><label>댓글</label><div id="eventCommentsList" class="event-comments"></div><div class="comment-compose"><textarea id="eventCommentText" rows="2" maxlength="2000" placeholder="일정에 대한 의견이나 진행 상황을 남겨보세요."></textarea><button id="addEventComment" class="primary">댓글 등록</button></div></div>
  <div class="field span2"><label>작업계획서 및 첨부파일 (파일당 최대 20MB)</label><input id="eventFiles" type="file" multiple><div id="existingAttachments" class="attachment-list"></div></div>
 </div>
 <p id="eventOwner" class="settings-note"></p><div class="dialog-actions"><button id="deleteEvent" class="danger hidden" style="margin-right:auto">일정 삭제</button><button data-close="eventModal">취소</button><button id="saveEvent" class="primary">저장</button></div>
</div></div>

<div class="modal" id="dayModal"><div class="dialog wide"><div class="dialog-head"><h2 id="dayDialogTitle">일정 상세</h2><div class="day-dialog-actions"><button id="addDayEvent" class="primary">＋ 이 날짜에 일정 추가</button><button class="close" data-close="dayModal" aria-label="닫기">×</button></div></div><div id="dayEventList" class="event-list"></div></div></div>
<div class="modal" id="searchModal"><div class="dialog wide"><div class="dialog-head"><h2>일정 검색</h2><button class="close" data-close="searchModal">×</button></div>
 <div class="search-filters"><input id="searchQuery" placeholder="제목, 설명, 담당자, 작업번호"><select id="searchCategory"><option value="">모든 유형</option><option>SR</option><option>CM</option><option>회의</option><option>휴가</option><option>기타</option></select><select id="searchEnvironment"><option value="">모든 환경</option><option>Dev</option><option>Stg</option><option>Prd</option></select><input id="searchFrom" type="date" aria-label="시작 날짜"><input id="searchTo" type="date" aria-label="종료 날짜"></div>
 <button id="runSearch" class="primary" style="margin-bottom:13px">검색 실행</button><div id="searchResults" class="event-list"></div></div></div>
<div class="modal" id="settingsModal"><div class="dialog"><div class="dialog-head"><h2>사용자 설정</h2><button class="close" data-close="settingsModal">×</button></div><div class="field"><label>캘린더 표시 이름</label><input id="displayName" maxlength="30"></div><p class="settings-note">이름은 서버의 사용자 정보에 저장됩니다. 같은 브라우저로 다시 접속하면 이름을 다시 입력할 필요가 없습니다. 브라우저 저장소를 초기화한 경우에도 해당 IP에 등록 사용자가 한 명이면 기존 사용자를 연결합니다.</p><div class="dialog-actions"><button data-close="settingsModal">취소</button><button id="saveSettings" class="primary">이름 저장</button></div></div></div>
<div class="toast" id="toast"></div>

<script>
const $=id=>document.getElementById(id);
let user=null,cursor=new Date(),events=[],editing=null,dayFocus=null,viewMode='month',todos=[];
const pad=n=>String(n).padStart(2,'0');
const dateKey=d=>`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
const localInput=d=>`${dateKey(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
const parseDate=s=>new Date(`${s.slice(0,10)}T00:00:00`);
const headers=()=>({'X-User-Id':user.id});
async function api(url,opts={}){const h={...headers(),...(opts.headers||{})};if(opts.body&&!(opts.body instanceof FormData))h['Content-Type']='application/json';const r=await fetch(url,{...opts,headers:h});if(!r.ok){const data=await r.json().catch(()=>({}));throw Error(data.detail||'요청을 처리하지 못했습니다.')}const ct=r.headers.get('content-type')||'';return ct.includes('application/json')?r.json():r}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function showToast(message){$('toast').textContent=message;$('toast').style.display='block';clearTimeout(showToast.timer);showToast.timer=setTimeout(()=>$('toast').style.display='none',2600)}
function openModal(id){$(id).style.display='flex'}function closeModal(id){$(id).style.display='none'}
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>closeModal(b.dataset.close));
function createBrowserUuid(){
 if(window.crypto&&typeof window.crypto.randomUUID==='function')return window.crypto.randomUUID();
 if(window.crypto&&typeof window.crypto.getRandomValues==='function'){
  const bytes=new Uint8Array(16);window.crypto.getRandomValues(bytes);
  bytes[6]=(bytes[6]&0x0f)|0x40;bytes[8]=(bytes[8]&0x3f)|0x80;
  const hex=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
  return hex.slice(0,8)+'-'+hex.slice(8,12)+'-'+hex.slice(12,16)+'-'+hex.slice(16,20)+'-'+hex.slice(20);
 }
 // 구형/제한 브라우저의 최후 호환 경로. UUID는 식별용이며 인증 수단이 아닙니다.
 return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g,c=>{
  const r=Math.floor(Math.random()*16);return (c==='x'?r:(r&0x3|0x8)).toString(16);
 });
}
function getBrowserId(){let id=localStorage.getItem('company-calendar-user');if(!id){id=createBrowserUuid();localStorage.setItem('company-calendar-user',id)}return id}
async function identify(){let id=getBrowserId();let savedName=localStorage.getItem('company-calendar-name')||null;let result=await fetch('/api/users/identify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:id,display_name:savedName})}).then(async r=>{if(!r.ok)throw Error('사용자 식별 실패');return r.json()});
 if(result.needs_name){let name=prompt('캘린더에 표시할 이름을 입력하세요');if(!name||!name.trim())name='익명 사용자';result=await fetch('/api/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:result.id,display_name:name.trim()})}).then(r=>r.json());localStorage.setItem('company-calendar-user',result.id);localStorage.setItem('company-calendar-name',result.display_name)}else{localStorage.setItem('company-calendar-user',result.id);localStorage.setItem('company-calendar-name',result.display_name)}
 user=result;$('who').textContent=user.display_name;$('userDot').style.background=user.color;$('displayName').value=user.display_name;
}
function eventEndDate(e){return (e.end_at||e.start_at).slice(0,10)}
function eventOccursOn(e,key){return e.start_at.slice(0,10)<=key&&eventEndDate(e)>=key}
function dayEvents(key){return events.filter(e=>eventOccursOn(e,key)).sort((a,b)=>(b.all_day-a.all_day)||a.start_at.localeCompare(b.start_at))}
function eventTimeLabel(e){if(e.all_day)return '종일';return `${e.start_at.slice(11,16)}${e.end_at?' – '+e.end_at.slice(11,16):''}`}
function categoryTag(e){return `<span class="pill">${esc(e.category||'기타')}</span>${e.environment?`<span class="pill">${esc(e.environment)}</span>`:''}`}
function eventChip(e,key,week=false){let start=e.start_at.slice(0,10),end=eventEndDate(e);let cls=(start<key?' continues-left ':'')+(end>key?' continues-right':'');let title=`${e.all_day?'종일':e.start_at.slice(11,16)} ${e.title}`;return `<button class="${week?'week-chip':'event-chip'}${cls}" data-event-id="${e.id}" style="background:${esc(e.color)}" title="${esc(title)}">${e.category==='SR'||e.category==='CM'?`<b class="kind">${esc(e.category)}</b>`:''}${esc(e.title)}</button>`}
function monthDraw(){const y=cursor.getFullYear(),m=cursor.getMonth();$('periodTitle').textContent=`${y}년 ${m+1}월`;const first=new Date(y,m,1),gridStart=new Date(y,m,1-first.getDay());let html=['일','월','화','수','목','금','토'].map(d=>`<div class="weekday">${d}</div>`).join('');
 for(let i=0;i<42;i++){let d=new Date(gridStart);d.setDate(gridStart.getDate()+i);let key=dateKey(d),list=dayEvents(key),visible=list.slice(0,3);html+=`<div class="day ${d.getMonth()!==m?'other':''} ${key===dateKey(new Date())?'today':''}" data-day="${key}"><div class="day-head"><span class="num">${d.getDate()}</span><span class="muted" style="font-size:9px">${list.length?list.length+'건':''}</span></div><div class="day-events">${visible.map(e=>eventChip(e,key)).join('')}${list.length>3?`<button class="more-link" data-open-day="${key}">+ ${list.length-3}건 더보기</button>`:''}</div></div>`}
 $('calendar').className='calendar';$('calendar').innerHTML=html;
 $('calendar').querySelectorAll('.day').forEach(el=>el.addEventListener('click',ev=>{if(ev.target.closest('[data-event-id]'))return;openDay(el.dataset.day)}));
 $('calendar').querySelectorAll('[data-event-id]').forEach(el=>el.addEventListener('click',ev=>{ev.stopPropagation();openEvent(Number(el.dataset.eventId))}));
 $('calendar').querySelectorAll('[data-open-day]').forEach(el=>el.onclick=ev=>{ev.stopPropagation();openDay(el.dataset.openDay)});
}
function weekStart(d){let x=new Date(d);x.setDate(x.getDate()-x.getDay());x.setHours(0,0,0,0);return x}
function weekDraw(){const start=weekStart(cursor),end=new Date(start);end.setDate(start.getDate()+6);$('periodTitle').textContent=`${start.getFullYear()}년 ${start.getMonth()+1}월 ${start.getDate()}일 – ${end.getMonth()+1}월 ${end.getDate()}일`;let html='<div class="week-corner"></div>';
 for(let i=0;i<7;i++){let d=new Date(start);d.setDate(start.getDate()+i);let key=dateKey(d);html+=`<div class="week-head ${key===dateKey(new Date())?'today':''}" data-day-header="${key}">${['일','월','화','수','목','금','토'][i]}<br>${d.getMonth()+1}/${d.getDate()}</div>`}
 const rows=[{label:'종일',type:'all-day'}, {label:'기타 시간',type:'off'}];for(let h=7;h<=21;h++)rows.push({label:`${pad(h)}:00`,type:'hour',hour:h});
 for(const row of rows){html+=`<div class="week-label">${row.label}</div>`;for(let i=0;i<7;i++){let d=new Date(start);d.setDate(start.getDate()+i);let key=dateKey(d),list=dayEvents(key).filter(e=>{
 if(row.type==='all-day')return !!e.all_day;
 if(e.all_day)return false;
 const sh=Number(e.start_at.slice(11,13)),eh=e.end_at?Number(e.end_at.slice(11,13)):sh;
 const startDate=e.start_at.slice(0,10),continues=startDate<key&&eventEndDate(e)>=key;
 if(row.type==='off')return sh<7||sh>21||eh>21||(continues&&(sh<7||sh>21));
 return (startDate===key&&sh===row.hour)||(continues&&sh===row.hour);
 });list.sort((a,b)=>a.start_at.localeCompare(b.start_at));html+=`<div class="week-cell ${row.type==='all-day'?'all-day':''}" data-week-day="${key}" data-week-hour="${row.type==='hour'?row.hour:''}">${list.map(e=>eventChip(e,key,true)).join('')}</div>`}}
 $('calendar').className='week-scroll';$('calendar').innerHTML=`<div class="week-grid">${html}</div>`;
 $('calendar').querySelectorAll('[data-event-id]').forEach(el=>el.onclick=ev=>{ev.stopPropagation();openEvent(Number(el.dataset.eventId))});
 $('calendar').querySelectorAll('[data-week-day]').forEach(el=>el.onclick=ev=>{if(ev.target.closest('[data-event-id]')||ev.target.closest('[data-week-more]'))return;let hour=el.dataset.weekHour;openNew(el.dataset.weekDay,hour!==''?Number(hour):9)});
 $('calendar').querySelectorAll('[data-week-more]').forEach(el=>el.onclick=ev=>{ev.stopPropagation();openDay(el.dataset.weekMore)});
 $('calendar').querySelectorAll('[data-day-header]').forEach(el=>el.onclick=()=>openDay(el.dataset.dayHeader));
}
function draw(){if(viewMode==='month')monthDraw();else weekDraw()}
async function refresh(){const start=viewMode==='month'?new Date(cursor.getFullYear(),cursor.getMonth(),1):weekStart(cursor);const end=viewMode==='month'?new Date(cursor.getFullYear(),cursor.getMonth()+1,7):new Date(weekStart(cursor).getTime()+6*86400000);start.setDate(start.getDate()-7);end.setDate(end.getDate()+7);events=await api(`/api/events?start=${dateKey(start)}&end=${dateKey(end)}`);draw();await loadTodos()}
function openNew(day=dateKey(new Date()),hour=9,focusTarget='eventTitle'){editing=null;$('eventCommentsSection').classList.add('hidden');$('eventCommentsList').innerHTML='';$('eventCommentText').value='';$('eventDialogTitle').textContent='일정 등록';$('eventTitle').value='';$('eventDesc').value='';$('eventCategory').value='기타';$('eventEnvironment').value='';$('eventAssignee').value='';$('eventTicket').value='';$('eventStart').value=`${day}T${pad(hour)}:00`;let end=new Date(`${day}T${pad(hour)}:00`);end.setHours(end.getHours()+1);$('eventEnd').value=localInput(end);$('eventAllDay').checked=false;$('eventFiles').value='';$('existingAttachments').innerHTML='';$('deleteEvent').classList.add('hidden');$('saveEvent').disabled=false;$('eventOwner').textContent='일정은 팀 전체에 공유됩니다. 본인이 등록한 일정만 수정·삭제할 수 있습니다.';updateEnvironment();openModal('eventModal');requestAnimationFrame(()=>$(focusTarget).focus())}
function updateEnvironment(){const isSrCm=['SR','CM'].includes($('eventCategory').value);$('environmentField').classList.toggle('hidden',!isSrCm);$('srCmFields').classList.toggle('hidden',!isSrCm);if(!isSrCm){$('eventEnvironment').value='';$('eventAssignee').value='';$('eventTicket').value=''}}
$('eventCategory').onchange=updateEnvironment;
function openEvent(event){const e=event&&typeof event==='object'?event:events.find(item=>item.id===Number(event));if(!e){showToast('일정을 찾을 수 없습니다.');return}editing=e;$('eventCommentsSection').classList.remove('hidden');$('eventCommentsList').innerHTML='<p class="muted" style="font-size:11px">댓글을 불러오는 중...</p>';loadEventComments(e.id).catch(err=>showToast(err.message));
async function loadEventComments(eventId){
 const comments=await api('/api/events/'+eventId+'/comments');
 $('eventCommentsList').innerHTML=comments.length?comments.map(c=>'<div class="event-comment"><div class="event-comment-head"><span>'+esc(c.display_name)+' · '+esc(c.created_at.replace('T',' '))+'</span>'+(c.user_id===user.id?'<span class="event-comment-actions"><button data-comment-edit="'+c.id+'">수정</button><button data-comment-delete="'+c.id+'">삭제</button></span>':'')+'</div><div class="event-comment-body">'+esc(c.content)+'</div></div>').join(''):'<p class="muted" style="font-size:11px">아직 댓글이 없습니다.</p>';
 $('eventCommentsList').querySelectorAll('[data-comment-edit]').forEach(b=>b.onclick=async()=>{const c=comments.find(x=>x.id===Number(b.dataset.commentEdit));const content=prompt('댓글 내용을 수정하세요.',c?c.content:'');if(content===null)return;try{await api('/api/event-comments/'+c.id,{method:'PATCH',body:JSON.stringify({content})});await loadEventComments(eventId)}catch(e){showToast(e.message)}});
 $('eventCommentsList').querySelectorAll('[data-comment-delete]').forEach(b=>b.onclick=async()=>{if(!confirm('댓글을 삭제할까요?'))return;try{await api('/api/event-comments/'+b.dataset.commentDelete,{method:'DELETE'});await loadEventComments(eventId)}catch(e){showToast(e.message)}});
}
$('addEventComment').onclick=async()=>{if(!editing)return;const content=$('eventCommentText').value.trim();if(!content){showToast('댓글 내용을 입력하세요.');return}try{await api('/api/events/'+editing.id+'/comments',{method:'POST',body:JSON.stringify({content})});$('eventCommentText').value='';await loadEventComments(editing.id);showToast('댓글을 등록했습니다.')}catch(e){showToast(e.message)}};
$('eventDialogTitle').textContent='일정 수정';$('eventTitle').value=e.title;$('eventDesc').value=e.description||'';$('eventCategory').value=e.category||'기타';$('eventEnvironment').value=e.environment||'';$('eventAssignee').value=e.assignee||'';$('eventTicket').value=e.ticket_no||'';$('eventStart').value=e.start_at.slice(0,16);$('eventEnd').value=e.end_at?e.end_at.slice(0,16):'';$('eventAllDay').checked=!!e.all_day;$('eventFiles').value='';updateEnvironment();$('deleteEvent').classList.toggle('hidden',e.owner_id!==user.id);$('saveEvent').disabled=e.owner_id!==user.id;$('eventOwner').textContent=`등록자: ${e.display_name} · ${eventTimeLabel(e)}`;$('existingAttachments').innerHTML=(e.attachments||[]).map(a=>`<span class="attachment"><a href="#" data-download="${a.id}">${esc(a.original_name)}</a> <small>${Math.ceil(a.size/1024)}KB</small>${e.owner_id===user.id?`<button class="quiet" data-attachment-delete="${a.id}" title="첨부 삭제">×</button>`:''}</span>`).join('');$('existingAttachments').querySelectorAll('[data-download]').forEach(a=>a.onclick=ev=>{ev.preventDefault();downloadAttachment(a.dataset.download)});$('existingAttachments').querySelectorAll('[data-attachment-delete]').forEach(b=>b.onclick=async()=>{if(confirm('첨부파일을 삭제할까요?')){try{await api(`/api/attachments/${b.dataset.attachmentDelete}`,{method:'DELETE'});showToast('첨부파일을 삭제했습니다.');await refresh();let latest=events.find(x=>x.id===e.id);if(latest)openEvent(latest)}catch(err){showToast(err.message)}}});openModal('eventModal');requestAnimationFrame(()=>$('eventDesc').focus())}
function openDay(key){dayFocus=key;let d=parseDate(key);$('dayDialogTitle').textContent=`${d.getFullYear()}년 ${d.getMonth()+1}월 ${d.getDate()}일 일정`;renderDayEvents();openModal('dayModal')}
function renderDayEvents(){let list=dayEvents(dayFocus);$('dayEventList').innerHTML=list.length?list.map(e=>`<div class="event-row"><div class="event-color" style="background:${e.color}"></div><div class="event-row-main"><div>${categoryTag(e)}</div><div class="event-row-title">${esc(e.title)}</div><div class="event-row-meta">${eventTimeLabel(e)} · ${esc(e.display_name)}${e.assignee?' · 담당: '+esc(e.assignee):''}${e.ticket_no?' · 작업번호: '+esc(e.ticket_no):''}<br>${esc(e.description||'')}</div>${(e.attachments||[]).length?`<div class="attachment-list">${e.attachments.map(a=>`<span class="attachment"><a href="#" data-day-download="${a.id}">${esc(a.original_name)}</a></span>`).join('')}</div>`:''}</div><button data-day-edit="${e.id}">상세 / 수정</button></div>`).join(''):'<p class="muted" style="font-size:12px">등록된 일정이 없습니다.</p>';$('dayEventList').querySelectorAll('[data-day-edit]').forEach(b=>b.onclick=()=>{let e=events.find(x=>x.id==b.dataset.dayEdit);if(e){closeModal('dayModal');openEvent(e)}});$('dayEventList').querySelectorAll('[data-day-download]').forEach(a=>a.onclick=ev=>{ev.preventDefault();downloadAttachment(a.dataset.dayDownload)})}
$('addDayEvent').onclick=()=>{closeModal('dayModal');openNew(dayFocus)};
async function downloadAttachment(id){try{const response=await fetch(`/api/attachments/${id}`,{headers:headers()});if(!response.ok)throw Error('첨부파일을 내려받지 못했습니다.');const blob=await response.blob();const disposition=response.headers.get('content-disposition')||'';let filename='attachment';const utf8=disposition.match(/filename\*=UTF-8''([^;]+)/i);const plain=disposition.match(/filename="?([^";]+)"?/i);if(utf8)filename=decodeURIComponent(utf8[1]);else if(plain)filename=plain[1];const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=filename;document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url)}catch(e){showToast(e.message)}}
$('saveEvent').onclick=async()=>{try{let start=$('eventStart').value,end=$('eventEnd').value||null,allDay=$('eventAllDay').checked;if(allDay){start=start.slice(0,10)+'T00:00';if(end)end=end.slice(0,10)+'T23:59'}let body={title:$('eventTitle').value.trim(),description:$('eventDesc').value,start_at:start,end_at:end,all_day:allDay,category:$('eventCategory').value,environment:$('eventEnvironment').value||null,assignee:$('eventAssignee').value.trim()||null,ticket_no:$('eventTicket').value.trim()||null};if(!body.title)throw Error('일정 제목을 입력하세요.');if(['SR','CM'].includes(body.category)&&!body.environment)throw Error('SR 및 CM은 환경(Dev/Stg/Prd)을 선택해야 합니다.');let id;if(editing){await api(`/api/events/${editing.id}`,{method:'PUT',body:JSON.stringify(body)});id=editing.id}else{id=(await api('/api/events',{method:'POST',body:JSON.stringify(body)})).id}let files=$('eventFiles').files;if(files.length){let form=new FormData();for(const f of files)form.append('files',f);await api(`/api/events/${id}/attachments`,{method:'POST',body:form})}$('eventModal').style.display='none';showToast('일정을 저장했습니다.');await refresh();if(dayFocus){renderDayEvents()}}catch(e){showToast(e.message)}};
$('deleteEvent').onclick=async()=>{if(editing&&confirm('일정을 삭제할까요?')){try{await api(`/api/events/${editing.id}`,{method:'DELETE'});closeModal('eventModal');showToast('일정을 삭제했습니다.');await refresh()}catch(e){showToast(e.message)}}};
$('eventAllDay').onchange=()=>{if($('eventAllDay').checked){let s=$('eventStart').value,e=$('eventEnd').value;if(s)$('eventStart').value=s.slice(0,10)+'T00:00';if(e)$('eventEnd').value=e.slice(0,10)+'T23:59'}};
$('prev').onclick=()=>{if(viewMode==='month')cursor.setMonth(cursor.getMonth()-1);else cursor.setDate(cursor.getDate()-7);refresh()};$('next').onclick=()=>{if(viewMode==='month')cursor.setMonth(cursor.getMonth()+1);else cursor.setDate(cursor.getDate()+7);refresh()};$('today').onclick=()=>{cursor=new Date();refresh()};$('addEvent').onclick=()=>openNew();$('monthView').onclick=()=>{viewMode='month';$('monthView').classList.add('active');$('weekView').classList.remove('active');refresh()};$('weekView').onclick=()=>{viewMode='week';$('weekView').classList.add('active');$('monthView').classList.remove('active');refresh()};
$('parseBtn').onclick=async()=>{try{if(!$('naturalInput').value.trim())throw Error('일정 내용을 입력하세요.');let parsed=await api('/api/parse',{method:'POST',body:JSON.stringify({text:$('naturalInput').value})});$('parseResult').style.display='block';$('parseResult').textContent=`분석 결과: ${parsed.date}${parsed.all_day&&parsed.end_at&&parsed.end_at.slice(0,10)!==parsed.date?' ~ '+parsed.end_at.slice(0,10):''} ${parsed.time_detected?parsed.start_at.slice(11,16):'종일'} · ${parsed.time_detected&&parsed.end_at?'종료 '+parsed.end_at.slice(11,16):''} · 제목: ${parsed.title}`;openNew(parsed.date,Number(parsed.start_at.slice(11,13)),'eventDesc');$('eventTitle').value=parsed.title;$('eventStart').value=parsed.start_at;$('eventEnd').value=parsed.end_at||'';$('eventAllDay').checked=parsed.all_day;if(parsed.all_day){$('eventStart').value=parsed.date+'T00:00';$('eventEnd').value=parsed.date+'T23:59'} }catch(e){showToast(e.message)}};
$('naturalInput').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();$('parseBtn').click()}});
$('todoAdd').onclick=async()=>{try{let title=$('todoTitle').value.trim();if(!title)throw Error('Todo 내용을 입력하세요.');await api('/api/todos',{method:'POST',body:JSON.stringify({title,due_date:$('todoDue').value||null,priority:$('todoPriority').value,visibility:$('todoScope').value})});$('todoTitle').value='';showToast('Todo를 추가했습니다.');await loadTodos()}catch(e){showToast(e.message)}};
$('todoFilter').onchange=renderTodos;
async function loadTodos(){todos=await api('/api/todos');renderTodos()}
function renderTodos(){let filter=$('todoFilter').value,list=todos.filter(t=>filter==='all'||(filter==='open'&&!t.completed)||(filter==='done'&&t.completed));$('todoList').innerHTML=list.map(t=>`<div class="todo-item ${t.completed?'done':''}"><input type="checkbox" data-todo="${t.id}" ${t.completed?'checked':''}><div class="todo-main"><span class="todo-title">${esc(t.title)}</span><div class="todo-meta">${t.visibility==='shared'?'공용':'개인'} · ${t.due_date||'마감일 없음'} · ${esc(t.display_name)}</div></div><button class="todo-delete" data-delete-todo="${t.id}" title="Todo 삭제">♲</button></div>`).join('')||'<p class="muted" style="font-size:11px">표시할 Todo가 없습니다.</p>';$('todoList').querySelectorAll('[data-todo]').forEach(c=>c.onchange=async()=>{try{await api(`/api/todos/${c.dataset.todo}`,{method:'PATCH'});await loadTodos()}catch(e){showToast(e.message);c.checked=!c.checked}});$('todoList').querySelectorAll('[data-delete-todo]').forEach(b=>b.onclick=async()=>{if(confirm('Todo를 삭제할까요?')){try{await api(`/api/todos/${b.dataset.deleteTodo}`,{method:'DELETE'});showToast('Todo를 삭제했습니다.');await loadTodos()}catch(e){showToast(e.message)}}})}
async function runSearch(){let params=new URLSearchParams({q:$('searchQuery').value,category:$('searchCategory').value,environment:$('searchEnvironment').value,from_date:$('searchFrom').value,to_date:$('searchTo').value});let results=await api(`/api/events/search?${params}`);$('searchResults').innerHTML=results.map(e=>`<div class="event-row"><div class="event-color" style="background:${e.color}"></div><div class="event-row-main"><div>${categoryTag(e)}</div><div class="event-row-title">${esc(e.title)}</div><div class="event-row-meta">${e.start_at.replace('T',' ')}${e.end_at?' ~ '+e.end_at.replace('T',' '):''} · ${esc(e.display_name)}${e.assignee?' · '+esc(e.assignee):''}${e.ticket_no?' · '+esc(e.ticket_no):''}</div></div><button data-search-edit="${e.id}">열기</button></div>`).join('')||'<p class="muted" style="font-size:12px">검색 결과가 없습니다.</p>';$('searchResults').querySelectorAll('[data-search-edit]').forEach(b=>b.onclick=()=>{let e=results.find(x=>x.id==b.dataset.searchEdit);closeModal('searchModal');openEvent(e)})}
$('searchBtn').onclick=()=>{$('searchQuery').value=$('searchInput').value;openModal('searchModal');runSearch()};$('searchInput').onkeydown=e=>{if(e.key==='Enter')$('searchBtn').click()};$('runSearch').onclick=()=>runSearch();$('searchQuery').onkeydown=e=>{if(e.key==='Enter')runSearch()};
$('settingsBtn').onclick=()=>{$('displayName').value=user.display_name;openModal('settingsModal')};$('saveSettings').onclick=async()=>{try{let name=$('displayName').value.trim();if(!name)throw Error('이름을 입력하세요.');user=await api('/api/users/me',{method:'PATCH',body:JSON.stringify({display_name:name})});localStorage.setItem('company-calendar-name',name);$('who').textContent=name;closeModal('settingsModal');showToast('사용자 이름을 변경했습니다.')}catch(e){showToast(e.message)}};
async function start(){
 try{
  await identify();
  await refresh();
 }catch(e){
  // DB 초기화/이전 등으로 브라우저에 저장된 UUID가 서버에 없으면 한 번 새 ID로 재등록합니다.
  if(String(e.message||'').includes('등록되지 않은 사용자')){
   localStorage.removeItem('company-calendar-user');
   try{
    await identify();
    await refresh();
    showToast('사용자 정보를 다시 등록했습니다.');
    return;
   }catch(retryError){
    showToast(retryError.message);
    return;
   }
  }
  showToast(e.message);
 }
}
start();
</script></body></html>'''


KANBAN_HTML = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>팀 TODO 칸반 보드</title>
<style>
:root{font-family:Inter,"Pretendard","Noto Sans KR","Malgun Gothic",system-ui,sans-serif;color:#172033;background:#f4f6fa;--line:#e6eaf2;--muted:#758096}
*{box-sizing:border-box}body{margin:0;background:#f4f6fa}.shell{max-width:1700px;margin:auto;padding:24px 28px 40px}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:20px}.top h1{font-size:21px;margin:0}.top p{font-size:12px;color:var(--muted);margin:5px 0 0}.top-actions{display:flex;gap:8px;align-items:center}a,button{font:inherit}a{color:#344054;text-decoration:none;border:1px solid #dce2ed;border-radius:9px;padding:9px 12px;background:white;font-size:12px}button{border:1px solid #dce2ed;border-radius:9px;padding:9px 12px;background:white;cursor:pointer}.board-note{font-size:12px;color:#69758b;margin:0 0 14px}.board-scroll{overflow:auto;border:1px solid var(--line);border-radius:13px;background:white}.board{min-width:900px}.board-head,.board-row{display:grid;grid-template-columns:170px repeat(5,minmax(190px,1fr))}.board-head{position:sticky;top:0;z-index:2;background:#f8faff;border-bottom:1px solid var(--line)}.board-head>div{padding:14px 13px;font-size:12px;font-weight:750;border-right:1px solid var(--line)}.board-head>div:nth-child(2){color:#64748b}.board-head>div:nth-child(3){color:#3563c9}.board-head>div:nth-child(4){color:#b7791f}.board-head>div:nth-child(5){color:#16836d}.board-head>div:nth-child(6){color:#16836d}.board-head>div:nth-child(7){color:#b84b58}.dropzone[data-status="trash"]{background:#fffafa}.dropzone[data-status="trash"] .empty{color:#c18c8c}.board-row{border-bottom:1px solid var(--line)}.board-row:last-child{border-bottom:0}.member{padding:14px 12px;border-right:1px solid var(--line);background:#fbfcfe;display:flex;align-items:flex-start;gap:8px;font-size:12px;font-weight:700}.member-count{display:block;font-size:10px;color:#8a94a6;font-weight:500;margin-top:4px}.dropzone{min-height:112px;padding:9px;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:8px;transition:background .12s}.dropzone:last-child{border-right:0}.dropzone.drag-over{background:#eef3ff;outline:2px dashed #8ba3ed;outline-offset:-4px}.task{background:white;border:1px solid #e5eaf2;border-left:3px solid #9aa7bd;border-radius:9px;padding:10px 11px;box-shadow:0 2px 7px rgba(20,35,65,.04);cursor:grab}.task:active{cursor:grabbing}.task.high{border-left-color:#d97757}.task-title{font-size:12px;font-weight:700;line-height:1.5;overflow-wrap:anywhere}.task-meta{font-size:10px;color:#7c879b;margin-top:7px;display:flex;justify-content:space-between;gap:6px}.empty{color:#a0a8b7;font-size:10px;text-align:center;padding:12px 0}.status-count{font-size:10px;color:#8b95a7;font-weight:500;margin-left:5px}.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#202b43;color:white;padding:10px 15px;border-radius:9px;font-size:12px;display:none}
@media(max-width:700px){.shell{padding:15px}.top{align-items:flex-start;flex-direction:column}.board-head,.board-row{grid-template-columns:125px repeat(3,minmax(190px,1fr))}}
</style></head><body><div class="shell">
<header class="top"><div><h1>팀 TODO 칸반 보드</h1><p>담당자별 업무 진행 현황</p></div><div class="top-actions"><a href="/">← 캘린더로 돌아가기</a><button id="reload">새로고침</button></div></header>
<p class="board-note">공용 TODO만 표시됩니다. 카드를 다른 칸으로 옮기면 진행 단계가 변경되고, 다른 구성원의 행으로 옮기면 담당자도 변경됩니다. 완료됨은 메인 TODO에 완료로 반영됩니다. 휴지통으로 이동한 항목은 목록에서 숨겨지고 관리자 페이지에서 복구할 수 있습니다.</p>
<div class="board-scroll"><div class="board" id="board"><div class="empty">보드를 불러오는 중...</div></div></div>
</div><div class="toast" id="toast"></div>
<script>
const $=id=>document.getElementById(id);let userId=localStorage.getItem('company-calendar-user');let draggedId=null;
const statuses=[{id:'todo',label:'대기'},{id:'in_progress',label:'진행 중'},{id:'blocked',label:'보류됨'},{id:'done',label:'완료됨'},{id:'trash',label:'휴지통'}];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(message){$('toast').textContent=message;$('toast').style.display='block';setTimeout(()=>$('toast').style.display='none',2500)}
async function api(url,opts={}){const r=await fetch(url,{...opts,headers:{'X-User-Id':userId,'Content-Type':'application/json',...(opts.headers||{})}});if(!r.ok){const data=await r.json().catch(()=>({}));throw Error(data.detail||'요청을 처리하지 못했습니다.')}return r.json()}
async function load(){if(!userId){toast('사용자 정보가 없습니다. 캘린더에서 다시 접속해 주세요.');return}try{const data=await api('/api/kanban');render(data)}catch(e){toast(e.message)}}
function render(data){const board=$('board');let html='<div class="board-head"><div>담당자</div>'+statuses.map(s=>'<div>'+s.label+' <span class="status-count">'+data.todos.filter(t=>t.status===s.id).length+'</span></div>').join('')+'</div>';
for(const member of data.users){const memberTodos=data.todos.filter(t=>t.assignee_id===member.id);html+='<div class="board-row"><div class="member"><div>'+esc(member.display_name)+'<span class="member-count">'+memberTodos.length+'개 업무</span></div></div>';
for(const status of statuses){const items=memberTodos.filter(t=>t.status===status.id);html+='<div class="dropzone" data-owner="'+esc(member.id)+'" data-status="'+status.id+'">'+(items.length?items.map(t=>'<article class="task '+(t.priority==='high'?'high':'')+'" draggable="true" data-todo="'+t.id+'"><div class="task-title">'+esc(t.title)+'</div><div class="task-meta"><span>'+({high:'높음',normal:'보통',low:'낮음'}[t.priority]||'보통')+'</span><span>'+(t.due_date?'마감 '+esc(t.due_date):'마감일 없음')+'</span></div></article>').join(''):'<div class="empty">업무 없음</div>')+'</div>'}
html+='</div>'}board.innerHTML=html;
board.querySelectorAll('.task').forEach(card=>{card.addEventListener('dragstart',e=>{draggedId=Number(card.dataset.todo);e.dataTransfer.setData('text/plain',String(draggedId));e.dataTransfer.effectAllowed='move'});card.addEventListener('dragend',()=>{draggedId=null;board.querySelectorAll('.drag-over').forEach(el=>el.classList.remove('drag-over'))})});
board.querySelectorAll('.dropzone').forEach(zone=>{zone.addEventListener('dragover',e=>{e.preventDefault();zone.classList.add('drag-over');e.dataTransfer.dropEffect='move'});zone.addEventListener('dragleave',e=>{if(!zone.contains(e.relatedTarget))zone.classList.remove('drag-over')});zone.addEventListener('drop',async e=>{e.preventDefault();zone.classList.remove('drag-over');const id=draggedId||Number(e.dataTransfer.getData('text/plain'));if(!id)return;if(zone.dataset.status==='trash'&&!confirm('이 TODO를 휴지통으로 이동할까요?\n메인 TODO 목록에서 숨겨지며 관리자 페이지에서 복구할 수 있습니다.'))return;try{await api('/api/kanban/todos/'+id,{method:'PATCH',body:JSON.stringify({owner_id:zone.dataset.owner,status:zone.dataset.status})});await load();toast(zone.dataset.status==='trash'?'TODO를 휴지통으로 이동했습니다.':'업무 상태를 변경했습니다.')}catch(err){toast(err.message)}})});
}
$('reload').onclick=load;load();
</script></body></html>'''



ADMIN_HTML = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>관리자 이력 및 DB 조회</title>
<style>
:root{font-family:Inter,"Pretendard","Noto Sans KR","Malgun Gothic",system-ui,sans-serif;color:#172033;background:#f4f6fa;--line:#e6eaf2;--muted:#758096}
*{box-sizing:border-box}body{margin:0;background:#f4f6fa}.shell{max-width:1500px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-bottom:20px}.top h1{font-size:21px;margin:0}.top p{font-size:12px;color:var(--muted);margin:5px 0 0}button,a,input,select{font:inherit}button,a{border:1px solid #dce2ed;background:white;border-radius:9px;padding:9px 12px;font-size:12px;color:#344054;text-decoration:none;cursor:pointer}.primary{background:#3659d9;color:white;border-color:#3659d9}.danger{color:#b42332;border-color:#f1d5d8}.panel{background:white;border:1px solid var(--line);border-radius:13px;padding:16px;margin-bottom:15px}.auth{max-width:520px;margin:60px auto}.auth h2{font-size:16px;margin:0 0 8px}.auth p,.hint{font-size:11px;color:var(--muted);line-height:1.6}.auth-row{display:flex;gap:8px}.auth-row input{flex:1;min-width:0}input,select{border:1px solid #dce2ed;border-radius:9px;padding:9px 10px;font-size:12px}.stats{display:grid;grid-template-columns:repeat(7,minmax(100px,1fr));gap:9px}.stat{background:#f8faff;border:1px solid var(--line);border-radius:10px;padding:12px}.stat strong{display:block;font-size:20px}.stat span{font-size:10px;color:var(--muted)}.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.toolbar select{min-width:190px}.toolbar input{min-width:220px;flex:1}.table-wrap{overflow:auto;max-height:65vh;border:1px solid var(--line);border-radius:9px;margin-top:12px}table{border-collapse:collapse;width:100%;font-size:11px;white-space:nowrap}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #edf0f5;vertical-align:top}th{position:sticky;top:0;background:#f8faff;z-index:1;color:#59657b}td pre{white-space:pre-wrap;overflow-wrap:anywhere;max-width:360px;max-height:130px;overflow:auto;margin:0;font:inherit}.muted{color:var(--muted)}.hidden{display:none!important}.status{font-size:11px;margin-left:auto;color:#667085}@media(max-width:900px){.stats{grid-template-columns:repeat(3,1fr)}.top{align-items:flex-start;flex-direction:column}.shell{padding:14px}}
</style></head><body><main class="shell">
<header class="top"><div><h1>관리자 이력 및 DB 조회</h1><p>일정, TODO, 사용자, 댓글, 활동 로그를 읽기 전용으로 확인합니다.</p></div><div><a href="/">캘린더</a> <a href="/kanban">팀 칸반</a></div></header>
<section class="panel auth" id="auth"><h2>관리자 인증</h2><p>서버 운영자가 설정한 CALENDAR_ADMIN_TOKEN을 입력하세요. 토큰은 이 브라우저 탭의 세션에만 보관됩니다.</p><div class="auth-row"><input id="token" type="password" placeholder="관리자 토큰"><button id="login" class="primary">인증</button></div><p id="authError"></p></section>
<section id="app" class="hidden">
<div class="panel stats" id="stats"></div>
<div class="panel"><div class="toolbar"><select id="table"><option value="activity_logs">활동 로그</option><option value="events">일정 전체 이력</option><option value="todos">TODO 전체 이력</option><option value="users">사용자</option><option value="event_comments">일정 댓글</option><option value="attachments">첨부파일 메타데이터</option></select><input id="filter" placeholder="현재 조회 결과에서 검색"><button id="refresh" class="primary">새로고침</button><span id="count" class="status"></span></div><p class="hint">DB 레코드는 읽기 전용으로 표시됩니다. 삭제된 일정과 TODO는 deleted_at 값으로 구분되며, 복구 버튼으로 다시 활성화할 수 있습니다. 실제 DB 파일이나 비밀번호 등 서버 비밀정보는 제공하지 않습니다.</p><div class="table-wrap"><table><thead id="thead"></thead><tbody id="tbody"></tbody></table></div><div class="toolbar" style="margin-top:10px"><button id="prev">이전</button><button id="next">다음</button><span id="page" class="status"></span><button id="logout" style="margin-left:auto">로그아웃</button></div></div>
</section></main>
<script>
const $=id=>document.getElementById(id);let token=sessionStorage.getItem('calendar-admin-token')||'';let offset=0,limit=100,currentRows=[];
const labels={users:'사용자',events:'활성 일정',deleted_events:'삭제된 일정',todos:'활성 TODO',deleted_todos:'삭제된 TODO',comments:'댓글',logs:'활동 로그'};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,opts={}){const r=await fetch(url,{...opts,headers:{'X-Admin-Token':token,'Content-Type':'application/json',...(opts.headers||{})}});const data=await r.json().catch(()=>({}));if(!r.ok)throw Error(data.detail||'요청 실패');return data}
function showError(e){$('authError').textContent=e.message}
async function unlock(){token=$('token').value.trim();try{await loadSummary();sessionStorage.setItem('calendar-admin-token',token);$('auth').classList.add('hidden');$('app').classList.remove('hidden');await loadRecords()}catch(e){token='';showError(e)}}
async function loadSummary(){const data=await api('/api/admin/summary');$('stats').innerHTML=Object.entries(data).map(([k,v])=>'<div class="stat"><strong>'+v+'</strong><span>'+labels[k]+'</span></div>').join('')}
async function loadRecords(){try{const table=$('table').value;const data=await api('/api/admin/records?table='+encodeURIComponent(table)+'&limit='+limit+'&offset='+offset);currentRows=data.records;$('thead').innerHTML='<tr>'+data.columns.map(c=>'<th>'+esc(c)+'</th>').join('')+'<th>관리</th></tr>';renderRows(data.columns);$('count').textContent=data.total+'건';$('page').textContent=(offset+1)+'–'+Math.min(offset+limit,data.total)+' / '+data.total;$('prev').disabled=offset===0;$('next').disabled=offset+limit>=data.total}catch(e){if(e.message.includes('인증')){sessionStorage.removeItem('calendar-admin-token');$('app').classList.add('hidden');$('auth').classList.remove('hidden')}showError(e)}}
function renderRows(columns){const q=$('filter').value.trim().toLowerCase();const rows=currentRows.filter(row=>!q||columns.some(c=>String(row[c]??'').toLowerCase().includes(q)));$('tbody').innerHTML=rows.map(row=>'<tr>'+columns.map(c=>'<td><pre>'+esc(typeof row[c]==='object'?JSON.stringify(row[c]):row[c])+'</pre></td>').join('')+'<td>'+((row.deleted_at&&['events','todos'].includes($('table').value))?'<button class="primary" data-restore="'+row.id+'">복구</button>':'—')+'</td></tr>').join('')||'<tr><td colspan="'+(columns.length+1)+'" class="muted">표시할 데이터가 없습니다.</td></tr>';$('tbody').querySelectorAll('[data-restore]').forEach(b=>b.onclick=async()=>{if(!confirm('이 항목을 복구할까요?'))return;try{await api('/api/admin/restore/'+$('table').value+'/'+b.dataset.restore,{method:'POST'});await loadSummary();await loadRecords()}catch(e){showError(e)}})}
$('login').onclick=unlock;$('token').addEventListener('keydown',e=>{if(e.key==='Enter')unlock()});$('table').onchange=()=>{offset=0;loadRecords()};$('refresh').onclick=async()=>{await loadSummary();await loadRecords()};$('filter').oninput=()=>{const cols=currentRows.length?Object.keys(currentRows[0]):[];renderRows(cols)};$('prev').onclick=()=>{offset=Math.max(0,offset-limit);loadRecords()};$('next').onclick=()=>{offset+=limit;loadRecords()};$('logout').onclick=()=>{sessionStorage.removeItem('calendar-admin-token');token='';$('app').classList.add('hidden');$('auth').classList.remove('hidden');$('token').value=''};
if(token){$('token').value=token;unlock()}
</script></body></html>'''
