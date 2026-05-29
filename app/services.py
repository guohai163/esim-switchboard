from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.adb import AdbClient, parse_isub_output, parse_sms_query_output
from app.collab import CollabSessionService
from app.config import AppConfig
from app.db import Database
from app.models import SyncResponse


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EsimSyncService:
    """Handles eSIM snapshot collection and persistence."""

    def __init__(self, db: Database, adb_client: AdbClient) -> None:
        self.db = db
        self.adb_client = adb_client

    def sync(self) -> dict[str, Any]:
        output = self.adb_client.read_isub()
        snapshot = parse_isub_output(output)
        snapshot_id = self.db.save_esim_snapshot(snapshot)
        latest = self.db.get_latest_esim_snapshot() or {}
        self.db.set_app_state(
            "last_esim_sync",
            {
                "ok": True,
                "snapshot_id": snapshot_id,
                "embedded_total_count": latest.get("embedded_total_count", 0),
                "embedded_active_count": latest.get("embedded_active_count", 0),
                "synced_at": snapshot.collected_at,
            },
            snapshot.collected_at,
        )
        return {
            "ok": True,
            "fetched_count": len(snapshot.subscriptions),
            "inserted_count": len(snapshot.subscriptions),
            "duplicate_count": 0,
            "synced_at": snapshot.collected_at,
            "detail": "eSIM snapshot synced",
        }

    def latest(self) -> dict[str, Any]:
        snapshot = self.db.get_latest_esim_snapshot()
        return snapshot or {
            "id": None,
            "collected_at": None,
            "embedded_total_count": 0,
            "embedded_active_count": 0,
            "subscriptions": [],
        }


class SmsSyncService:
    """Handles inbox sync and deduped persistence."""

    def __init__(self, db: Database, adb_client: AdbClient) -> None:
        self.db = db
        self.adb_client = adb_client

    def sync_all_inbox(self) -> dict[str, Any]:
        return self._sync(limit=None, detail="full inbox sync")

    def sync_latest(self, limit: int = 5) -> dict[str, Any]:
        return self._sync(limit=limit, detail=f"latest {limit} inbox messages sync")

    def list_messages(
        self,
        page: int,
        page_size: int,
        address: str | None = None,
        sub_id: str | None = None,
        display_name: str | None = None,
        keyword: str | None = None,
    ) -> dict[str, Any]:
        return self.db.list_sms_messages(
            page,
            page_size,
            address=address,
            sub_id=sub_id,
            display_name=display_name,
            keyword=keyword,
        )

    def _sync(self, limit: int | None, detail: str) -> dict[str, Any]:
        output = self.adb_client.query_sms_inbox(limit=limit)
        messages = parse_sms_query_output(output)
        synced_at = utc_now_iso()
        inserted_count, duplicate_count = self.db.upsert_sms_messages(
            messages,
            created_at=synced_at,
            updated_at=synced_at,
        )
        result = {
            "ok": True,
            "fetched_count": len(messages),
            "inserted_count": inserted_count,
            "duplicate_count": duplicate_count,
            "synced_at": synced_at,
            "detail": detail,
            "latest_message": None,
        }
        latest_query = self.db.list_sms_messages(page=1, page_size=1)
        if latest_query["items"]:
            result["latest_message"] = latest_query["items"][0]
        self.db.set_app_state("last_sms_sync", result, synced_at)
        return result


@dataclass(slots=True)
class AppServices:
    """Service container used by the API and the startup lifecycle."""

    config: AppConfig
    db: Database
    adb_client: AdbClient
    esim_service: EsimSyncService
    sms_service: SmsSyncService
    monitor: Any
    switch_service: Any
    sms_event_service: Any
    collab_service: CollabSessionService
    keepalive_service: Any | None = None
