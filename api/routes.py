"""نقاط النهاية المتوافقة مع OpenAI API."""
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

# تسجيل أدوات الخادم مرة واحدة عند تحميل الوحدة (يكفي الاستيراد لتفعيل register_tool داخله)
from services import tool_executor  # noqa: F401

logger = logging.getLogger("gateway.api")
router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])
client = AsyncClient()
runner = AgentRunner(client)

_STREAM_CHUNK_SIZE = 40  # عدد الأحرف تقريباً في كل جزء عند محاكاة التدفق لناتج الوكيل


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
        return StreamingResponse(_stream(request, model), media_type="text/event-stream")

    content, tool_calls, finish_reason = await runner.run(request)
    message = ChatMessage(role="assistant", content=content or None)
    if tool_calls:
        message.tool_calls = tool_calls
    return ChatCompletionResponse(
        model=model,
        choices=[Choice(message=message, finish_reason=finish_reason)],
    )


def _chunk_text(text: str, size: int = _STREAM_CHUNK_SIZE):
    """يقسّم نصاً جاهزاً إلى أجزاء صغيرة لمحاكاة تدفق SSE بعد أن أنهت حلقة الوكيل عملها كاملة."""
    for i in range(0, len(text), size):
        yield text[i:i + size]


async def _stream(request: ChatCompletionRequest, model: str):
    """
    مولّد SSE متوافق مع OpenAI (data: {...}\\n\\n ثم [DONE]).

    === الإصلاح الجوهري ===
    النسخة السابقة كانت تُمرر الطلب مباشرة إلى client.chat.completions.stream()
    بغض النظر عن وجود tools، أي أن كل منطق الوكيل (كشف الاستدعاءات، البروتوكول
    الصارم، التبديل التلقائي) كان يُتجاوز بالكامل في وضع stream=True.

    السبب التقني لعدم إمكان تفعيل الوكيل أثناء التدفق الحقيقي حرفياً حرفاً:
    قرار "هل هذا استدعاء أداة صالح؟" يحتاج الرد كاملاً (JSON غير مكتمل لا يمكن
    تحليله وسط التدفق). لذلك عند وجود tools في الطلب، نُشغّل حلقة الوكيل الكاملة
    داخلياً حتى تنتهي، ثم "نحاكي" التدفق للعميل بإرسال النتيجة على دفعات — هذا
    يحافظ على واجهة SSE المتوقعة من العميل بينما يضمن صحة قرار استدعاء الأداة.

    عندما لا توجد tools في الطلب (محادثة عادية)، يبقى التدفق حقيقياً وحرفاً بحرف
    كما كان، لأنه لا داعي لأي تحليل بنيوي في هذه الحالة.
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    try:
        if request.tools and settings.STREAM_USES_AGENT_LOOP:
            content, tool_calls, finish_reason = await runner.run(request)

            if tool_calls:
                chunk = ChatCompletionChunk(
                    id=chunk_id, model=model,
                    choices=[StreamChoice(delta=DeltaMessage(role="assistant", tool_calls=tool_calls))],
                )
                yield f"data: {chunk.model_dump_json()}\n\n"
                final = ChatCompletionChunk(
                    id=chunk_id, model=model,
                    choices=[StreamChoice(delta=DeltaMessage(), finish_reason="tool_calls")],
                )
                yield f"data: {final.model_dump_json()}\n\n"
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
            return

        # لا أدوات في الطلب → تدفق حقيقي مباشر (السلوك الأصلي، محادثة عادية فقط)
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