from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from app.adb import AdbClient
from app.config import AppConfig


BROWSER_SUPPORTED_HINT = "仅支持桌面版 Chromium 浏览器，移动端和 Safari/Firefox 暂不支持。"
ACTION_KEYCODE_MAP = {
    "back": 4,
    "home": 3,
    "recent": 187,
    "volume_up": 24,
    "volume_down": 25,
    "power": 26,
}


class DeviceMonitorUnavailableError(RuntimeError):
    """Raised when the H264 device monitor pipeline cannot be started."""


class DeviceMonitorConflictError(RuntimeError):
    """Raised when another browser already owns the monitor session."""


class DeviceMonitorLockedError(RuntimeError):
    """Raised when a switch task blocks monitoring and remote control."""


@dataclass(slots=True)
class ActiveMonitorSession:
    """Tracks the single live video stream and its owned resources."""

    transport: str
    adb_process: subprocess.Popen[bytes]
    ffmpeg_process: subprocess.Popen[bytes]
    pump_thread: threading.Thread
    websocket: WebSocket
    loop: asyncio.AbstractEventLoop
    stop_reason: str | None = None


class DeviceMonitorService:
    """Owns the single-screen monitor session and lightweight remote controls."""

    def __init__(self, config: AppConfig, adb_client: AdbClient) -> None:
        self.config = config
        self.adb_client = adb_client
        self._lock = threading.Lock()
        self._session: ActiveMonitorSession | None = None

    def get_status(self, *, switch_running: bool) -> dict[str, Any]:
        """Report availability and current session state for the dashboard."""

        if switch_running:
            return {
                "available": False,
                "running": self.is_running(),
                "reason": "eSIM 切换进行中，手机监控已禁用",
                "browser_supported_hint": BROWSER_SUPPORTED_HINT,
            }

        ffmpeg_error = self._resolve_ffmpeg_error()
        if ffmpeg_error:
            return {
                "available": False,
                "running": self.is_running(),
                "reason": ffmpeg_error,
                "browser_supported_hint": BROWSER_SUPPORTED_HINT,
            }

        try:
            devices = self.adb_client.get_devices(timeout=self.config.adb_healthcheck_timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            return {
                "available": False,
                "running": self.is_running(),
                "reason": str(exc),
                "browser_supported_hint": BROWSER_SUPPORTED_HINT,
            }
        if not devices:
            return {
                "available": False,
                "running": self.is_running(),
                "reason": "未检测到可用的 Android 设备",
                "browser_supported_hint": BROWSER_SUPPORTED_HINT,
            }

        try:
            self.adb_client.wake_screen()
            if not self._supports_h264_screenrecord():
                return {
                    "available": False,
                    "running": self.is_running(),
                    "reason": "当前设备的 screenrecord 不支持 H264 直出",
                    "browser_supported_hint": BROWSER_SUPPORTED_HINT,
                }
        except Exception as exc:  # noqa: BLE001
            return {
                "available": False,
                "running": self.is_running(),
                "reason": str(exc),
                "browser_supported_hint": BROWSER_SUPPORTED_HINT,
            }

        return {
            "available": True,
            "running": self.is_running(),
            "reason": None,
            "browser_supported_hint": BROWSER_SUPPORTED_HINT,
        }

    def is_running(self) -> bool:
        """Expose whether the single monitor slot is currently occupied."""

        with self._lock:
            return self._session is not None

    def start_session(self, websocket: WebSocket, *, switch_running: bool) -> ActiveMonitorSession:
        """Reserve the single monitor slot and launch the stream subprocesses."""

        if switch_running:
            raise DeviceMonitorLockedError("eSIM 切换进行中，暂时不能打开手机监控")

        status = self.get_status(switch_running=False)
        if not status["available"]:
            raise DeviceMonitorUnavailableError(status["reason"] or "手机监控当前不可用")

        with self._lock:
            if self._session is not None:
                raise DeviceMonitorConflictError("已有浏览器正在占用手机监控")
            session = self._spawn_session(websocket)
            self._session = session
            return session

    def finish_session(self, session: ActiveMonitorSession) -> None:
        """Release the monitor slot after the websocket ends."""

        with self._lock:
            if self._session is session:
                self._session = None
        self._terminate_process(session.ffmpeg_process)
        self._terminate_process(session.adb_process)
        if session.pump_thread.is_alive():
            session.pump_thread.join(timeout=1)

    def force_stop(self, reason: str) -> None:
        """Terminate the current monitor stream so switch automation can take over."""

        with self._lock:
            session = self._session
            if session is None:
                return
            session.stop_reason = reason
        self._terminate_process(session.ffmpeg_process)
        self._terminate_process(session.adb_process)

    def send_action(self, action: str, *, switch_running: bool) -> dict[str, Any]:
        """Dispatch a lightweight remote-control key event through adb."""

        self._ensure_control_allowed(switch_running=switch_running)
        keycode = ACTION_KEYCODE_MAP[action]
        self.adb_client.send_keyevent(keycode)
        return {"ok": True, "action": action}

    def send_tap(self, x_ratio: float, y_ratio: float, *, switch_running: bool) -> dict[str, Any]:
        """Map normalized tap coordinates to the physical device screen."""

        self._ensure_control_allowed(switch_running=switch_running)
        width, height = self.adb_client.get_screen_size()
        x = min(width - 1, max(0, round(width * x_ratio)))
        y = min(height - 1, max(0, round(height * y_ratio)))
        self.adb_client.send_tap(x, y)
        return {"ok": True, "x": x, "y": y}

    async def stream_to_websocket(self, session: ActiveMonitorSession) -> None:
        """Send status frames first, then binary fragmented MP4 chunks to the browser."""

        await session.websocket.send_json({"type": "status", "status": "starting"})
        await session.websocket.send_json({"type": "status", "status": "streaming"})
        try:
            while True:
                chunk = await asyncio.to_thread(session.ffmpeg_process.stdout.read, 65536)
                if not chunk:
                    break
                await session.websocket.send_bytes(chunk)
        finally:
            stderr_text = await self._collect_stderr(session)
            if session.stop_reason:
                await session.websocket.send_json({"type": "stopped", "reason": session.stop_reason})
            elif stderr_text:
                await session.websocket.send_json({"type": "error", "reason": stderr_text})

    def _spawn_session(self, websocket: WebSocket) -> ActiveMonitorSession:
        return self._spawn_session_with_transport(websocket, transport="exec-out")

    def _ensure_control_allowed(self, *, switch_running: bool) -> None:
        if switch_running:
            raise DeviceMonitorLockedError("eSIM 切换进行中，暂时不能控制手机")
        status = self.get_status(switch_running=False)
        if not status["available"]:
            raise DeviceMonitorUnavailableError(status["reason"] or "手机监控当前不可用")

    def _resolve_ffmpeg_error(self) -> str | None:
        ffmpeg_path = self.config.ffmpeg_path
        if "/" in ffmpeg_path:
            return None if Path(ffmpeg_path).exists() else f"FFmpeg executable not found: {ffmpeg_path}"
        return None if shutil.which(ffmpeg_path) else f"FFmpeg executable not found: {ffmpeg_path}"

    def _supports_h264_screenrecord(self) -> bool:
        """Probe the device instead of trusting help text, which is inconsistent across ROMs."""

        process = self.adb_client.open_h264_screenrecord(
            bitrate=self.config.device_monitor_bitrate,
            time_limit_seconds=1,
            transport="exec-out",
        )
        try:
            stdout = process.stdout.read(64) if process.stdout is not None else b""
            stderr = process.stderr.read().decode("utf-8", errors="ignore") if process.stderr is not None else ""
            if stdout:
                return True
            lowered = stderr.lower()
            if any(marker in lowered for marker in ("unknown option", "unrecognized option", "invalid option", "error")):
                return False
            return False
        finally:
            self._terminate_process(process)

    def _spawn_session_with_transport(self, websocket: WebSocket, *, transport: str) -> ActiveMonitorSession:
        self.adb_client.wake_screen()
        adb_process = self.adb_client.open_h264_screenrecord(
            bitrate=self.config.device_monitor_bitrate,
            transport=transport,
        )
        command = [
            self.config.ffmpeg_path,
            "-loglevel",
            "error",
            "-probesize",
            f"{max(512, self.config.device_monitor_buffer_kb * 8)}k",
            "-analyzeduration",
            "2000000",
            "-f",
            "h264",
            "-fflags",
            "+genpts",
            "-use_wallclock_as_timestamps",
            "1",
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "copy",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "pipe:1",
        ]
        try:
            ffmpeg_process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            self._terminate_process(adb_process)
            raise DeviceMonitorUnavailableError(f"FFmpeg executable not found: {self.config.ffmpeg_path}") from exc
        pump_thread = threading.Thread(
            target=self._pump_h264_stream,
            args=(adb_process, ffmpeg_process),
            name=f"device-monitor-pump-{transport}",
            daemon=True,
        )
        pump_thread.start()
        if not self._ensure_pipeline_started(ffmpeg_process, adb_process):
            self._terminate_process(ffmpeg_process)
            self._terminate_process(adb_process)
            stderr = self._read_stderr(ffmpeg_process) or self._read_stderr(adb_process) or f"{transport} 模式启动后立即退出"
            raise DeviceMonitorUnavailableError(stderr)
        return ActiveMonitorSession(
            transport=transport,
            adb_process=adb_process,
            ffmpeg_process=ffmpeg_process,
            pump_thread=pump_thread,
            websocket=websocket,
            loop=asyncio.get_running_loop(),
        )

    def _ensure_pipeline_started(self, ffmpeg_process: subprocess.Popen[bytes], adb_process: subprocess.Popen[bytes]) -> bool:
        """Only fail fast on immediate process crashes; do not require MP4 bytes within a tiny window."""

        for _ in range(10):
            if ffmpeg_process.poll() is not None or adb_process.poll() is not None:
                return False
            time.sleep(0.1)
        return True

    def _pump_h264_stream(self, adb_process: subprocess.Popen[bytes], ffmpeg_process: subprocess.Popen[bytes]) -> None:
        """Normalize adb screenrecord output before ffmpeg sees it."""

        if adb_process.stdout is None or ffmpeg_process.stdin is None:
            return

        source = adb_process.stdout
        sink = ffmpeg_process.stdin
        mode: str | None = None
        buffer = b""
        nal_length_size = 4

        try:
            while True:
                chunk = source.read(65536)
                if not chunk:
                    break
                if mode == "annexb":
                    sink.write(chunk)
                    sink.flush()
                    continue

                buffer += chunk
                if mode is None:
                    mode = self._detect_h264_stream_mode(buffer)
                    if mode == "annexb":
                        sink.write(buffer)
                        sink.flush()
                        buffer = b""
                        continue
                    if mode == "avcc_config":
                        original_buffer = buffer
                        annexb_prefix, buffer, nal_length_size = self._consume_avcc_config_record(buffer)
                        if not annexb_prefix and buffer == original_buffer:
                            mode = None
                            continue
                        if annexb_prefix:
                            sink.write(annexb_prefix)
                            sink.flush()
                        mode = "avcc"
                    if mode == "avcc_config_prefixed":
                        original_buffer = buffer
                        annexb_prefix, buffer, nal_length_size = self._consume_length_prefixed_avcc_config_record(buffer)
                        if not annexb_prefix and buffer == original_buffer:
                            mode = None
                            continue
                        if annexb_prefix:
                            sink.write(annexb_prefix)
                            sink.flush()
                        mode = "avcc"

                if mode == "avcc":
                    payload, buffer = self._convert_avcc_buffer_to_annexb(buffer, nal_length_size=nal_length_size)
                    if payload:
                        sink.write(payload)
                        sink.flush()
                    continue

                if len(buffer) >= 64:
                    # Fall back to passthrough if we cannot confidently classify the stream.
                    mode = "annexb"
                    sink.write(buffer)
                    sink.flush()
                    buffer = b""
            if mode == "avcc" and buffer:
                payload, _ = self._convert_avcc_buffer_to_annexb(buffer, nal_length_size=nal_length_size, finalize=True)
                if payload:
                    sink.write(payload)
                    sink.flush()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                sink.close()
            except Exception:  # noqa: BLE001
                return

    def _detect_h264_stream_mode(self, payload: bytes) -> str | None:
        if b"\x00\x00\x00\x01" in payload[:16] or b"\x00\x00\x01" in payload[:16]:
            return "annexb"
        if self._looks_like_length_prefixed_avcc_config(payload):
            return "avcc_config_prefixed"
        if self._looks_like_avcc_config(payload):
            return "avcc_config"
        if len(payload) < 5:
            return None
        nal_size = int.from_bytes(payload[:4], "big")
        nal_header = payload[4] & 0x1F
        if 0 < nal_size <= 2_000_000 and nal_header in {1, 5, 6, 7, 8, 9, 12, 19, 20}:
            return "avcc"
        return None

    def _looks_like_avcc_config(self, payload: bytes) -> bool:
        if len(payload) < 8:
            return False
        if payload[0] != 1:
            return False
        # AVCDecoderConfigurationRecord layout:
        # version(1) profile(1) compat(1) level(1) reserved+lengthSize(1) reserved+numSPS(1)
        num_sps = payload[5] & 0x1F
        return num_sps > 0 and self._can_fully_parse_avcc_config(payload)

    def _looks_like_length_prefixed_avcc_config(self, payload: bytes) -> bool:
        if len(payload) < 10:
            return False
        record_length = int.from_bytes(payload[:4], "big")
        if record_length <= 0 or record_length > 512:
            return False
        if payload[4] != 1:
            return False
        if len(payload) < 4 + record_length:
            return False
        num_sps = payload[9] & 0x1F
        return num_sps > 0

    def _can_fully_parse_avcc_config(self, payload: bytes) -> bool:
        if len(payload) < 8 or payload[0] != 1:
            return False
        cursor = 6
        num_sps = payload[5] & 0x1F
        for _ in range(num_sps):
            if cursor + 2 > len(payload):
                return False
            sps_length = int.from_bytes(payload[cursor : cursor + 2], "big")
            cursor += 2
            if cursor + sps_length > len(payload):
                return False
            cursor += sps_length
        if cursor >= len(payload):
            return False
        num_pps = payload[cursor]
        cursor += 1
        for _ in range(num_pps):
            if cursor + 2 > len(payload):
                return False
            pps_length = int.from_bytes(payload[cursor : cursor + 2], "big")
            cursor += 2
            if cursor + pps_length > len(payload):
                return False
            cursor += pps_length
        return True

    def _consume_avcc_config_record(self, payload: bytes) -> tuple[bytes, bytes, int]:
        if len(payload) < 8:
            return b"", payload, 4

        cursor = 0
        if payload[cursor] != 1:
            return b"", payload, 4
        cursor += 4  # version/profile/compat/level
        nal_length_size = (payload[cursor] & 0x03) + 1
        cursor += 1  # lengthSizeMinusOne
        if cursor >= len(payload):
            return b"", payload, nal_length_size

        num_sps = payload[cursor] & 0x1F
        cursor += 1
        output = bytearray()

        for _ in range(num_sps):
            if cursor + 2 > len(payload):
                return b"", payload, nal_length_size
            sps_length = int.from_bytes(payload[cursor : cursor + 2], "big")
            cursor += 2
            if cursor + sps_length > len(payload):
                return b"", payload, nal_length_size
            output.extend(b"\x00\x00\x00\x01")
            output.extend(payload[cursor : cursor + sps_length])
            cursor += sps_length

        if cursor >= len(payload):
            return b"", payload, nal_length_size
        num_pps = payload[cursor]
        cursor += 1

        for _ in range(num_pps):
            if cursor + 2 > len(payload):
                return b"", payload, nal_length_size
            pps_length = int.from_bytes(payload[cursor : cursor + 2], "big")
            cursor += 2
            if cursor + pps_length > len(payload):
                return b"", payload, nal_length_size
            output.extend(b"\x00\x00\x00\x01")
            output.extend(payload[cursor : cursor + pps_length])
            cursor += pps_length

        return bytes(output), payload[cursor:], nal_length_size

    def _consume_length_prefixed_avcc_config_record(self, payload: bytes) -> tuple[bytes, bytes, int]:
        if len(payload) < 10:
            return b"", payload, 4
        record_length = int.from_bytes(payload[:4], "big")
        if record_length <= 0 or len(payload) < 4 + record_length:
            return b"", payload, 4
        converted, _, nal_length_size = self._consume_avcc_config_record(payload[4 : 4 + record_length])
        return converted, payload[4 + record_length :], nal_length_size

    def _convert_avcc_buffer_to_annexb(
        self,
        payload: bytes,
        *,
        nal_length_size: int = 4,
        finalize: bool = False,
    ) -> tuple[bytes, bytes]:
        output = bytearray()
        cursor = 0
        limit = len(payload)
        while cursor + nal_length_size <= limit:
            nal_size = int.from_bytes(payload[cursor : cursor + nal_length_size], "big")
            if nal_size <= 0:
                cursor += nal_length_size
                continue
            if cursor + nal_length_size + nal_size > limit:
                if finalize:
                    return bytes(output), b""
                break
            cursor += nal_length_size
            output.extend(b"\x00\x00\x00\x01")
            output.extend(payload[cursor : cursor + nal_size])
            cursor += nal_size
        return bytes(output), payload[cursor:]

    async def _collect_stderr(self, session: ActiveMonitorSession) -> str | None:
        stderr_parts: list[str] = []
        adb_stderr = await asyncio.to_thread(self._read_stderr, session.adb_process)
        ffmpeg_stderr = await asyncio.to_thread(self._read_stderr, session.ffmpeg_process)
        if adb_stderr:
            stderr_parts.append(adb_stderr)
        if ffmpeg_stderr:
            stderr_parts.append(ffmpeg_stderr)
        if not stderr_parts:
            return None
        return " | ".join(part for part in stderr_parts if part).strip() or None

    def _read_stderr(self, process: subprocess.Popen[bytes]) -> str:
        if process.stderr is None:
            return ""
        try:
            return process.stderr.read().decode("utf-8", errors="ignore").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                return
