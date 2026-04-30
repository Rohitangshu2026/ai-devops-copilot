from elasticsearch import Elasticsearch
import logging
import datetime

es = Elasticsearch("http://localhost:9200")

def get_logger():
    logger = logging.getLogger("app_logger")
    logger.setLevel(logging.INFO)

    # 🚫 prevent duplicate handlers
    if logger.hasHandlers():
        return logger

    class ElasticHandler(logging.Handler):
        def emit(self, record):
            if isinstance(record.msg, dict):
                doc = record.msg
            else:
                doc = {"message": record.msg}

            doc.update({
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "level": record.levelname,
                "service": "sample-app"
            })

            try:
                es.index(index="app-logs", document=doc)
            except Exception as e:
                print("Elasticsearch logging failed:", e)

    logger.addHandler(ElasticHandler())

    return logger