import json
import logging
import sys
from datetime import datetime, timezone


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        if isinstance(record.msg, dict):
            data = dict(record.msg)
        else:
            data = {"message": record.getMessage()}
        data.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        data.setdefault("level", record.levelname)
        data.setdefault("service", "agent-backend")
        return json.dumps(data)


def get_logger(name: str = "agent") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.hasHandlers():
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    return logger
