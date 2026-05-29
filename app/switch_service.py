from __future__ import annotations

import asyncio
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import uiautomator2 as u2

from app.adb import parse_isub_output
from app.config import AppConfig
from app.db import Database
from app.services import EsimSyncService, utc_now_iso


SwitchTaskStatus = Literal["idle", "running", "succeeded", "failed"]
SwitchStepStatus = Literal["pending", "running", "succeeded", "failed"]
SwitchLogEventType = Literal["attempt_started", "blocked_running", "blocked_locked", "succeeded", "failed"]

LOCK_STATE_KEY = "esim_switch_lock"
LOG_STATE_KEY = "esim_switch_logs"
MAX_LOG_ENTRIES = 30


@dataclass(slots=True)
class SwitchStep:
    step_key: str
    title: str
    status: SwitchStepStatus
    timestamp: str
    screenshot_url: str | None = None
    detail: str | None = None


@dataclass(slots=True)
class SwitchLogEntry:
    event_type: SwitchLogEventType
    display_name: str | None
    lock_minutes: int | None
    created_at: str
    message: str
    task_id: str | None = None


@dataclass(slots=True)
class SwitchLockState:
    lock_until: str
    lock_minutes: int
    last_target_display_name: str | None
    locked_at: str


@dataclass(slots=True)
class SwitchTask:
    task_id: str | None = None
    status: SwitchTaskStatus = "idle"
    target_display_name: str | None = None
    requested_lock_minutes: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    current_step: str | None = None
    latest_screenshot_url: str | None = None
    error: str | None = None
    steps: list[SwitchStep] = field(default_factory=list)


class EsimSwitchConflictError(RuntimeError):
    """Raised when a switch task is already running."""


class EsimSwitchLockedError(RuntimeError):
    """Raised when a switch is blocked by the post-switch lock window."""


