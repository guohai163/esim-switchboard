from __future__ import annotations

import hashlib
import shlex
import re
import subprocess
from datetime import datetime, timezone
from typing import Any

from app.config import AppConfig
from app.models import EsimSnapshotRecord, EsimSubscriptionRecord, SmsMessageRecord


class AdbCommandError(RuntimeError):
    """Raised when an adb command fails or adb is not available."""

    def __init__(self, command: list[str], message: str, stderr: str | None = None) -> None:
        self.command = command
        self.stderr = stderr
        super().__init__(message)


class AdbClient:
    """Thin wrapper around adb subprocess calls."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def _base_command(self) -> list[str]:
        command = [self.config.adb_path]
        if self.config.adb_device_serial:
            command.extend(["-s", self.config.adb_device_serial])
        return command

    def run(self, args: list[str], timeout: float = 30) -> str:
        command = [*self._base_command(), *args]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AdbCommandError(command, f"ADB executable not found: {self.config.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbCommandError(command, f"ADB command timed out after {timeout}s") from exc
        if result.returncode != 0:
            error_text = (result.stderr or result.stdout).strip()
            raise AdbCommandError(command, error_text or "ADB command failed", stderr=result.stderr)
        return result.stdout

    def get_devices(self, timeout: float = 30) -> list[str]:
        output = self.run(["devices"], timeout=timeout)
        devices: list[str] = []
        for line in output.splitlines():
            if "\tdevice" in line:
                devices.append(line.split("\t", 1)[0].strip())
        return devices

    def read_isub(self) -> str:
        return self.run(["shell", "dumpsys", "isub"], timeout=30)

    def query_sms_inbox(self, limit: int | None = None) -> str:
        attempts = [
            [
                "shell",
                "sh",
                "-c",
                _build_content_query_command(
                    uri="content://sms/inbox",
                    projection="address:body:sub_id:_id:date",
                    sort="date DESC",
                ),
            ],
            [
                "shell",
                "sh",
                "-c",
                _build_content_query_command(
                    uri="content://sms/inbox",
                    projection="address:body:sub_id:_id:date",
                    sort=None,
                ),
            ],
            [
                "shell",
                "sh",
                "-c",
                _build_content_query_command(
                    uri="content://sms/inbox",
                    projection=None,
                    sort="date DESC",
                ),
            ],
            [
                "shell",
                "sh",
                "-c",
                _build_content_query_command(
                    uri="content://sms/inbox",
                    projection=None,
                    sort=None,
                ),
            ],
            [
                "shell",
                "content",
                "query",
                "--uri",
                "content://sms/inbox",
                "--projection",
                "address:body:sub_id:_id:date",
            ],
        ]
        last_error_text: str | None = None
        for command in attempts:
            try:
                output = self.run(command, timeout=60)
                if _looks_like_content_usage_error(output.strip()):
                    last_error_text = output.strip()
                    continue
                if not output.strip():
                    last_error_text = "ADB content query returned empty output"
                    continue
                parse_sms_query_output(output)
                if limit is None:
                    return output
                records = parse_sms_query_output(output)
                return "\n".join(record.raw_row for record in records[:limit])
            except AdbCommandError as exc:
                last_error_text = exc.stderr or str(exc)
                continue
        raise AdbCommandError(
            ["shell", "content", "query"],
            f"ADB content query failed after multiple attempts. Last output: {last_error_text or 'unknown error'}",
        )

    def stream_logcat(self, buffers: tuple[str, ...] | None = None, tags: tuple[str, ...] | None = None) -> subprocess.Popen[str]:
        resolved_buffers = buffers or self.config.sms_log_buffers
        resolved_tags = tags or self.config.sms_log_tags
        command = [*self._base_command(), "logcat"]
        for buffer_name in resolved_buffers:
            command.extend(["-b", buffer_name])
        command.extend(["-s", *resolved_tags])
        try:
            return subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise AdbCommandError(command, f"ADB executable not found: {self.config.adb_path}") from exc

    def send_sms_via_ui(self, target_phone: str, message: str, device: Any) -> None:
        command = (
            "am start -a android.intent.action.SENDTO "
            f"-d sms:{shlex.quote(target_phone)} "
            f'--es sms_body {shlex.quote(message)}'
        )
        device.shell(command)


def parse_isub_output(output: str) -> EsimSnapshotRecord:
    """Parse dumpsys isub into a normalized snapshot."""

    active_entries = _parse_subscription_section(output, "ActiveSubInfoList")
    all_entries = _parse_subscription_section(output, "AllSubInfoList")

    merged: dict[str, EsimSubscriptionRecord] = {}
    active_ids = {entry.sub_id for entry in active_entries if entry.is_embedded}

    for entry in all_entries:
        if entry.sub_id in merged:
            continue
        merged[entry.sub_id] = EsimSubscriptionRecord(
            sub_id=entry.sub_id,
            display_name=entry.display_name,
            carrier_name=entry.carrier_name,
            is_embedded=entry.is_embedded,
            is_active=entry.sub_id in active_ids,
            sim_slot_index=entry.sim_slot_index,
        )

    for entry in active_entries:
        if entry.sub_id not in merged:
            merged[entry.sub_id] = entry
        else:
            merged[entry.sub_id].is_active = True
            if merged[entry.sub_id].sim_slot_index is None:
                merged[entry.sub_id].sim_slot_index = entry.sim_slot_index

    subscriptions = sorted(merged.values(), key=lambda item: (not item.is_active, item.sub_id))
    embedded_total_count = len({item.sub_id for item in subscriptions if item.is_embedded})
    embedded_active_count = len({item.sub_id for item in subscriptions if item.is_embedded and item.is_active})
    return EsimSnapshotRecord(
        collected_at=datetime.now(timezone.utc).isoformat(),
        embedded_total_count=embedded_total_count,
        embedded_active_count=embedded_active_count,
        raw_output=output,
        subscriptions=subscriptions,
    )


def _parse_subscription_section(output: str, section_name: str) -> list[EsimSubscriptionRecord]:
    section_match = re.search(
        rf"{re.escape(section_name)}:\s*(.*?)(?:\n\s*\+{{10,}}|\Z)",
        output,
        re.DOTALL,
    )
    if not section_match:
        return []
    section_body = section_match.group(1)
    return [_parse_subscription_record(item.group(0)) for item in re.finditer(r"\{.*?\}", section_body, re.DOTALL)]


def _parse_subscription_record(record_text: str) -> EsimSubscriptionRecord:
    payload = record_text.strip().strip("{}")
    field_map = _parse_key_value_payload(payload)
    return EsimSubscriptionRecord(
        sub_id=str(field_map.get("id", "")).strip(),
        display_name=_empty_to_none(field_map.get("displayName")),
        carrier_name=_empty_to_none(field_map.get("carrierName")),
        is_embedded=str(field_map.get("isEmbedded", "")).lower() == "true",
        is_active=int_or_none(field_map.get("simSlotIndex")) is not None and int_or_none(field_map.get("simSlotIndex")) >= 0,
        sim_slot_index=int_or_none(field_map.get("simSlotIndex")),
    )


def parse_sms_query_output(output: str) -> list[SmsMessageRecord]:
    """Parse adb content query output without breaking on commas or newlines inside body."""

    trimmed = output.strip()
    if not trimmed or "No result found." in trimmed:
        return []
    if _looks_like_content_usage_error(trimmed):
        raise AdbCommandError(["shell", "content", "query"], "ADB content query returned usage/error output")

    rows = _split_content_rows(trimmed)
    records: list[SmsMessageRecord] = []
    for row in rows:
        field_map = _parse_ordered_row_fields(row, ["address", "body", "sub_id", "_id", "date"])
        if not field_map:
            continue
        sms_id = field_map.get("_id")
        if not sms_id:
            sms_id = build_sms_fingerprint(
                field_map.get("address"),
                field_map.get("body"),
                field_map.get("sub_id"),
                field_map.get("date"),
            )
        records.append(
            SmsMessageRecord(
                sms_id=sms_id,
                address=_empty_to_none(field_map.get("address")),
                body=_empty_to_none(field_map.get("body")),
                sub_id=_empty_to_none(field_map.get("sub_id")),
                date_ts=int_or_none(field_map.get("date")),
                raw_row=row,
            )
        )
    return records


def _looks_like_content_usage_error(output: str) -> bool:
    lowered = output.lower()
    return lowered.startswith("usage: adb shell content") or lowered.startswith("usage: content ") or "[error]" in lowered


def _build_content_query_command(uri: str, projection: str | None, sort: str | None) -> str:
    command = ["content", "query", "--uri", uri]
    if projection:
        command.extend(["--projection", projection])
    if sort:
        command.extend(["--sort", sort])
    return " ".join(shlex.quote(part) for part in command)


def _split_content_rows(output: str) -> list[str]:
    matches = list(re.finditer(r"Row:\s*\d+", output))
    if not matches:
        return [output]
    rows: list[str] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(output)
        rows.append(output[start:end].strip())
    return rows


def _parse_ordered_row_fields(row: str, field_order: list[str]) -> dict[str, str]:
    payload = re.sub(r"^Row:\s*\d+\s*", "", row, count=1, flags=re.DOTALL).strip()
    markers: list[tuple[str, int, int]] = []
    for field_name in field_order:
        marker = re.search(rf"(^|,\s+|\n){re.escape(field_name)}=", payload)
        if marker:
            key_start = marker.start() + len(marker.group(1))
            value_start = key_start + len(field_name) + 1
            markers.append((field_name, key_start, value_start))
    markers.sort(key=lambda item: item[1])
    field_map: dict[str, str] = {}
    for index, (field_name, _, value_start) in enumerate(markers):
        next_start = markers[index + 1][1] if index + 1 < len(markers) else len(payload)
        raw_value = payload[value_start:next_start].rstrip(", ").strip()
        field_map[field_name] = raw_value
    return field_map


def _parse_key_value_payload(payload: str) -> dict[str, str]:
    key_matches = list(re.finditer(r"([A-Za-z0-9_]+)=", payload))
    field_map: dict[str, str] = {}
    for index, match in enumerate(key_matches):
        key = match.group(1)
        value_start = match.end()
        value_end = key_matches[index + 1].start() if index + 1 < len(key_matches) else len(payload)
        field_map[key] = payload[value_start:value_end].strip()
    return field_map


def build_sms_fingerprint(address: str | None, body: str | None, sub_id: str | None, date_value: str | None) -> str:
    source = "|".join(
        [
            address or "",
            body or "",
            sub_id or "",
            date_value or "",
        ]
    )
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return f"fallback:{digest}"


def int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or stripped.lower() == "null":
        return None
    try:
        return int(stripped)
    except ValueError:
        return None


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if stripped == "" or stripped.lower() == "null":
        return None
    return stripped
