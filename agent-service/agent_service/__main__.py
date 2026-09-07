import sys

import uvicorn
from pydantic import ValidationError

from .api import create_app
from .config import Settings
from .events import configure_logging


def main():
    configure_logging()
    try:
        settings = Settings()
    except ValidationError:
        print(
            '{"event":"startup_failed","error_code":"invalid_configuration"}',
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=8080,
        workers=1,
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=22,
    )


if __name__ == "__main__":
    main()
