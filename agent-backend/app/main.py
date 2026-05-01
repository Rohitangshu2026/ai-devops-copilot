from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.services.elk_service import close_client
from app.utils.logger import get_logger

logger = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info({"message": "agent_backend_starting"})
    yield
    await close_client()
    logger.info({"message": "agent_backend_stopped"})


app = FastAPI(title="AI DevOps Copilot", version="0.1.0", lifespan=lifespan)
app.include_router(router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok"}
