from __future__ import annotations

import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.adb import AdbClient, AdbCommandError
from app.collab import CollabSessionService
from app.config import AppConfig
from app.db import Database
from app.keepalive_service import KeepaliveService
from app.models import (
    AuthLoginRequest,
    AuthStatusResponse,
    EsimLatestResponse,
    EsimSwitchRequest,
    EsimSwitchStatusResponse,
    HealthResponse,
    KeepaliveRuleListResponse,
    KeepaliveRuleOut,
    KeepaliveRulePatchRequest,
    KeepaliveRuleUpsertRequest,
    MonitorStatusResponse,
    SmsEventStatusResponse,
    SmsListResponse,
    SyncResponse,
)
from app.monitor import SmsMonitor
from app.sms_event_service import SmsEventService
from app.services import AppServices, EsimSyncService, SmsSyncService, utc_now_iso
from app.switch_service import EsimSwitchConflictError, EsimSwitchLockedError, EsimSwitchService


TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def build_services(config: AppConfig) -> AppServices:
    db = Database(config.db_path)
    adb_client = AdbClient(config)
    esim_service = EsimSyncService(db, adb_client)
    sms_service = SmsSyncService(db, adb_client)
    sms_event_service = SmsEventService()
    monitor = SmsMonitor(config, db, sms_service, adb_client, sms_event_service=sms_event_service)
    switch_service = EsimSwitchService(config, db, esim_service)
    collab_service = CollabSessionService(config)
    keepalive_service = KeepaliveService(config, db, esim_service, switch_service, adb_client)
    return AppServices(
        config=config,
        db=db,
        adb_client=adb_client,
        esim_service=esim_service,
        sms_service=sms_service,
        monitor=monitor,
        switch_service=switch_service,
        sms_event_service=sms_event_service,
        collab_service=collab_service,
        keepalive_service=keepalive_service,
    )


async def run_blocking(func, *args, **kwargs):
    return await run_in_threadpool(func, *args, **kwargs)


