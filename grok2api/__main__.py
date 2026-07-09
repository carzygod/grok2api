from __future__ import annotations

import uvicorn

from .config import settings


def main() -> None:
    uvicorn.run(
        "grok2api.server:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        access_log=settings.access_log,
    )


if __name__ == "__main__":
    main()
