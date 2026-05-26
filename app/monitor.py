from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from app.adb import AdbCommandError
from app.config import AppConfig
from app.db import Database
from app.services import SmsSyncService


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SmsMonitor:
    """Background logcat watcher that pulls recent inbox messages on SMS events."""

    def __init__(self, config: AppConfig, db: Database, sms_service: SmsSyncService, adb_client: Any) -> None:
        self.config = config
        self.db = db
        self.sms_service = sms_service
        self.adb_client = adb_client
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._process = None
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "running": False,
            "connected": False,
            "restart_count": 0,
            "last_event_at": None,
            "last_error": None,
            "last_log_line": None,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="sms-monitor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop_event.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._update_status(running=False, connected=False)

    def get_status(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def _run(self) -> None:
        self._update_status(running=True, connected=False, last_error=None)
        while not self._stop_event.is_set():
            try:
                self._process = self.adb_client.stream_logcat()
                self._update_status(connected=True, last_error=None)
                assert self._process.stdout is not None
                while not self._stop_event.is_set():
                    line = self._process.stdout.readline()
                    if line == "" and self._process.poll() is not None:
                        raise AdbCommandError([], f"logcat exited with code {self._process.returncode}")
                    if not line:
                        continue
                    normalized = line.strip()
                    if not normalized:
                        continue
                    self._update_status(last_log_line=normalized)
                    if self._is_sms_signal(normalized):
                        self._update_status(last_event_at=utc_now_iso())
                        time.sleep(self.config.sms_sync_delay_seconds)
                        try:
                            self.sms_service.sync_latest(limit=5)
                        except Exception as exc:  # noqa: BLE001
                            self._update_status(last_error=f"Sync latest SMS failed: {exc}")
            except Exception as exc:  # noqa: BLE001
                if self._stop_event.is_set():
                    break
                self._update_status(
                    connected=False,
                    last_error=f"Monitor reconnecting after error: {exc}",
                )
                with self._status_lock:
                    self._status["restart_count"] += 1
                self._persist_status()
                time.sleep(self.config.adb_reconnect_delay_seconds)
            finally:
                if self._process and self._process.poll() is None:
                    self._process.terminate()
                self._process = None
        self._update_status(running=False, connected=False)

    def _is_sms_signal(self, line: str) -> bool:
        lowered = line.lower()
        return any(keyword in lowered for keyword in self.config.sms_log_trigger_keywords)

    def _update_status(self, **kwargs: Any) -> None:
        with self._status_lock:
            self._status.update(kwargs)
        self._persist_status()

    def _persist_status(self) -> None:
        try:
            self.db.set_app_state("monitor_status", self.get_status(), utc_now_iso())
        except Exception:  # noqa: BLE001
            # The database may not be initialized yet in test or dry-run scenarios.
            return
