from contextlib import asynccontextmanager
import asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from .config import Settings
from .database import Database
from .store import Store
from .maintenance import maintenance_loop


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    database = Database(settings.database_path)
    store = Store(settings.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await database.initialize()
        await asyncio.to_thread(store.initialize)
        task = asyncio.create_task(maintenance_loop(settings, store))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.store = store

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            same_origin = str(request.base_url).rstrip("/")
            allowed = {value.strip().rstrip("/") for value in settings.allowed_origins.split(",")}
            if origin and origin.rstrip("/") not in allowed | {same_origin}:
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "不允许跨站提交"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.app_name}

    # Feature routers must be registered before the SPA fallback.
    from .routes import router
    app.include_router(router, prefix="/api/v1")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def frontend(full_path: str, request: Request):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(404, "API not found")
        root = settings.static_dir.resolve()
        candidate = (root / full_path).resolve()
        if not candidate.is_relative_to(root):
            raise HTTPException(404, "File not found")
        if candidate.is_file():
            return FileResponse(candidate)
        if candidate.suffix or full_path.startswith(("assets/", "examples/")):
            raise HTTPException(404, "File not found")
        index = root / "index.html"
        if index.is_file() and (not full_path or "text/html" in request.headers.get("accept", "")):
            return FileResponse(index)
        raise HTTPException(404, "Page not found; build the frontend first")

    return app


app = create_app()
