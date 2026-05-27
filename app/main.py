from __future__ import annotations

import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.adb import AdbClient, AdbCommandError
from app.config import AppConfig
from app.db import Database
from app.models import (
    AuthLoginRequest,
    AuthStatusResponse,
    EsimLatestResponse,
    EsimSwitchRequest,
    EsimSwitchStatusResponse,
    HealthResponse,
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
    return AppServices(
        config=config,
        db=db,
        adb_client=adb_client,
        esim_service=esim_service,
        sms_service=sms_service,
        monitor=monitor,
        switch_service=switch_service,
        sms_event_service=sms_event_service,
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
        if auto_startup:
            initialize_services(resolved_services)
        try:
            yield
        finally:
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

    def is_authenticated(request: Request) -> bool:
        if not resolved_config.app_password:
            return True
        cookie_value = request.cookies.get(resolved_config.app_auth_cookie_name)
        if not cookie_value:
            return False
        return hmac.compare_digest(cookie_value, resolved_config.app_auth_cookie_value)

    def require_auth(request: Request) -> None:
        if not is_authenticated(request):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={"title": "Android SMS Dashboard"},
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


def get_services(request: Request) -> AppServices:
    return request.app.state.services


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
