"""
حلقة التنفيذ الذكية: محاولات محدودة، بروتوكول JSON صارم، تنفيذ الأدوات، وتبديل تلقائي للنموذج.
"""
import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Optional

from g4f.client import AsyncClient

from core.config import settings
from models.openai_schemas import ChatCompletionRequest, ToolDef
from services.prompts import build_agent_directive, build_agent_reminder
from services.tool_processor import normalize_tool_calls, validate_call, extract_final_answer

logger = logging.getLogger("gateway.agent")

# --- إضافة: حد أقصى لعدد تبديلات النموذج عند الفشل التقني، حتى لا تتراكم عدة
# انتظارات كاملة لـ REQUEST_TIMEOUT قبل الوصول لسقف الـ120 ثانية. ثابت محلي هنا
# (وليس في settings) حتى لا يتعطل الملف إن لم تُضِف الحقل هناك.
MAX_MODEL_SWITCHES = 2

# --- أدوات الخادم (تُملأ من tool_executor.py) ---
ToolExecutor = Callable[[dict], Awaitable[str]]
SERVER_TOOLS: dict[str, ToolExecutor] = {}

def register_tool(name: str, executor: ToolExecutor) -> None:
    """تسجيل أداة في قاموس SERVER_TOOLS."""
    SERVER_TOOLS[name] = executor

# --- Fallback Heuristics (عند تعطيل JSON الصارم) ---
_HEDGING = re.compile(
    r"(سأقوم|سأفعل|سأتحقق|سأبحث|سوف أ|دعني|لحظة من فضلك|i will|i'll|let me|going to|one moment)",
    re.IGNORECASE,
)
_FINAL_MARKERS = re.compile(r"(الخلاصة|النتيجة النهائية|باختصار|in summary|final answer|to conclude)", re.IGNORECASE)

def is_hedging(text: str) -> bool:
    return len(text.strip()) < 250 and bool(_HEDGING.search(text))

def is_final(text: str) -> bool:
    return len(text.strip()) >= 250 or bool(_FINAL_MARKERS.search(text))

# --- توجيه JSON الصارم (يُحقن في نهاية الرسائل) ---
_STRICT_ENVELOPE_NOTICE = {
    "role": "system",
    "content": (
        "تعليمة إلزامية بخصوص صيغة الرد: إذا لم تكن بحاجة لاستدعاء أي أداة، "
        'فيجب أن يكون ردك بالكامل عبارة عن JSON صالح وفقط لا غير، بالشكل: '
        '{"final": "نص إجابتك الكامل هنا"}. '
        "لا تكتب أي نص خارج هذا الكائن. لا تستخدم علامات ```. "
        "أي رد لا يطابق هذه الصيغة ولا يحتوي استدعاء أداة صالح سيُرفض وتُطلب منك إعادة الصياغة."
    ),
}


