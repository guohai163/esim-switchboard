import asyncio
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.adb import AdbClient, AdbCommandError
from app.collab import CollabSessionService
from app.config import AppConfig
from app.db import Database
from app.device_monitor import BROWSER_SUPPORTED_HINT
from app.keepalive_service import KeepaliveService
from app.main import build_services, create_app
from app.services import AppServices, EsimSyncService
from app.models import SmsMessageRecord
from app.switch_service import EsimSwitchConflictError


class FakeAdbClient:
    def __init__(self) -> None:
        self.isub_output = """
        ActiveSubInfoList:
         {id=7 simSlotIndex=1 displayName=giffgaff sws carrierName=giffgaff isEmbedded=true}
        ++++++++++++++++++++++++++++++++
        AllSubInfoList:
         {id=1 simSlotIndex=0 displayName=CARD carrierName=运营商未知 isEmbedded=false}
         {id=5 simSlotIndex=-1 displayName=Club carrierName=没有服务 isEmbedded=true}
         {id=7 simSlotIndex=1 displayName=giffgaff sws carrierName=giffgaff isEmbedded=true}
        ++++++++++++++++++++++++++++++++
        """
        self.sms_output = """Row: 0 address=10010, body=hello, sub_id=7, _id=1, date=1716680000000
Row: 1 address=10086, body=world, sub_id=5, _id=2, date=1716670000000
"""
        self.keyevents: list[int] = []
        self.taps: list[tuple[int, int]] = []
        self.wake_calls = 0
        self.screen_size = (1080, 2400)
        self.screenrecord_help = "Usage: screenrecord [options] --output-format=h264"
        self.last_screenrecord_bitrate: str | None = None
        self.last_screenrecord_time_limit: int | None = None

    def get_devices(self, timeout: float = 30) -> list[str]:  # noqa: ARG002
        return ["device-001"]

    def read_isub(self) -> str:
        return self.isub_output

    def query_sms_inbox(self, limit=None) -> str:  # noqa: ANN001
        return self.sms_output

    def run(self, args: list[str], timeout: float = 30) -> str:  # noqa: ARG002
        if args == ["shell", "screenrecord", "--help"]:
            return self.screenrecord_help
        if args == ["shell", "wm", "size"]:
            return f"Physical size: {self.screen_size[0]}x{self.screen_size[1]}"
        raise AssertionError(f"Unexpected adb run command: {args}")

    def stream_logcat(self):  # noqa: ANN201
        class _DummyProcess:
            stdout = None

            @staticmethod
            def poll() -> int:
                return 0

        return _DummyProcess()

    def send_keyevent(self, keycode: int) -> None:
        self.keyevents.append(keycode)

    def wake_screen(self) -> None:
        self.wake_calls += 1
        self.send_keyevent(224)

    def send_tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def get_screen_size(self) -> tuple[int, int]:
        return self.screen_size

    def open_h264_screenrecord(
        self,
        *,
        bitrate: str | None = None,
        time_limit_seconds: int | None = None,
        transport: str = "exec-out",
    ):  # noqa: ANN201
        import io

        payload = (
            b"\x00\x00\x00\x01\x67\x42\x80\x0a"
            b"\x00\x00\x00\x01\x68\xce\x06\xf2"
            b"\x00\x00\x00\x01\x65\xb8\x43\x71"
        )

        class _PeekPipe(io.BufferedReader):
            def __init__(self, data: bytes) -> None:
                self._raw = io.BytesIO(data)
                super().__init__(self._raw)

        class _Process:
            def __init__(self) -> None:
                self.stdout = _PeekPipe(payload)
                self.stderr = io.BytesIO(b"")

            @staticmethod
            def poll() -> int | None:
                return None

            @staticmethod
            def terminate() -> None:
                return None

            @staticmethod
            def wait(timeout: float = 0) -> int:  # noqa: ARG004
                return 0

            @staticmethod
            def kill() -> None:
                return None

            @staticmethod
            def communicate(timeout: float | None = None):  # noqa: ARG004, ANN205
                return payload, b""

        self.last_screenrecord_bitrate = bitrate
        self.last_screenrecord_time_limit = time_limit_seconds
        return _Process()


class FakeSwitchService:
    def __init__(self) -> None:
        self.task = {
            "task_id": None,
            "status": "idle",
            "target_display_name": None,
            "started_at": None,
            "finished_at": None,
            "current_step": None,
            "latest_screenshot_url": None,
            "error": None,
            "lock_active": False,
            "lock_until": None,
            "lock_remaining_seconds": 0,
            "lock_minutes": None,
            "logs": [],
            "steps": [],
        }
        self.locked = False
        self.pre_switch_hooks: list[tuple[str, bool]] = []

    def get_status(self) -> dict:
        return self.task

    def restore_state(self) -> None:
        return None

    def register_pre_switch_hook(self, hook) -> None:  # noqa: ANN001
        self._pre_switch_hook = hook

    def start_switch(self, display_name: str, lock_minutes: int) -> dict:
        if self.task["status"] == "running":
            raise EsimSwitchConflictError("An eSIM switch task is already running")
        if self.locked:
            raise RuntimeError("当前处于切换锁定期，需等待到 2026-05-26 08:30:00 后再试，剩余约 10 分钟")
        if hasattr(self, "_pre_switch_hook"):
            self._pre_switch_hook(display_name, False)
            self.pre_switch_hooks.append((display_name, False))
        self.task = {
            "task_id": "task-001",
            "status": "running",
            "target_display_name": display_name,
            "started_at": "2026-05-26T00:00:00+00:00",
            "finished_at": None,
            "current_step": "打开网络设置",
            "latest_screenshot_url": "/switch-screenshots/task-001/01_open_settings.png",
            "error": None,
            "lock_active": False,
            "lock_until": None,
            "lock_remaining_seconds": 0,
            "lock_minutes": None,
            "logs": [
                {
                    "event_type": "attempt_started",
                    "display_name": display_name,
                    "lock_minutes": lock_minutes,
                    "created_at": "2026-05-26T00:00:00+00:00",
                    "message": f"发起切换到 {display_name}，成功后将锁定 {lock_minutes} 分钟",
                    "task_id": "task-001",
                }
            ],
            "steps": [
                {
                    "step_key": "open_settings",
                    "title": "打开网络设置",
                    "status": "succeeded",
                    "timestamp": "2026-05-26T00:00:01+00:00",
                    "screenshot_url": "/switch-screenshots/task-001/01_open_settings.png",
                    "detail": "已打开无线设置页",
                }
            ],
        }
        return self.task

    async def subscribe(self):  # noqa: ANN201
        import asyncio

        queue = asyncio.Queue()
        await queue.put({"event": "snapshot", "data": self.task})
        return queue

    def unsubscribe(self, queue):  # noqa: ANN001
        return None


class FakeSmsEventService:
    def __init__(self) -> None:
        self.latest = {
            "event_type": None,
            "occurred_at": None,
            "inserted_count": 0,
            "fetched_count": 0,
            "latest_sms_id": None,
            "latest_address": None,
            "latest_display_name": None,
            "event_source": None,
            "trigger_buffer": None,
            "trigger_log_line": None,
        }

    def get_status(self) -> dict:
        return self.latest

    def broadcast_new_sms(self, payload: dict) -> None:
        self.latest = {
            "event_type": "new_sms",
            "occurred_at": payload.get("occurred_at"),
            "inserted_count": payload.get("inserted_count", 0),
            "fetched_count": payload.get("fetched_count", 0),
            "latest_sms_id": payload.get("latest_sms_id"),
            "latest_address": payload.get("latest_address"),
            "latest_display_name": payload.get("latest_display_name"),
            "event_source": payload.get("event_source"),
            "trigger_buffer": payload.get("trigger_buffer"),
            "trigger_log_line": payload.get("trigger_log_line"),
        }

    async def subscribe(self):  # noqa: ANN201
        import asyncio

        queue = asyncio.Queue()
        await queue.put({"event": "snapshot", "data": self.latest})
        return queue

    def unsubscribe(self, queue):  # noqa: ANN001
        return None


class FakeKeepaliveService:
    def __init__(self) -> None:
        self.rules: dict[str, dict] = {}
        self.started = False
        self.testing_conflict = False

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 5) -> None:  # noqa: ARG002
        self.started = False

    def list_rules(self) -> list[dict]:
        return list(self.rules.values())

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
    ) -> dict:
        rule = {
            "esim_sub_id": esim_sub_id,
            "esim_display_name": display_name,
            "esim_carrier_name": carrier_name,
            "timezone_name": timezone_name,
            "window_start_hour": 9,
            "window_end_hour": 19,
            "target_phone": target_phone,
            "interval_days": interval_days,
            "enabled": enabled,
            "message_preview": f"保号 {display_name or ''} 2026-05-29".strip(),
            "last_attempt_at": None,
            "last_success_at": None,
            "next_run_at": "2026-05-30T00:00:00+00:00" if enabled else None,
            "retry_after_at": None,
            "last_status": "scheduled" if enabled else "idle",
            "last_error": None,
            "created_at": "2026-05-29T00:00:00+00:00",
            "updated_at": "2026-05-29T00:00:00+00:00",
        }
        self.rules[esim_sub_id] = rule
        return rule

    def patch_rule(self, esim_sub_id: str, *, timezone_name=None, target_phone=None, interval_days=None, enabled=None):  # noqa: ANN001
        current = self.rules.get(esim_sub_id)
        if current is None:
            return None
        if timezone_name is not None:
            current["timezone_name"] = timezone_name
        if target_phone is not None:
            current["target_phone"] = target_phone
        if interval_days is not None:
            current["interval_days"] = interval_days
        if enabled is not None:
            current["enabled"] = enabled
            current["last_status"] = "scheduled" if enabled else "idle"
        current["updated_at"] = "2026-05-29T00:10:00+00:00"
        return current

    def delete_rule(self, esim_sub_id: str) -> bool:
        return self.rules.pop(esim_sub_id, None) is not None

    def run_rule_test(self, esim_sub_id: str) -> dict:
        if self.testing_conflict:
            raise RuntimeError("An eSIM switch task is already running")
        current = self.rules.get(esim_sub_id)
        if current is None:
            raise KeyError("Keepalive rule not found")
        current["last_attempt_at"] = "2026-05-29T01:00:00+00:00"
        current["last_success_at"] = "2026-05-29T01:00:00+00:00"
        current["next_run_at"] = "2026-05-30T01:00:00+00:00"
        current["last_status"] = "succeeded"
        current["last_error"] = None
        current["retry_after_at"] = None
        current["updated_at"] = "2026-05-29T01:00:00+00:00"
        return current


