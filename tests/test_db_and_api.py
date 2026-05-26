from pathlib import Path

from fastapi.testclient import TestClient

from app.config import AppConfig
from app.db import Database
from app.main import build_services, create_app
from app.models import SmsMessageRecord
from app.switch_service import EsimSwitchConflictError


class FakeAdbClient:
    def __init__(self) -> None:
        self.isub_output = """
        ActiveSubInfoList:
         {id=7 simSlotIndex=1 displayName=giffgaff sws carrierName=giffgaff isEmbedded=true}
        ++++++++++++++++++++++++++++++++
        AllSubInfoList:
         {id=5 simSlotIndex=-1 displayName=Club carrierName=没有服务 isEmbedded=true}
         {id=7 simSlotIndex=1 displayName=giffgaff sws carrierName=giffgaff isEmbedded=true}
        ++++++++++++++++++++++++++++++++
        """
        self.sms_output = """Row: 0 address=10010, body=hello, sub_id=7, _id=1, date=1716680000000
Row: 1 address=10086, body=world, sub_id=5, _id=2, date=1716670000000
"""

    def get_devices(self) -> list[str]:
        return ["device-001"]

    def read_isub(self) -> str:
        return self.isub_output

    def query_sms_inbox(self, limit=None) -> str:  # noqa: ANN001
        return self.sms_output

    def stream_logcat(self):  # noqa: ANN201
        class _DummyProcess:
            stdout = None

            @staticmethod
            def poll() -> int:
                return 0

        return _DummyProcess()


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
            "steps": [],
        }

    def get_status(self) -> dict:
        return self.task

    def start_switch(self, display_name: str) -> dict:
        if self.task["status"] == "running":
            raise EsimSwitchConflictError("An eSIM switch task is already running")
        self.task = {
            "task_id": "task-001",
            "status": "running",
            "target_display_name": display_name,
            "started_at": "2026-05-26T00:00:00+00:00",
            "finished_at": None,
            "current_step": "打开网络设置",
            "latest_screenshot_url": "/switch-screenshots/task-001/01_open_settings.png",
            "error": None,
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


def test_api_endpoints_return_dashboard_data(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    login = client.post("/api/auth/login", json={"password": "secret123"})

    assert login.status_code == 200
    health = client.get("/api/health")
    latest_esim = client.get("/api/esim/latest")
    sms = client.get("/api/sms?page=1&page_size=10")
    sms_filtered = client.get("/api/sms?page=1&page_size=10&display_name=Club")
    monitor = client.get("/api/monitor/status")
    sync_all = client.post("/api/sms/sync-all")
    switch_status = client.get("/api/esim/switch/status")
    switch_start = client.post("/api/esim/switch", json={"display_name": "Club"})

    assert health.status_code == 200
    assert health.json()["adb_available"] is True
    assert latest_esim.status_code == 200
    assert latest_esim.json()["embedded_total_count"] == 2
    assert sms.status_code == 200
    assert sms.json()["total"] == 2
    assert sms.json()["items"][0]["display_name"] == "giffgaff sws"
    assert sms.json()["items"][1]["display_name"] == "Club"
    assert sms_filtered.status_code == 200
    assert sms_filtered.json()["total"] == 1
    assert sms_filtered.json()["items"][0]["display_name"] == "Club"
    assert monitor.status_code == 200
    assert sync_all.status_code == 200
    assert sync_all.json()["detail"] == "full inbox sync"
    assert switch_status.status_code == 200
    assert switch_status.json()["status"] == "idle"
    assert switch_start.status_code == 200
    assert switch_start.json()["status"] == "running"
    assert switch_start.json()["target_display_name"] == "Club"


def test_switch_conflict_returns_409(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)
    client.post("/api/auth/login", json={"password": "secret123"})

    first = client.post("/api/esim/switch", json={"display_name": "Club"})
    second = client.post("/api/esim/switch", json={"display_name": "giffgaff sws"})

    assert first.status_code == 200
    assert second.status_code == 409


def test_auth_protects_business_apis(tmp_path: Path) -> None:
    client = make_test_app(tmp_path)

    unauthorized = client.get("/api/health")
    wrong_login = client.post("/api/auth/login", json={"password": "wrong"})
    auth_status_before = client.get("/api/auth/status")
    good_login = client.post("/api/auth/login", json={"password": "secret123"})
    auth_status_after = client.get("/api/auth/status")
    authorized = client.get("/api/health")
    logout = client.post("/api/auth/logout")
    auth_status_final = client.get("/api/auth/status")
    unauthorized_again = client.get("/api/health")

    assert unauthorized.status_code == 401
    assert wrong_login.status_code == 401
    assert auth_status_before.status_code == 200
    assert auth_status_before.json()["authenticated"] is False
    assert good_login.status_code == 200
    assert auth_status_after.json()["authenticated"] is True
    assert authorized.status_code == 200
    assert logout.status_code == 200
    assert auth_status_final.json()["authenticated"] is False
    assert unauthorized_again.status_code == 401


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
