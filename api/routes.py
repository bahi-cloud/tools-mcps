"""
نقاط النهاية المتوافقة مع OpenAI API.
الإصدار النهائي - يعطل التدفق في وضع الوكيل، ويفعله في الوضع العادي.
"""
import json
import logging
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

    # وضع غير تدفقي (يعمل مع الأدوات)
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

    ✅ المنطق الصحيح:
    - إذا كان هناك أدوات (tool) → نعطل التدفق ونستخدم حلقة الوكيل.
    - إذا لم يكن هناك أدوات → نفعّل التدفق الحقيقي (محادثة عادية).
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    try:
        # ✅ وضع الوكيل (مع أدوات) → تدفق معطل
        if request.tools:
            content, tool_calls, finish_reason = await runner.run(request)

            # إرسال الرد كقطعة واحدة (بدلاً من تدفق حقيقي)
            delta = DeltaMessage(role="assistant", content=content)
            if tool_calls:
                # إرسال استدعاءات الأدوات كـ JSON في المحتوى (لتجنب خطأ DeltaMessage)
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

        # ✅ وضع النموذج العادي (بدون أدوات) → تدفق حقيقي
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