def create_app(
    config: AppConfig | None = None,
    services: AppServices | None = None,
    auto_startup: bool = True,
) -> FastAPI:
    resolved_config = config or AppConfig.from_env()
    resolved_services = services or build_services(resolved_config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = resolved_services
        app.state.config = resolved_config
        app.state.collab_stop_event = asyncio.Event()
        app.state.collab_cleanup_task = asyncio.create_task(
            resolved_services.collab_service.run_cleanup_loop(app.state.collab_stop_event)
        )
        if auto_startup:
            initialize_services(resolved_services)
        try:
            yield
        finally:
            app.state.collab_stop_event.set()
            app.state.collab_cleanup_task.cancel()
            try:
                await app.state.collab_cleanup_task
            except asyncio.CancelledError:
                pass
            if resolved_services.keepalive_service:
                resolved_services.keepalive_service.stop()
            resolved_services.monitor.stop()

    app = FastAPI(
        title="Android SMS Web API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.services = resolved_services
    app.state.config = resolved_config
    resolved_config.switch_screenshot_dir.mkdir(parents=True, exist_ok=True)
    if ASSETS_DIR.exists():
        app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")
    app.mount("/switch-screenshots", StaticFiles(directory=str(resolved_config.switch_screenshot_dir)), name="switch-screenshots")

    def is_authenticated(connection: Request | WebSocket) -> bool:
        if not resolved_config.app_password:
            return True
        cookie_value = connection.cookies.get(resolved_config.app_auth_cookie_name)
        if not cookie_value:
            return False
        return hmac.compare_digest(cookie_value, resolved_config.app_auth_cookie_value)

    def require_auth(request: Request) -> None:
        if not is_authenticated(request):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    def resolve_client_ip(connection: Request | WebSocket) -> str:
        default_ip = connection.client.host if connection.client else "unknown"
        if not resolved_config.trust_proxy_headers:
            return default_ip

        cf_connecting_ip = connection.headers.get("cf-connecting-ip")
        if cf_connecting_ip:
            return cf_connecting_ip.strip()

        forwarded = connection.headers.get("forwarded")
        if forwarded:
            first_entry = forwarded.split(",", 1)[0]
            for segment in first_entry.split(";"):
                key, _, value = segment.strip().partition("=")
                if key.lower() != "for" or not value:
                    continue
                normalized = value.strip().strip('"')
                if normalized.startswith("[") and "]" in normalized:
                    normalized = normalized[1 : normalized.index("]")]
                elif ":" in normalized and normalized.count(":") == 1:
                    normalized = normalized.split(":", 1)[0]
                return normalized

        x_forwarded_for = connection.headers.get("x-forwarded-for")
        if x_forwarded_for:
            return x_forwarded_for.split(",", 1)[0].strip()

        x_real_ip = connection.headers.get("x-real-ip")
        if x_real_ip:
            return x_real_ip.strip()

        return default_ip

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "title": "Android SMS Dashboard",
                "collab_ping_interval_ms": int(resolved_config.collab_ping_interval_seconds * 1000),
                "collab_cursor_throttle_ms": resolved_config.collab_cursor_throttle_ms,
            },
        )

    @app.post("/api/auth/login", response_model=AuthStatusResponse)
    async def auth_login(payload: AuthLoginRequest, response: Response) -> dict:
        if not resolved_config.app_password or not hmac.compare_digest(payload.password, resolved_config.app_password):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password")
        response.set_cookie(
            key=resolved_config.app_auth_cookie_name,
            value=resolved_config.app_auth_cookie_value,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return {"authenticated": True}

    @app.post("/api/auth/logout", response_model=AuthStatusResponse)
    async def auth_logout(response: Response) -> dict:
        response.delete_cookie(key=resolved_config.app_auth_cookie_name, path="/")
        return {"authenticated": False}

    @app.get("/api/auth/status", response_model=AuthStatusResponse)
    async def auth_status(request: Request) -> dict:
        return {"authenticated": is_authenticated(request)}

    @app.get("/api/health", response_model=HealthResponse)
    async def health(request: Request) -> dict:
        require_auth(request)
        runtime = get_services(request)
        adb_available = False
        adb_devices: list[str] = []
        adb_error: str | None = None
        try:
            adb_devices = await run_blocking(
                runtime.adb_client.get_devices,
                timeout=runtime.config.adb_healthcheck_timeout_seconds,
            )
            adb_available = len(adb_devices) > 0
            if not adb_devices:
                adb_error = "No connected adb devices detected"
        except Exception as exc:  # noqa: BLE001
            adb_error = str(exc)
        return {
            "ok": adb_available,
            "db_path": str(runtime.config.db_path),
            "adb_path": runtime.config.adb_path,
            "adb_available": adb_available,
            "adb_devices": adb_devices,
            "adb_error": adb_error,
            "monitor": await run_blocking(runtime.monitor.get_status),
            "last_sms_sync": await run_blocking(runtime.db.get_app_state_json, "last_sms_sync"),
            "last_esim_sync": await run_blocking(runtime.db.get_app_state_json, "last_esim_sync"),
        }

    @app.get("/api/esim/latest", response_model=EsimLatestResponse)
    async def get_latest_esim(request: Request) -> dict:
        require_auth(request)
        runtime = get_services(request)
        return await run_blocking(runtime.esim_service.latest)

    @app.post("/api/esim/switch", response_model=EsimSwitchStatusResponse)
    async def switch_esim(request: Request, payload: EsimSwitchRequest) -> dict:
        require_auth(request)
        runtime = get_services(request)
        try:
            return await run_blocking(runtime.switch_service.start_switch, payload.display_name, payload.lock_minutes)
        except EsimSwitchConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except EsimSwitchLockedError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/esim/switch/status", response_model=EsimSwitchStatusResponse)
    async def esim_switch_status(request: Request) -> dict:
        require_auth(request)
        runtime = get_services(request)
        return await run_blocking(runtime.switch_service.get_status)

    @app.get("/api/esim/switch/stream")
    async def esim_switch_stream(request: Request) -> StreamingResponse:
        require_auth(request)
        runtime = get_services(request)

        async def event_generator():
            queue = await runtime.switch_service.subscribe()
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"event: {message['event']}\n"
                        yield f"data: {json.dumps(message['data'], ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                runtime.switch_service.unsubscribe(queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.post("/api/esim/sync", response_model=SyncResponse)
    async def sync_esim(request: Request) -> dict:
        require_auth(request)
        runtime = get_services(request)
        try:
            return await run_blocking(runtime.esim_service.sync)
        except Exception as exc:  # noqa: BLE001
            await run_blocking(
                runtime.db.set_app_state,
                "last_esim_sync",
                {"ok": False, "detail": str(exc), "synced_at": utc_now_iso()},
                utc_now_iso(),
            )
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/sms", response_model=SmsListResponse)
    async def list_sms(
        request: Request,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=resolved_config.default_page_size, ge=1, le=resolved_config.max_page_size),
        address: str | None = None,
        sub_id: str | None = None,
        display_name: str | None = None,
        keyword: str | None = None,
    ) -> dict:
        require_auth(request)
        runtime = get_services(request)
        return await run_blocking(
            runtime.sms_service.list_messages,
            page=page,
            page_size=page_size,
            address=address,
            sub_id=sub_id,
            display_name=display_name,
            keyword=keyword,
        )

    @app.post("/api/sms/sync", response_model=SyncResponse)
    async def sync_sms(request: Request) -> dict:
        require_auth(request)
        runtime = get_services(request)
        try:
            return await run_blocking(runtime.sms_service.sync_all_inbox)
        except Exception as exc:  # noqa: BLE001
            await run_blocking(
                runtime.db.set_app_state,
                "last_sms_sync",
                {"ok": False, "detail": str(exc), "synced_at": utc_now_iso()},
                utc_now_iso(),
            )
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/sms/sync-all", response_model=SyncResponse)
    async def sync_all_sms_history(request: Request) -> dict:
        require_auth(request)
        runtime = get_services(request)
        try:
            return await run_blocking(runtime.sms_service.sync_all_inbox)
        except Exception as exc:  # noqa: BLE001
            await run_blocking(
                runtime.db.set_app_state,
                "last_sms_sync",
                {"ok": False, "detail": str(exc), "synced_at": utc_now_iso()},
                utc_now_iso(),
            )
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/sms/stream")
    async def sms_stream(request: Request) -> StreamingResponse:
        require_auth(request)
        runtime = get_services(request)

        async def event_generator():
            queue = await runtime.sms_event_service.subscribe()
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"event: {message['event']}\n"
                        yield f"data: {json.dumps(message['data'], ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                runtime.sms_event_service.unsubscribe(queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.get("/api/monitor/status", response_model=MonitorStatusResponse)
    async def monitor_status(request: Request) -> dict:
        require_auth(request)
        runtime = get_services(request)
        return await run_blocking(runtime.monitor.get_status)

    @app.get("/api/keepalive/rules", response_model=KeepaliveRuleListResponse)
    async def list_keepalive_rules(request: Request) -> dict:
        require_auth(request)
        runtime = get_services(request)
        return {"items": await run_blocking(runtime.keepalive_service.list_rules)}

    @app.put("/api/keepalive/rules/{sub_id}", response_model=KeepaliveRuleOut)
    async def upsert_keepalive_rule(request: Request, sub_id: str, payload: KeepaliveRuleUpsertRequest) -> dict:
        require_auth(request)
        runtime = get_services(request)
        latest = await run_blocking(runtime.esim_service.latest)
        target = next(
            (item for item in latest.get("subscriptions", []) if item.get("sub_id") == sub_id and item.get("is_embedded")),
            None,
        )
        if target is None:
            raise HTTPException(status_code=404, detail="Target eSIM not found")
        return await run_blocking(
            runtime.keepalive_service.upsert_rule,
            sub_id,
            display_name=payload.display_name or target.get("display_name"),
            carrier_name=payload.carrier_name or target.get("carrier_name"),
            timezone_name=payload.timezone_name,
            target_phone=payload.target_phone,
            interval_days=payload.interval_days,
            enabled=payload.enabled,
        )

    @app.patch("/api/keepalive/rules/{sub_id}", response_model=KeepaliveRuleOut)
    async def patch_keepalive_rule(request: Request, sub_id: str, payload: KeepaliveRulePatchRequest) -> dict:
        require_auth(request)
        runtime = get_services(request)
        result = await run_blocking(
            runtime.keepalive_service.patch_rule,
            sub_id,
            timezone_name=payload.timezone_name,
            target_phone=payload.target_phone,
            interval_days=payload.interval_days,
            enabled=payload.enabled,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Keepalive rule not found")
        return result

    @app.delete("/api/keepalive/rules/{sub_id}")
    async def delete_keepalive_rule(request: Request, sub_id: str) -> dict:
        require_auth(request)
        runtime = get_services(request)
        deleted = await run_blocking(runtime.keepalive_service.delete_rule, sub_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Keepalive rule not found")
        return {"ok": True}

    @app.post("/api/keepalive/rules/{sub_id}/test", response_model=KeepaliveRuleOut)
    async def test_keepalive_rule(request: Request, sub_id: str) -> dict:
        require_auth(request)
        runtime = get_services(request)
        try:
            return await run_blocking(runtime.keepalive_service.run_rule_test, sub_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Keepalive rule not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.websocket("/api/collab/ws")
    async def collab_ws(websocket: WebSocket) -> None:
        if not is_authenticated(websocket):
            await websocket.close(code=4401, reason="Authentication required")
            return

        await websocket.accept()
        runtime = websocket.app.state.services
        participant_id: str | None = None
        try:
            join_message = await websocket.receive_json()
            if join_message.get("type") != "join":
                await websocket.close(code=4400, reason="First message must be join")
                return

            name = str(join_message.get("name", "")).strip()
            if not name:
                await websocket.close(code=4400, reason="Participant name is required")
                return

            snapshot = await runtime.collab_service.join(websocket, name, resolve_client_ip(websocket))
            participant_id = snapshot["self_id"]
            await websocket.send_json(snapshot)

            while True:
                message = await websocket.receive_json()
                message_type = message.get("type")
                if message_type == "ping":
                    await runtime.collab_service.heartbeat(participant_id)
                    continue
                if message_type == "cursor":
                    await runtime.collab_service.update_cursor(
                        participant_id,
                        message.get("x_ratio"),
                        message.get("y_ratio"),
                    )
                    continue
                await websocket.close(code=4400, reason="Unsupported message type")
                return
        except WebSocketDisconnect:
            pass
        finally:
            if participant_id:
                await runtime.collab_service.disconnect(participant_id)

    return app


def initialize_services(services: AppServices) -> None:
    """Startup path that initializes the database, syncs data, and starts monitoring."""

    services.db.init_schema()
    services.switch_service.restore_state()
    services.db.set_app_state(
        "startup",
        {"ok": True, "detail": "Service startup initialized", "started_at": utc_now_iso()},
        utc_now_iso(),
    )
    try:
        services.esim_service.sync()
    except Exception as exc:  # noqa: BLE001
        services.db.set_app_state(
            "last_esim_sync",
            {"ok": False, "detail": str(exc), "synced_at": utc_now_iso()},
            utc_now_iso(),
        )
    try:
        services.sms_service.sync_all_inbox()
    except Exception as exc:  # noqa: BLE001
        services.db.set_app_state(
            "last_sms_sync",
            {"ok": False, "detail": str(exc), "synced_at": utc_now_iso()},
            utc_now_iso(),
        )
    services.monitor.start()
    if services.keepalive_service:
        services.keepalive_service.start()


def get_services(request: Request) -> AppServices:
    return request.app.state.services


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
