"""نقطة تشغيل البوابة."""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.config import settings
from services.model_registry import registry
from api.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """دورة الحياة: تحميل اللقطة، فحص الإقلاع، جدولة الفحص الدوري."""
    registry.discover()
    registry.load_snapshot()
    scan_task = None
    if settings.SCAN_ON_STARTUP:
        asyncio.create_task(registry.scan_all())
    scan_task = asyncio.create_task(registry.periodic_scan())
    logger.info("البوابة جاهزة على /v1/chat/completions")
    yield
    if scan_task:
        scan_task.cancel()


app = FastAPI(title="G4F OpenAI-Compatible Gateway", version="1.0.0", lifespan=lifespan)
app.include_router(router)