class RecordingAdbClient:
    def __init__(self, config: AppConfig) -> None:
        from app.adb import AdbClient

        self._client = AdbClient(config)
        self.commands: list[list[str]] = []

    def run(self, args: list[str], timeout: float = 30) -> str:
        self.commands.append(args)
        return """Row: 0 address=10010, body=hello, sub_id=7, _id=1, date=1716680000000
Row: 1 address=10086, body=world, sub_id=5, _id=2, date=1716670000000
"""

    def query_sms_inbox(self, limit=None) -> str:  # noqa: ANN001
        return self._client.query_sms_inbox(limit=limit)


class FailingDevicesAdbClient(FakeAdbClient):
    def get_devices(self, timeout: float = 30) -> list[str]:  # noqa: ARG002
        raise AdbCommandError(["adb", "devices"], "ADB command timed out after 3s")


def make_test_app(tmp_path: Path) -> TestClient:
    config = AppConfig.from_env(tmp_path)
    config.adb_path = "/tmp/fake-adb"
    config.app_password = "secret123"
    config.app_auth_cookie_name = "esim_switch_auth"
    config.app_auth_cookie_value = "signed-secret-cookie"
    services = build_services(config)
    services.adb_client = FakeAdbClient()
    services.esim_service.adb_client = services.adb_client
    services.sms_service.adb_client = services.adb_client
    services.monitor.adb_client = services.adb_client
    services.monitor.sms_service = services.sms_service
    services.switch_service = FakeSwitchService()
    services.sms_event_service = FakeSmsEventService()
    services.monitor.sms_event_service = services.sms_event_service
    services.keepalive_service = FakeKeepaliveService()
    services.device_monitor_service.adb_client = services.adb_client
    services.switch_service.register_pre_switch_hook(
        lambda display_name, keepalive: services.device_monitor_service.force_stop(
            "eSIM 切换已开始，手机监控已自动关闭" if not keepalive else "保号任务需要独占手机，监控已自动关闭"
        )
    )
    services.db.init_schema()
    services.esim_service.sync()
    services.sms_service.sync_all_inbox()
    app = create_app(config=config, services=services, auto_startup=False)
    return TestClient(app)


def make_test_services(tmp_path: Path) -> tuple[AppConfig, AppServices]:
    config = AppConfig.from_env(tmp_path)
    config.adb_path = "/tmp/fake-adb"
    config.app_password = "secret123"
    config.app_auth_cookie_name = "esim_switch_auth"
    config.app_auth_cookie_value = "signed-secret-cookie"
    services = build_services(config)
    services.adb_client = FakeAdbClient()
    services.esim_service.adb_client = services.adb_client
    services.sms_service.adb_client = services.adb_client
    services.monitor.adb_client = services.adb_client
    services.monitor.sms_service = services.sms_service
    services.switch_service = FakeSwitchService()
    services.sms_event_service = FakeSmsEventService()
    services.monitor.sms_event_service = services.sms_event_service
    services.keepalive_service = FakeKeepaliveService()
    services.device_monitor_service.adb_client = services.adb_client
    services.switch_service.register_pre_switch_hook(
        lambda display_name, keepalive: services.device_monitor_service.force_stop(
            "eSIM 切换已开始，手机监控已自动关闭" if not keepalive else "保号任务需要独占手机，监控已自动关闭"
        )
    )
    services.db.init_schema()
    return config, services


def make_test_client_with_config(tmp_path: Path, *, trust_proxy_headers: bool = False) -> TestClient:
    config = AppConfig.from_env(tmp_path)
    config.adb_path = "/tmp/fake-adb"
    config.app_password = "secret123"
    config.app_auth_cookie_name = "esim_switch_auth"
    config.app_auth_cookie_value = "signed-secret-cookie"
    config.trust_proxy_headers = trust_proxy_headers
    config.collab_presence_timeout_seconds = 0.1
    config.collab_ping_interval_seconds = 0.05
    services = build_services(config)
    services.adb_client = FakeAdbClient()
    services.esim_service.adb_client = services.adb_client
    services.sms_service.adb_client = services.adb_client
    services.monitor.adb_client = services.adb_client
    services.monitor.sms_service = services.sms_service
    services.switch_service = FakeSwitchService()
    services.sms_event_service = FakeSmsEventService()
    services.monitor.sms_event_service = services.sms_event_service
    services.keepalive_service = FakeKeepaliveService()
    services.device_monitor_service.adb_client = services.adb_client
    services.switch_service.register_pre_switch_hook(
        lambda display_name, keepalive: services.device_monitor_service.force_stop(
            "eSIM 切换已开始，手机监控已自动关闭" if not keepalive else "保号任务需要独占手机，监控已自动关闭"
        )
    )
    services.db.init_schema()
    services.esim_service.sync()
    services.sms_service.sync_all_inbox()
    app = create_app(config=config, services=services, auto_startup=False)
    return TestClient(app)


