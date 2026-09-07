import json
import logging
import sys
from datetime import UTC, datetime

ALLOWED = {
    "correlation_id",
    "original_correlation_id",
    "conversation_id",
    "message_id",
    "quote_operation_id",
    "handoff_id",
    "old_status",
    "new_status",
    "attempt",
    "elapsed_ms",
    "upstream_status",
    "outcome",
    "error_code",
}


def configure_logging():
    # Dependencies must never print request bodies, headers, URLs or exception inputs.
    logging.getLogger().handlers.clear()
    logging.getLogger().addHandler(logging.NullHandler())
    for name in (
        "httpx",
        "httpcore",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "google",
        "google.genai",
    ):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        logger.disabled = True
    logger = logging.getLogger("agent.events")
    logger.handlers.clear()
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.setLevel(logging.INFO)
    logger.propagate = False


class Events:
    def __init__(self, settings):
        self.settings = settings

    def __call__(self, event, **fields):
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": "INFO",
            "event": event,
            "environment": self.settings.app_env,
            "app_version": self.settings.app_version,
            "git_sha": self.settings.git_sha,
        }
        record.update(
            {k: v for k, v in fields.items() if k in ALLOWED and v is not None}
        )
        logging.getLogger("agent.events").info(json.dumps(record, ensure_ascii=False))
