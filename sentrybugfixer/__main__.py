"""Run the server: python -m sentrybugfixer"""

import uvicorn

from .config import settings


def main() -> None:
    settings.ensure_dirs()
    uvicorn.run("sentrybugfixer.app:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