def test_database_upsert_sms_deduplicates(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db.init_schema()
    messages = [
        SmsMessageRecord(
            sms_id="1",
            address="10010",
            body="hello",
            sub_id="7",
            date_ts=1716680000000,
            raw_row="Row: 0 address=10010, body=hello",
        )
    ]

    first_inserted, first_duplicates = db.upsert_sms_messages(messages, "2026-05-26T00:00:00+00:00", "2026-05-26T00:00:00+00:00")
    second_inserted, second_duplicates = db.upsert_sms_messages(messages, "2026-05-26T00:01:00+00:00", "2026-05-26T00:01:00+00:00")

    assert first_inserted == 1
    assert first_duplicates == 0
    assert second_inserted == 0
    assert second_duplicates == 1


def test_database_upsert_sms_persists_raw_row(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db.init_schema()
    message = SmsMessageRecord(
        sms_id="42",
        address="10010",
        body="hello",
        sub_id="7",
        date_ts=1716680000000,
        raw_row="Row: 0 address=10010, body=hello, sub_id=7, _id=42, date=1716680000000",
    )

    inserted, duplicates = db.upsert_sms_messages([message], "2026-05-29T00:00:00+00:00", "2026-05-29T00:00:00+00:00")

    assert inserted == 1
    assert duplicates == 0
    with db._connect() as conn:  # noqa: SLF001
        row = conn.execute("SELECT raw_row FROM sms_messages WHERE sms_id = ?", ("42",)).fetchone()
    assert row is not None
    assert row["raw_row"] == message.raw_row


def test_database_migrates_legacy_app_state_schema(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE app_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?)",
        ("legacy_state", '{"ok": true}', "2026-05-29T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    db = Database(db_path)
    db.init_schema()
    payload = db.get_app_state_json("legacy_state")

    assert payload == {"ok": True}


def test_keepalive_database_rule_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db.init_schema()

    created = db.upsert_keepalive_rule(
        "5",
        esim_display_name="Club",
        esim_carrier_name="没有服务",
        timezone_name="Asia/Shanghai",
        window_start_hour=9,
        window_end_hour=19,
        target_phone="13800138000",
        interval_days=2,
        enabled=True,
        message_preview="保号 Club 2026-05-29",
        next_run_at="2026-05-31T00:00:00+00:00",
        last_status="scheduled",
        updated_at="2026-05-29T00:00:00+00:00",
    )
    patched = db.patch_keepalive_rule(
        "5",
        enabled=False,
        last_status="idle",
        updated_at="2026-05-29T00:01:00+00:00",
    )
    listed = db.list_keepalive_rules()
    deleted = db.delete_keepalive_rule("5")

    assert created["enabled"] is True
    assert created["target_phone"] == "13800138000"
    assert created["timezone_name"] == "Asia/Shanghai"
    assert patched is not None
    assert patched["enabled"] is False
    assert listed[0]["esim_sub_id"] == "5"
    assert deleted is True


def test_keepalive_database_rule_insert_accepts_full_payload(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db.init_schema()

    created = db.upsert_keepalive_rule(
        "7",
        esim_display_name="gg+4407719752845",
        esim_carrier_name="只能拨打紧急呼救电话",
        timezone_name="Asia/Shanghai",
        window_start_hour=9,
        window_end_hour=19,
        target_phone="+8518500050982",
        interval_days=90,
        enabled=True,
        message_preview="保号 gg+4407719752845 2026-05-29",
        next_run_at="2026-08-27T00:00:00+00:00",
        last_status="scheduled",
        updated_at="2026-05-29T00:00:00+00:00",
    )

    assert created["esim_sub_id"] == "7"
    assert created["target_phone"] == "+8518500050982"
    assert created["interval_days"] == 90
    assert created["last_error"] is None


def test_database_esim_remark_roundtrip_and_snapshot_mapping(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db.init_schema()
    fake_adb = FakeAdbClient()
    esim_service = EsimSyncService(db, fake_adb)
    esim_service.sync()

    created = db.upsert_esim_remark("5", "备用英国卡", "2026-05-29T00:00:00+00:00")
    updated = db.upsert_esim_remark("5", "主切换卡", "2026-05-29T00:01:00+00:00")
    remarks = db.list_esim_remarks()
    snapshot = db.get_latest_esim_snapshot()
    deleted = db.delete_esim_remark("5")
    snapshot_after_delete = db.get_latest_esim_snapshot()

    assert created["remark"] == "备用英国卡"
    assert updated["remark"] == "主切换卡"
    assert remarks == {"5": "主切换卡"}
    assert snapshot is not None
    assert next(item for item in snapshot["subscriptions"] if item["sub_id"] == "5")["remark"] == "主切换卡"
    assert next(item for item in snapshot["subscriptions"] if item["sub_id"] == "7")["remark"] is None
    assert deleted is True
    assert snapshot_after_delete is not None
    assert next(item for item in snapshot_after_delete["subscriptions"] if item["sub_id"] == "5")["remark"] is None


def test_keepalive_message_preview_truncates_to_40_chars(tmp_path: Path) -> None:
    config, services = make_test_services(tmp_path)
    keepalive = KeepaliveService(config, services.db, services.esim_service, FakeSwitchService(), FakeAdbClient())

    preview = keepalive.build_message_preview("非常非常非常非常非常非常非常非常长的号码名称ABCDEFG", "2026-05-29T00:00:00+00:00")

    assert preview.startswith("保号 ")
    assert "2026-05-29" in preview
    assert len(preview) <= 40


def test_keepalive_marks_waiting_when_manual_switch_running(tmp_path: Path) -> None:
    config, services = make_test_services(tmp_path)
    services.adb_client = FakeAdbClient()
    services.esim_service.adb_client = services.adb_client
    services.esim_service.sync()
    fake_switch = FakeSwitchService()
    fake_switch.task["status"] = "running"
    keepalive = KeepaliveService(config, services.db, services.esim_service, fake_switch, FakeAdbClient())
    keepalive.upsert_rule(
        "5",
        display_name="Club",
        carrier_name="没有服务",
        timezone_name="Asia/Shanghai",
        target_phone="13800138000",
        interval_days=1,
        enabled=True,
    )
    services.db.patch_keepalive_rule("5", next_run_at="2000-01-01T00:00:00+00:00", updated_at="2026-05-29T00:00:00+00:00")

    keepalive.execute_rule(services.db.get_keepalive_rule("5"))
    updated = services.db.get_keepalive_rule("5")

    assert updated is not None
    assert updated["last_status"] == "waiting"
    assert updated["retry_after_at"] is not None


def test_keepalive_rejects_invalid_timezone_name(tmp_path: Path) -> None:
    config, services = make_test_services(tmp_path)
    keepalive = KeepaliveService(config, services.db, services.esim_service, FakeSwitchService(), FakeAdbClient())

    try:
        keepalive.upsert_rule(
            "5",
            display_name="Club",
            carrier_name="没有服务",
            timezone_name="Invalid/Timezone",
            target_phone="13800138000",
            interval_days=1,
            enabled=True,
        )
    except ValueError as exc:
        assert "timezone_name" in str(exc)
    else:
        raise AssertionError("Expected invalid timezone to raise ValueError")


def test_keepalive_defers_to_same_day_window_start_before_9am(tmp_path: Path) -> None:
    config, services = make_test_services(tmp_path)
    keepalive = KeepaliveService(config, services.db, services.esim_service, FakeSwitchService(), FakeAdbClient())

    inside_window, deferred_run_at = keepalive._resolve_send_window("2026-05-29T00:30:00+00:00", __import__("zoneinfo").ZoneInfo("Asia/Shanghai"), 9, 19)  # noqa: SLF001

    assert inside_window is False
    assert deferred_run_at == "2026-05-29T01:00:00+00:00"


def test_keepalive_defers_to_next_day_window_start_after_7pm(tmp_path: Path) -> None:
    config, services = make_test_services(tmp_path)
    keepalive = KeepaliveService(config, services.db, services.esim_service, FakeSwitchService(), FakeAdbClient())

    inside_window, deferred_run_at = keepalive._resolve_send_window("2026-05-29T12:30:00+00:00", __import__("zoneinfo").ZoneInfo("Asia/Shanghai"), 9, 19)  # noqa: SLF001

    assert inside_window is False
    assert deferred_run_at == "2026-05-30T01:00:00+00:00"


def test_keepalive_send_sms_clicks_button_when_label_is_sms(tmp_path: Path) -> None:
    class FakeSelector:
        def __init__(self, exists: bool, recorder: list[tuple], selector_kwargs: dict) -> None:
            self.exists = exists
            self.recorder = recorder
            self.selector_kwargs = selector_kwargs

        def click(self) -> None:
            self.recorder.append(("click", self.selector_kwargs))

    class FakeDevice:
        def __init__(self) -> None:
            self.recorder: list[tuple] = []

        def __call__(self, **kwargs):  # noqa: ANN003
            if kwargs.get("resourceIdMatches") == ".*recipient.*":
                return FakeSelector(True, self.recorder, kwargs)
            if kwargs.get("resourceIdMatches") == ".*compose_message_text.*":
                return FakeSelector(True, self.recorder, kwargs)
            if kwargs.get("text") == "短信":
                return FakeSelector(True, self.recorder, kwargs)
            return FakeSelector(False, self.recorder, kwargs)

    class FakeAdb:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def send_sms_via_ui(self, target_phone: str, message: str, device):  # noqa: ANN001
            self.calls.append((target_phone, message))

    config, services = make_test_services(tmp_path)
    config.switch_step_delay_seconds = 0
    fake_switch = FakeSwitchService()
    keepalive = KeepaliveService(config, services.db, services.esim_service, fake_switch, FakeAdb())
    device = FakeDevice()

    keepalive._send_sms(device, "+8518500050982", "保号 gg+4407719752845 2026-05-29")

    assert ("click", {"text": "短信"}) in device.recorder


def test_keepalive_send_sms_allows_existing_thread_without_recipient_field(tmp_path: Path) -> None:
    class FakeSelector:
        def __init__(self, exists: bool, recorder: list[tuple], selector_kwargs: dict) -> None:
            self.exists = exists
            self.recorder = recorder
            self.selector_kwargs = selector_kwargs

        def click(self) -> None:
            self.recorder.append(("click", self.selector_kwargs))

    class FakeDevice:
        def __init__(self) -> None:
            self.recorder: list[tuple] = []

        def __call__(self, **kwargs):  # noqa: ANN003
            if kwargs.get("resourceIdMatches") == ".*recipient.*":
                return FakeSelector(False, self.recorder, kwargs)
            if kwargs.get("resourceIdMatches") == ".*compose_message_text.*":
                return FakeSelector(True, self.recorder, kwargs)
            if kwargs.get("text") == "短信":
                return FakeSelector(True, self.recorder, kwargs)
            return FakeSelector(False, self.recorder, kwargs)

    class FakeAdb:
        def send_sms_via_ui(self, target_phone: str, message: str, device):  # noqa: ANN001, ARG002
            return None

    config, services = make_test_services(tmp_path)
    config.switch_step_delay_seconds = 0
    fake_switch = FakeSwitchService()
    keepalive = KeepaliveService(config, services.db, services.esim_service, fake_switch, FakeAdb())
    recorded_steps: list[tuple] = []
    fake_switch._record_step = lambda step_key, title, status, detail: recorded_steps.append((step_key, title, status, detail))  # type: ignore[attr-defined]
    device = FakeDevice()

    keepalive._send_sms(device, "+8618500050982", "保号 gg+4407719752845 2026-05-29")

    assert ("click", {"text": "短信"}) in device.recorder
    assert any(step[0] == "send_sms" and "既有会话页继续发送" in step[3] for step in recorded_steps)


def test_api_endpoints_return_dashboard_data(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    login = client.post("/api/auth/login", json={"password": "secret123"})

    assert login.status_code == 200
    health = client.get("/api/health")
    latest_esim = client.get("/api/esim/latest")
    sms = client.get("/api/sms?page=1&page_size=10")
    sms_filtered = client.get("/api/sms?page=1&page_size=10&display_name=Club")
    monitor = client.get("/api/monitor/status")
    keepalive = client.get("/api/keepalive/rules")
    sync_all = client.post("/api/sms/sync-all")
    switch_status = client.get("/api/esim/switch/status")
    switch_start = client.post("/api/esim/switch", json={"display_name": "Club", "lock_minutes": 10})

    assert health.status_code == 200
    assert health.json()["adb_available"] is True
    assert health.json()["device_monitor"]["available"] is True
    assert health.json()["device_monitor"]["browser_supported_hint"] == BROWSER_SUPPORTED_HINT
    assert latest_esim.status_code == 200
    assert latest_esim.json()["embedded_total_count"] == 2
    assert latest_esim.json()["subscriptions"][0]["remark"] is None
    assert sms.status_code == 200
    assert sms.json()["total"] == 2
    assert sms.json()["items"][0]["display_name"] == "giffgaff sws"
    assert sms.json()["items"][1]["display_name"] == "Club"
    assert sms_filtered.status_code == 200
    assert sms_filtered.json()["total"] == 1
    assert sms_filtered.json()["items"][0]["display_name"] == "Club"
    assert monitor.status_code == 200
    assert monitor.json()["active_log_buffers"] == ["radio"]
    assert keepalive.status_code == 200
    assert keepalive.json()["items"] == []
    assert sync_all.status_code == 200
    assert sync_all.json()["detail"] == "full inbox sync"
    assert switch_status.status_code == 200
    assert switch_status.json()["status"] == "idle"
    assert switch_start.status_code == 200
    assert switch_start.json()["status"] == "running"
    assert switch_start.json()["target_display_name"] == "Club"
    assert switch_start.json()["logs"][0]["event_type"] == "attempt_started"
    assert switch_start.json()["logs"][0]["lock_minutes"] == 10


def test_switch_conflict_returns_409(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})

    first = client.post("/api/esim/switch", json={"display_name": "Club", "lock_minutes": 10})
    second = client.post("/api/esim/switch", json={"display_name": "giffgaff sws", "lock_minutes": 20})

    assert first.status_code == 200
    assert second.status_code == 409


def test_esim_remark_endpoint_supports_save_update_and_clear(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})

    created = client.put("/api/esim/remarks/5", json={"remark": "备用英国卡"})
    updated = client.put("/api/esim/remarks/5", json={"remark": "  主切换卡  "})
    latest = client.get("/api/esim/latest")
    cleared = client.put("/api/esim/remarks/5", json={"remark": "   "})
    latest_after_clear = client.get("/api/esim/latest")

    assert created.status_code == 200
    assert created.json() == {"sub_id": "5", "remark": "备用英国卡"}
    assert updated.status_code == 200
    assert updated.json() == {"sub_id": "5", "remark": "主切换卡"}
    assert latest.status_code == 200
    assert next(item for item in latest.json()["subscriptions"] if item["sub_id"] == "5")["remark"] == "主切换卡"
    assert cleared.status_code == 200
    assert cleared.json() == {"sub_id": "5", "remark": None}
    assert latest_after_clear.status_code == 200
    assert next(item for item in latest_after_clear.json()["subscriptions"] if item["sub_id"] == "5")["remark"] is None


def test_esim_remark_endpoint_supports_physical_sim_and_rejects_missing_sub_id(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})

    physical = client.put("/api/esim/remarks/1", json={"remark": "实体卡备注"})
    missing = client.put("/api/esim/remarks/999", json={"remark": "不存在"})

    assert physical.status_code == 200
    assert physical.json() == {"sub_id": "1", "remark": "实体卡备注"}
    assert missing.status_code == 404


def test_keepalive_rule_crud_endpoints(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})

    created = client.put(
        "/api/keepalive/rules/5",
        json={
            "display_name": "Club",
            "carrier_name": "没有服务",
            "timezone_name": "Asia/Shanghai",
            "target_phone": "13800138000",
            "interval_days": 3,
            "enabled": True,
        },
    )
    listed = client.get("/api/keepalive/rules")
    patched = client.patch("/api/keepalive/rules/5", json={"enabled": False})
    deleted = client.delete("/api/keepalive/rules/5")

    assert created.status_code == 200
    assert created.json()["target_phone"] == "13800138000"
    assert created.json()["interval_days"] == 3
    assert created.json()["timezone_name"] == "Asia/Shanghai"
    assert listed.status_code == 200
    assert listed.json()["items"][0]["esim_sub_id"] == "5"
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True


def test_keepalive_rule_test_endpoint(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})
    client.put(
        "/api/keepalive/rules/5",
        json={
            "display_name": "Club",
            "carrier_name": "没有服务",
            "timezone_name": "Asia/Shanghai",
            "target_phone": "13800138000",
            "interval_days": 3,
            "enabled": True,
        },
    )

    tested = client.post("/api/keepalive/rules/5/test")

    assert tested.status_code == 200
    assert tested.json()["last_status"] == "succeeded"
    assert tested.json()["last_success_at"] == "2026-05-29T01:00:00+00:00"
    assert tested.json()["next_run_at"] == "2026-05-30T01:00:00+00:00"


def test_keepalive_rule_test_endpoint_returns_409_on_conflict(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})
    client.put(
        "/api/keepalive/rules/5",
        json={
            "display_name": "Club",
            "carrier_name": "没有服务",
            "timezone_name": "Asia/Shanghai",
            "target_phone": "13800138000",
            "interval_days": 3,
            "enabled": True,
        },
    )
    client.app.state.services.keepalive_service.testing_conflict = True

    tested = client.post("/api/keepalive/rules/5/test")

    assert tested.status_code == 409


