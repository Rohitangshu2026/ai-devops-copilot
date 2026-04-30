from fastapi import FastAPI
from logger import get_logger

app = FastAPI()
logger = get_logger()

@app.get("/")
def root():
    logger.info({
        "event": "root_hit",
        "endpoint": "/",
        "status": 200
    })
    return {"message": "Version 2 deployed 🚀"}

@app.get("/health")
def health():
    logger.info({
        "event": "health_check",
        "endpoint": "/health",
        "status": 200
    })
    return {"status": "ok"}

@app.get("/error")
def error():
    try:
        raise Exception("Simulated failure for testing")
    except Exception as e:
        logger.error({
            "event": "error",
            "endpoint": "/error",
            "error": str(e),
            "status": 500
        })
        return {"status": "error", "message": str(e)}