from __future__ import annotations

import argparse
import calendar
import os
import sqlite3
import sys
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("CALENDAR_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "calendar.db"
UPLOAD_DIR = DATA_DIR / "uploads"
ARCHIVE_DIR = Path(os.getenv("CALENDAR_ARCHIVE_DIR", DATA_DIR / "archives"))


def subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_counts(conn: sqlite3.Connection, cutoff: str) -> dict[str, int]:
    return {
        "events": conn.execute(
            """SELECT COUNT(*) FROM events
               WHERE date(COALESCE(end_at,start_at)) < ?
                  OR (deleted_at IS NOT NULL AND date(deleted_at) < ?)""",
            (cutoff, cutoff),
        ).fetchone()[0],
        "todos": conn.execute(
            """SELECT COUNT(*) FROM todos
               WHERE (deleted_at IS NOT NULL AND date(deleted_at) < ?)
                  OR (completed=1 AND date(created_at) < ?)""",
            (cutoff, cutoff),
        ).fetchone()[0],
        "comments": conn.execute(
            "SELECT COUNT(*) FROM event_comments WHERE date(created_at) < ?",
            (cutoff,),
        ).fetchone()[0],
        "activity_logs": conn.execute(
            "SELECT COUNT(*) FROM activity_logs WHERE date(created_at) < ?",
            (cutoff,),
        ).fetchone()[0],
    }


def create_archive(cutoff: str) -> Path:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = ARCHIVE_DIR / f"calendar_archive_{stamp}.zip"

    with tempfile.TemporaryDirectory(prefix="calendar_archive_") as temp:
        snapshot = Path(temp) / "calendar.db"
        source = connect(DB_PATH)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
            check = target.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(f"백업 DB 무결성 검사 실패: {check}")
        finally:
            target.close()
            source.close()

        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(snapshot, "calendar.db")
            if UPLOAD_DIR.exists():
                for path in UPLOAD_DIR.rglob("*"):
                    if path.is_file():
                        zf.write(path, Path("uploads") / path.relative_to(UPLOAD_DIR))
            zf.writestr(
                "ARCHIVE_INFO.txt",
                f"Created: {datetime.now().isoformat(timespec='seconds')}\n"
                f"Prune cutoff: {cutoff}\n"
                "This archive contains a consistent SQLite snapshot and the upload files.\n",
            )
    return archive_path


def prune(cutoff: str) -> dict[str, int]:
    conn = connect(DB_PATH)
    removed_uploads: list[str] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        old_event_rows = conn.execute(
            """SELECT id FROM events
               WHERE date(COALESCE(end_at,start_at)) < ?
                  OR (deleted_at IS NOT NULL AND date(deleted_at) < ?)""",
            (cutoff, cutoff),
        ).fetchall()
        event_ids = [row["id"] for row in old_event_rows]
        if event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            removed_uploads = [
                row["stored_name"]
                for row in conn.execute(
                    f"SELECT stored_name FROM attachments WHERE event_id IN ({placeholders})",
                    event_ids,
                ).fetchall()
            ]
            conn.execute(f"DELETE FROM events WHERE id IN ({placeholders})", event_ids)

        todo_result = conn.execute(
            """DELETE FROM todos
               WHERE (deleted_at IS NOT NULL AND date(deleted_at) < ?)
                  OR (completed=1 AND date(created_at) < ?)""",
            (cutoff, cutoff),
        )
        comment_result = conn.execute(
            "DELETE FROM event_comments WHERE date(created_at) < ?", (cutoff,)
        )
        log_result = conn.execute(
            "DELETE FROM activity_logs WHERE date(created_at) < ?", (cutoff,)
        )
        conn.commit()
        counts = {
            "events": len(event_ids),
            "todos": todo_result.rowcount,
            "comments": comment_result.rowcount,
            "activity_logs": log_result.rowcount,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    for stored_name in removed_uploads:
        (UPLOAD_DIR / stored_name).unlink(missing_ok=True)

    checkpoint = connect(DB_PATH)
    try:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        checkpoint.execute("VACUUM")
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        checkpoint.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Back up the calendar DB and prune records older than the retention period."
    )
    parser.add_argument("--months", type=int, default=3, help="Retention period in months (default: 3)")
    parser.add_argument("--apply", action="store_true", help="Create archive and actually delete old records")
    args = parser.parse_args()

    if args.months < 1:
        parser.error("--months must be at least 1")
    if not DB_PATH.is_file():
        print(f"DB 파일을 찾을 수 없습니다: {DB_PATH}", file=sys.stderr)
        return 2

    cutoff = subtract_months(date.today(), args.months).isoformat()
    with connect(DB_PATH) as conn:
        counts = get_counts(conn, cutoff)

    print(f"DB: {DB_PATH}")
    print(f"보관 기준일: {cutoff} 이전 데이터")
    for table, count in counts.items():
        print(f"  {table}: {count:,}건")
    print("주의: 사용자 계정은 삭제하지 않으며, 미완료 TODO도 보존합니다.")

    if not args.apply:
        print("\n미리보기만 수행했습니다. 백업 및 삭제를 실행하려면 --apply 옵션을 추가하세요.")
        return 0

    archive_path = create_archive(cutoff)
    print(f"백업 완료: {archive_path}")
    removed = prune(cutoff)
    print("정리 완료:")
    for table, count in removed.items():
        print(f"  {table}: {count:,}건 삭제")
    print("백업 ZIP은 별도로 보관되며, calendar.db와 uploads 폴더를 포함합니다.")
    print("VACUUM을 수행해 현 DB 파일의 미사용 공간도 회수했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
