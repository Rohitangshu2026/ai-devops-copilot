from fastapi import FastAPI
from logger import get_logger

app = FastAPI()
logger = get_logger()

@app.get("/")
def root():
    logger.info("Root endpoint hit")
    return {"message": "AI DevOps Copilot is running"}

@app.get("/health")
def health():
    logger.info("Health check OK")
    return {"status": "ok"}

@app.get("/error")
def error():
    try:
        raise Exception("Simulated failure for testing")
    except Exception as e:
        logger.error(f"Error occurred: {str(e)}")
        return {"status": "error", "message": str(e)}