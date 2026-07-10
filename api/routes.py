"""نقاط النهاية المتوافقة مع OpenAI API."""
import asyncio
import json
import logging
import time
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
    """
    نقطة النهاية الرئيسية.
    - stream=true بدون أدوات  -> تدفق حقيقي مباشر من g4f (سريع، كما كان سابقاً).
    - stream=true مع أدوات    -> يمر عبر AgentRunner كاملاً (أدوات + كشف وعود فارغة +
                                  تبديل نماذج عند الفشل) ثم يُحاكى الناتج النهائي كتدفق SSE،
                                  لأن التدفق الحي أثناء تنفيذ tool calls غير مدعوم بشكل
                                  موثوق عبر مزودي g4f.
    - stream=false             -> كما كان، عبر AgentRunner مباشرة بلا تدفق.
    """
    model = request.model or settings.DEFAULT_MODEL
    if registry.models and model in registry.models and not registry.models[model].healthy:
        raise HTTPException(status_code=400, detail=f"النموذج '{model}' غير صحي حالياً")

    has_tools = bool(request.tools)

    if request.stream and not has_tools:
        return StreamingResponse(_stream(request, model), media_type="text/event-stream")

    if request.stream and has_tools:
        return StreamingResponse(_agent_as_stream(request, model), media_type="text/event-stream")

    content, tool_calls, finish_reason = await runner.run(request)
    message = ChatMessage(role="assistant", content=content or None)
    if tool_calls:
        message.tool_calls = tool_calls
    return ChatCompletionResponse(
        model=model,
        choices=[Choice(message=message, finish_reason=finish_reason)],
    )


async def _stream(request: ChatCompletionRequest, model: str):
    """مولّد SSE حقيقي مباشر من g4f (فقط عند غياب الأدوات) — بلا تغيير عن السابق."""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    try:
        messages = runner._prepare_messages(request, strict=False)
        stream = client.chat.completions.stream(model=model, messages=messages)
        first = True
        async for part in stream:
            delta = part.choices[0].delta.content if part.choices else None
            if delta:
                chunk = ChatCompletionChunk(
                    id=chunk_id, model=model,
                    choices=[StreamChoice(delta=DeltaMessage(role="assistant" if first else None, content=delta))],
                )
                first = False
                yield f"data: {chunk.model_dump_json()}\n\n"
        final = ChatCompletionChunk(
            id=chunk_id, model=model,
            choices=[StreamChoice(delta=DeltaMessage(), finish_reason="stop")],
        )
        yield f"data: {final.model_dump_json()}\n\n"
    except Exception as exc:
        logger.exception("خطأ أثناء التدفق")
        yield f"data: {json.dumps({'error': str(exc)})}\n\n"
    yield "data: [DONE]\n\n"


async def _agent_as_stream(request: ChatCompletionRequest, model: str):
    """
    يشغّل AgentRunner كاملاً في الخلفية (أدوات + كشف وعود فارغة + تبديل نماذج عند
    الفشل)، ويرسل نبضات "ping" دورية أثناء الانتظار حتى لا يقطع أي بروكسي/متصفح
    الاتصال بسبب الصمت الطويل، ويعطي إحساساً بأن النظام لا يزال يعمل. بعد الاكتمال
    يُبث الناتج النهائي على شكل أجزاء صغيرة بصيغة SSE متوافقة مع OpenAI.
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    task = asyncio.create_task(runner.run(request))

    try:
        while not task.done():
            yield ": ping\n\n"
            await asyncio.sleep(5)
        content, tool_calls, finish_reason = task.result()
    except Exception as exc:
        logger.exception("خطأ أثناء تنفيذ الوكيل في وضع التدفق")
        yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        yield "data: [DONE]\n\n"
        return

    if tool_calls:
        # ملاحظة: هذا يفترض أن DeltaMessage يدعم حقل tool_calls. إن لم يكن موجوداً
        # في models/openai_schemas.py حالياً، يجب إضافته هناك أولاً (Optional[list] = None)
        # وإلا فسيفشل هذا الجزء تحديداً فقط (حالة الأدوات في وضع التدفق).
        chunk = ChatCompletionChunk(
            id=chunk_id, model=model,
            choices=[StreamChoice(
                delta=DeltaMessage(role="assistant", tool_calls=tool_calls),
                finish_reason="tool_calls",
            )],
        )
        yield f"data: {chunk.model_dump_json()}\n\n"
        yield "data: [DONE]\n\n"
        return

    first = True
    for piece in _chunk_text(content or ""):
        chunk = ChatCompletionChunk(
            id=chunk_id, model=model,
            choices=[StreamChoice(delta=DeltaMessage(role="assistant" if first else None, content=piece))],
        )
        first = False
        yield f"data: {chunk.model_dump_json()}\n\n"

    final = ChatCompletionChunk(
        id=chunk_id, model=model,
        choices=[StreamChoice(delta=DeltaMessage(), finish_reason=finish_reason or "stop")],
    )
    yield f"data: {final.model_dump_json()}\n\n"
    yield "data: [DONE]\n\n"


def _chunk_text(text: str, size: int = 20):
    """يقسّم النص النهائي إلى أجزاء صغيرة لإرسالها كأجزاء SSE متتابعة (محاكاة تدفق)."""
    for i in range(0, len(text), size):
        yield text[i:i + size]