def test_keepalive_rejects_non_embedded_sub_id(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})

    response = client.put(
        "/api/keepalive/rules/999",
        json={
            "display_name": "Missing",
            "carrier_name": "Unknown",
            "timezone_name": "Asia/Shanghai",
            "target_phone": "13800138000",
            "interval_days": 3,
            "enabled": True,
        },
    )

    assert response.status_code == 404


def test_switch_request_rejects_invalid_lock_minutes(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})

    response = client.post("/api/esim/switch", json={"display_name": "Club", "lock_minutes": 15})

    assert response.status_code == 422


def test_auth_protects_business_apis(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)

    unauthorized = client.get("/api/health")
    unauthorized_remark = client.put("/api/esim/remarks/5", json={"remark": "test"})
    unauthorized_action = client.post("/api/device/monitor/control/action", json={"action": "home"})
    unauthorized_tap = client.post("/api/device/monitor/control/tap", json={"x_ratio": 0.5, "y_ratio": 0.5})
    wrong_login = client.post("/api/auth/login", json={"password": "wrong"})
    auth_status_before = client.get("/api/auth/status")
    good_login = client.post("/api/auth/login", json={"password": "secret123"})
    auth_status_after = client.get("/api/auth/status")
    authorized = client.get("/api/health")
    logout = client.post("/api/auth/logout")
    auth_status_final = client.get("/api/auth/status")
    unauthorized_again = client.get("/api/health")

    assert unauthorized.status_code == 401
    assert unauthorized_remark.status_code == 401
    assert unauthorized_action.status_code == 401
    assert unauthorized_tap.status_code == 401
    assert wrong_login.status_code == 401
    assert auth_status_before.status_code == 200
    assert auth_status_before.json()["authenticated"] is False
    assert good_login.status_code == 200
    assert auth_status_after.json()["authenticated"] is True
    assert authorized.status_code == 200
    assert logout.status_code == 200
    assert auth_status_final.json()["authenticated"] is False
    assert unauthorized_again.status_code == 401


def test_collab_websocket_requires_auth(tmp_path: Path) -> None:
    client = make_test_client_with_config(tmp_path)

    try:
        with client.websocket_connect("/api/collab/ws"):
            raise AssertionError("Expected websocket authentication to fail")
    except WebSocketDisconnect as exc:
        assert exc.code == 4401


def test_collab_join_and_cursor_broadcast(tmp_path: Path) -> None:
    client = make_test_client_with_config(tmp_path)
    login = client.post("/api/auth/login", json={"password": "secret123"})

    assert login.status_code == 200

    with client.websocket_connect("/api/collab/ws") as ws_a, client.websocket_connect("/api/collab/ws") as ws_b:
        ws_a.send_json({"type": "join", "name": "Alice"})
        snapshot_a = ws_a.receive_json()
        assert snapshot_a["type"] == "snapshot"
        assert snapshot_a["online_count"] == 1
        self_a = snapshot_a["self_id"]

        ws_b.send_json({"type": "join", "name": "Bob"})
        presence_for_a = ws_a.receive_json()
        snapshot_b = ws_b.receive_json()

        assert presence_for_a["type"] == "presence"
        assert presence_for_a["online_count"] == 2
        assert {item["name"] for item in presence_for_a["participants"]} == {"Alice", "Bob"}
        assert snapshot_b["type"] == "snapshot"
        assert snapshot_b["online_count"] == 2
        assert {item["name"] for item in snapshot_b["participants"]} == {"Alice", "Bob"}

        ws_a.send_json({"type": "cursor", "x_ratio": 1.4, "y_ratio": -2})
        cursor_for_b = ws_b.receive_json()

        assert cursor_for_b["type"] == "cursor"
        assert cursor_for_b["participant"]["id"] == self_a
        assert cursor_for_b["participant"]["cursor"]["x_ratio"] == 1.0
        assert cursor_for_b["participant"]["cursor"]["y_ratio"] == 0.0


def test_collab_service_disconnect_broadcasts_remove_and_presence(tmp_path: Path) -> None:
    config = AppConfig.from_env(tmp_path)
    service = CollabSessionService(config)

    class DummyWebSocket:
        def __init__(self) -> None:
            self.messages: list[dict] = []

        async def send_json(self, payload):  # noqa: ANN001, ANN201
            self.messages.append(payload)

    async def run_disconnect_case() -> None:
        socket_a = DummyWebSocket()
        socket_b = DummyWebSocket()
        snapshot_a = await service.join(socket_a, "Alice", "127.0.0.1")
        snapshot_b = await service.join(socket_b, "Bob", "127.0.0.2")
        await service.disconnect(snapshot_b["self_id"])

        assert socket_a.messages[0]["type"] == "presence"
        assert socket_a.messages[-2]["type"] == "remove"
        assert socket_a.messages[-2]["participant_id"] == snapshot_b["self_id"]
        assert socket_a.messages[-1]["type"] == "presence"
        assert socket_a.messages[-1]["online_count"] == 1
        assert [item["name"] for item in socket_a.messages[-1]["participants"]] == ["Alice"]
        assert snapshot_a["self_id"] != snapshot_b["self_id"]

    import asyncio

    asyncio.run(run_disconnect_case())


def test_collab_prefers_cf_connecting_ip(tmp_path: Path) -> None:
    client = make_test_client_with_config(tmp_path, trust_proxy_headers=True)
    client.post("/api/auth/login", json={"password": "secret123"})

    with client.websocket_connect(
        "/api/collab/ws",
        headers={
            "CF-Connecting-IP": "198.51.100.7",
            "Forwarded": 'for="198.51.100.8:443";proto=https',
            "X-Forwarded-For": "203.0.113.9",
        },
    ) as websocket:
        websocket.send_json({"type": "join", "name": "ProxyUser"})
        snapshot = websocket.receive_json()

    participant = snapshot["participants"][0]
    assert participant["ip"] == "198.51.100.7"


def test_collab_falls_back_to_forwarded_client_ip(tmp_path: Path) -> None:
    client = make_test_client_with_config(tmp_path, trust_proxy_headers=True)
    client.post("/api/auth/login", json={"password": "secret123"})

    with client.websocket_connect(
        "/api/collab/ws",
        headers={"Forwarded": 'for="198.51.100.8:443";proto=https', "X-Forwarded-For": "203.0.113.9"},
    ) as websocket:
        websocket.send_json({"type": "join", "name": "ProxyUser"})
        snapshot = websocket.receive_json()

    participant = snapshot["participants"][0]
    assert participant["ip"] == "198.51.100.8"


def test_collab_ignores_forward_headers_when_proxy_trust_disabled(tmp_path: Path) -> None:
    client = make_test_client_with_config(tmp_path, trust_proxy_headers=False)
    client.post("/api/auth/login", json={"password": "secret123"})

    with client.websocket_connect(
        "/api/collab/ws",
        headers={"X-Forwarded-For": "203.0.113.9"},
    ) as websocket:
        websocket.send_json({"type": "join", "name": "LocalUser"})
        snapshot = websocket.receive_json()

    participant = snapshot["participants"][0]
    assert participant["ip"] in {"testclient", "127.0.0.1"}