class AgentRunner:
    """يدير حلقة while مع ميزانيتي محاولات منفصلتين (أدوات / تنسيق) وتبديل تلقائي للنموذج."""

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

        if settings.STRICT_JSON_PROTOCOL:
            msgs.append(_STRICT_ENVELOPE_NOTICE)

        return msgs

    def _next_healthy_model(self, current: str, excluded: list[str]) -> Optional[str]:
        """يختار نموذجاً بديلاً سليماً من السجل عند فشل النموذج الحالي (Failover)."""
        try:
            from services.model_registry import registry  # استيراد مؤجل لتفادي أي حلقة استيراد
            candidates = [m for m in registry.healthy_models() if m not in excluded and m != current]
        except Exception:
            logger.debug("تعذر الوصول لسجل النماذج من أجل التبديل التلقائي", exc_info=True)
            return None
        return candidates[0] if candidates else None

    async def _call_model(self, model: str, messages: list[dict], request: ChatCompletionRequest):
        """
        استدعاء فعلي للمزوّد مع تمرير الأدوات رسمياً (الإصلاح الجوهري).
        """
        return await asyncio.wait_for(
            self.client.chat.completions.create(
                model=model,
                messages=messages,
                tools=request.tools or None,
                tool_choice=getattr(request, "tool_choice", None) or ("auto" if request.tools else None),
            ),
            timeout=settings.REQUEST_TIMEOUT,
        )

    async def run(self, request: ChatCompletionRequest) -> tuple[str, list, str]:
        """
        حلقة التنفيذ الذكية مع منع تكرار الأدوات وحد زمني.
        تُرجع (المحتوى النهائي, استدعاءات أدوات للعميل, finish_reason).
        """
        model = request.model or settings.DEFAULT_MODEL
        tool_attempts = 0          # عدد جولات استدعاء الأدوات الناجحة
        format_attempts = 0        # عدد محاولات إصلاح رد مشوّه
        strict = False
        working_messages: Optional[list[dict]] = None
        failed_models: list[str] = []
        content = ""
        start_time = time.time()
        used_tools_in_loop = set()  # منع تكرار الأدوات في نفس الطلب

        while tool_attempts < settings.MAX_TOOL_ITERATIONS and format_attempts < settings.MAX_FORMAT_RETRIES:
            # حد زمني للحلقة (120 ثانية)
            if time.time() - start_time > 120:
                logger.warning("⏰ انتهت المهلة الكلية للوكيل (120 ثانية)")
                return "آسف، استغرق التنفيذ وقتاً طويلاً. حاول مرة أخرى.", [], "stop"

            messages = working_messages or self._prepare_messages(request, strict)
            logger.info(
                "محاولة (أدوات %d/%d، تنسيق %d/%d) على النموذج %s",
                tool_attempts, settings.MAX_TOOL_ITERATIONS,
                format_attempts, settings.MAX_FORMAT_RETRIES, model,
            )

            # --- استدعاء النموذج مع تبديل تلقائي عند الفشل (محدود بـ MAX_MODEL_SWITCHES) ---
            try:
                resp = await self._call_model(model, messages, request)
            except Exception as exc:
                logger.warning("فشل الاستدعاء على النموذج '%s': %s", model, exc)
                failed_models.append(model)
                # --- إضافة: لا نبدّل أكثر من MAX_MODEL_SWITCHES مرة، لتفادي تراكم
                # عدة انتظارات REQUEST_TIMEOUT متتالية قبل الوصول لسقف الـ120 ثانية.
                if len(failed_models) > MAX_MODEL_SWITCHES:
                    logger.error("تجاوزنا الحد الأقصى لتبديل النماذج (%d)، إيقاف المحاولة", MAX_MODEL_SWITCHES)
                    raise
                fallback = self._next_healthy_model(model, failed_models)
                if fallback:
                    logger.info("🔄 تبديل تلقائي إلى نموذج بديل سليم: %s", fallback)
                    model = fallback
                    continue
                raise

            message = resp.choices[0].message
            content = message.content or ""

            # --- 1) معالجة استدعاءات الأدوات ---
            calls = [validate_call(c, request.tools or []) for c in normalize_tool_calls(message)]
            valid_calls = [c for c in calls if c.valid]
            invalid_calls = [c for c in calls if not c.valid]

            for bad in invalid_calls:
                logger.warning("استدعاء مرفوض '%s': %s", bad.name, bad.rejection_reason)

            if valid_calls:
                # ✅ منع تكرار الأدوات
                unique_calls = []
                for call in valid_calls:
                    if call.name not in used_tools_in_loop:
                        used_tools_in_loop.add(call.name)
                        unique_calls.append(call)
                    else:
                        logger.warning("⛔ منع تكرار الأداة '%s' في نفس الطلب", call.name)
                valid_calls = unique_calls

                if not valid_calls:
                    # جميع الأدوات مكررة → اعتبر المهمة منتهية
                    return content, [], "stop"

                tool_attempts += 1
                server_side = [c for c in valid_calls if c.name in SERVER_TOOLS]

                if server_side:
                    # تنفيذ أدوات الخادم (المسجلة محلياً)
                    working_messages = messages + [
                        {"role": "assistant", "content": content, "tool_calls": [c.to_openai() for c in server_side]}
                    ]
                    for call in server_side:
                        try:
                            result = await SERVER_TOOLS[call.name](call.arguments)
                        except Exception as exc:
                            result = json.dumps({"error": str(exc)}, ensure_ascii=False)
                        logger.info("✅ نُفذت الأداة %s", call.name)
                        working_messages.append(
                            {"role": "tool", "tool_call_id": call.id, "content": result}
                        )
                    continue

                # أدوات غير مسجلة → تُعاد للعميل (أو يمكن تنفيذها عبر call_tool)
                return content, [c.to_openai() for c in valid_calls], "tool_calls"

            # --- 2) استدعاءات مرفوضة → فرصة تصحيح ---
            if invalid_calls:
                format_attempts += 1
                if format_attempts >= settings.MAX_FORMAT_RETRIES:
                    return content, [], "stop"
                reasons = "; ".join(f"{c.name}: {c.rejection_reason}" for c in invalid_calls)
                working_messages = messages + [
                    {"role": "assistant", "content": content},
                    {"role": "system", "content": f"استدعاء الأداة السابق مرفوض: {reasons}. صحّح الاستدعاء أو أرجع {{\"final\": ...}}."},
                ]
                strict = True
                continue

            # --- 3) بروتوكول JSON الصارم (البحث عن {"final": "..."}) ---
            if settings.STRICT_JSON_PROTOCOL and request.tools:
                final_text = extract_final_answer(content)
                if final_text is not None:
                    return final_text, [], "stop"

                # رد لا يطابق الصيغة → إعادة محاولة
                format_attempts += 1
                if format_attempts >= settings.MAX_FORMAT_RETRIES:
                    return content, [], "stop"
                strict = True
                working_messages = None
                continue

            # --- 4) مسار Fallback القديم (عند تعطيل JSON الصارم) ---
            if is_hedging(content) and format_attempts < settings.MAX_FORMAT_RETRIES:
                format_attempts += 1
                logger.warning("⚠️ رُصد وعد فارغ (heuristic)، إعادة المحاولة بتذكير حاد")
                strict = True
                working_messages = None
                continue

            # --- 5) رد نهائي (أو نهاية المحاولات) ---
            return content, [], "stop"

        # نفذت جميع المحاولات دون نتيجة
        return content, [], "stop"