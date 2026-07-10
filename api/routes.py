"""
نقاط النهاية المتوافقة مع OpenAI API.
الإصدار النهائي والمستقر كلياً: تم إصلاح الـ Follow-up Context وتنظيف البادئات التقنية محافظاً.
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

# ==================== ثوابت قواعد التوجيه الذكي والمصحح ====================

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

# تصفية بروتوكولات الوكيل الداخلية والخام
TECHNICAL_LINE_PATTERNS = [
    re.compile(r"^\s*\[TOOL_CALLS\]\s*:?.*$", re.IGNORECASE),
    re.compile(r"^\s*(thought|action|observation)\s*:\s*.*$", re.IGNORECASE),
]

# إزالة البادئة التقنية للإجابة النهائية دون حذف سياق السطر نفسه
FINAL_PREFIX_PATTERN = re.compile(
    r"^\s*(?:final|final answer)\s*:\s*",
    re.IGNORECASE,
)

def _clean_agent_content(text: str | None) -> str | None:
    """
    منظف سطري محافظ يحمي الأكواد والـ Markdown، ويزيل فقط العناوين والبادئات التقنية.
    """
    if not text:
        return text
        
    lines = text.splitlines()
    cleaned_lines = []
    inside_code_block = False
    
    for line in lines:
        stripped = line.strip()
        
        if stripped.startswith("```"):
            inside_code_block = not inside_code_block
            cleaned_lines.append(line)
            continue
            
        if inside_code_block:
            cleaned_lines.append(line)
            continue
            
        if any(pattern.match(line) for pattern in TECHNICAL_LINE_PATTERNS):
            continue
            
        # إزالة بادئة Final: الذكية مع الحفاظ على محتوى الإجابة
        line = FINAL_PREFIX_PATTERN.sub("", line)
        cleaned_lines.append(line)
        
    result = "\n".join(cleaned_lines).strip()
    return result or None


def _should_use_agent_mode(request: ChatCompletionRequest) -> bool:
    """
    موجه محافظ مصحح: يربط نية المتابعة بوجود سياق استخدام أدوات سابق.
    """
    if request.tools or getattr(request, 'functions', None):
        logger.info("🎯 ROUTER: mode=agent reason=explicit_tools")
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
        logger.info("🎯 ROUTER: mode=chat reason=fast_path_greeting")
        return False

    # فحص النية التنفيذية المركبة المباشرة
    if AGENT_EXECUTION_PATTERN.search(last_msg_clean):
        logger.info("🎯 ROUTER: mode=agent reason=composite_execution_intent")
        return True

    if any(re.search(p, last_msg_clean) for p in NEGATIVE_EXPLANATION_PATTERNS):
        logger.info("🎯 ROUTER: mode=chat reason=explanation_intent")
        return False

    # تحليل سياق المتابعة التاريخي (Contextual Follow-up) لرسائل المستخدم السابقة
    previous_user_messages = [
        m.content for m in request.messages if m.role == "user" and m.content
    ][:-1]
    
    previous_context = " ".join(previous_user_messages[-2:]).lower()
    
    has_follow_up = any(re.search(p, last_msg_clean) for p in FOLLOW_UP_PATTERNS)
    has_previous_tool_context = any(
        re.search(pattern, previous_context) for pattern in TOOL_RESOURCES.values()
    )

    if has_follow_up and has_previous_tool_context:
        logger.info("🎯 ROUTER: mode=agent reason=contextual_follow_up_with_tool_context")
        return True

    logger.info("🎯 ROUTER: mode=chat reason=no_execution_intent")
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
        
        content = _clean_agent_content(content)
        
        message = ChatMessage(role="assistant", content=content or None)
        if tool_calls and request.tools:
            message.tool_calls = tool_calls

        return ChatCompletionResponse(
            model=model,
            choices=[Choice(message=message, finish_reason=finish_reason)],
        )

    logger.info("📝 تشغيل الطلب عبر وضع المحادثة العادية (Non-Streaming Chat)")
    messages = runner._prepare_messages(request, strict=False)
    response = await client.chat.completions.create(model=model, messages=messages, stream=False)
    return response


async def _stream(request: ChatCompletionRequest, model: str):
    """
    مولّد SSE متوافق مع OpenAI.
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    try:
        if _should_use_agent_mode(request):
            logger.info("🧠 الوكيل في وضع Agent (انتظار تنفيذ الأدوات بالخلفية)")
            content, tool_calls, finish_reason = await runner.run(request)

            content = _clean_agent_content(content)
            delta = DeltaMessage(role="assistant", content=content)

            chunk = ChatCompletionChunk(
                id=chunk_id,
                model=model,
                choices=[StreamChoice(delta=delta, finish_reason=None)],
            )
            yield f"data: {chunk.model_dump_json()}\n\n"

            final = ChatCompletionChunk(
                id=chunk_id,
                model=model,
                choices=[StreamChoice(delta=DeltaMessage(), finish_reason=finish_reason or "stop")],
            )
            yield f"data: {final.model_dump_json()}\n\n"
            yield "data: [DONE]\n\n"
            return

        logger.info("📝 الوكيل في وضع Chat (محادثة عادية متدفقة ونظيفة)")
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
                        finish_reason=None
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
