import json
import logging
import os
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
        data.setdefault("service", "sample-app")
        return json.dumps(data)


def get_logger():
    logger = logging.getLogger("app")
    if logger.hasHandlers():
        return logger

    logger.setLevel(logging.INFO)
    fmt = _JsonFormatter()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    logger.addHandler(stdout_handler)

    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.FileHandler(os.path.join(log_dir, "app.log"))
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
