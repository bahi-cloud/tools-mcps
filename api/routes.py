"""
نقاط النهاية المتوافقة مع OpenAI API.
الإصدار النظيف: يقوم بتصفية النصوص وتطهيرها من الكلمات والرموز التقنية مثل [TOOL_CALLS] و final.
"""
import json
import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from g4f.client import AsyncClient

from core.config import settings
from core.security import verify_api_key
from models.openai_schemas import (
    ChatCompletionRequest, ChatCompletionResponse, ChatMessage, Choice,
    ChatCompletionChunk, DeltaMessage, StreamChoice,
)
from services.agent_loop import AgentRunner
from services.model_registry import registry
from services import tool_executor  # تفعيل الأدوات

logger = logging.getLogger("gateway.api")

router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])

client = AsyncClient()
runner = AgentRunner(client)

# ==================== ثوابت قواعد التوجيه والفلترة ====================

FAST_CHAT_PATTERNS = [
    r"^(مرحباً|مرحبا|سلام|هلا|هاي|صباح الخير|مساء الخير|hi|hello|hey)$"
]

NEGATIVE_EXPLANATION_PATTERNS = [
    r"^(كيف|اشرح|وضح|فسر|ما هو|ما هي|ما معنى|علمني)\b",
    r"^(explain|how to|what is|what are|teach me|tutorial)\b",
]

ACTION_WORDS = r"(أنشئ|اعمل|سوي|صمم|احفظ|صنع|تعديل|تغيير|تحويل|اقرأ|استخرج|اقطع|قص|دمج|تجميع|تركيب|تسريع|تبطيء|حذف|إضافة|وضع|حرق|ابحث|تصفح|سيرش|جلب|سحب|كشط|إرسال|ربط|اتصال|تنزيل|تحميل)"

TOOL_RESOURCES = {
    "files": r"(pdf|ملف|اكسل|word|xlsx|docx|pptx|ppt|مستند|جدول|عرض)",
    "media": r"(مقطع|فيديو|صوت|أغنية|ترجمة|subtitle|srt|شعار|صورة|ووترمارك|لوجو)",
    "web_and_docs": r"(الويب|الإنترنت|نت|موقع|رابط|ويب|جوجل|صفحة|scrape|firecrawl|توثيق|مكتبة|docs|context7|library)",
    "automation": r"(جيميل|سلاك|جيت هاب|gmail|slack|github|email|بريد|composio)"
}

AGENT_EXECUTION_PATTERN = re.compile(
    rf"{ACTION_WORDS}"
    rf".{{0,80}}?"
    rf"(?:" + "|".join(TOOL_RESOURCES.values()) + ")",
    re.IGNORECASE | re.DOTALL
)

FOLLOW_UP_PATTERNS = [
    r"(والآن|الان|الحين|ثم)\s+(احفظ|حول|ارسل|أنشئ|سوي)",
    r"(save this|convert it|send to)"
]

# ==================== دالة التطهير والتنظيف الذكي للنصوص ====================

def _clean_assistant_content(text: str) -> str:
    """
    تنظيف مخرجات النموذج من المخلفات التقنية، علامات النظام، والكلمات المفتاحية للـ JSON.
    """
    if not text:
        return text

    # 1. إزالة كلمة final أو [final] أو final: التي تظهر بالخطأ في بعض النماذج
    text = re.sub(r"\b(final|done|stop)\b[:\s]*", "", text, flags=re.IGNORECASE)
    
    # 2. تنظيف مخلفات استدعاء الأدوات النصية إن وجدت مثل [TOOL_CALLS] أو الـ JSON الملتصق
    text = re.sub(r"\[TOOL_CALLS\].*$", "", text, flags=re.DOTALL)
    text = re.sub(r"\{\s*\"tool\".*?\}", "", text, flags=re.DOTALL)
    
    # 3. تنظيف علامات الترقيم الزائدة أو الأقواس الفارغة التي تتركها الفلترة
    text = re.sub(r"\[\s*\]|\(\s*\)", "", text)
    
    return text.strip()


