import logging
import json
import sys

class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "service": "sample-app",
            "level": record.levelname,
            "message": record.getMessage(),
        }
        return json.dumps(log_record)

def get_logger():
    logger = logging.getLogger("app_logger")
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    logger.handlers = []
    logger.addHandler(handler)

    return logger