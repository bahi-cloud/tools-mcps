"""اكتشاف النماذج ديناميكياً، فحصها دورياً، وإدارة صحتها مع لقطة على القرص."""
import asyncio
import json
import logging
import time
from enum import Enum
from pathlib import Path

import g4f.models
from g4f.client import AsyncClient
from core.config import settings

logger = logging.getLogger("gateway.registry")


class FailureKind(str, Enum):
    UNSUPPORTED = "unsupported"   # النموذج غير مدعوم نهائياً
    TEMPORARY = "temporary"       # خطأ شبكة/مزود مؤقت
    RATE_LIMIT = "rate_limit"     # تجاوز حد المعدل


def classify_error(exc: Exception) -> FailureKind:
    """يصنف الاستثناء إلى فئة فشل قابلة للتنفيذ."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(k in text for k in ("rate", "429", "quota", "too many")):
        return FailureKind.RATE_LIMIT
    if any(k in text for k in ("not supported", "no provider", "modelnotfound", "404")):
        return FailureKind.UNSUPPORTED
    return FailureKind.TEMPORARY


class ModelHealth:
    """آلة حالة: فشلان متتاليان → غير صحي، نجاحان متتاليان → صحي."""

    def __init__(self, name: str):
        self.name = name
        self.healthy = True
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.last_error: str | None = None
        self.last_checked: float = 0.0

    def record_success(self):
        self.consecutive_successes += 1
        self.consecutive_failures = 0
        if self.consecutive_successes >= 2:
            self.healthy = True
        self.last_checked = time.time()

    def record_failure(self, kind: FailureKind, detail: str):
        self.consecutive_failures += 1
        self.consecutive_successes = 0
        self.last_error = f"{kind.value}: {detail[:200]}"
        if kind == FailureKind.UNSUPPORTED or self.consecutive_failures >= 2:
            self.healthy = False
        self.last_checked = time.time()


class ModelRegistry:
    """سجل النماذج: اكتشاف، فحص، لقطات."""

    def __init__(self):
        self.models: dict[str, ModelHealth] = {}
        self.client = AsyncClient()
        self._lock = asyncio.Lock()

    def discover(self) -> list[str]:
        """اكتشاف ديناميكي عبر g4f.models.ModelUtils."""
        names = sorted(g4f.models.ModelUtils.convert.keys())
        for name in names:
            self.models.setdefault(name, ModelHealth(name))
        logger.info("تم اكتشاف %d نموذجاً", len(names))
        return names

    async def probe(self, name: str) -> bool:
        """فحص صحي لنموذج واحد برسالة قصيرة."""
        health = self.models.setdefault(name, ModelHealth(name))
        try:
            resp = await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=name, messages=[{"role": "user", "content": "ping"}], max_tokens=8
                ),
                timeout=30,
            )
            if resp.choices and resp.choices[0].message.content:
                health.record_success()
                return True
            raise RuntimeError("empty response")
        except Exception as exc:
            kind = classify_error(exc)
            health.record_failure(kind, str(exc))
            logger.debug("فشل فحص %s (%s)", name, kind.value)
            return False

    async def scan_all(self, concurrency: int = 8):
        """فحص كل النماذج المكتشفة بتوازٍ محدود ثم حفظ اللقطة."""
        self.discover()
        sem = asyncio.Semaphore(concurrency)

        async def _bounded(n: str):
            async with sem:
                await self.probe(n)

        await asyncio.gather(*(_bounded(n) for n in self.models))
        healthy = self.healthy_models()
        logger.info("الفحص اكتمل: %d/%d نموذجاً صحياً", len(healthy), len(self.models))
        self.save_snapshot()

    def healthy_models(self) -> list[str]:
        return [n for n, h in self.models.items() if h.healthy]

    def save_snapshot(self):
        """حفظ لقطة النماذج النشطة على القرص."""
        snapshot = {
            "timestamp": time.time(),
            "models": {
                n: {"healthy": h.healthy, "last_error": h.last_error, "last_checked": h.last_checked}
                for n, h in self.models.items()
            },
        }
        Path(settings.SNAPSHOT_PATH).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
        logger.info("تم حفظ اللقطة في %s", settings.SNAPSHOT_PATH)

    def load_snapshot(self) -> bool:
        """استعادة اللقطة إن وُجدت (لتسريع الإقلاع)."""
        path = Path(settings.SNAPSHOT_PATH)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text())
            for name, info in data.get("models", {}).items():
                h = self.models.setdefault(name, ModelHealth(name))
                h.healthy = info.get("healthy", True)
                h.last_error = info.get("last_error")
            logger.info("تم تحميل لقطة بـ %d نموذجاً", len(data.get("models", {})))
            return True
        except Exception:
            logger.exception("تعذر تحميل اللقطة")
            return False

    async def periodic_scan(self):
        """مهمة خلفية للفحص الدوري."""
        interval = settings.SCAN_INTERVAL_HOURS * 3600
        while True:
            await asyncio.sleep(interval)
            logger.info("بدء الفحص الدوري للنماذج")
            await self.scan_all()


registry = ModelRegistry()
