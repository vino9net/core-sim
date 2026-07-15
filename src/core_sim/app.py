"""Litestar app factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from litestar import Litestar
from litestar.di import Provide

from core_sim.api import provide_engine, router
from core_sim.config import Settings
from core_sim.engines import build_engine
from core_sim.logging import configure_logging, get_logger, litestar_logging_config

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> Litestar:
    s = settings or Settings.from_env()
    configure_logging(level=s.log_level, json_logs=s.log_json)

    @asynccontextmanager
    async def lifespan(app: Litestar) -> AsyncGenerator[None]:
        engine = build_engine(s)
        await engine.start()
        app.state.engine = engine
        app.state.settings = s
        log.info("app.started", engine=s.engine, stream=s.stream_key, port=s.port)
        try:
            yield
        finally:
            await engine.close()
            log.info("app.stopped")

    middleware = []
    if s.log_requests:
        # Opt-in only. ~10-50µs/request, which at 100k tps is the dominant term in the
        # thing we are trying to measure. Debug aid, never on during a run.
        from litestar.middleware.logging import LoggingMiddlewareConfig

        middleware.append(LoggingMiddlewareConfig().middleware)

    return Litestar(
        route_handlers=[router],
        dependencies={"engine": Provide(provide_engine)},
        lifespan=[lifespan],
        logging_config=litestar_logging_config(s.log_level),
        middleware=middleware,
        debug=False,
    )


def app() -> Litestar:
    """Factory target: ``uvicorn core_sim.app:app --factory``."""
    return create_app()
