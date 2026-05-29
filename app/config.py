from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    """Application configuration loaded from the local environment."""

    base_dir: Path
    db_path: Path
    adb_path: str
    adb_device_serial: str | None
    adb_healthcheck_timeout_seconds: float
    sms_sync_delay_seconds: float
    adb_reconnect_delay_seconds: float
    default_page_size: int
    max_page_size: int
    sms_log_buffers: tuple[str, ...]
    sms_log_tags: tuple[str, ...]
    sms_log_trigger_keywords: tuple[str, ...]
    sms_event_poll_interval_seconds: float
    switch_screenshot_dir: Path
    switch_step_delay_seconds: float
    switch_confirm_wait_seconds: float
    keepalive_poll_interval_seconds: float
    keepalive_retry_minutes: int
    keepalive_switch_settle_seconds: float
    esim_settings_label: tuple[str, ...]
    esim_toggle_label: tuple[str, ...]
    esim_confirm_label: tuple[str, ...]
    sms_compose_package: str
    sms_send_button_labels: tuple[str, ...]
    app_password: str
    app_auth_cookie_name: str
    app_auth_cookie_value: str
    trust_proxy_headers: bool
    collab_presence_timeout_seconds: float
    collab_ping_interval_seconds: float
    collab_cursor_throttle_ms: int

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "AppConfig":
        resolved_base_dir = base_dir or Path.cwd()
        app_password = os.getenv("APP_PASSWORD", "")
        app_auth_cookie_name = os.getenv("APP_AUTH_COOKIE_NAME", "esim_switch_auth")
        auth_source = f"{app_auth_cookie_name}:{app_password}".encode("utf-8")
        app_auth_cookie_value = hashlib.sha256(auth_source).hexdigest()
        trust_proxy_headers = os.getenv("TRUST_PROXY_HEADERS", "false").strip().lower() in {"1", "true", "yes", "on"}
        return cls(
            base_dir=resolved_base_dir,
            db_path=Path(os.getenv("DB_PATH", resolved_base_dir / "db.sqlite")),
            adb_path=os.getenv("ADB_PATH", "adb"),
            adb_device_serial=os.getenv("ADB_DEVICE_SERIAL") or None,
            adb_healthcheck_timeout_seconds=float(os.getenv("ADB_HEALTHCHECK_TIMEOUT_SECONDS", "3")),
            sms_sync_delay_seconds=float(os.getenv("SMS_SYNC_DELAY_SECONDS", "1.5")),
            adb_reconnect_delay_seconds=float(os.getenv("ADB_RECONNECT_DELAY_SECONDS", "5")),
            default_page_size=int(os.getenv("DEFAULT_PAGE_SIZE", "50")),
            max_page_size=int(os.getenv("MAX_PAGE_SIZE", "200")),
            sms_log_buffers=_parse_env_tuple("SMS_LOG_BUFFERS", ("radio",)),
            sms_log_tags=_parse_env_tuple(
                "SMS_LOG_TAGS",
                (
                    "RILJ",
                    "GsmInboundSmsHandler",
                    "SmsBroadcastReceiver",
                    "SmsMessage",
                    "TelephonyProvider",
                    "MmsSmsProvider",
                ),
            ),
            sms_log_trigger_keywords=_parse_env_tuple(
                "SMS_LOG_TRIGGER_KEYWORDS",
                (
                    "unsol_response_new_sms",
                    "event_new_sms",
                    "android.provider.telephony.sms_received",
                    "delivering sms to",
                    "ordered broadcast completed for android.provider.telephony.sms_received",
                    "successful broadcast, deleting from raw table",
                    "sms_received",
                    "saving message",
                    "insertsms",
                    "smsbroadcastreceiver",
                    "mmssmsprovider",
                    "inboundsmshandler",
                    "dispatchsmsdeliveryintent",
                    "sms deliver",
                    "sms deliver action",
                    "added message to uri",
                ),
            ),
            sms_event_poll_interval_seconds=float(os.getenv("SMS_EVENT_POLL_INTERVAL_SECONDS", "12")),
            switch_screenshot_dir=Path(os.getenv("SWITCH_SCREENSHOT_DIR", resolved_base_dir / "runtime" / "switch_screenshots")),
            switch_step_delay_seconds=float(os.getenv("SWITCH_STEP_DELAY_SECONDS", "1")),
            switch_confirm_wait_seconds=float(os.getenv("SWITCH_CONFIRM_WAIT_SECONDS", "10")),
            keepalive_poll_interval_seconds=float(os.getenv("KEEPALIVE_POLL_INTERVAL_SECONDS", "60")),
            keepalive_retry_minutes=int(os.getenv("KEEPALIVE_RETRY_MINUTES", "15")),
            keepalive_switch_settle_seconds=float(os.getenv("KEEPALIVE_SWITCH_SETTLE_SECONDS", "5")),
            esim_settings_label=("SIM 卡", "SIMs", "SIM cards"),
            esim_toggle_label=("使用 SIM 卡", "启用", "开启", "Use SIM", "Turn on SIM"),
            esim_confirm_label=("是", "开启", "确定", "继续", "OK"),
            sms_compose_package=os.getenv("SMS_COMPOSE_PACKAGE", "com.google.android.apps.messaging"),
            sms_send_button_labels=("发送", "Send", "发送 SMS", "发送短信"),
            app_password=app_password,
            app_auth_cookie_name=app_auth_cookie_name,
            app_auth_cookie_value=app_auth_cookie_value,
            trust_proxy_headers=trust_proxy_headers,
            collab_presence_timeout_seconds=float(os.getenv("COLLAB_PRESENCE_TIMEOUT_SECONDS", "15")),
            collab_ping_interval_seconds=float(os.getenv("COLLAB_PING_INTERVAL_SECONDS", "5")),
            collab_cursor_throttle_ms=int(os.getenv("COLLAB_CURSOR_THROTTLE_MS", "50")),
        )


def _parse_env_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Allow comma/newline separated overrides while keeping stable defaults."""

    raw_value = os.getenv(name)
    if not raw_value:
        return default
    values = [item.strip() for item in raw_value.replace("\n", ",").split(",")]
    normalized = tuple(item for item in values if item)
    return normalized or default
