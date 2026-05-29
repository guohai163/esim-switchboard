from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import AppConfig
from app.db import Database
from app.services import EsimSyncService, utc_now_iso


KEEPALIVE_STATUSES = {"idle", "scheduled", "running", "succeeded", "failed", "waiting"}
DEFAULT_WINDOW_START_HOUR = 9
DEFAULT_WINDOW_END_HOUR = 19


class KeepaliveService:
    """Schedules and executes periodic SMS keepalive tasks."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        esim_service: EsimSyncService,
        switch_service: Any,
        adb_client: Any,
    ) -> None:
        self.config = config
        self.db = db
        self.esim_service = esim_service
        self.switch_service = switch_service
        self.adb_client = adb_client
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._active_execution_sub_id: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="keepalive-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def list_rules(self) -> list[dict[str, Any]]:
        return self.db.list_keepalive_rules()

    def upsert_rule(
        self,
        esim_sub_id: str,
        *,
        display_name: str | None,
        carrier_name: str | None,
        timezone_name: str,
        target_phone: str,
        interval_days: int,
        enabled: bool,
    ) -> dict[str, Any]:
        self._validate_timezone_name(timezone_name)
        updated_at = utc_now_iso()
        next_run_at = self._next_run_after(updated_at, interval_days) if enabled else None
        status = "scheduled" if enabled else "idle"
        preview = self.build_message_preview(display_name, updated_at)
        return self.db.upsert_keepalive_rule(
            esim_sub_id,
            esim_display_name=display_name,
            esim_carrier_name=carrier_name,
            timezone_name=timezone_name,
            window_start_hour=DEFAULT_WINDOW_START_HOUR,
            window_end_hour=DEFAULT_WINDOW_END_HOUR,
            target_phone=target_phone.strip(),
            interval_days=interval_days,
            enabled=enabled,
            message_preview=preview,
            next_run_at=next_run_at,
            last_status=status,
            updated_at=updated_at,
        )

    def patch_rule(
        self,
        esim_sub_id: str,
        *,
        timezone_name: str | None = None,
        target_phone: str | None = None,
        interval_days: int | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any] | None:
        current = self.db.get_keepalive_rule(esim_sub_id)
        if current is None:
            return None
        if timezone_name is not None:
            self._validate_timezone_name(timezone_name)
        updated_at = utc_now_iso()
        resolved_interval = interval_days if interval_days is not None else current["interval_days"]
        resolved_enabled = enabled if enabled is not None else current["enabled"]
        resolved_timezone_name = timezone_name if timezone_name is not None else current.get("timezone_name")
        preview = self.build_message_preview(current.get("esim_display_name"), updated_at)
        next_run_at = current.get("next_run_at")
        last_status = current.get("last_status", "idle")
        retry_after_at = current.get("retry_after_at")
        last_error = current.get("last_error")

        if enabled is not None:
            if resolved_enabled:
                next_run_at = self._next_run_after(updated_at, resolved_interval)
                last_status = "scheduled"
                retry_after_at = None
                last_error = None
            else:
                next_run_at = None
                last_status = "idle"
                retry_after_at = None

        if interval_days is not None and resolved_enabled:
            next_run_at = self._next_run_after(updated_at, resolved_interval)
            last_status = "scheduled"
            retry_after_at = None
            last_error = None

        return self.db.patch_keepalive_rule(
            esim_sub_id,
            timezone_name=resolved_timezone_name,
            window_start_hour=current.get("window_start_hour", DEFAULT_WINDOW_START_HOUR),
            window_end_hour=current.get("window_end_hour", DEFAULT_WINDOW_END_HOUR),
            target_phone=target_phone.strip() if target_phone is not None else current["target_phone"],
            interval_days=resolved_interval,
            enabled=resolved_enabled,
            message_preview=preview,
            next_run_at=next_run_at,
            retry_after_at=retry_after_at,
            last_status=last_status,
            last_error=last_error,
            updated_at=updated_at,
        )

    def delete_rule(self, esim_sub_id: str) -> bool:
        return self.db.delete_keepalive_rule(esim_sub_id)

    def run_rule_test(self, esim_sub_id: str) -> dict[str, Any]:
        rule = self.db.get_keepalive_rule(esim_sub_id)
        if rule is None:
            raise KeyError("Keepalive rule not found")
        with self._lock:
            if self._active_execution_sub_id is not None:
                raise RuntimeError("Another keepalive execution is already running")
            if self.switch_service.get_status().get("status") == "running":
                raise RuntimeError("An eSIM switch task is already running")
            self._active_execution_sub_id = esim_sub_id
            try:
                self.execute_rule(rule, ignore_time_window=True, persist_success_schedule=True)
            finally:
                self._active_execution_sub_id = None
        refreshed = self.db.get_keepalive_rule(esim_sub_id)
        if refreshed is None:
            raise KeyError("Keepalive rule not found")
        return refreshed

    def execute_rule(
        self,
        rule: dict[str, Any],
        *,
        ignore_time_window: bool = False,
        persist_success_schedule: bool = True,
    ) -> None:
        now_iso = utc_now_iso()
        timezone_name = rule.get("timezone_name")
        if not ignore_time_window and not timezone_name:
            self._mark_rule_waiting(
                rule["esim_sub_id"],
                "等待补全时区",
                next_run_at=self._next_run_after(now_iso, int(rule["interval_days"])),
                retry_after_at=None,
            )
            return

        if not ignore_time_window:
            try:
                zone = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                self._mark_rule_failed(rule["esim_sub_id"], "无效时区")
                return

            window_start_hour = int(rule.get("window_start_hour") or DEFAULT_WINDOW_START_HOUR)
            window_end_hour = int(rule.get("window_end_hour") or DEFAULT_WINDOW_END_HOUR)
            inside_window, deferred_run_at = self._resolve_send_window(now_iso, zone, window_start_hour, window_end_hour)
            if not inside_window:
                self._mark_rule_waiting(
                    rule["esim_sub_id"],
                    None,
                    next_run_at=deferred_run_at,
                    retry_after_at=None,
                )
                return

        self.db.patch_keepalive_rule(
            rule["esim_sub_id"],
            last_attempt_at=now_iso,
            last_status="running",
            last_error=None,
            retry_after_at=None,
            updated_at=now_iso,
        )
        latest_snapshot = self.esim_service.latest()
        target = next(
            (item for item in latest_snapshot.get("subscriptions", []) if item.get("sub_id") == rule["esim_sub_id"] and item.get("is_embedded")),
            None,
        )
        if target is None:
            self._mark_rule_failed(rule["esim_sub_id"], "目标 eSIM 已不存在或不是可切换 eSIM")
            return

        display_name = target.get("display_name") or rule.get("esim_display_name") or rule["esim_sub_id"]
        patch_base = {
            "esim_display_name": target.get("display_name"),
            "esim_carrier_name": target.get("carrier_name"),
            "message_preview": self.build_message_preview(display_name, now_iso),
            "updated_at": now_iso,
        }
        self.db.patch_keepalive_rule(rule["esim_sub_id"], **patch_base)

        switch_status = self.switch_service.get_status()
        if switch_status.get("status") == "running":
            if ignore_time_window:
                raise RuntimeError("An eSIM switch task is already running")
            self._mark_rule_waiting(rule["esim_sub_id"], "人工切换进行中，保号任务稍后重试")
            return

        try:
            self.switch_service.run_keepalive_switch(display_name)
            time.sleep(self.config.keepalive_switch_settle_seconds)
            device = self.switch_service._connect_device()  # noqa: SLF001
            device.screen_on()
            message = self.build_message_preview(display_name, utc_now_iso())
            self._send_sms(device, rule["target_phone"], message)
        except Exception as exc:  # noqa: BLE001
            self._mark_rule_failed(rule["esim_sub_id"], str(exc))
            if ignore_time_window:
                raise
            return

        finished_at = utc_now_iso()
        next_run_at = self._next_run_after(finished_at, int(rule["interval_days"])) if persist_success_schedule else rule.get("next_run_at")
        self.db.patch_keepalive_rule(
            rule["esim_sub_id"],
            last_attempt_at=finished_at,
            last_success_at=finished_at,
            next_run_at=next_run_at,
            retry_after_at=None,
            last_status="succeeded",
            last_error=None,
            updated_at=finished_at,
            message_preview=self.build_message_preview(display_name, finished_at),
            esim_display_name=display_name,
            esim_carrier_name=target.get("carrier_name"),
        )

    def build_message_preview(self, display_name: str | None, now_iso: str) -> str:
        date_text = self._format_date(now_iso)
        prefix = "保号 "
        suffix = f" {date_text}"
        max_length = 40
        available = max_length - len(prefix) - len(suffix)
        trimmed = (display_name or "").strip()
        if available < 0:
            return f"保号 {date_text}"[:max_length]
        if len(trimmed) > available:
            trimmed = trimmed[:available]
        return f"{prefix}{trimmed}{suffix}".strip()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                now_iso = utc_now_iso()
                for rule in self.db.list_due_keepalive_rules(now_iso):
                    if self._stop_event.is_set():
                        break
                    with self._lock:
                        self.execute_rule(rule)
            except Exception:
                pass
            self._stop_event.wait(self.config.keepalive_poll_interval_seconds)

    def _send_sms(self, device: Any, target_phone: str, message: str) -> None:
        self.adb_client.send_sms_via_ui(target_phone, message, device)
        time.sleep(self.config.switch_step_delay_seconds)

        if hasattr(device, "app_wait"):
            try:
                device.app_wait(self.config.sms_compose_package, timeout=5)
            except Exception:
                pass

        phone_field = self._find_first_existing_selector(
            device,
            [
                {"resourceIdMatches": ".*recipient.*"},
                {"textContains": target_phone},
            ],
        )
        if phone_field is None:
            raise RuntimeError("未找到短信接收号码输入区域")

        message_field = self._find_first_existing_selector(
            device,
            [
                {"resourceIdMatches": ".*compose_message_text.*"},
                {"resourceIdMatches": ".*message_text.*"},
                {"textContains": message[: min(8, len(message))] if message else ""},
            ],
        )
        if message_field is None:
            raise RuntimeError("未找到短信内容输入框")

        send_button = self._find_first_existing_selector(
            device,
            [{"text": label} for label in self.config.sms_send_button_labels]
            + [{"description": label} for label in self.config.sms_send_button_labels]
            + [{"resourceIdMatches": ".*send.*"}],
        )
        if send_button is None:
            raise RuntimeError("未找到发送按钮")
        send_button.click()

    def _mark_rule_failed(self, esim_sub_id: str, message: str) -> None:
        now_iso = utc_now_iso()
        self.db.patch_keepalive_rule(
            esim_sub_id,
            last_attempt_at=now_iso,
            retry_after_at=self._after_minutes(now_iso, self.config.keepalive_retry_minutes),
            last_status="failed",
            last_error=message,
            updated_at=now_iso,
        )

    def _mark_rule_waiting(
        self,
        esim_sub_id: str,
        message: str | None,
        *,
        next_run_at: str | None = None,
        retry_after_at: str | None = None,
    ) -> None:
        now_iso = utc_now_iso()
        self.db.patch_keepalive_rule(
            esim_sub_id,
            last_attempt_at=now_iso,
            next_run_at=next_run_at,
            retry_after_at=retry_after_at if retry_after_at is not None else self._after_minutes(now_iso, self.config.keepalive_retry_minutes),
            last_status="waiting",
            last_error=message,
            updated_at=now_iso,
        )

    def _find_first_existing_selector(self, device: Any, selectors: list[dict[str, Any]]) -> Any | None:
        for selector_kwargs in selectors:
            if not selector_kwargs:
                continue
            selector = device(**selector_kwargs)
            if getattr(selector, "exists", False):
                return selector
        return None

    def _next_run_after(self, from_iso: str, interval_days: int) -> str:
        base = datetime.fromisoformat(from_iso)
        return (base + timedelta(days=interval_days)).isoformat()

    def _after_minutes(self, from_iso: str, minutes: int) -> str:
        base = datetime.fromisoformat(from_iso)
        return (base + timedelta(minutes=minutes)).isoformat()

    def _format_date(self, value: str) -> str:
        dt = datetime.fromisoformat(value)
        return dt.astimezone().strftime("%Y-%m-%d")

    def _validate_timezone_name(self, timezone_name: str) -> None:
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Invalid timezone_name") from exc

    def _resolve_send_window(
        self,
        now_iso: str,
        zone: ZoneInfo,
        window_start_hour: int,
        window_end_hour: int,
    ) -> tuple[bool, str | None]:
        now_utc = datetime.fromisoformat(now_iso)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        local_now = now_utc.astimezone(zone)
        local_start = local_now.replace(hour=window_start_hour, minute=0, second=0, microsecond=0)
        local_end = local_now.replace(hour=window_end_hour, minute=0, second=0, microsecond=0)
        if local_start <= local_now < local_end:
            return True, None
        if local_now < local_start:
            return False, local_start.astimezone(timezone.utc).isoformat()
        next_start_local = local_start + timedelta(days=1)
        return False, next_start_local.astimezone(timezone.utc).isoformat()
