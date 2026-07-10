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
    قرار ذكي: تفعيل وضع الوكيل فقط عند طلب صريح لإنشاء أو تعديل ملف.
    """
    # 1. وجود أدوات في الطلب (ثقة عالية)
    if request.tools:
        return True

    # 2. تحليل نية المستخدم من النص
    user_text = " ".join([m.content for m in request.messages if m.role == "user"]).lower()

    # أنماط لإنشاء/تعديل ملفات محددة
    creation_patterns = [
        r"أنشئ\s*(لي\s*)?(pdf|ملف\s*pdf|اكسل|word|ppt|فيديو|صوت|مستند)",
        r"(pdf|اكسل|word|ppt|فيديو|صوت|مستند)\s*(جديد|ملف)",
        r"حوّل\s*(الصورة|الفيديو|الصوت|المستند)",
        r"اقطع\s*(فيديو|صوت|مقطع)",
        r"دمج\s*(فيديو|صوت|ملفات)",
        r"استخرج\s*(الصوت|النص|الفيديو)",
        r"تعديل\s*(pdf|اكسل|word|فيديو|صوت)",
    ]

    for pattern in creation_patterns:
        if re.search(pattern, user_text):
            return True

    # 3. إذا كانت الكلمات المفتاحية عامة دون طلب إنشاء، لا نفعّل الوكيل
    general_keywords = ["مرحباً", "كيف", "ما هو", "أخبرني", "اشرح"]
    if any(kw in user_text for kw in general_keywords):
        return False

    # 4. افتراضياً: لا نستخدم وضع الوكيل (تدفق عادي)
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
        # ✅ القرار الذكي: تفعيل الوكيل فقط عند طلب إنشاء ملف
        if _should_use_agent_mode(request):
            logger.info("🧠 الوكيل في وضع Agent (طلب إنشاء ملف)")
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

        # ✅ وضع المحادثة العادية (تدفق حقيقي)
        logger.info("📝 الوكيل في وضع Chat (محادثة عادية)")
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