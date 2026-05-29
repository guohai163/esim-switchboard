from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.models import EsimSnapshotRecord, SmsMessageRecord


class Database:
    """SQLite persistence layer for dashboard state and historical data."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS esim_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collected_at TEXT NOT NULL,
                    embedded_total_count INTEGER NOT NULL,
                    embedded_active_count INTEGER NOT NULL,
                    raw_output TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS esim_subscriptions (
                    snapshot_id INTEGER NOT NULL,
                    sub_id TEXT NOT NULL,
                    display_name TEXT,
                    carrier_name TEXT,
                    is_embedded INTEGER NOT NULL,
                    is_active INTEGER NOT NULL,
                    sim_slot_index INTEGER,
                    FOREIGN KEY(snapshot_id) REFERENCES esim_snapshots(id)
                );

                CREATE INDEX IF NOT EXISTS idx_esim_subscriptions_snapshot_id
                ON esim_subscriptions(snapshot_id);

                CREATE INDEX IF NOT EXISTS idx_esim_subscriptions_sub_id
                ON esim_subscriptions(sub_id);

                CREATE TABLE IF NOT EXISTS sms_messages (
                    sms_id TEXT PRIMARY KEY,
                    address TEXT,
                    body TEXT,
                    sub_id TEXT,
                    date_ts INTEGER,
                    raw_row TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sms_messages_date_ts
                ON sms_messages(date_ts DESC, sms_id DESC);

                CREATE INDEX IF NOT EXISTS idx_sms_messages_sub_id
                ON sms_messages(sub_id);

                CREATE TABLE IF NOT EXISTS app_state (
                    state_key TEXT PRIMARY KEY,
                    json_value TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS keepalive_rules (
                    esim_sub_id TEXT PRIMARY KEY,
                    esim_display_name TEXT,
                    esim_carrier_name TEXT,
                    timezone_name TEXT,
                    window_start_hour INTEGER NOT NULL DEFAULT 9,
                    window_end_hour INTEGER NOT NULL DEFAULT 19,
                    target_phone TEXT NOT NULL,
                    interval_days INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    message_preview TEXT NOT NULL,
                    last_attempt_at TEXT,
                    last_success_at TEXT,
                    next_run_at TEXT,
                    retry_after_at TEXT,
                    last_status TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_keepalive_rules_next_run
                ON keepalive_rules(enabled, next_run_at, retry_after_at);
                """
            )
            self._ensure_app_state_schema(conn)
            self._ensure_column(conn, "sms_messages", "raw_row", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "keepalive_rules", "timezone_name", "TEXT")
            self._ensure_column(conn, "keepalive_rules", "window_start_hour", "INTEGER NOT NULL DEFAULT 9")
            self._ensure_column(conn, "keepalive_rules", "window_end_hour", "INTEGER NOT NULL DEFAULT 19")
            conn.commit()

    def save_esim_snapshot(self, snapshot: EsimSnapshotRecord) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO esim_snapshots (
                    collected_at,
                    embedded_total_count,
                    embedded_active_count,
                    raw_output
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    snapshot.collected_at,
                    snapshot.embedded_total_count,
                    snapshot.embedded_active_count,
                    snapshot.raw_output,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO esim_subscriptions (
                    snapshot_id,
                    sub_id,
                    display_name,
                    carrier_name,
                    is_embedded,
                    is_active,
                    sim_slot_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        item.sub_id,
                        item.display_name,
                        item.carrier_name,
                        1 if item.is_embedded else 0,
                        1 if item.is_active else 0,
                        item.sim_slot_index,
                    )
                    for item in snapshot.subscriptions
                ],
            )
            conn.commit()
            return snapshot_id

    def get_latest_esim_snapshot(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            snapshot_row = conn.execute(
                """
                SELECT id, collected_at, embedded_total_count, embedded_active_count
                FROM esim_snapshots
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            if snapshot_row is None:
                return None

            subscription_rows = conn.execute(
                """
                SELECT sub_id, display_name, carrier_name, is_embedded, is_active, sim_slot_index
                FROM esim_subscriptions
                WHERE snapshot_id = ?
                ORDER BY is_active DESC, sub_id ASC
                """,
                (snapshot_row["id"],),
            ).fetchall()
            return {
                "id": snapshot_row["id"],
                "collected_at": snapshot_row["collected_at"],
                "embedded_total_count": snapshot_row["embedded_total_count"],
                "embedded_active_count": snapshot_row["embedded_active_count"],
                "subscriptions": [
                    {
                        "sub_id": row["sub_id"],
                        "display_name": row["display_name"],
                        "carrier_name": row["carrier_name"],
                        "is_embedded": bool(row["is_embedded"]),
                        "is_active": bool(row["is_active"]),
                        "sim_slot_index": row["sim_slot_index"],
                    }
                    for row in subscription_rows
                ],
            }

    def upsert_sms_messages(
        self,
        messages: list[SmsMessageRecord],
        created_at: str,
        updated_at: str,
    ) -> tuple[int, int]:
        inserted_count = 0
        duplicate_count = 0
        with self._connect() as conn:
            for item in messages:
                existing = conn.execute(
                    "SELECT 1 FROM sms_messages WHERE sms_id = ?",
                    (item.sms_id,),
                ).fetchone()
                cursor = conn.execute(
                """
                INSERT INTO sms_messages (
                    sms_id,
                    address,
                    body,
                    sub_id,
                    date_ts,
                    raw_row,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sms_id) DO UPDATE SET
                    address = excluded.address,
                    body = excluded.body,
                    sub_id = excluded.sub_id,
                    date_ts = excluded.date_ts,
                    raw_row = excluded.raw_row,
                    updated_at = excluded.updated_at
                """,
                (
                    item.sms_id,
                    item.address,
                    item.body,
                    item.sub_id,
                    item.date_ts,
                    item.raw_row,
                    created_at,
                    updated_at,
                ),
            )
                if cursor.rowcount <= 0:
                    duplicate_count += 1
                elif existing is None:
                    inserted_count += 1
                else:
                    duplicate_count += 1
            conn.commit()
        return inserted_count, duplicate_count

    def list_sms_messages(
        self,
        page: int,
        page_size: int,
        *,
        address: str | None = None,
        sub_id: str | None = None,
        display_name: str | None = None,
        keyword: str | None = None,
    ) -> dict[str, Any]:
        where_clauses: list[str] = []
        params: list[Any] = []
        if address:
            where_clauses.append("m.address = ?")
            params.append(address)
        if sub_id:
            where_clauses.append("m.sub_id = ?")
            params.append(sub_id)
        if display_name:
            where_clauses.append("COALESCE(s.display_name, '') LIKE ?")
            params.append(f"%{display_name}%")
        if keyword:
            where_clauses.append("(COALESCE(m.address, '') LIKE ? OR COALESCE(m.body, '') LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        latest_snapshot_sql = "(SELECT id FROM esim_snapshots ORDER BY id DESC LIMIT 1)"
        from_sql = f"""
            FROM sms_messages m
            LEFT JOIN esim_subscriptions s
              ON s.snapshot_id = {latest_snapshot_sql}
             AND s.sub_id = m.sub_id
            {where_sql}
        """

        with self._connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) {from_sql}", params).fetchone()[0])
            offset = (page - 1) * page_size
            rows = conn.execute(
                f"""
                SELECT
                    m.sms_id,
                    m.address,
                    m.body,
                    m.sub_id,
                    s.display_name,
                    m.date_ts,
                    m.created_at,
                    m.updated_at
                {from_sql}
                ORDER BY m.date_ts DESC, m.sms_id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()

        return {
            "page": page,
            "page_size": page_size,
            "total": total,
            "items": [
                {
                    "sms_id": row["sms_id"],
                    "address": row["address"],
                    "body": row["body"],
                    "sub_id": row["sub_id"],
                    "display_name": row["display_name"],
                    "date_ts": row["date_ts"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ],
        }

    def set_app_state(self, state_key: str, payload: Any, updated_at: str) -> None:
        json_value = None if payload is None else json.dumps(payload, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_state (state_key, json_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    json_value = excluded.json_value,
                    updated_at = excluded.updated_at
                """,
                (state_key, json_value, updated_at),
            )
            conn.commit()

    def get_app_state_json(self, state_key: str) -> Any:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT json_value FROM app_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
        if row is None or row["json_value"] is None:
            return None
        return json.loads(row["json_value"])

    def get_keepalive_rule(self, esim_sub_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM keepalive_rules WHERE esim_sub_id = ?",
                (esim_sub_id,),
            ).fetchone()
        return self._serialize_keepalive_row(row) if row else None

    def list_keepalive_rules(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM keepalive_rules
                ORDER BY enabled DESC, COALESCE(next_run_at, '9999-12-31T00:00:00+00:00') ASC, esim_display_name ASC
                """
            ).fetchall()
        return [self._serialize_keepalive_row(row) for row in rows]

    def upsert_keepalive_rule(
        self,
        esim_sub_id: str,
        *,
        esim_display_name: str | None,
        esim_carrier_name: str | None,
        timezone_name: str | None,
        window_start_hour: int,
        window_end_hour: int,
        target_phone: str,
        interval_days: int,
        enabled: bool,
        message_preview: str,
        next_run_at: str | None,
        last_status: str,
        updated_at: str,
    ) -> dict[str, Any]:
        current = self.get_keepalive_rule(esim_sub_id)
        created_at = current["created_at"] if current else updated_at
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO keepalive_rules (
                    esim_sub_id,
                    esim_display_name,
                    esim_carrier_name,
                    timezone_name,
                    window_start_hour,
                    window_end_hour,
                    target_phone,
                    interval_days,
                    enabled,
                    message_preview,
                    last_attempt_at,
                    last_success_at,
                    next_run_at,
                    retry_after_at,
                    last_status,
                    last_error,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, ?, NULL, ?, ?)
                ON CONFLICT(esim_sub_id) DO UPDATE SET
                    esim_display_name = excluded.esim_display_name,
                    esim_carrier_name = excluded.esim_carrier_name,
                    timezone_name = excluded.timezone_name,
                    window_start_hour = excluded.window_start_hour,
                    window_end_hour = excluded.window_end_hour,
                    target_phone = excluded.target_phone,
                    interval_days = excluded.interval_days,
                    enabled = excluded.enabled,
                    message_preview = excluded.message_preview,
                    next_run_at = excluded.next_run_at,
                    retry_after_at = NULL,
                    last_status = excluded.last_status,
                    last_error = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    esim_sub_id,
                    esim_display_name,
                    esim_carrier_name,
                    timezone_name,
                    window_start_hour,
                    window_end_hour,
                    target_phone,
                    interval_days,
                    1 if enabled else 0,
                    message_preview,
                    next_run_at,
                    last_status,
                    created_at,
                    updated_at,
                ),
            )
            conn.commit()
        return self.get_keepalive_rule(esim_sub_id) or {}

    def patch_keepalive_rule(
        self,
        esim_sub_id: str,
        **changes: Any,
    ) -> dict[str, Any] | None:
        current = self.get_keepalive_rule(esim_sub_id)
        if current is None:
            return None
        merged = dict(current)
        merged.update({key: value for key, value in changes.items() if value is not None or key in {"last_error", "retry_after_at", "last_attempt_at", "last_success_at", "next_run_at"}})
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE keepalive_rules
                SET
                    esim_display_name = ?,
                    esim_carrier_name = ?,
                    timezone_name = ?,
                    window_start_hour = ?,
                    window_end_hour = ?,
                    target_phone = ?,
                    interval_days = ?,
                    enabled = ?,
                    message_preview = ?,
                    last_attempt_at = ?,
                    last_success_at = ?,
                    next_run_at = ?,
                    retry_after_at = ?,
                    last_status = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE esim_sub_id = ?
                """,
                (
                    merged.get("esim_display_name"),
                    merged.get("esim_carrier_name"),
                    merged.get("timezone_name"),
                    merged.get("window_start_hour", 9),
                    merged.get("window_end_hour", 19),
                    merged["target_phone"],
                    merged["interval_days"],
                    1 if merged.get("enabled") else 0,
                    merged["message_preview"],
                    merged.get("last_attempt_at"),
                    merged.get("last_success_at"),
                    merged.get("next_run_at"),
                    merged.get("retry_after_at"),
                    merged.get("last_status"),
                    merged.get("last_error"),
                    merged.get("updated_at"),
                    esim_sub_id,
                ),
            )
            conn.commit()
        return self.get_keepalive_rule(esim_sub_id)

    def delete_keepalive_rule(self, esim_sub_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM keepalive_rules WHERE esim_sub_id = ?",
                (esim_sub_id,),
            )
            conn.commit()
        return cursor.rowcount > 0

    def list_due_keepalive_rules(self, now_iso: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM keepalive_rules
                WHERE enabled = 1
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= ?
                  AND (retry_after_at IS NULL OR retry_after_at <= ?)
                ORDER BY next_run_at ASC, updated_at ASC
                """,
                (now_iso, now_iso),
            ).fetchall()
        return [self._serialize_keepalive_row(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _serialize_keepalive_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "esim_sub_id": row["esim_sub_id"],
            "esim_display_name": row["esim_display_name"],
            "esim_carrier_name": row["esim_carrier_name"],
            "timezone_name": row["timezone_name"],
            "window_start_hour": row["window_start_hour"],
            "window_end_hour": row["window_end_hour"],
            "target_phone": row["target_phone"],
            "interval_days": row["interval_days"],
            "enabled": bool(row["enabled"]),
            "message_preview": row["message_preview"],
            "last_attempt_at": row["last_attempt_at"],
            "last_success_at": row["last_success_at"],
            "next_run_at": row["next_run_at"],
            "retry_after_at": row["retry_after_at"],
            "last_status": row["last_status"],
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name in columns:
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def _ensure_app_state_schema(self, conn: sqlite3.Connection) -> None:
        table_info = conn.execute("PRAGMA table_info(app_state)").fetchall()
        if not table_info:
            return
        columns = {row["name"] for row in table_info}
        if "json_value" in columns and "state_key" in columns:
            return

        legacy_key_column = None
        for candidate in ("key", "name", "state_name"):
            if candidate in columns:
                legacy_key_column = candidate
                break
        if legacy_key_column is None:
            legacy_key_column = next(iter(columns))

        legacy_value_column = None
        for candidate in ("json", "value", "payload", "content"):
            if candidate in columns:
                legacy_value_column = candidate
                break

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state_v2 (
                state_key TEXT PRIMARY KEY,
                json_value TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )

        if legacy_value_column is None:
            conn.execute(
                f"""
                INSERT OR REPLACE INTO app_state_v2 (state_key, json_value, updated_at)
                SELECT CAST({legacy_key_column} AS TEXT), NULL, COALESCE(updated_at, '')
                FROM app_state
                """
            )
        else:
            conn.execute(
                f"""
                INSERT OR REPLACE INTO app_state_v2 (state_key, json_value, updated_at)
                SELECT CAST({legacy_key_column} AS TEXT), {legacy_value_column}, COALESCE(updated_at, '')
                FROM app_state
                """
            )

        conn.execute("DROP TABLE app_state")
        conn.execute("ALTER TABLE app_state_v2 RENAME TO app_state")
