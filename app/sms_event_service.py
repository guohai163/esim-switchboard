from __future__ import annotations

import asyncio
import threading
from typing import Any


class SmsEventService:
    """Broadcasts newly inserted SMS events to subscribed clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._latest_event: dict[str, Any] = {
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

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._latest_event)

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        await queue.put({"event": "snapshot", "data": self.get_status()})
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def broadcast_new_sms(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._latest_event = {
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
            dead_queues: list[asyncio.Queue[dict[str, Any]]] = []
            for queue in self._subscribers:
                try:
                    queue.put_nowait({"event": "new_sms", "data": dict(self._latest_event)})
                except RuntimeError:
                    dead_queues.append(queue)
            for queue in dead_queues:
                self._subscribers.discard(queue)
