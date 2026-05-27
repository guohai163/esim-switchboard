from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

from app.config import AppConfig
from app.models import CollabCursorOut, CollabParticipantOut


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clamp_ratio(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 0.0
    return max(0.0, min(1.0, numeric))


@dataclass(slots=True)
class ParticipantSession:
    """Tracks one live browser tab in the in-memory collaboration session."""

    id: str
    name: str
    ip: str
    websocket: WebSocket
    connected_at: str
    last_seen_at: str
    last_seen_monotonic: float
    cursor_x_ratio: float | None = None
    cursor_y_ratio: float | None = None
    cursor_updated_at: str | None = None


class CollabSessionService:
    """Keeps track of online participants and broadcasts cursor/presence updates."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._participants: dict[str, ParticipantSession] = {}
        self._lock = asyncio.Lock()

    async def join(self, websocket: WebSocket, name: str, ip: str) -> dict[str, Any]:
        """Register a new participant and broadcast the updated presence list."""

        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Participant name is required")

        now_iso = utc_now_iso()
        now_monotonic = asyncio.get_running_loop().time()
        participant = ParticipantSession(
            id=uuid.uuid4().hex,
            name=normalized_name[:80],
            ip=ip,
            websocket=websocket,
            connected_at=now_iso,
            last_seen_at=now_iso,
            last_seen_monotonic=now_monotonic,
        )
        async with self._lock:
            self._participants[participant.id] = participant
            snapshot = self._snapshot_payload_locked(participant.id)
            recipients, presence_payload = self._presence_recipients_locked(exclude_id=participant.id)

        await self._broadcast(recipients, presence_payload)
        return snapshot

    async def heartbeat(self, participant_id: str) -> None:
        """Refresh the participant heartbeat without changing visible state."""

        async with self._lock:
            participant = self._participants.get(participant_id)
            if participant is None:
                return
            self._touch_locked(participant)

    async def update_cursor(self, participant_id: str, x_ratio: Any, y_ratio: Any) -> None:
        """Store the latest cursor position and broadcast it to every other participant."""

        async with self._lock:
            participant = self._participants.get(participant_id)
            if participant is None:
                return
            self._touch_locked(participant)
            participant.cursor_x_ratio = clamp_ratio(x_ratio)
            participant.cursor_y_ratio = clamp_ratio(y_ratio)
            participant.cursor_updated_at = utc_now_iso()
            recipients = self._recipient_websockets_locked(exclude_id=participant.id)
            payload = {
                "type": "cursor",
                "participant": self._serialize_participant(participant),
            }

        await self._broadcast(recipients, payload)

    async def disconnect(self, participant_id: str) -> None:
        """Remove a participant immediately after the websocket closes."""

        removed_payloads = await self._remove_participants({participant_id})
        await self._broadcast_removals(removed_payloads)

    async def run_cleanup_loop(self, stop_event: asyncio.Event) -> None:
        """Prune stale participants so dead tabs disappear even without a clean close."""

        interval = max(0.5, self.config.collab_ping_interval_seconds)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                removed_payloads = await self._remove_expired_participants()
                await self._broadcast_removals(removed_payloads)

    async def _remove_expired_participants(self) -> list[tuple[list[WebSocket], dict[str, Any]]]:
        deadline = asyncio.get_running_loop().time() - max(0.1, self.config.collab_presence_timeout_seconds)
        async with self._lock:
            expired_ids = {
                participant_id
                for participant_id, participant in self._participants.items()
                if participant.last_seen_monotonic <= deadline
            }
        return await self._remove_participants(expired_ids)

    async def _remove_participants(self, participant_ids: set[str]) -> list[tuple[list[WebSocket], dict[str, Any]]]:
        if not participant_ids:
            return []

        async with self._lock:
            removed_ids: list[str] = []
            for participant_id in participant_ids:
                if self._participants.pop(participant_id, None) is not None:
                    removed_ids.append(participant_id)
            if not removed_ids:
                return []

            recipients = self._recipient_websockets_locked()
            payloads = [
                (recipients, {"type": "remove", "participant_id": participant_id, "online_count": len(self._participants)})
                for participant_id in removed_ids
            ]
            payloads.append((recipients, self._presence_payload_locked()))
            return payloads

    async def _broadcast_removals(self, payloads: list[tuple[list[WebSocket], dict[str, Any]]]) -> None:
        for recipients, payload in payloads:
            await self._broadcast(recipients, payload)

    async def _broadcast(self, recipients: list[WebSocket], payload: dict[str, Any]) -> None:
        for websocket in recipients:
            try:
                await websocket.send_json(payload)
            except Exception:  # noqa: BLE001
                continue

    def _touch_locked(self, participant: ParticipantSession) -> None:
        now_iso = utc_now_iso()
        participant.last_seen_at = now_iso
        participant.last_seen_monotonic = asyncio.get_running_loop().time()

    def _snapshot_payload_locked(self, self_id: str) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "self_id": self_id,
            "participants": [self._serialize_participant(participant) for participant in self._participants.values()],
            "online_count": len(self._participants),
        }

    def _presence_payload_locked(self) -> dict[str, Any]:
        return {
            "type": "presence",
            "participants": [self._serialize_participant(participant) for participant in self._participants.values()],
            "online_count": len(self._participants),
        }

    def _presence_recipients_locked(self, exclude_id: str | None = None) -> tuple[list[WebSocket], dict[str, Any]]:
        recipients = self._recipient_websockets_locked(exclude_id=exclude_id)
        return recipients, self._presence_payload_locked()

    def _recipient_websockets_locked(self, exclude_id: str | None = None) -> list[WebSocket]:
        return [
            participant.websocket
            for participant_id, participant in self._participants.items()
            if participant_id != exclude_id
        ]

    def _serialize_participant(self, participant: ParticipantSession) -> dict[str, Any]:
        cursor = None
        if (
            participant.cursor_x_ratio is not None
            and participant.cursor_y_ratio is not None
            and participant.cursor_updated_at is not None
        ):
            cursor = CollabCursorOut(
                x_ratio=participant.cursor_x_ratio,
                y_ratio=participant.cursor_y_ratio,
                updated_at=participant.cursor_updated_at,
            ).model_dump()

        return CollabParticipantOut(
            id=participant.id,
            name=participant.name,
            ip=participant.ip,
            connected_at=participant.connected_at,
            last_seen_at=participant.last_seen_at,
            cursor=cursor,
        ).model_dump()
