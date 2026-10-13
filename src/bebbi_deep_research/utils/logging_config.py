"""JSON logs on stdout (one object per line)."""

import json
import logging
import sys
import time


class JsonFormatter(logging.Formatter):
    """Formats a log record as one JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a JSON line with timestamp, level, logger, message and extra fields."""
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update(getattr(record, "fields", {}))
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Send JSON logs to stdout and quiet the noisy HTTP loggers."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpx2", "httpcore", "openai"):  # one line per request is too noisy
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def log(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields) -> None:
    """Log a message with structured fields."""
    logger.log(level, msg, extra={"fields": fields})