def test_collab_service_timeout_prunes_stale_participants(tmp_path: Path) -> None:
    config = AppConfig.from_env(tmp_path)
    config.collab_presence_timeout_seconds = 0.01
    service = CollabSessionService(config)

    class DummyWebSocket:
        async def send_json(self, payload):  # noqa: ANN001, ANN201
            return payload

    async def run_timeout_case() -> None:
        snapshot = await service.join(DummyWebSocket(), "Alice", "127.0.0.1")
        participant_id = snapshot["self_id"]
        async with service._lock:  # noqa: SLF001
            service._participants[participant_id].last_seen_monotonic -= 1  # noqa: SLF001
        removed_payloads = await service._remove_expired_participants()  # noqa: SLF001
        assert removed_payloads
        assert removed_payloads[0][1]["type"] == "remove"

    import asyncio

    asyncio.run(run_timeout_case())


def test_index_and_favicon_are_publicly_accessible(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)

    index = client.get("/")
    favicon = client.get("/assets/favicon.ico")

    assert index.status_code == 200
    assert '/assets/favicon.ico' in index.text
    assert favicon.status_code == 200
    assert favicon.headers["content-type"] in {"image/x-icon", "image/vnd.microsoft.icon"}


def test_health_degrades_when_adb_device_probe_fails(tmp_path: Path) -> None:
    config, services = make_test_services(tmp_path)
    services.adb_client = FailingDevicesAdbClient()
    services.esim_service.adb_client = services.adb_client
    services.sms_service.adb_client = services.adb_client
    services.monitor.adb_client = services.adb_client
    services.monitor.sms_service = services.sms_service
    services.switch_service = FakeSwitchService()
    services.sms_event_service = FakeSmsEventService()
    services.monitor.sms_event_service = services.sms_event_service
    services.device_monitor_service.adb_client = services.adb_client
    services.esim_service.sync()
    services.sms_service.sync_all_inbox()
    client = TestClient(create_app(config=config, services=services, auto_startup=False))

    login = client.post("/api/auth/login", json={"password": "secret123"})
    assert login.status_code == 200

    health = client.get("/api/health")
    latest_esim = client.get("/api/esim/latest")
    sms = client.get("/api/sms?page=1&page_size=10")

    assert health.status_code == 200
    assert health.json()["ok"] is False
    assert health.json()["adb_available"] is False
    assert "timed out" in health.json()["adb_error"]
    assert health.json()["monitor"]["running"] is False
    assert health.json()["device_monitor"]["available"] is False
    assert "timed out" in health.json()["device_monitor"]["reason"]
    assert latest_esim.status_code == 200
    assert latest_esim.json()["embedded_total_count"] == 2
    assert sms.status_code == 200
    assert sms.json()["total"] == 2


def test_device_monitor_reports_unavailable_when_ffmpeg_missing(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.app.state.config.ffmpeg_path = "/tmp/missing-ffmpeg"
    client.post("/api/auth/login", json={"password": "secret123"})

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["adb_available"] is True
    assert response.json()["device_monitor"]["available"] is False
    assert "FFmpeg executable not found" in response.json()["device_monitor"]["reason"]


def test_device_monitor_reports_unavailable_when_screenrecord_lacks_h264(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})
    client.app.state.services.adb_client.open_h264_screenrecord = lambda **kwargs: type(  # type: ignore[method-assign]
        "_Proc",
        (),
        {
            "stdout": __import__("io").BytesIO(b""),
            "stderr": __import__("io").BytesIO(b"unknown option --output-format"),
            "poll": staticmethod(lambda: 0),
            "terminate": staticmethod(lambda: None),
            "wait": staticmethod(lambda timeout=0: 0),
            "kill": staticmethod(lambda: None),
            "communicate": staticmethod(lambda timeout=None: (b"", b"unknown option --output-format")),
        },
    )()

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["device_monitor"]["available"] is False
    assert "unknown option --output-format" in response.json()["device_monitor"]["reason"]


def test_device_monitor_reports_unavailable_when_screenrecord_outputs_mp4(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})
    client.app.state.services.adb_client.open_h264_screenrecord = lambda **kwargs: type(  # type: ignore[method-assign]
        "_Proc",
        (),
        {
            "stdout": __import__("io").BytesIO(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"),
            "stderr": __import__("io").BytesIO(b""),
            "poll": staticmethod(lambda: 0),
            "terminate": staticmethod(lambda: None),
            "wait": staticmethod(lambda timeout=0: 0),
            "kill": staticmethod(lambda: None),
            "communicate": staticmethod(lambda timeout=None: (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2", b"")),
        },
    )()

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["device_monitor"]["available"] is False
    assert "未输出 H264 数据" in response.json()["device_monitor"]["reason"]


def test_device_monitor_probe_ignores_missing_help_text_when_h264_bytes_exist(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})
    client.app.state.services.adb_client.screenrecord_help = "Usage: screenrecord without output format"

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["device_monitor"]["available"] is True


def test_device_monitor_health_probe_caches_recent_result(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})
    calls = {"count": 0}

    original = client.app.state.services.adb_client.open_h264_screenrecord

    def counted_open(**kwargs):  # noqa: ANN003
        calls["count"] += 1
        return original(**kwargs)

    client.app.state.services.adb_client.open_h264_screenrecord = counted_open  # type: ignore[method-assign]

    first = client.get("/api/health")
    second = client.get("/api/health")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls["count"] == 1


def test_device_monitor_startup_reports_bad_mp4_header_when_exec_out_fails(tmp_path: Path) -> None:
    from app.device_monitor import DeviceMonitorService, DeviceMonitorUnavailableError

    class FakeWebSocket:  # noqa: D401
        pass

    config = AppConfig.from_env(tmp_path)
    config.ffmpeg_path = "/tmp/fake-ffmpeg"
    service = DeviceMonitorService(config, FakeAdbClient())
    service._resolve_ffmpeg_error = lambda: None  # type: ignore[method-assign]
    service._spawn_ffmpeg = lambda process: (_ for _ in ()).throw(DeviceMonitorUnavailableError("bad header"))  # type: ignore[method-assign]

    try:
        service._spawn_session(FakeWebSocket())  # type: ignore[arg-type]
    except DeviceMonitorUnavailableError as exc:
        assert "bad header" in str(exc)
    else:
        raise AssertionError("Expected bad MP4 header to bubble up as unavailable")


def test_device_monitor_error_details_prefer_ffmpeg_stderr_over_fallback(tmp_path: Path) -> None:
    from app.device_monitor import ActiveMonitorSession, DeviceMonitorService

    class DummyWebSocket:
        async def send_json(self, payload):  # noqa: ANN001, ANN201
            return payload

    class DummyProcess:
        def __init__(self, *, returncode: int = 1) -> None:
            import io

            self.stdin = io.BytesIO(b"")
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")
            self._returncode = returncode

        def poll(self) -> int | None:
            return self._returncode

    async def run_case() -> None:
        config = AppConfig.from_env(tmp_path)
        service = DeviceMonitorService(config, FakeAdbClient())
        session = ActiveMonitorSession(
            adb_process=DummyProcess(),
            ffmpeg_process=DummyProcess(),
            websocket=DummyWebSocket(),
            loop=asyncio.get_running_loop(),
            mime_codec='video/mp4; codecs="avc1.42E028"',
        )
        session.ffmpeg_stderr_tail.append("ffmpeg stderr detail")
        session.adb_stderr_tail.append("adb stderr detail")

        reason, diagnostic = service._build_error_details(session, fallback_reason="fallback")  # noqa: SLF001

        assert reason == "ffmpeg stderr detail"
        assert diagnostic is not None
        assert "ffmpeg stderr detail" in diagnostic
        assert "adb stderr detail" in diagnostic

    asyncio.run(run_case())




def test_device_monitor_default_bitrate_matches_manual_success_path(tmp_path: Path) -> None:
    from app.device_monitor import DeviceMonitorService

    config = AppConfig.from_env(tmp_path)
    service = DeviceMonitorService(config, FakeAdbClient())

    assert service._screenrecord_bitrate_arg() is None  # noqa: SLF001


def test_device_monitor_allows_explicit_integer_bitrate(tmp_path: Path) -> None:
    from app.device_monitor import DeviceMonitorService

    config = AppConfig.from_env(tmp_path)
    config.device_monitor_bitrate = "2500000"
    service = DeviceMonitorService(config, FakeAdbClient())

    assert service._screenrecord_bitrate_arg() == "2500000"  # noqa: SLF001


def test_device_monitor_ffmpeg_command_matches_manual_success_path(tmp_path: Path) -> None:
    from app.device_monitor import DeviceMonitorService

    config = AppConfig.from_env(tmp_path)
    config.ffmpeg_path = "/tmp/fake-ffmpeg"
    service = DeviceMonitorService(config, FakeAdbClient())

    class DummyProcess:
        def __init__(self) -> None:
            import io

            self.stdout = io.BufferedReader(io.BytesIO(b"\x00\x00\x00\x01\x67\x42\x80\x0a"))
            self.stderr = io.BytesIO(b"")

        @staticmethod
        def poll() -> int | None:
            return None

        @staticmethod
        def terminate() -> None:
            return None

        @staticmethod
        def wait(timeout: float = 0) -> int:  # noqa: ARG004
            return 0

        @staticmethod
        def kill() -> None:
            return None

    adb_process = DummyProcess()
    captured: list[list[str]] = []

    def fake_popen(command, stdin=None, stdout=None, stderr=None, bufsize=None):  # noqa: ANN001
        captured.append(command)

        class FakeFfmpeg:
            def __init__(self) -> None:
                import io

                self.stdin = io.BytesIO(b"")
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")

            @staticmethod
            def poll() -> int | None:
                return None

            @staticmethod
            def terminate() -> None:
                return None

            @staticmethod
            def wait(timeout: float = 0) -> int:  # noqa: ARG004
                return 0

            @staticmethod
            def kill() -> None:
                return None

        return FakeFfmpeg()

    with patch("app.device_monitor.subprocess.Popen", side_effect=fake_popen):
        service._spawn_ffmpeg(adb_process)  # noqa: SLF001

    assert captured == [[
        "/tmp/fake-ffmpeg",
        "-loglevel",
        "error",
        "-f",
        "h264",
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-vf",
        "scale=720:-2",
        "-b:v",
        "2M",
        "-maxrate",
        "2M",
        "-bufsize",
        "4M",
        "-profile:v",
        "baseline",
        "-level",
        "4.0",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "empty_moov+omit_tfhd_offset+default_base_moof+frag_every_frame",
        "-f",
        "mp4",
        "pipe:1",
    ]]


def test_device_monitor_control_action_maps_keyevents(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})

    response = client.post("/api/device/monitor/control/action", json={"action": "home"})

    assert response.status_code == 200
    assert client.app.state.services.adb_client.keyevents[-1] == 3


