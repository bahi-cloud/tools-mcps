"""
نقاط النهاية المتوافقة مع OpenAI API.
الإصدار النهائي المتكامل (يجمع بين النسخة القديمة والجديدة).
"""
import json
import logging
import uuid
from typing import Optional

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

# ✅ تسجيل أدوات الخادم (يكفي الاستيراد لتفعيل register_tool)
from services import tool_executor  # noqa: F401

logger = logging.getLogger("gateway.api")

router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])

client = AsyncClient()
runner = AgentRunner(client)

# حجم الدفعة لمحاكاة التدفق
_STREAM_CHUNK_SIZE = 40


@router.get("/models")
async def list_models():
    """قائمة النماذج الصحية بصيغة OpenAI."""
    return {
        "object": "list",
        "data": [{"id": n, "object": "model", "owned_by": "g4f"} for n in registry.healthy_models()],
    }


def _chunk_text(text: str, size: int = _STREAM_CHUNK_SIZE):
    """يقسّم نصاً جاهزاً إلى أجزاء صغيرة لمحاكاة تدفق SSE."""
    for i in range(0, len(text), size):
        yield text[i:i + size]


@router.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """نقطة النهاية الرئيسية: تدعم الوضع العادي و SSE Streaming."""
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

    # ✅ وضع غير تدفقي: استخدام حلقة الوكيل الكاملة
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
    مولّد SSE متوافق مع OpenAI (data: {...}\\n\\n ثم [DONE]).

    === الإصلاح الجوهري ===
    - عند وجود أدوات (tools): نُشغّل حلقة الوكيل الكاملة داخلياً، ثم نحاكي التدفق.
    - عند عدم وجود أدوات: تدفق حقيقي مباشر (محادثة عادية).
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    try:
        # ✅ المسار 1: مع أدوات → استخدم حلقة الوكيل ثم حاكِ التدفق
        if request.tools and settings.STREAM_USES_AGENT_LOOP:
            content, tool_calls, finish_reason = await runner.run(request)

            # إرسال استدعاءات الأدوات (إذا وجدت)
            if tool_calls:
                chunk = ChatCompletionChunk(
                    id=chunk_id,
                    model=model,
                    choices=[StreamChoice(
                        delta=DeltaMessage(role="assistant", tool_calls=tool_calls),
                        finish_reason="tool_calls",
                    )],
                )
                yield f"data: {chunk.model_dump_json()}\n\n"

                final = ChatCompletionChunk(
                    id=chunk_id,
                    model=model,
                    choices=[StreamChoice(delta=DeltaMessage(), finish_reason="tool_calls")],
                )
                yield f"data: {final.model_dump_json()}\n\n"
                yield "data: [DONE]\n\n"
                return

            # إرسال المحتوى النهائي على دفعات (محاكاة التدفق)
            first = True
            for piece in _chunk_text(content or ""):
                chunk = ChatCompletionChunk(
                    id=chunk_id,
                    model=model,
                    choices=[StreamChoice(
                        delta=DeltaMessage(role="assistant" if first else None, content=piece),
                    )],
                )
                first = False
                yield f"data: {chunk.model_dump_json()}\n\n"

            final = ChatCompletionChunk(
                id=chunk_id,
                model=model,
                choices=[StreamChoice(delta=DeltaMessage(), finish_reason=finish_reason or "stop")],
            )
            yield f"data: {final.model_dump_json()}\n\n"
            yield "data: [DONE]\n\n"
            return

        # ✅ المسار 2: بدون أدوات → تدفق حقيقي مباشر (محادثة عادية)
        messages = runner._prepare_messages(request, strict=False)
        stream = client.chat.completions.stream(
            model=model,
            messages=messages,
            tools=request.tools or None,
            tool_choice="auto" if request.tools else None,
        )

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