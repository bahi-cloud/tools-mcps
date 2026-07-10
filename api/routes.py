"""
نقاط النهاية المتوافقة مع OpenAI API.
الإصدار الذكي: يفهم نية المستخدم ولا يتأخر عند التحية.
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


def _should_use_agent_mode(request: ChatCompletionRequest) -> bool:
    """
    قرار ذكي ومحسن جداً: يحدد بدقة متناهية متى يتم الانتقال لوضع الوكيل (Agent Mode)
    ومتى يتم الإبقاء على وضع المحادثة المتدفقة العادية (Chat Mode).
    """
    # 1. إذا قام العميل بتمرير أدوات صريحة في مصفوفة الأدوات أو حقول الـ functions (أعلى درجات الثقة)
    if request.tools or getattr(request, 'functions', None):
        return True

    # 2. استخراج وتجميع النص الكامل لرسائل المستخدم الأخيرة لتحليل النية بدقة
    user_messages = [m.content for m in request.messages if m.role == "user" and m.content]
    if not user_messages:
        return False
        
    user_text = " ".join(user_messages).lower().strip()

    # 3. الفحص الاستباقي: إذا كانت الرسالة مجرد تحية قصيرة جداً أو سؤال عام واشتقاقي، لا نفعّل الوكيل مطلقاً
    if len(user_text.split()) <= 3:
        fast_chat_keywords = ["مرحباً", "مرحبا", "سلام", "هلا", "hi", "hello", "hey", "صباح", "مساء"]
        if any(user_text.startswith(kw) or user_text.endswith(kw) for kw in fast_chat_keywords):
            return False

    # 4. ترسانة الأنماط الذكية لتغطية جميع أدوات الباك اند الـ 120+ (ملفات، ميديا، أتمتة، بحث)
    agent_intent_patterns = [
        # أدوات المستندات والملفات والترجمة
        r"(أنشئ|اعمل|صمم|اكتب|سوي|صنع|تعديل|تغيير|تحويل)\s*(لي\s*)?(pdf|ملف|اكسل|word|xlsx|docx|pptx|ppt|مستند|جدول|عرض)",
        r"(اقرأ|استخرج|بيانات|محتوى|معلومات)\s*(من\s*)?(pdf|ملف|مستند|اكسل)",
        r"markdown\s*to\s*html|تحويل\s*ماركداون",
        
        # أدوات الفيديو، الميديا، الصلاحيات، والصوتيات
        r"(اقطع|قص|دمج|تجميع|تركيب|تعديل|تحويل|تغيير)\s*(مقطع|فيديو|صوت|أغنية|ترجمة|subtitle|srt)",
        r"(استخراج|فصل)\s*(الصوت|الموسيقى|النص)\s*(من\s*)?(الفيديو)",
        r"(تسريع|تبطيء|سرعة|حذف\s*الصمت|صمت)\s*(الفيديو|الصوت)",
        r"(إضافة|وضع|حرق)\s*(نص|كتابة|شعار|صورة|ووترمارك|لوجو)\s*(على|فوق)\s*(الفيديو)",
        r"b-roll|b\s*roll|انتقالات\s*فيديو",
        
        # أدوات البحث المتقدم والتصفح (Firecrawl) والتوثيق (Context7)
        r"(ابحث|بحث|تصفح|سيرش|جوجل|نتائج)\s*(في\s*)?(الويب|الإنترنت|نت|موقع|رابط)",
        r"(جلب|سحب|كشط|scrape|firecrawl)\s*(محتوى|صفحة|موقع)",
        r"(توثيق|مكتبة|دليل|docs|context7|library)\s*(برمجية|كود)",
        
        # أدوات الأتمتة والربط المتقدم (Composio)
        r"(إرسال|ربط|اتصال|جلسة|composio|جيميل|سلاك|جيت هاب|gmail|slack|github|email|بريد)",
        r"(تنزيل|تحميل|حفظ)\s*(رابط|فيديو|صوت|ملف|ميديا)"
    ]

    # فحص تطابق الأنماط مع النص الممرر
    for pattern in agent_intent_patterns:
        if re.search(pattern, user_text):
            logger.info(f"🎯 تم رصد نية أتمتة مطابقة للنمط: {pattern}")
            return True

    # 5. افتراضياً: محادثة عادية وتدفق فوري (Streaming Mode) لضمان السرعة والتجربة السلسة
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

    content, tool_calls, finish_reason = await runner.run(request)

    message = ChatMessage(role="assistant", content=content or None)
    if tool_calls:
        message.tool_calls = tool_calls

    return ChatCompletionResponse(
        model=model,
        choices=[Choice(message=message, finish_reason=finish_reason)],
    )


async def _stream(request: ChatCompletionRequest, model: str):
    """
    مولّد SSE متوافق مع OpenAI.
    يقرر تلقائياً: هل يتدفق حرفياً أم يستخدم الوكيل (معطل التدفق).
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    try:
        # ✅ القرار الذكي المعدّل: تفعيل الوكيل بناءً على تحليل النية الموسع والعميق
        if _should_use_agent_mode(request):
            logger.info("🧠 الوكيل في وضع Agent (طلب يحتاج أتمتة واستدعاء أدوات)")
            content, tool_calls, finish_reason = await runner.run(request)

            delta = DeltaMessage(role="assistant", content=content)
            if tool_calls:
                delta.content = f"{content}\n\n[TOOL_CALLS]: {json.dumps(tool_calls, ensure_ascii=False)}"

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

        # ✅ وضع المحادثة العادية (تدفق حقيقي وسريع)
        logger.info("📝 الوكيل في وضع Chat (محادثة عادية متدفقة)")
        messages = runner._prepare_messages(request, strict=False)
        stream = client.chat.completions.stream(model=model, messages=messages)

        first = True
        async for part in stream:
            delta_content = part.choices[0].delta.content if part.choices else None
            if delta_content:
                chunk = ChatCompletionChunk(
                    id=chunk_id,
                    model=model,
                    choices=[StreamChoice(
                        delta=DeltaMessage(role="assistant" if first else None, content=delta_content),
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