def test_device_monitor_health_probe_wakes_screen_before_capability_check(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})

    response = client.get("/api/health")

    assert response.status_code == 200
    assert client.app.state.services.adb_client.wake_calls >= 1
    assert 224 in client.app.state.services.adb_client.keyevents


def test_device_monitor_tap_maps_ratios_to_device_pixels(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})

    response = client.post("/api/device/monitor/control/tap", json={"x_ratio": 0.5, "y_ratio": 0.25})

    assert response.status_code == 200
    assert client.app.state.services.adb_client.taps == [(540, 600)]


def test_device_monitor_control_returns_423_during_switch(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})
    client.app.state.services.switch_service.task["status"] = "running"

    action = client.post("/api/device/monitor/control/action", json={"action": "back"})
    tap = client.post("/api/device/monitor/control/tap", json={"x_ratio": 0.5, "y_ratio": 0.5})

    assert action.status_code == 423
    assert tap.status_code == 423


def test_switch_start_forces_device_monitor_stop(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})
    stop_calls: list[str] = []
    client.app.state.services.device_monitor_service.force_stop = lambda reason: stop_calls.append(reason)  # type: ignore[method-assign]

    response = client.post("/api/esim/switch", json={"display_name": "Club", "lock_minutes": 10})

    assert response.status_code == 200
    assert stop_calls == ["eSIM 切换已开始，手机监控已自动关闭"]


def test_device_monitor_websocket_requires_auth(tmp_path: Path) -> None:
    client = make_test_client_with_config(tmp_path)

    try:
        with client.websocket_connect("/api/device/monitor/ws"):
            raise AssertionError("Expected websocket authentication to fail")
    except WebSocketDisconnect as exc:
        assert exc.code == 4401


def test_device_monitor_websocket_rejects_when_switch_running(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})
    client.app.state.services.switch_service.task["status"] = "running"

    with client.websocket_connect("/api/device/monitor/ws") as websocket:
        message = websocket.receive_json()
        assert message["type"] == "error"
        try:
            websocket.receive_json()
            raise AssertionError("Expected websocket to close after locked error")
        except WebSocketDisconnect as exc:
            assert exc.code == 4423


def test_device_monitor_websocket_surfaces_startup_diagnostic(tmp_path: Path) -> None:
    from app.device_monitor import DeviceMonitorUnavailableError

    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})
    client.app.state.services.device_monitor_service.start_session = lambda websocket, switch_running: (_ for _ in ()).throw(  # type: ignore[method-assign]
        DeviceMonitorUnavailableError("bad header", diagnostic="ffmpeg: missing SPS/PPS")
    )

    with client.websocket_connect("/api/device/monitor/ws") as websocket:
        message = websocket.receive_json()
        assert message["type"] == "error"
        assert message["reason"] == "bad header"
        assert message["diagnostic"] == "ffmpeg: missing SPS/PPS"
        try:
            websocket.receive_json()
            raise AssertionError("Expected websocket to close after unavailable error")
        except WebSocketDisconnect as exc:
            assert exc.code == 4410


def test_device_monitor_websocket_rejects_second_viewer(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})
    client.app.state.services.device_monitor_service._session = object()  # type: ignore[attr-defined]
    client.app.state.services.device_monitor_service.get_status = lambda **kwargs: {  # type: ignore[method-assign]
        "available": True,
        "running": True,
        "reason": None,
        "browser_supported_hint": BROWSER_SUPPORTED_HINT,
    }

    with client.websocket_connect("/api/device/monitor/ws") as websocket:
        message = websocket.receive_json()
        assert message["type"] == "error"
        try:
            websocket.receive_json()
            raise AssertionError("Expected websocket to close after conflict error")
        except WebSocketDisconnect as exc:
            assert exc.code == 4409


def test_device_monitor_stream_to_websocket_reports_runtime_diagnostic(tmp_path: Path) -> None:
    from app.device_monitor import ActiveMonitorSession, DeviceMonitorService

    class DummyWebSocket:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []
            self.binary_messages: list[bytes] = []

        async def send_json(self, payload):  # noqa: ANN001, ANN201
            self.messages.append(payload)

        async def send_bytes(self, payload: bytes) -> None:
            self.binary_messages.append(payload)

    class DummyProcess:
        def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 1) -> None:
            import io

            self.stdin = io.BytesIO(b"")
            self.stdout = io.BytesIO(stdout)
            self.stderr = io.BytesIO(stderr)
            self._returncode = returncode

        def poll(self) -> int | None:
            return self._returncode

    async def run_case() -> None:
        config = AppConfig.from_env(tmp_path)
        service = DeviceMonitorService(config, FakeAdbClient())
        socket = DummyWebSocket()
        session = ActiveMonitorSession(
            adb_process=DummyProcess(returncode=0),
            ffmpeg_process=DummyProcess(returncode=1),
            websocket=socket,
            loop=asyncio.get_running_loop(),
            mime_codec='video/mp4; codecs="avc1.42E028"',
        )
        session.ffmpeg_stderr_tail.append("ffmpeg: invalid data found when processing input")

        await service.stream_to_websocket(session)

        assert socket.messages[0] == {"type": "status", "status": "starting"}
        assert socket.messages[1] == {"type": "media", "mime_codec": 'video/mp4; codecs="avc1.42E028"'}
        assert socket.messages[2] == {"type": "status", "status": "streaming"}
        assert socket.binary_messages == []
        error = socket.messages[-1]
        assert error["type"] == "error"
        assert "ffmpeg: invalid data found" in str(error["reason"])
        assert "ffmpeg: invalid data found" in str(error["diagnostic"])

    asyncio.run(run_case())


def test_device_monitor_stream_to_websocket_ignores_clean_eof(tmp_path: Path) -> None:
    from app.device_monitor import ActiveMonitorSession, DeviceMonitorService

    class DummyWebSocket:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []

        async def send_json(self, payload):  # noqa: ANN001, ANN201
            self.messages.append(payload)

        async def send_bytes(self, payload: bytes) -> None:  # noqa: ARG002
            raise AssertionError("Did not expect binary payloads for clean EOF")

    class DummyProcess:
        def __init__(self) -> None:
            import io

            self.stdin = io.BytesIO(b"")
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")

        @staticmethod
        def poll() -> int | None:
            return 0

    async def run_case() -> None:
        config = AppConfig.from_env(tmp_path)
        service = DeviceMonitorService(config, FakeAdbClient())
        socket = DummyWebSocket()
        session = ActiveMonitorSession(
            adb_process=DummyProcess(),
            ffmpeg_process=DummyProcess(),
            websocket=socket,
            loop=asyncio.get_running_loop(),
            mime_codec='video/mp4; codecs="avc1.42E028"',
        )

        await service.stream_to_websocket(session)

        assert socket.messages == [
            {"type": "status", "status": "starting"},
            {"type": "media", "mime_codec": 'video/mp4; codecs="avc1.42E028"'},
            {"type": "status", "status": "streaming"},
        ]

    asyncio.run(run_case())


def test_sms_stream_requires_auth_and_allows_authenticated_stream(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)

    unauthorized = client.get("/api/sms/stream")
    assert unauthorized.status_code == 401

    login = client.post("/api/auth/login", json={"password": "secret123"})
    assert login.status_code == 200

    response = client.build_request("GET", "/api/sms/stream")
    cookies = client.cookies
    assert cookies.get("esim_switch_auth") == "signed-secret-cookie"


def test_sms_stream_snapshot_contains_latest_event_fields(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})

    services = client.app.state.services
    services.sms_event_service.broadcast_new_sms(
        {
            "occurred_at": "2026-05-26T10:45:27.958875+00:00",
            "inserted_count": 1,
            "fetched_count": 5,
            "latest_sms_id": "588",
            "latest_address": "ALIPAY",
            "latest_display_name": "gg+4407719752845",
            "event_source": "logcat",
            "trigger_buffer": "radio",
            "trigger_log_line": "RILJ    : [UNSL]< UNSOL_RESPONSE_NEW_SMS",
        }
    )

    snapshot = services.sms_event_service.get_status()
    assert snapshot["event_type"] == "new_sms"
    assert snapshot["event_source"] == "logcat"
    assert snapshot["trigger_buffer"] == "radio"