def _should_use_agent_mode(request: ChatCompletionRequest) -> bool:
    """مواجه محافظ لتحديد وضع الوكيل."""
    if request.tools or getattr(request, 'functions', None):
        return True

    if not request.messages:
        return False

    last_user_message = next(
        (m.content for m in reversed(request.messages) if m.role == "user" and m.content),
        "",
    )
    last_msg_clean = last_user_message.lower().strip()
    
    if not last_msg_clean:
        return False
    
    if any(re.match(p, last_msg_clean) for p in FAST_CHAT_PATTERNS):
        return False

    if AGENT_EXECUTION_PATTERN.search(last_msg_clean):
        return True

    if any(re.search(p, last_msg_clean) for p in NEGATIVE_EXPLANATION_PATTERNS):
        return False

    recent_messages = [m.content for m in request.messages[-3:] if m.role == "user" and m.content]
    recent_text = " ".join(recent_messages).lower()
    if any(re.search(p, recent_text) for p in FOLLOW_UP_PATTERNS):
        return True

    return False


@router.get("/models")
async def list_models():
    return {
        "object": "list",
        "data": [{"id": n, "object": "model", "owned_by": "g4f"} for n in registry.healthy_models()],
    }


@router.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    model = request.model or settings.DEFAULT_MODEL

    if registry.models and model in registry.models and not registry.models[model].healthy:
        raise HTTPException(status_code=400, detail=f"النموذج '{model}' غير صحي حالياً")

    if request.stream:
        return StreamingResponse(
            _stream(request, model),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            }
        )

    if _should_use_agent_mode(request):
        logger.info("🧠 تشغيل الطلب عبر وضع الوكيل (Non-Streaming Agent)")
        content, tool_calls, finish_reason = await runner.run(request)
        
        # تصفية النص وتطهيره قبل التغليف النهائي لضمان خروجه نظيفاً ومقروءاً للمستخدم
        clean_content = _clean_assistant_content(content)
        
        message = ChatMessage(role="assistant", content=clean_content or None)
        if tool_calls and request.tools:
            message.tool_calls = tool_calls

        return ChatCompletionResponse(
            model=model,
            choices=[Choice(message=message, finish_reason=finish_reason)],
        )

    logger.info("📝 تشغيل الطلب عبر وضع المحادثة العادية (Non-Streaming Chat)")
    messages = runner._prepare_messages(request, strict=False)
    response = await client.chat.completions.create(model=model, messages=messages, stream=False)
    
    # تطبيق الفلترة أيضاً على المحادثات العادية لضمان التنسيق
    if response.choices and response.choices[0].message.content:
        response.choices[0].message.content = _clean_assistant_content(response.choices[0].message.content)
        
    return response


async def _stream(request: ChatCompletionRequest, model: str):
    """
    مولّد SSE متوافق مع OpenAI مع فلترة البث المباشر.
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    try:
        if _should_use_agent_mode(request):
            logger.info("🧠 الوكيل في وضع Agent (البث المصفى للأتمتة)")
            content, tool_calls, finish_reason = await runner.run(request)

            # تنظيف المحتوى النهائي الناتج عن الوكيل قبل دفعه للمتصفح/المستمع
            clean_content = _clean_assistant_content(content)
            delta = DeltaMessage(role="assistant", content=clean_content)

            chunk = ChatCompletionChunk(
                id=chunk_id,
                model=model,
                choices=[StreamChoice(delta=delta, finish_reason=finish_reason)],
            )
            yield f"data: {chunk.model_dump_json()}\n\n"

            final = ChatCompletionChunk(
                id=chunk_id,
                model=model,
                choices=[StreamChoice(delta=DeltaMessage(), finish_reason="stop")],
            )
            yield f"data: {final.model_dump_json()}\n\n"
            yield "data: [DONE]\n\n"
            return

        logger.info("📝 الوكيل في وضع Chat (محادثة عادية متدفقة)")
        messages = runner._prepare_messages(request, strict=False)
        stream = client.chat.completions.stream(model=model, messages=messages)

        first = True
        async for part in stream:
            delta_content = part.choices[0].delta.content if part.choices else None
            if delta_content:
                # تطبيق الفلترة السريعة على الأجزاء المتدفقة لمنع تسرب كلمات مثل final
                clean_delta = _clean_assistant_content(delta_content)
                if clean_delta:
                    chunk = ChatCompletionChunk(
                        id=chunk_id,
                        model=model,
                        choices=[StreamChoice(
                            delta=DeltaMessage(role="assistant" if first else None, content=clean_delta),
                        )],
                    )
                    first = False
                    yield f"data: {chunk.model_dump_json()}\n\n"

        final = ChatCompletionChunk(
            id=chunk_id,
            model=model,
            choices=[StreamChoice(delta=DeltaMessage(), finish_reason="stop")],
        )
        yield f"data: {final.model_dump_json()}\n\n"
        yield "data: [DONE]\n\n"

    except Exception as exc:
        logger.exception("خطأ أثناء التدفق")
        yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        yield "data: [DONE]\n\n"
