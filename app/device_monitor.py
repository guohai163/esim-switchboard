from __future__ import annotations

import asyncio
import io
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

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
STDERR_TAIL_LIMIT = 16_384
DIAGNOSTIC_LIMIT = 1_200
ERROR_REASON_LIMIT = 240
WAKE_DELAY_SECONDS = 0.4
PROBE_CACHE_TTL_SECONDS = 5.0
PROBE_TIMEOUT_SECONDS = 2.0


class DeviceMonitorUnavailableError(RuntimeError):
    """Raised when the H264 device monitor pipeline cannot be started."""

    def __init__(self, message: str, *, diagnostic: str | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class DeviceMonitorConflictError(RuntimeError):
    """Raised when another browser already owns the monitor session."""


class DeviceMonitorLockedError(RuntimeError):
    """Raised when a switch task blocks monitoring and remote control."""


class _TextTailBuffer:
    """Keep only the latest stderr text so long-running streams cannot grow unbounded."""

    def __init__(self, limit: int = STDERR_TAIL_LIMIT) -> None:
        self._limit = limit
        self._size = 0
        self._parts: deque[str] = deque()
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._parts.append(text)
            self._size += len(text)
            while self._size > self._limit and self._parts:
                overflow = self._size - self._limit
                head = self._parts[0]
                if len(head) <= overflow:
                    self._parts.popleft()
                    self._size -= len(head)
                    continue
                self._parts[0] = head[overflow:]
                self._size -= overflow
                break

    def get_text(self) -> str:
        with self._lock:
            return "".join(self._parts).strip()


@dataclass(slots=True)
class ActiveMonitorSession:
    """Tracks one live ffmpeg pipeline that mirrors the validated shell command."""

    adb_process: subprocess.Popen[bytes]
    ffmpeg_process: subprocess.Popen[bytes]
    websocket: WebSocket
    loop: asyncio.AbstractEventLoop
    mime_codec: str
    stop_reason: str | None = None
    adb_stderr_tail: _TextTailBuffer = field(default_factory=_TextTailBuffer)
    ffmpeg_stderr_tail: _TextTailBuffer = field(default_factory=_TextTailBuffer)
    stderr_threads: tuple[threading.Thread, ...] = ()


class DeviceMonitorService:
    """Owns the single-screen monitor session and the light remote-control endpoints."""

    def __init__(self, config: AppConfig, adb_client: AdbClient) -> None:
        self.config = config
        self.adb_client = adb_client
        self._lock = threading.Lock()
        self._session: ActiveMonitorSession | None = None
        self._probe_cache_lock = threading.Lock()
        self._probe_cache_value: bool | None = None
        self._probe_cache_reason: str | None = None
        self._probe_cache_expires_at = 0.0

    def get_status(self, *, switch_running: bool) -> dict[str, Any]:
        """Report availability and the current monitor slot status."""

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
            self._wake_screen_for_monitor()
            supported, probe_reason = self._supports_h264_screenrecord()
            if not supported:
                return {
                    "available": False,
                    "running": self.is_running(),
                    "reason": probe_reason or "当前设备的 screenrecord 不支持 H264 直出",
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
        with self._lock:
            return self._session is not None

    def start_session(self, websocket: WebSocket, *, switch_running: bool) -> ActiveMonitorSession:
        """Reserve the single monitor slot and launch the minimal adb->ffmpeg pipeline."""

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
        """Release the monitor slot after the websocket closes."""

        with self._lock:
            if self._session is session:
                self._session = None
        self._terminate_process(session.ffmpeg_process)
        self._terminate_process(session.adb_process)
        self._join_threads(session.stderr_threads, timeout=1)

    def force_stop(self, reason: str) -> None:
        """Terminate the current stream so UI automation can take over the phone."""

        with self._lock:
            session = self._session
            if session is None:
                return
            session.stop_reason = reason
        self._terminate_process(session.ffmpeg_process)
        self._terminate_process(session.adb_process)

    def send_action(self, action: str, *, switch_running: bool) -> dict[str, Any]:
        self._ensure_control_allowed(switch_running=switch_running)
        self.adb_client.send_keyevent(ACTION_KEYCODE_MAP[action])
        return {"ok": True, "action": action}

    def send_tap(self, x_ratio: float, y_ratio: float, *, switch_running: bool) -> dict[str, Any]:
        self._ensure_control_allowed(switch_running=switch_running)
        width, height = self.adb_client.get_screen_size()
        x = min(width - 1, max(0, round(width * x_ratio)))
        y = min(height - 1, max(0, round(height * y_ratio)))
        self.adb_client.send_tap(x, y)
        return {"ok": True, "x": x, "y": y}

    async def stream_to_websocket(self, session: ActiveMonitorSession) -> None:
        """Send status, negotiated codec info, then binary fMP4 chunks."""

        await session.websocket.send_json({"type": "status", "status": "starting"})
        await session.websocket.send_json({"type": "media", "mime_codec": session.mime_codec})
        await session.websocket.send_json({"type": "status", "status": "streaming"})
        try:
            while True:
                chunk = await asyncio.to_thread(session.ffmpeg_process.stdout.read, 65536)
                if not chunk:
                    break
                await session.websocket.send_bytes(chunk)
        finally:
            if session.stop_reason:
                await session.websocket.send_json({"type": "stopped", "reason": session.stop_reason})
            else:
                reason, diagnostic = await self._collect_error_details(session)
                if reason:
                    payload: dict[str, Any] = {"type": "error", "reason": reason}
                    if diagnostic:
                        payload["diagnostic"] = diagnostic
                    await session.websocket.send_json(payload)

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

    def _wake_screen_for_monitor(self) -> None:
        """Mirror the manual success recipe: wake first, then wait briefly for screenrecord to emit bytes."""

        self.adb_client.wake_screen()
        time.sleep(WAKE_DELAY_SECONDS)

    def _supports_h264_screenrecord(self) -> tuple[bool, str | None]:
        now = time.monotonic()
        with self._probe_cache_lock:
            if now < self._probe_cache_expires_at and self._probe_cache_value is not None:
                return self._probe_cache_value, self._probe_cache_reason

        process = self.adb_client.open_h264_screenrecord(
            bitrate=self._screenrecord_bitrate_arg(),
            time_limit_seconds=1,
            transport="exec-out",
        )
        try:
            stdout, stderr_bytes = process.communicate(timeout=PROBE_TIMEOUT_SECONDS)
            stderr = stderr_bytes.decode("utf-8", errors="ignore") if stderr_bytes else ""
            if stdout and not self._looks_like_mp4_container(stdout):
                self._set_probe_cache(True, None)
                return True, None
            lowered = stderr.lower()
            if any(marker in lowered for marker in ("unknown option", "unrecognized option", "invalid option", "error")):
                reason = stderr.strip() or "当前设备的 screenrecord 不支持 H264 直出"
                self._set_probe_cache(False, reason)
                return False, reason
            reason = "当前设备的 screenrecord 未输出 H264 数据"
            self._set_probe_cache(False, reason)
            return False, reason
        except subprocess.TimeoutExpired:
            self._terminate_process(process)
            reason = "screenrecord 探测超时"
            self._set_probe_cache(False, reason)
            return False, reason
        finally:
            self._terminate_process(process)

    def _spawn_session(self, websocket: WebSocket) -> ActiveMonitorSession:
        self._wake_screen_for_monitor()
        adb_process = self.adb_client.open_h264_screenrecord(
            bitrate=self._screenrecord_bitrate_arg(),
            transport="exec-out",
        )
        mime_codec = 'video/mp4; codecs="avc1.42E028"'
        ffmpeg_process = self._spawn_ffmpeg(adb_process)
        session = ActiveMonitorSession(
            adb_process=adb_process,
            ffmpeg_process=ffmpeg_process,
            websocket=websocket,
            loop=asyncio.get_running_loop(),
            mime_codec=mime_codec,
        )
        session.stderr_threads = tuple(
            thread
            for thread in (
                self._start_stderr_drain(adb_process.stderr, session.adb_stderr_tail, name="device-monitor-adb-stderr"),
                self._start_stderr_drain(ffmpeg_process.stderr, session.ffmpeg_stderr_tail, name="device-monitor-ffmpeg-stderr"),
            )
            if thread is not None
        )
        if not self._ensure_pipeline_started(ffmpeg_process, adb_process):
            self._terminate_process(ffmpeg_process)
            self._terminate_process(adb_process)
            self._join_threads(session.stderr_threads, timeout=0.5)
            reason, diagnostic = self._build_error_details(session, fallback_reason="exec-out 模式启动后立即退出")
            raise DeviceMonitorUnavailableError(reason, diagnostic=diagnostic)
        return session

    def _spawn_ffmpeg(self, adb_process: subprocess.Popen[bytes]) -> subprocess.Popen[bytes]:
        command = [
            self.config.ffmpeg_path,
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
        ]
        try:
            return subprocess.Popen(
                command,
                stdin=adb_process.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            self._terminate_process(adb_process)
            raise DeviceMonitorUnavailableError(f"FFmpeg executable not found: {self.config.ffmpeg_path}") from exc

    def _extract_sps(self, payload: bytes) -> bytes | None:
        start_positions: list[int] = []
        for pattern in (b"\x00\x00\x00\x01", b"\x00\x00\x01"):
            offset = 0
            while True:
                index = payload.find(pattern, offset)
                if index == -1:
                    break
                start_positions.append(index)
                offset = index + len(pattern)
        if not start_positions:
            return None
        start_positions = sorted(set(start_positions))
        for idx, start in enumerate(start_positions):
            prefix_len = 4 if payload[start : start + 4] == b"\x00\x00\x00\x01" else 3
            nal_start = start + prefix_len
            nal_end = len(payload)
            if idx + 1 < len(start_positions):
                nal_end = start_positions[idx + 1]
            nal = payload[nal_start:nal_end]
            if nal and (nal[0] & 0x1F) == 7 and len(nal) >= 4:
                return nal
        return None

    def _screenrecord_bitrate_arg(self) -> str | None:
        """Match the manual success command by default: do not pass --bit-rate unless explicitly configured as integer bps."""

        raw = (self.config.device_monitor_bitrate or "").strip()
        if not raw:
            return None
        if not raw.isdigit():
            return None
        return raw

    def _set_probe_cache(self, supported: bool, reason: str | None) -> None:
        with self._probe_cache_lock:
            self._probe_cache_value = supported
            self._probe_cache_reason = reason
            self._probe_cache_expires_at = time.monotonic() + PROBE_CACHE_TTL_SECONDS

    def _ensure_pipeline_started(self, ffmpeg_process: subprocess.Popen[bytes], adb_process: subprocess.Popen[bytes]) -> bool:
        for _ in range(10):
            if ffmpeg_process.poll() is not None or adb_process.poll() is not None:
                return False
            time.sleep(0.1)
        return True

    async def _collect_error_details(self, session: ActiveMonitorSession) -> tuple[str | None, str | None]:
        await asyncio.to_thread(self._join_threads, session.stderr_threads, 0.2)
        reason, diagnostic = self._build_error_details(session, fallback_reason=None)
        if reason:
            return reason, diagnostic
        if self._has_unexpected_process_exit(session):
            return "视频流已中断", diagnostic
        return None, None

    def _build_error_details(self, session: ActiveMonitorSession, *, fallback_reason: str | None) -> tuple[str, str | None]:
        ffmpeg_stderr = self._stderr_snapshot(session.ffmpeg_process, session.ffmpeg_stderr_tail)
        adb_stderr = self._stderr_snapshot(session.adb_process, session.adb_stderr_tail)
        diagnostic_parts = [part for part in (ffmpeg_stderr, adb_stderr) if part]
        diagnostic = self._trim_text(" | ".join(diagnostic_parts), DIAGNOSTIC_LIMIT) if diagnostic_parts else None
        preferred = ffmpeg_stderr or adb_stderr or fallback_reason
        if not preferred:
            return "", diagnostic
        return self._summarize_reason(preferred), diagnostic

    def _looks_like_mp4_container(self, payload: bytes) -> bool:
        if len(payload) < 8:
            return False
        return payload[4:8] == b"ftyp"

    def _read_stderr(self, process: subprocess.Popen[bytes]) -> str:
        if process.stderr is None:
            return ""
        try:
            return process.stderr.read().decode("utf-8", errors="ignore").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _stderr_snapshot(self, process: subprocess.Popen[bytes], tail_buffer: _TextTailBuffer) -> str:
        buffered = tail_buffer.get_text()
        if buffered or process.poll() is None:
            return buffered
        return self._read_stderr(process)

    def _start_stderr_drain(
        self,
        stream: BinaryIO | None,
        tail_buffer: _TextTailBuffer,
        *,
        name: str,
    ) -> threading.Thread | None:
        if stream is None:
            return None
        thread = threading.Thread(
            target=self._drain_stream_to_tail,
            args=(stream, tail_buffer),
            name=name,
            daemon=True,
        )
        thread.start()
        return thread

    def _drain_stream_to_tail(self, stream: BinaryIO, tail_buffer: _TextTailBuffer) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                tail_buffer.append(chunk.decode("utf-8", errors="ignore"))
        except Exception as exc:  # noqa: BLE001
            tail_buffer.append(f"[stderr drain failed: {exc}]")



    def _join_threads(self, threads: tuple[threading.Thread, ...], timeout: float) -> None:
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=timeout)

    def _has_unexpected_process_exit(self, session: ActiveMonitorSession) -> bool:
        ffmpeg_code = session.ffmpeg_process.poll()
        adb_code = session.adb_process.poll()
        return ffmpeg_code not in (None, 0) or adb_code not in (None, 0)

    def _summarize_reason(self, text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        preferred = next((line for line in lines if not line.startswith("Traceback ")), "") or text.strip() or "视频流已中断"
        return self._trim_text(preferred, ERROR_REASON_LIMIT)

    def _trim_text(self, text: str, limit: int) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3].rstrip() + "..."

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
