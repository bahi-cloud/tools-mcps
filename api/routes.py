"""
نقاط النهاية المتوافقة مع OpenAI API.
"""
import json
import logging
import time
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

logger = logging.getLogger("gateway.api")

router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])

client = AsyncClient()
runner = AgentRunner(client)


@router.get("/models")
async def list_models():
    """قائمة النماذج الصحية بصيغة OpenAI."""
    return {
        "object": "list",
        "data": [{"id": n, "object": "model", "owned_by": "g4f"} for n in registry.healthy_models()],
    }


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
    ✅ الإصدار القديم مع إصلاح بسيط لتفادي خطأ tool_calls.
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    try:
        # ✅ الإصلاح: استخدام AgentRunner للحصول على الرد (غير تدفقي)
        content, tool_calls, finish_reason = await runner.run(request)

        # بناء الـ DeltaMessage دون استخدام tool_calls (لتجنب الخطأ)
        delta = DeltaMessage(role="assistant", content=content)
        
        # ✅ إذا كانت هناك tool_calls، نرسلها كجزء من الـ content (حل مؤقت)
        if tool_calls:
            # تحويل tool_calls إلى نص JSON وإضافته للمحتوى
            tool_calls_text = json.dumps(tool_calls, ensure_ascii=False)
            delta.content = f"{content}\n\n[TOOL_CALLS]: {tool_calls_text}"

        chunk = ChatCompletionChunk(
            id=chunk_id,
            model=model,
            choices=[StreamChoice(delta=delta, finish_reason=finish_reason)],
        )
        yield f"data: {chunk.model_dump_json()}\n\n"

        # نهاية البث
        final_chunk = ChatCompletionChunk(
            id=chunk_id,
            model=model,
            choices=[StreamChoice(delta=DeltaMessage(), finish_reason="stop")],
        )
        yield f"data: {final_chunk.model_dump_json()}\n\n"
        yield "data: [DONE]\n\n"

    except Exception as exc:
        logger.exception("خطأ أثناء التدفق")
        yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        yield "data: [DONE]\n\n"