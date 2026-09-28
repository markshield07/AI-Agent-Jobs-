"""Application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from jobagent.api.routes import router
from jobagent.config import Settings, get_settings
from jobagent.db.database import open_database


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = open_database(settings.db_path)
        try:
            yield
        finally:
            app.state.db.close()

    app = FastAPI(title="Job Agent", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    return app


def run() -> None:
    import uvicorn

    uvicorn.run("jobagent.main:create_app", factory=True, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    run()