def test_sms_monitor_broadcasts_only_when_new_messages_inserted(tmp_path: Path) -> None:
    from app.monitor import SmsMonitor

    class FakeSmsService:
        def __init__(self, inserted_count: int) -> None:
            self.inserted_count = inserted_count

        def sync_latest(self, limit: int = 5) -> dict:  # noqa: ARG002
            return {
                "inserted_count": self.inserted_count,
                "fetched_count": 2,
                "latest_message": {
                    "sms_id": "123",
                    "address": "10010",
                    "display_name": "Club",
                },
            }

    class RecordingSmsEventService:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        def broadcast_new_sms(self, payload: dict) -> None:
            self.payloads.append(payload)

    config = AppConfig.from_env(tmp_path)
    event_service = RecordingSmsEventService()
    monitor = SmsMonitor(config, Database(tmp_path / "db.sqlite"), FakeSmsService(inserted_count=1), object(), sms_event_service=event_service)
    result = monitor.sms_service.sync_latest(limit=5)
    if result.get("inserted_count", 0) > 0 and monitor.sms_event_service:
        latest_message = result.get("latest_message") or {}
        monitor.sms_event_service.broadcast_new_sms(
            {
                "occurred_at": "2026-05-26T00:00:00+00:00",
                "inserted_count": result.get("inserted_count", 0),
                "fetched_count": result.get("fetched_count", 0),
                "latest_sms_id": latest_message.get("sms_id"),
                "latest_address": latest_message.get("address"),
                "latest_display_name": latest_message.get("display_name"),
            }
        )
    assert len(event_service.payloads) == 1
    assert event_service.payloads[0]["latest_sms_id"] == "123"

    empty_event_service = RecordingSmsEventService()
    monitor_empty = SmsMonitor(config, Database(tmp_path / "db2.sqlite"), FakeSmsService(inserted_count=0), object(), sms_event_service=empty_event_service)
    monitor_empty._update_status(
        last_sms_log_hit_at="2026-05-26T00:10:00+00:00",
        last_sms_log_hit_line="RILJ    : [UNSL]< UNSOL_RESPONSE_NEW_SMS",
        last_sms_log_hit_buffer="radio",
    )
    monitor_empty._sync_and_broadcast(
        source="logcat",
        trigger_buffer="radio",
        trigger_log_line="RILJ    : [UNSL]< UNSOL_RESPONSE_NEW_SMS",
    )
    assert empty_event_service.payloads == []
    assert monitor_empty.get_status()["last_sms_log_hit_buffer"] == "radio"
    assert monitor_empty.get_status()["last_sms_log_hit_line"] == "RILJ    : [UNSL]< UNSOL_RESPONSE_NEW_SMS"


def test_sms_monitor_poll_fallback_broadcasts_new_messages(tmp_path: Path) -> None:
    from app.monitor import SmsMonitor

    class FakeSmsService:
        def __init__(self) -> None:
            self.calls = 0

        def sync_latest(self, limit: int = 5) -> dict:  # noqa: ARG002
            self.calls += 1
            return {
                "inserted_count": 1,
                "fetched_count": 1,
                "latest_message": {
                    "sms_id": "999",
                    "address": "Bank",
                    "display_name": "Club",
                },
            }

    class RecordingSmsEventService:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        def broadcast_new_sms(self, payload: dict) -> None:
            self.payloads.append(payload)

    config = AppConfig.from_env(tmp_path)
    event_service = RecordingSmsEventService()
    sms_service = FakeSmsService()
    monitor = SmsMonitor(config, Database(tmp_path / "db3.sqlite"), sms_service, object(), sms_event_service=event_service)

    monitor._sync_and_broadcast(source="poll")

    assert sms_service.calls == 1
    assert len(event_service.payloads) == 1
    assert event_service.payloads[0]["event_source"] == "poll"
    assert monitor.get_status()["last_broadcast_source"] == "poll"


def test_sms_monitor_detects_radio_keywords_case_insensitively(tmp_path: Path) -> None:
    from app.monitor import SmsMonitor

    config = AppConfig.from_env(tmp_path)
    monitor = SmsMonitor(config, Database(tmp_path / "db4.sqlite"), object(), object())

    assert monitor._is_sms_signal("RILJ    : [UNSL]< UNSOL_RESPONSE_NEW_SMS")
    assert monitor._is_sms_signal("GsmInboundSmsHandler: Ordered Broadcast Completed for android.provider.telephony.SMS_RECEIVED")
    assert monitor._is_sms_signal("GsmInboundSmsHandler: Successful broadcast, deleting from raw table.")


def test_default_sms_log_config_includes_radio_and_real_device_keywords(tmp_path: Path) -> None:
    config = AppConfig.from_env(tmp_path)

    assert config.sms_log_buffers == ("radio",)
    assert "RILJ" in config.sms_log_tags
    assert "GsmInboundSmsHandler" in config.sms_log_tags
    assert "unsol_response_new_sms" in config.sms_log_trigger_keywords
    assert "android.provider.telephony.sms_received" in config.sms_log_trigger_keywords


def test_adb_stream_logcat_uses_radio_buffer_and_sms_tags(tmp_path: Path) -> None:
    config = AppConfig.from_env(tmp_path)
    config.adb_path = "/tmp/fake-adb"
    adb_client = AdbClient(config)

    with patch("app.adb.subprocess.Popen") as popen:
        adb_client.stream_logcat()

    command = popen.call_args.args[0]
    assert command[:3] == ["/tmp/fake-adb", "logcat", "-b"]
    assert "radio" in command
    assert "-s" in command
    assert "RILJ" in command
    assert "GsmInboundSmsHandler" in command


def test_switch_service_unlocks_before_opening_settings(tmp_path: Path) -> None:
    from app.services import EsimSyncService
    from app.switch_service import EsimSwitchService

    class FakeDevice:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def screen_on(self) -> None:
            self.calls.append(("screen_on",))

        def unlock(self) -> None:
            self.calls.append(("unlock",))

        def swipe_ext(self, direction: str, scale: float = 0.9) -> None:
            self.calls.append(("swipe_ext", direction, scale))

        def shell(self, command: str) -> None:
            self.calls.append(("shell", command))

        def app_stop(self, package_name: str) -> None:
            self.calls.append(("app_stop", package_name))

        def screenshot(self, format: str = "pillow"):  # noqa: A002
            from PIL import Image

            self.calls.append(("screenshot", format))
            return Image.new("RGB", (10, 10), color="white")

        def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("UI element interaction should not be reached in this test")

    class FakeEsimSyncService:
        def sync(self) -> dict:
            return {"ok": True}

    config = AppConfig.from_env(tmp_path)
    config.switch_step_delay_seconds = 0
    service = EsimSwitchService(config, FakeEsimSyncService())  # type: ignore[arg-type]
    fake_device = FakeDevice()

    service._connect_device = lambda: fake_device  # type: ignore[method-assign]
    service._click_first_match = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop after reset"))  # type: ignore[method-assign]
    service._task.task_id = "task-seq"
    service._task.status = "running"
    service._task.target_display_name = "Club"
    service._task.started_at = "2026-05-26T00:00:00+00:00"

    service._run_switch_flow("task-seq", "Club")

    screen_on_index = fake_device.calls.index(("screen_on",))
    unlock_index = fake_device.calls.index(("unlock",))
    swipe_index = next(i for i, item in enumerate(fake_device.calls) if item[0] == "swipe_ext")
    reset_index = fake_device.calls.index(("app_stop", "com.android.settings"))
    shell_index = fake_device.calls.index(("shell", "am start -a android.settings.WIRELESS_SETTINGS"))

    assert screen_on_index < unlock_index < swipe_index < reset_index < shell_index


def test_switch_service_prefers_dynamic_confirm_button() -> None:
    from app.switch_service import EsimSwitchService

    class FakeSelector:
        def __init__(self, exists: bool, recorder: list[tuple], selector_kind: str, selector_value: str) -> None:
            self.exists = exists
            self.recorder = recorder
            self.selector_kind = selector_kind
            self.selector_value = selector_value

        def click(self) -> None:
            self.recorder.append(("click", self.selector_kind, self.selector_value))

    class FakeDevice:
        def __init__(self) -> None:
            self.recorder: list[tuple] = []

        def __call__(self, **kwargs):  # noqa: ANN003
            if "text" in kwargs:
                value = kwargs["text"]
                return FakeSelector(value == "切换到Club+85264220597", self.recorder, "text", value)
            if "textContains" in kwargs:
                value = kwargs["textContains"]
                return FakeSelector(value == "Club+85264220597", self.recorder, "textContains", value)
            return FakeSelector(False, self.recorder, "unknown", "unknown")

    config = AppConfig.from_env(Path.cwd())
    service = EsimSwitchService(config, object())  # type: ignore[arg-type]
    device = FakeDevice()

    service._confirm_switch_dialog(device, "Club+85264220597")

    assert ("click", "text", "切换到Club+85264220597") in device.recorder


def test_switch_service_waits_then_verifies_switch(tmp_path: Path) -> None:
    from app.switch_service import EsimSwitchService

    class FakeDevice:
        def screenshot(self, format: str = "pillow"):  # noqa: A002
            from PIL import Image

            return Image.new("RGB", (10, 10), color="white")

    class FakeEsimSyncService:
        def latest(self) -> dict:
            return {
                "subscriptions": [
                    {"display_name": "Club+85264220597", "is_active": True},
                    {"display_name": "giffgaff sws", "is_active": False},
                ]
            }

        def sync(self) -> dict:
            return {"ok": True}

        @property
        def adb_client(self):  # noqa: ANN201
            class _FakeAdb:
                @staticmethod
                def read_isub() -> str:
                    return """
                    ActiveSubInfoList:
                     {id=5 simSlotIndex=1 displayName=Club+85264220597 carrierName=CHN-UNICOM isEmbedded=true}
                    ++++++++++++++++++++++++++++++++
                    AllSubInfoList:
                     {id=5 simSlotIndex=1 displayName=Club+85264220597 carrierName=CHN-UNICOM isEmbedded=true}
                     {id=7 simSlotIndex=-1 displayName=giffgaff sws carrierName=giffgaff isEmbedded=true}
                    ++++++++++++++++++++++++++++++++
                    """

            return _FakeAdb()

    config = AppConfig.from_env(tmp_path)
    config.switch_confirm_wait_seconds = 0
    service = EsimSwitchService(config, FakeEsimSyncService())  # type: ignore[arg-type]
    detail = service._wait_and_verify_switch(FakeDevice(), "task-1", tmp_path, "Club+85264220597")

    assert "dumpsys isub 确认成功" in detail
    assert "Club+85264220597" in detail


