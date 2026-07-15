"""Server entrypoint: ``uv run core-sim``.

Granian is the faster alternative (ARCH_DESIGN.md D2); uvicorn+uvloop is here because
it is one less moving part for a scaffold. Swap once the rig is producing numbers::

    uv run granian --interface asgi core_sim.app:app --factory
"""

from __future__ import annotations

import uvicorn

from core_sim.config import Settings
from core_sim.logging import configure_logging, get_logger

log = get_logger(__name__)


def main() -> None:
    s = Settings.from_env()
    configure_logging(level=s.log_level, json_logs=s.log_json)
    log.info("server.starting", host=s.host, port=s.port, engine=s.engine)
    uvicorn.run(
        "core_sim.app:app",
        factory=True,
        host=s.host,
        port=s.port,
        loop="uvloop",
        http="httptools",
        access_log=False,  # see config.log_requests
        log_level=s.log_level.lower(),
    )


if __name__ == "__main__":
    main()
