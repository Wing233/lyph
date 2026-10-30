from contextlib import asynccontextmanager, closing
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from . import auth, content, db, navigation, settings_routes, storage, tasks
from .config import settings


def _modules():
    # Explicit imports: missing modules are a startup error, never a silent feature bypass.
    from . import models_routes, knowledge, chat, devices, daily, operations, ui_routes, maintenance, chat_cache, chat_views, agent_protocol
    return [models_routes, knowledge, chat, devices, daily, operations, ui_routes, maintenance, chat_cache, chat_views, agent_protocol]


def init_all(*, recover_requests: bool = True) -> list:
    settings.validate()
    db.init_schema()
    for module in [auth, storage, tasks, content, settings_routes, navigation]:
        module.init_schema()
    modules = _modules()
    for module in modules:
        module.init_schema()
    from . import knowledge
    content.set_publication_callback(knowledge.sync_content)
    if recover_requests:
        from . import chat
        chat.recover_interrupted()
    return modules


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_all()
    from . import ui_routes
    await ui_routes.recover()
    if settings.scheduler_enabled:
        scheduler = tasks.start()
        from . import daily, maintenance
        daily.register_jobs(scheduler)
        maintenance.register_jobs(scheduler)
    try:
        from . import agent_protocol
        app.state.mcp_manager = agent_protocol.make_manager()
        async with app.state.mcp_manager.run():
            yield
    finally:
        await tasks.stop()


def create_app() -> FastAPI:
    application = FastAPI(title="lyph API", version="0.1.0", lifespan=lifespan)
    application.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin],
                               allow_credentials=True, allow_methods=["GET", "POST", "PUT", "DELETE"],
                               allow_headers=["Content-Type", "X-CSRF-Token", "Authorization"])
    for module in [auth, content, settings_routes, storage, tasks, navigation, *_modules()]:
        application.include_router(module.router)
        if hasattr(module, "public_router"):
            application.include_router(module.public_router)
        if hasattr(module, "device_router"):
            application.include_router(module.device_router)
    from . import agent_protocol
    agent_protocol.register(application)

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if request.url.path.startswith("/api/v1/admin") or request.url.path.startswith("/api/v1/auth"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok"}

    @application.get("/ready", include_in_schema=False)
    def ready():
        try:
            with closing(db.connect()) as conn:
                conn.execute("SELECT 1 FROM schema_migrations").fetchone()
            client = storage._client()
            if not client.bucket_exists(settings.minio_bucket):
                client.make_bucket(settings.minio_bucket)
            return {"status": "ready", "database": "ok", "object_storage": "ok"}
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)

    return application


app = create_app()