def test_switch_service_saves_optimized_jpeg_screenshots(tmp_path: Path) -> None:
    from app.switch_service import EsimSwitchService

    class FakeImage:
        def __init__(self) -> None:
            self.thumbnail_calls: list[tuple[int, int]] = []
            self.convert_calls: list[str] = []
            self.save_calls: list[tuple] = []
            self.mode = "RGBA"

        def thumbnail(self, size: tuple[int, int]) -> None:
            self.thumbnail_calls.append(size)

        def convert(self, mode: str):  # noqa: ANN001
            self.convert_calls.append(mode)
            self.mode = mode
            return self

        def save(self, path, **kwargs):  # noqa: ANN001
            self.save_calls.append((str(path), kwargs))

    class FakeEsimSyncService:
        def sync(self) -> dict:
            return {"ok": True}

    config = AppConfig.from_env(tmp_path)
    service = EsimSwitchService(config, FakeEsimSyncService())  # type: ignore[arg-type]
    image = FakeImage()

    service._save_optimized_screenshot(image, tmp_path / "shot.png")

    assert image.thumbnail_calls == [(720, 1280)]
    assert image.convert_calls == []
    assert image.save_calls
    assert image.save_calls[0][0].endswith("shot.png")
    assert image.save_calls[0][1]["format"] == "PNG"
    assert image.save_calls[0][1]["compress_level"] == 9


def test_switch_service_short_circuits_when_target_already_active(tmp_path: Path) -> None:
    from app.switch_service import EsimSwitchService

    class FakeEsimSyncService:
        def sync(self) -> dict:
            return {"ok": True}

        def latest(self) -> dict:
            return {
                "subscriptions": [
                    {"display_name": "gg+4407719752845", "is_active": True},
                    {"display_name": "Club", "is_active": False},
                ]
            }

        @property
        def adb_client(self):  # noqa: ANN201
            class _FakeAdb:
                @staticmethod
                def read_isub() -> str:
                    return """
                    ActiveSubInfoList:
                     {id=7 simSlotIndex=1 displayName=gg+4407719752845 carrierName=giffgaff isEmbedded=true}
                    ++++++++++++++++++++++++++++++++
                    AllSubInfoList:
                     {id=5 simSlotIndex=-1 displayName=Club carrierName=没有服务 isEmbedded=true}
                     {id=7 simSlotIndex=1 displayName=gg+4407719752845 carrierName=giffgaff isEmbedded=true}
                    ++++++++++++++++++++++++++++++++
                    """

            return _FakeAdb()

    config = AppConfig.from_env(tmp_path)
    service = EsimSwitchService(config, FakeEsimSyncService())  # type: ignore[arg-type]
    service._task.task_id = "task-short"
    service._task.status = "running"
    service._task.target_display_name = "gg+4407719752845"
    service._task.requested_lock_minutes = 10
    service._task.started_at = "2026-05-29T00:00:00+00:00"

    service._connect_device = lambda: (_ for _ in ()).throw(AssertionError("should not connect device"))  # type: ignore[method-assign]

    service._run_switch_flow("task-short", "gg+4407719752845")

    status = service.get_status()
    assert status["status"] == "succeeded"
    assert status["steps"][0]["step_key"] == "check_active_esim"
    assert "已经是当前激活卡" in status["steps"][0]["detail"]


def test_keepalive_switch_short_circuits_when_target_already_active(tmp_path: Path) -> None:
    from app.switch_service import EsimSwitchService

    class FakeDevice:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def screen_on(self) -> None:
            self.calls.append(("screen_on",))

        def unlock(self) -> None:
            self.calls.append(("unlock",))

        def swipe_ext(self, direction: str, scale: float = 0.9) -> None:
            self.calls.append(("swipe_ext", direction, scale))

        def screenshot(self, format: str = "pillow"):  # noqa: A002
            from PIL import Image

            self.calls.append(("screenshot", format))
            return Image.new("RGB", (10, 10), color="white")

    class FakeEsimSyncService:
        def sync(self) -> dict:
            return {"ok": True}

        def latest(self) -> dict:
            return {
                "subscriptions": [
                    {"display_name": "gg+4407719752845", "is_active": True},
                ]
            }

        @property
        def adb_client(self):  # noqa: ANN201
            class _FakeAdb:
                @staticmethod
                def read_isub() -> str:
                    return """
                    ActiveSubInfoList:
                     {id=7 simSlotIndex=1 displayName=gg+4407719752845 carrierName=giffgaff isEmbedded=true}
                    ++++++++++++++++++++++++++++++++
                    AllSubInfoList:
                     {id=7 simSlotIndex=1 displayName=gg+4407719752845 carrierName=giffgaff isEmbedded=true}
                    ++++++++++++++++++++++++++++++++
                    """

            return _FakeAdb()

    config = AppConfig.from_env(tmp_path)
    service = EsimSwitchService(config, FakeEsimSyncService())  # type: ignore[arg-type]
    config.switch_step_delay_seconds = 0
    service._task.task_id = "task-keepalive-short"
    service._task.status = "running"
    service._task.target_display_name = "gg+4407719752845"
    service._task.started_at = "2026-05-29T00:00:00+00:00"
    fake_device = FakeDevice()
    service._connect_device = lambda: fake_device  # type: ignore[method-assign]

    service._run_keepalive_flow("task-keepalive-short", "gg+4407719752845")

    status = service.get_status()
    assert status["status"] == "succeeded"
    assert status["steps"][0]["step_key"] == "check_active_esim"
    assert status["steps"][1]["step_key"] == "screen_on"
    assert status["steps"][2]["step_key"] == "unlock_screen"
    assert "已经是当前激活卡" in status["steps"][0]["detail"]
    assert ("screen_on",) in fake_device.calls
    assert ("unlock",) in fake_device.calls


def test_switch_service_persists_lock_and_logs_after_success(tmp_path: Path) -> None:
    from app.switch_service import EsimSwitchService

    class FakeEsimSyncService:
        def sync(self) -> dict:
            return {"ok": True}

    config = AppConfig.from_env(tmp_path)
    service = EsimSwitchService(config, FakeEsimSyncService())  # type: ignore[arg-type]

    def fake_run_switch_flow(task_id: str, display_name: str) -> None:
        with service._lock:
            service._task.steps = []
            service._task.current_step = "切换生效确认完成"
        service._finish_task("succeeded", None)

    class ImmediateThread:
        def __init__(self, target, args, **kwargs):  # noqa: ANN001, ANN003
            self.target = target
            self.args = args

        def start(self) -> None:
            self.target(*self.args)

    service._run_switch_flow = fake_run_switch_flow  # type: ignore[method-assign]

    with patch("app.switch_service.threading.Thread", ImmediateThread):
        status = service.start_switch("Club", 20)

    assert status["status"] == "succeeded"
    assert status["lock_active"] is True
    assert status["lock_minutes"] == 20
    assert status["logs"][0]["event_type"] == "succeeded"
    assert status["logs"][1]["event_type"] == "attempt_started"

    restored = EsimSwitchService(config, FakeEsimSyncService())  # type: ignore[arg-type]
    restored.restore_state()
    restored_status = restored.get_status()

    assert restored_status["lock_active"] is True
    assert restored_status["lock_minutes"] == 20
    assert restored_status["logs"][0]["event_type"] == "succeeded"


def test_switch_service_blocks_when_lock_is_active(tmp_path: Path) -> None:
    from app.switch_service import EsimSwitchLockedError, EsimSwitchService

    class FakeEsimSyncService:
        def sync(self) -> dict:
            return {"ok": True}

    config = AppConfig.from_env(tmp_path)
    service = EsimSwitchService(config, FakeEsimSyncService())  # type: ignore[arg-type]

    future_lock_until = "2099-05-26T00:10:00+00:00"
    service.db.set_app_state(
        "esim_switch_lock",
        {
            "lock_until": future_lock_until,
            "lock_minutes": 10,
            "last_target_display_name": "Club",
            "locked_at": "2099-05-26T00:00:00+00:00",
        },
        "2099-05-26T00:00:00+00:00",
    )
    service.restore_state()

    try:
        service.start_switch("giffgaff sws", 10)
    except EsimSwitchLockedError as exc:
        assert "切换锁定期" in str(exc)
    else:
        raise AssertionError("Expected EsimSwitchLockedError")

    status = service.get_status()
    assert status["lock_active"] is True
    assert status["logs"][0]["event_type"] == "blocked_locked"


def test_switch_service_failed_switch_does_not_create_lock(tmp_path: Path) -> None:
    from app.switch_service import EsimSwitchService

    class FakeEsimSyncService:
        def sync(self) -> dict:
            return {"ok": True}

    config = AppConfig.from_env(tmp_path)
    service = EsimSwitchService(config, FakeEsimSyncService())  # type: ignore[arg-type]

    def fake_run_switch_flow(task_id: str, display_name: str) -> None:  # noqa: ARG001
        service._finish_task("failed", "mock failure")

    class ImmediateThread:
        def __init__(self, target, args, **kwargs):  # noqa: ANN001, ANN003
            self.target = target
            self.args = args

        def start(self) -> None:
            self.target(*self.args)

    service._run_switch_flow = fake_run_switch_flow  # type: ignore[method-assign]

    with patch("app.switch_service.threading.Thread", ImmediateThread):
        status = service.start_switch("Club", 30)

    assert status["status"] == "failed"
    assert status["lock_active"] is False
    assert status["logs"][0]["event_type"] == "failed"


def test_adb_query_sms_inbox_uses_remote_shell_quoting(tmp_path: Path) -> None:
    config = AppConfig.from_env(tmp_path)
    config.adb_path = "/tmp/fake-adb"
    client = RecordingAdbClient(config)
    client._client.run = client.run  # type: ignore[method-assign]

    output = client.query_sms_inbox(limit=1)

    assert "Row: 0" in output
    assert client.commands
    assert client.commands[0][:3] == ["shell", "sh", "-c"]
    assert "--sort 'date DESC'" in client.commands[0][3]


def test_adb_query_sms_inbox_reports_last_attempt_output(tmp_path: Path) -> None:
    from app.adb import AdbClient, AdbCommandError

    config = AppConfig.from_env(tmp_path)
    config.adb_path = "/tmp/fake-adb"
    client = AdbClient(config)

    def fake_run(args: list[str], timeout: float = 30) -> str:  # noqa: ARG001
        return "usage: content query --uri <URI>\n[ERROR] Unsupported argument: DESC"

    client.run = fake_run  # type: ignore[method-assign]

    try:
        client.query_sms_inbox(limit=None)
    except AdbCommandError as exc:
        assert "Last output:" in str(exc)
        assert "Unsupported argument: DESC" in str(exc)
    else:
        raise AssertionError("Expected AdbCommandError to be raised")
