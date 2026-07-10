"""حلقة التنفيذ الذكية: محاولات محدودة، كشف الوعود الفارغة، تنفيذ الأدوات، وتبديل تلقائي عند الفشل التقني."""
import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from g4f.client import AsyncClient
from core.config import settings
from models.openai_schemas import ChatCompletionRequest, ToolDef
from services.model_registry import registry
from services.prompts import build_agent_directive, build_agent_reminder
from services.tool_processor import normalize_tool_calls, validate_call

logger = logging.getLogger("gateway.agent")

# أفعال مستقبلية تدل على وعد فارغ (عربي/إنجليزي)
_HEDGING = re.compile(
    r"(سأقوم|سأفعل|سأتحقق|سأبحث|سوف أ|دعني|لحظة من فضلك|i will|i'll|let me|going to|one moment)",
    re.IGNORECASE,
)
_FINAL_MARKERS = re.compile(r"(الخلاصة|النتيجة النهائية|باختصار|in summary|final answer|to conclude)", re.IGNORECASE)

# سجل الأدوات المنفذة على الخادم: name -> coroutine(arguments) -> str
ToolExecutor = Callable[[dict], Awaitable[str]]
SERVER_TOOLS: dict[str, ToolExecutor] = {}


def is_hedging(text: str) -> bool:
    """رد قصير (< 250 حرفاً) يحتوي أفعالاً مستقبلية → وعد فارغ."""
    return len(text.strip()) < 250 and bool(_HEDGING.search(text))


def is_final(text: str) -> bool:
    """رد نهائي: نص طويل أو يحتوي كلمات ختامية."""
    return len(text.strip()) >= 250 or bool(_FINAL_MARKERS.search(text))


class AgentRunner:
    """يدير حلقة while مع عداد محاولات، توجيه مزدوج، وتبديل تلقائي بين النماذج عند الفشل التقني."""

    def __init__(self, client: AsyncClient):
        self.client = client

    def _prepare_messages(self, request: ChatCompletionRequest, strict: bool) -> list[dict]:
        """حقن التوجيه الكامل في البداية والتذكير المكثف قرب النهاية."""
        msgs = [m.model_dump(exclude_none=True) for m in request.messages]
        tools = request.tools or []
        if tools:
            directive = build_agent_directive(tools)
            if not any(m["role"] == "system" and "القواعد الذهبية" in (m.get("content") or "") for m in msgs):
                msgs.insert(0, {"role": "system", "content": directive})
            msgs.append({"role": "system", "content": build_agent_reminder(tools, strict=strict)})
        return msgs

    def _next_healthy_model(self, exclude: list[str]) -> Optional[str]:
        """يبحث عن نموذج صحي بديل لم يُجرَّب بعد في هذا الطلب."""
        for name in registry.healthy_models():
            if name not in exclude:
                return name
        return None

    async def run(self, request: ChatCompletionRequest) -> tuple[str, list, str]:
        """
        نقطة الدخول العامة. تفرض سقفاً زمنياً كلياً (GLOBAL_REQUEST_BUDGET) على الطلب
        بأكمله بغض النظر عن عدد المحاولات أو التبديلات بين النماذج، لضمان ألا يتجاوز
        أي طلب هذه المهلة مهما حصل داخلياً.
        """
        try:
            return await asyncio.wait_for(
                self._run_inner(request),
                timeout=settings.GLOBAL_REQUEST_BUDGET,
            )
        except asyncio.TimeoutError:
            logger.error("تجاوز الطلب السقف الزمني الكلي (%ss)", settings.GLOBAL_REQUEST_BUDGET)
            return "انتهت مهلة الطلب الكلية، حاول مرة أخرى أو اختر نموذجاً آخر.", [], "stop"

    async def _run_inner(self, request: ChatCompletionRequest) -> tuple[str, list, str]:
        """
        حلقة التنفيذ الفعلية. تُرجع (المحتوى النهائي, استدعاءات أدوات للعميل, finish_reason).
        """
        model = request.model or settings.DEFAULT_MODEL
        attempts = 0
        hedge_attempts = 0
        strict = False
        working_messages: Optional[list[dict]] = None
        tried_models = [model]
        content = ""

        while attempts < settings.MAX_TOOL_RETRIES:
            attempts += 1
            messages = working_messages or self._prepare_messages(request, strict)
            logger.info("محاولة %d/%d على النموذج %s", attempts, settings.MAX_TOOL_RETRIES, model)

            try:
                resp = await asyncio.wait_for(
                    self.client.chat.completions.create(model=model, messages=messages),
                    timeout=settings.REQUEST_TIMEOUT,
                )
            except Exception as exc:
                # فشل تقني (مهلة/خطأ من المزود) وليس "وعداً فارغاً" -> لا داعي لإعادة
                # المحاولة على نفس النموذج المعطوب، بل التبديل الفوري لنموذج صحي آخر.
                logger.warning("فشل تقني على النموذج %s: %s", model, exc)
                fallback = self._next_healthy_model(exclude=tried_models)
                if fallback and len(tried_models) <= settings.MAX_FALLBACK_MODELS:
                    logger.info("التبديل إلى نموذج بديل صحي: %s", fallback)
                    tried_models.append(fallback)
                    model = fallback
                    working_messages = None
                    attempts -= 1  # التبديل بسبب عطل تقني لا يُحتسب من محاولات المحتوى
                    continue
                return "تعذر الوصول إلى أي نموذج متاح حالياً، حاول لاحقاً.", [], "stop"

            message = resp.choices[0].message
            content = message.content or ""

            # 1) استدعاءات أدوات؟
            calls = [validate_call(c, request.tools or []) for c in normalize_tool_calls(message)]
            valid_calls = [c for c in calls if c.valid]
            invalid_calls = [c for c in calls if not c.valid]

            for bad in invalid_calls:
                logger.warning("استدعاء مرفوض '%s': %s", bad.name, bad.rejection_reason)

            if valid_calls:
                server_side = [c for c in valid_calls if c.name in SERVER_TOOLS]
                if server_side:
                    # تنفيذ على الخادم وإضافة النتائج إلى السياق ثم الاستمرار
                    working_messages = messages + [
                        {"role": "assistant", "content": content, "tool_calls": [c.to_openai() for c in server_side]}
                    ]
                    for call in server_side:
                        try:
                            result = await SERVER_TOOLS[call.name](call.arguments)
                        except Exception as exc:
                            result = json.dumps({"error": str(exc)}, ensure_ascii=False)
                        logger.info("نُفذت الأداة %s", call.name)
                        working_messages.append(
                            {"role": "tool", "tool_call_id": call.id, "content": result}
                        )
                    continue
                # أدوات يديرها العميل → إرجاعها بصيغة OpenAI
                return content, [c.to_openai() for c in valid_calls], "tool_calls"

            # 2) وعد فارغ؟ → إعادة محاولة واحدة فقط (عداد منفصل عن الفشل التقني) مع تذكير حاد
            if is_hedging(content) and hedge_attempts < settings.MAX_HEDGE_RETRIES:
                logger.warning("رُصد وعد فارغ، إعادة المحاولة بتذكير حاد")
                hedge_attempts += 1
                strict = True
                working_messages = (working_messages or messages) + [
                    {"role": "system", "content": build_agent_reminder(request.tools or [], strict=True)}
                ]
                continue

            # 3) رد نهائي → كسر الحلقة
            if is_final(content) or attempts >= settings.MAX_TOOL_RETRIES:
                return content, [], "stop"

            strict = True
            working_messages = None

        return content, [], "stop"