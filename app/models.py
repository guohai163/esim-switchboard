from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field


@dataclass(slots=True)
class EsimSubscriptionRecord:
    """Normalized subscription info parsed from dumpsys output."""

    sub_id: str
    display_name: str | None
    carrier_name: str | None
    is_embedded: bool
    is_active: bool
    sim_slot_index: int | None


@dataclass(slots=True)
class EsimSnapshotRecord:
    """A full eSIM sync snapshot."""

    collected_at: str
    embedded_total_count: int
    embedded_active_count: int
    raw_output: str
    subscriptions: list[EsimSubscriptionRecord]


@dataclass(slots=True)
class SmsMessageRecord:
    """A single inbox SMS row parsed from Android content query output."""

    sms_id: str
    address: str | None
    body: str | None
    sub_id: str | None
    date_ts: int | None
    raw_row: str


class EsimSubscriptionOut(BaseModel):
    sub_id: str
    display_name: str | None = None
    carrier_name: str | None = None
    is_embedded: bool
    is_active: bool
    sim_slot_index: int | None = None


class EsimLatestResponse(BaseModel):
    id: int | None = None
    collected_at: str | None = None
    embedded_total_count: int = 0
    embedded_active_count: int = 0
    subscriptions: list[EsimSubscriptionOut] = Field(default_factory=list)


class SmsMessageOut(BaseModel):
    sms_id: str
    address: str | None = None
    body: str | None = None
    sub_id: str | None = None
    display_name: str | None = None
    date_ts: int | None = None
    created_at: str
    updated_at: str


class SmsListResponse(BaseModel):
    page: int
    page_size: int
    total: int
    items: list[SmsMessageOut]


class SmsEventStatusResponse(BaseModel):
    event_type: str | None = None
    occurred_at: str | None = None
    inserted_count: int = 0
    fetched_count: int = 0
    latest_sms_id: str | None = None
    latest_address: str | None = None
    latest_display_name: str | None = None
    event_source: str | None = None
    trigger_buffer: str | None = None
    trigger_log_line: str | None = None


class SyncResponse(BaseModel):
    ok: bool = True
    fetched_count: int = 0
    inserted_count: int = 0
    duplicate_count: int = 0
    synced_at: str
    detail: str | None = None


class AuthLoginRequest(BaseModel):
    password: str


class AuthStatusResponse(BaseModel):
    authenticated: bool


class EsimSwitchRequest(BaseModel):
    display_name: str
    lock_minutes: Literal[10, 20, 30]


class EsimSwitchLogEntryOut(BaseModel):
    event_type: Literal["attempt_started", "blocked_running", "blocked_locked", "succeeded", "failed"]
    display_name: str | None = None
    lock_minutes: int | None = None
    created_at: str
    message: str
    task_id: str | None = None


class EsimSwitchStepOut(BaseModel):
    step_key: str
    title: str
    status: Literal["pending", "running", "succeeded", "failed"]
    timestamp: str
    screenshot_url: str | None = None
    detail: str | None = None


class EsimSwitchStatusResponse(BaseModel):
    task_id: str | None = None
    status: Literal["idle", "running", "succeeded", "failed"] = "idle"
    target_display_name: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    current_step: str | None = None
    latest_screenshot_url: str | None = None
    error: str | None = None
    lock_active: bool = False
    lock_until: str | None = None
    lock_remaining_seconds: int = 0
    lock_minutes: int | None = None
    logs: list[EsimSwitchLogEntryOut] = Field(default_factory=list)
    steps: list[EsimSwitchStepOut] = Field(default_factory=list)


class MonitorStatusResponse(BaseModel):
    running: bool
    connected: bool
    restart_count: int
    last_event_at: str | None = None
    last_error: str | None = None
    last_log_line: str | None = None
    active_log_buffers: list[str] = Field(default_factory=list)
    last_sms_log_hit_at: str | None = None
    last_sms_log_hit_line: str | None = None
    last_sms_log_hit_buffer: str | None = None
    last_broadcast_source: str | None = None


class HealthResponse(BaseModel):
    ok: bool
    db_path: str
    adb_path: str
    adb_available: bool
    adb_devices: list[str] = Field(default_factory=list)
    adb_error: str | None = None
    monitor: MonitorStatusResponse
    last_sms_sync: dict[str, Any] | None = None
    last_esim_sync: dict[str, Any] | None = None