class EsimSwitchService:
    """Runs the eSIM switching flow and streams step snapshots to the UI."""

    def __init__(
        self,
        config: AppConfig,
        db: Database | EsimSyncService,
        esim_service: EsimSyncService | None = None,
    ) -> None:
        self.config = config
        if esim_service is None:
            self.db = Database(config.db_path)
            self.db.init_schema()
            self.esim_service = db  # type: ignore[assignment]
        else:
            self.db = db  # type: ignore[assignment]
            self.esim_service = esim_service
        self._lock = threading.Lock()
        self._task = SwitchTask()
        self._lock_state: SwitchLockState | None = None
        self._logs: list[SwitchLogEntry] = []
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._active_thread: threading.Thread | None = None

    def restore_state(self) -> None:
        with self._lock:
            raw_lock = self.db.get_app_state_json(LOCK_STATE_KEY)
            raw_logs = self.db.get_app_state_json(LOG_STATE_KEY)
            self._lock_state = self._deserialize_lock_state(raw_lock)
            self._logs = self._deserialize_logs(raw_logs)
            self._refresh_lock_locked()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_lock_locked()
            return self._serialize_task(self._task)

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        await queue.put({"event": "snapshot", "data": self.get_status()})
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def start_switch(self, display_name: str, lock_minutes: int) -> dict[str, Any]:
        with self._lock:
            if self._task.status == "running":
                message = "An eSIM switch task is already running"
                self._append_log_locked("blocked_running", display_name, lock_minutes, message, self._task.task_id)
                snapshot = self._serialize_task(self._task)
                self._notify_locked("snapshot", snapshot)
                raise EsimSwitchConflictError(message)

            self._refresh_lock_locked()
            if self._lock_state:
                remaining_seconds = self._remaining_seconds(self._lock_state.lock_until)
                remaining_minutes = max(1, math.ceil(remaining_seconds / 60))
                unlock_at = self._format_lock_until(self._lock_state.lock_until)
                message = f"当前处于切换锁定期，需等待到 {unlock_at} 后再试，剩余约 {remaining_minutes} 分钟"
                self._append_log_locked("blocked_locked", display_name, lock_minutes, message, None)
                snapshot = self._serialize_task(self._task)
                self._notify_locked("snapshot", snapshot)
                raise EsimSwitchLockedError(message)

            task_id = uuid.uuid4().hex
            started_at = utc_now_iso()
            self._append_log_locked(
                "attempt_started",
                display_name,
                lock_minutes,
                f"发起切换到 {display_name}，成功后将锁定 {lock_minutes} 分钟",
                task_id,
                created_at=started_at,
            )
            self._task = SwitchTask(
                task_id=task_id,
                status="running",
                target_display_name=display_name,
                requested_lock_minutes=lock_minutes,
                started_at=started_at,
                current_step="starting",
                steps=[],
            )
            self._notify_locked("task_started", self._serialize_task(self._task))

        thread = threading.Thread(
            target=self._run_switch_flow,
            args=(task_id, display_name),
            daemon=True,
            name=f"esim-switch-{task_id[:8]}",
        )
        self._active_thread = thread
        thread.start()
        return self.get_status()

    def run_keepalive_switch(self, display_name: str) -> None:
        with self._lock:
            if self._task.status == "running":
                raise EsimSwitchConflictError("An eSIM switch task is already running")
            task_id = uuid.uuid4().hex
            started_at = utc_now_iso()
            self._task = SwitchTask(
                task_id=task_id,
                status="running",
                target_display_name=display_name,
                requested_lock_minutes=None,
                started_at=started_at,
                current_step="starting",
                steps=[],
            )
            self._notify_locked("task_started", self._serialize_task(self._task))
        self._run_keepalive_flow(task_id, display_name)

    def _run_keepalive_flow(self, task_id: str, display_name: str) -> None:
        task_dir = self.config.switch_screenshot_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        try:
            device = self._connect_device()
            device.screen_on()
            self._capture_step(task_id, task_dir, "screen_on", "点亮屏幕", "succeeded", "屏幕已点亮", device)
            self._ensure_unlocked(device, task_id, task_dir)
            self._reset_settings_app(device, task_id, task_dir)
            device.shell("am start -a android.settings.WIRELESS_SETTINGS")
            time.sleep(self.config.switch_step_delay_seconds)
            self._capture_step(task_id, task_dir, "open_settings", "打开网络设置", "succeeded", "已打开无线设置页", device)
            self._click_first_match(device, self.config.esim_settings_label)
            time.sleep(self.config.switch_step_delay_seconds)
            self._capture_step(task_id, task_dir, "open_sim_list", "进入 SIM 卡列表", "succeeded", "已进入 SIM 卡设置", device)
            self._click_exact_text(device, display_name)
            time.sleep(self.config.switch_step_delay_seconds)
            self._capture_step(task_id, task_dir, "select_target", "选择目标 eSIM", "succeeded", f"已点击 {display_name}", device)
            self._click_first_match(device, self.config.esim_toggle_label)
            time.sleep(self.config.switch_step_delay_seconds)
            self._capture_step(task_id, task_dir, "toggle_target", "启用目标 eSIM", "succeeded", "已点击启用开关", device)
            if self._has_confirm_dialog(device, display_name):
                self._confirm_switch_dialog(device, display_name)
                time.sleep(self.config.switch_step_delay_seconds)
                self._capture_step(task_id, task_dir, "confirm_switch", "确认切换", "succeeded", f"已确认切换到 {display_name}", device)
            verification_detail = self._wait_and_verify_switch(device, task_id, task_dir, display_name)
            self.esim_service.sync()
            self._capture_step(task_id, task_dir, "verify_switch", "切换生效确认完成", "succeeded", verification_detail, device)
            self._finish_keepalive_task("succeeded", None)
        except Exception as exc:  # noqa: BLE001
            self._finish_keepalive_task("failed", str(exc))
            raise

    def _run_switch_flow(self, task_id: str, display_name: str) -> None:
        task_dir = self.config.switch_screenshot_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        try:
            device = self._connect_device()
            device.screen_on()
            self._capture_step(task_id, task_dir, "screen_on", "点亮屏幕", "succeeded", "屏幕已点亮", device)

            self._ensure_unlocked(device, task_id, task_dir)
            self._reset_settings_app(device, task_id, task_dir)

            device.shell("am start -a android.settings.WIRELESS_SETTINGS")
            time.sleep(self.config.switch_step_delay_seconds)
            self._capture_step(task_id, task_dir, "open_settings", "打开网络设置", "succeeded", "已打开无线设置页", device)

            self._click_first_match(device, self.config.esim_settings_label)
            time.sleep(self.config.switch_step_delay_seconds)
            self._capture_step(task_id, task_dir, "open_sim_list", "进入 SIM 卡列表", "succeeded", "已进入 SIM 卡设置", device)

            self._click_exact_text(device, display_name)
            time.sleep(self.config.switch_step_delay_seconds)
            self._capture_step(task_id, task_dir, "select_target", "选择目标 eSIM", "succeeded", f"已点击 {display_name}", device)

            self._click_first_match(device, self.config.esim_toggle_label)
            time.sleep(self.config.switch_step_delay_seconds)
            self._capture_step(task_id, task_dir, "toggle_target", "启用目标 eSIM", "succeeded", "已点击启用开关", device)

            if self._has_confirm_dialog(device, display_name):
                self._confirm_switch_dialog(device, display_name)
                time.sleep(self.config.switch_step_delay_seconds)
                self._capture_step(task_id, task_dir, "confirm_switch", "确认切换", "succeeded", f"已确认切换到 {display_name}", device)

            verification_detail = self._wait_and_verify_switch(device, task_id, task_dir, display_name)
            self.esim_service.sync()
            self._capture_step(
                task_id,
                task_dir,
                "verify_switch",
                "切换生效确认完成",
                "succeeded",
                verification_detail,
                device,
            )
            self._finish_task("succeeded", None)
        except Exception as exc:  # noqa: BLE001
            self._finish_task("failed", str(exc))

    def _ensure_unlocked(self, device: Any, task_id: str, task_dir: Path) -> None:
        """Try to dismiss the lock screen before opening settings."""

        detail_parts: list[str] = []

        if hasattr(device, "unlock"):
            try:
                device.unlock()
                detail_parts.append("已调用 unlock()")
            except Exception:  # noqa: BLE001
                detail_parts.append("unlock() 调用失败，继续尝试滑动解锁")

        time.sleep(self.config.switch_step_delay_seconds)

        if hasattr(device, "swipe_ext"):
            try:
                device.swipe_ext("up", scale=0.9)
                detail_parts.append("已向上滑动收起锁屏层")
            except Exception:  # noqa: BLE001
                detail_parts.append("swipe_ext('up') 失败")
        elif hasattr(device, "swipe"):
            try:
                device.swipe(0.5, 0.85, 0.5, 0.2)
                detail_parts.append("已执行备用上滑手势")
            except Exception:  # noqa: BLE001
                detail_parts.append("备用上滑手势失败")

        time.sleep(self.config.switch_step_delay_seconds)
        self._capture_step(
            task_id,
            task_dir,
            "unlock_screen",
            "解锁并收起锁屏层",
            "succeeded",
            "；".join(detail_parts) if detail_parts else "已尝试解锁设备",
            device,
        )

    def _reset_settings_app(self, device: Any, task_id: str, task_dir: Path) -> None:
        """Force-stop Android Settings so each switch starts from a clean state."""

        detail = "已强制停止设置应用 com.android.settings"
        try:
            if hasattr(device, "app_stop"):
                device.app_stop("com.android.settings")
            else:
                device.shell("am force-stop com.android.settings")
        except Exception as exc:  # noqa: BLE001
            detail = f"尝试强制停止设置应用失败，继续执行：{exc}"
        time.sleep(self.config.switch_step_delay_seconds)
        self._capture_step(
            task_id,
            task_dir,
            "reset_settings",
            "复位设置应用",
            "succeeded",
            detail,
            device,
        )

    def _finish_task(self, status: SwitchTaskStatus, error: str | None) -> None:
        with self._lock:
            finished_at = utc_now_iso()
            self._task.status = status
            self._task.finished_at = finished_at
            self._task.error = error
            self._task.current_step = self._task.steps[-1].title if self._task.steps else self._task.current_step

            if status == "succeeded":
                lock_minutes = self._task.requested_lock_minutes or 10
                lock_until = self._iso_after_minutes(lock_minutes, finished_at)
                self._lock_state = SwitchLockState(
                    lock_until=lock_until,
                    lock_minutes=lock_minutes,
                    last_target_display_name=self._task.target_display_name,
                    locked_at=finished_at,
                )
                self._persist_lock_locked()
                self._append_log_locked(
                    "succeeded",
                    self._task.target_display_name,
                    lock_minutes,
                    f"已切换到 {self._task.target_display_name}，锁定至 {self._format_lock_until(lock_until)}",
                    self._task.task_id,
                    created_at=finished_at,
                )
                event_name = "task_succeeded"
            else:
                self._append_log_locked(
                    "failed",
                    self._task.target_display_name,
                    self._task.requested_lock_minutes,
                    f"切换到 {self._task.target_display_name} 失败：{error or '未知错误'}",
                    self._task.task_id,
                    created_at=finished_at,
                )
                event_name = "task_failed"

            self._notify_locked(event_name, self._serialize_task(self._task))

    def _finish_keepalive_task(self, status: SwitchTaskStatus, error: str | None) -> None:
        with self._lock:
            finished_at = utc_now_iso()
            self._task.status = status
            self._task.finished_at = finished_at
            self._task.error = error
            self._task.current_step = self._task.steps[-1].title if self._task.steps else self._task.current_step
            if status == "succeeded":
                event_name = "task_succeeded"
            else:
                event_name = "task_failed"
            self._notify_locked(event_name, self._serialize_task(self._task))

    def _capture_step(
        self,
        task_id: str,
        task_dir: Path,
        step_key: str,
        title: str,
        status: SwitchStepStatus,
        detail: str,
        device: Any,
    ) -> None:
        timestamp = utc_now_iso()
        with self._lock:
            filename = f"{len(self._task.steps) + 1:02d}_{step_key}.png"
        file_path = task_dir / filename
        image = device.screenshot(format="pillow")
        image.save(file_path)
        screenshot_url = f"/switch-screenshots/{task_id}/{filename}"
        step = SwitchStep(
            step_key=step_key,
            title=title,
            status=status,
            timestamp=timestamp,
            screenshot_url=screenshot_url,
            detail=detail,
        )
        with self._lock:
            self._task.current_step = title
            self._task.latest_screenshot_url = screenshot_url
            self._task.steps.append(step)
            self._notify_locked("step", self._serialize_task(self._task))

    def _connect_device(self) -> Any:
        if self.config.adb_device_serial:
            return u2.connect(self.config.adb_device_serial)
        return u2.connect()

    def _click_exact_text(self, device: Any, text: str) -> None:
        if not device(text=text).exists:
            raise RuntimeError(f"未找到目标 eSIM: {text}")
        device(text=text).click()

    def _click_first_match(self, device: Any, labels: tuple[str, ...]) -> None:
        for label in labels:
            if device(text=label).exists:
                device(text=label).click()
                return
        raise RuntimeError(f"未找到可点击控件: {', '.join(labels)}")

    def _exists_any(self, device: Any, labels: tuple[str, ...]) -> bool:
        return any(device(text=label).exists for label in labels)

    def _has_confirm_dialog(self, device: Any, display_name: str) -> bool:
        return self._exists_any(device, self.config.esim_confirm_label) or device(textContains=display_name).exists

    def _confirm_switch_dialog(self, device: Any, display_name: str) -> None:
        dynamic_prefixes = (
            f"切换到{display_name}",
            f"切换至{display_name}",
            f"使用{display_name}",
            f"启用{display_name}",
        )
        for text_value in dynamic_prefixes:
            if device(text=text_value).exists:
                device(text=text_value).click()
                return
            if device(textContains=text_value).exists:
                device(textContains=text_value).click()
                return

        if device(textContains=display_name).exists:
            device(textContains=display_name).click()
            return

        self._click_first_match(device, self.config.esim_confirm_label)

    def _wait_and_verify_switch(self, device: Any, task_id: str, task_dir: Path, display_name: str) -> str:
        time.sleep(self.config.switch_confirm_wait_seconds)
        self.esim_service.sync()
        latest_snapshot = self.esim_service.latest()
        active_names = [
            item.get("display_name")
            for item in latest_snapshot.get("subscriptions", [])
            if item.get("is_active") and item.get("display_name")
        ]
        dumpsys_output = self.esim_service.adb_client.read_isub()
        parsed_snapshot = parse_isub_output(dumpsys_output)
        dumpsys_active_names = [
            item.display_name
            for item in parsed_snapshot.subscriptions
            if item.is_active and item.display_name
        ]
        if display_name in dumpsys_active_names:
            return f"等待 {self.config.switch_confirm_wait_seconds:.0f} 秒后通过 dumpsys isub 确认成功，当前激活 eSIM 为 {display_name}"
        if display_name in active_names:
            return f"等待 {self.config.switch_confirm_wait_seconds:.0f} 秒后本地快照已更新为 {display_name}，但 dumpsys isub 尚未确认"
        if dumpsys_active_names:
            return f"等待 {self.config.switch_confirm_wait_seconds:.0f} 秒后通过 dumpsys isub 确认，当前激活 eSIM: {', '.join(dumpsys_active_names)}"
        if active_names:
            return f"等待 {self.config.switch_confirm_wait_seconds:.0f} 秒后截图确认，当前激活 eSIM: {', '.join(active_names)}"
        return f"等待 {self.config.switch_confirm_wait_seconds:.0f} 秒后截图确认，暂未从快照识别到激活 eSIM"

    def _serialize_task(self, task: SwitchTask) -> dict[str, Any]:
        self._refresh_lock_locked()
        lock_active = self._lock_state is not None
        return {
            "task_id": task.task_id,
            "status": task.status,
            "target_display_name": task.target_display_name,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "current_step": task.current_step,
            "latest_screenshot_url": task.latest_screenshot_url,
            "error": task.error,
            "lock_active": lock_active,
            "lock_until": self._lock_state.lock_until if self._lock_state else None,
            "lock_remaining_seconds": self._remaining_seconds(self._lock_state.lock_until) if self._lock_state else 0,
            "lock_minutes": self._lock_state.lock_minutes if self._lock_state else None,
            "logs": [
                {
                    "event_type": entry.event_type,
                    "display_name": entry.display_name,
                    "lock_minutes": entry.lock_minutes,
                    "created_at": entry.created_at,
                    "message": entry.message,
                    "task_id": entry.task_id,
                }
                for entry in self._logs
            ],
            "steps": [
                {
                    "step_key": step.step_key,
                    "title": step.title,
                    "status": step.status,
                    "timestamp": step.timestamp,
                    "screenshot_url": step.screenshot_url,
                    "detail": step.detail,
                }
                for step in task.steps
            ],
        }

    def _append_log_locked(
        self,
        event_type: SwitchLogEventType,
        display_name: str | None,
        lock_minutes: int | None,
        message: str,
        task_id: str | None,
        *,
        created_at: str | None = None,
    ) -> None:
        entry = SwitchLogEntry(
            event_type=event_type,
            display_name=display_name,
            lock_minutes=lock_minutes,
            created_at=created_at or utc_now_iso(),
            message=message,
            task_id=task_id,
        )
        self._logs.insert(0, entry)
        self._logs = self._logs[:MAX_LOG_ENTRIES]
        self._persist_logs_locked()

    def _persist_lock_locked(self) -> None:
        if self._lock_state is None:
            self.db.set_app_state(LOCK_STATE_KEY, None, utc_now_iso())
            return
        self.db.set_app_state(
            LOCK_STATE_KEY,
            {
                "lock_until": self._lock_state.lock_until,
                "lock_minutes": self._lock_state.lock_minutes,
                "last_target_display_name": self._lock_state.last_target_display_name,
                "locked_at": self._lock_state.locked_at,
            },
            self._lock_state.locked_at,
        )

    def _persist_logs_locked(self) -> None:
        updated_at = self._logs[0].created_at if self._logs else utc_now_iso()
        self.db.set_app_state(
            LOG_STATE_KEY,
            [
                {
                    "event_type": entry.event_type,
                    "display_name": entry.display_name,
                    "lock_minutes": entry.lock_minutes,
                    "created_at": entry.created_at,
                    "message": entry.message,
                    "task_id": entry.task_id,
                }
                for entry in self._logs
            ],
            updated_at,
        )

    def _deserialize_lock_state(self, raw_value: Any) -> SwitchLockState | None:
        if not isinstance(raw_value, dict):
            return None
        lock_until = raw_value.get("lock_until")
        lock_minutes = raw_value.get("lock_minutes")
        locked_at = raw_value.get("locked_at")
        if not isinstance(lock_until, str) or not isinstance(lock_minutes, int) or not isinstance(locked_at, str):
            return None
        return SwitchLockState(
            lock_until=lock_until,
            lock_minutes=lock_minutes,
            last_target_display_name=raw_value.get("last_target_display_name"),
            locked_at=locked_at,
        )

    def _deserialize_logs(self, raw_value: Any) -> list[SwitchLogEntry]:
        if not isinstance(raw_value, list):
            return []
        logs: list[SwitchLogEntry] = []
        for item in raw_value[:MAX_LOG_ENTRIES]:
            if not isinstance(item, dict):
                continue
            event_type = item.get("event_type")
            created_at = item.get("created_at")
            message = item.get("message")
            if event_type not in {"attempt_started", "blocked_running", "blocked_locked", "succeeded", "failed"}:
                continue
            if not isinstance(created_at, str) or not isinstance(message, str):
                continue
            lock_minutes = item.get("lock_minutes")
            logs.append(
                SwitchLogEntry(
                    event_type=event_type,
                    display_name=item.get("display_name"),
                    lock_minutes=lock_minutes if isinstance(lock_minutes, int) else None,
                    created_at=created_at,
                    message=message,
                    task_id=item.get("task_id"),
                )
            )
        return logs

    def _refresh_lock_locked(self) -> None:
        if self._lock_state is None:
            return
        if self._remaining_seconds(self._lock_state.lock_until) > 0:
            return
        self._lock_state = None
        self._persist_lock_locked()

    def _remaining_seconds(self, lock_until: str) -> int:
        lock_until_dt = self._parse_iso(lock_until)
        remaining = int((lock_until_dt - datetime.now(timezone.utc)).total_seconds())
        return max(0, remaining)

    def _iso_after_minutes(self, minutes: int, start_at: str) -> str:
        base = self._parse_iso(start_at)
        return (base + timedelta(minutes=minutes)).isoformat()

    def _parse_iso(self, value: str) -> datetime:
        return datetime.fromisoformat(value)

    def _format_lock_until(self, value: str) -> str:
        dt = self._parse_iso(value).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _notify_locked(self, event: str, data: dict[str, Any]) -> None:
        dead_queues: list[asyncio.Queue[dict[str, Any]]] = []
        for queue in self._subscribers:
            try:
                queue.put_nowait({"event": event, "data": data})
            except RuntimeError:
                dead_queues.append(queue)
        for queue in dead_queues:
            self._subscribers.discard(queue)
