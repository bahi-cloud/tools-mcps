"""
OpenAI-compatible Chat Completions endpoints.

Routing policy:
- Normal conversational requests -> direct model streaming.
- Agentic/tool-oriented requests -> AgentRunner with streaming disabled internally.
- Explicit tools/functions always force agent mode.
- The same routing decision is used for stream=True and stream=False.

Design goals:
1. Fast path for ordinary chat.
2. Avoid activating the expensive agent loop unnecessarily.
3. Detect multi-step/action/tool-oriented requests conservatively.
4. Keep routing logic isolated and testable.
5. Preserve OpenAI-compatible SSE behavior.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Iterable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from g4f.client import AsyncClient

from core.config import settings
from core.security import verify_api_key

from models.openai_schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ChatCompletionChunk,
    DeltaMessage,
    StreamChoice,
)

from services.agent_loop import AgentRunner
from services.model_registry import registry

# Import is intentionally kept if importing the module registers tools.
from services import tool_executor  # noqa: F401


logger = logging.getLogger("gateway.api")

router = APIRouter(
    prefix="/v1",
    dependencies=[Depends(verify_api_key)],
)

client = AsyncClient()
runner = AgentRunner(client)


# ============================================================
# Routing
# ============================================================


class ExecutionMode(str, Enum):
    CHAT = "chat"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    mode: ExecutionMode
    reason: str
    confidence: float


# ------------------------------------------------------------
# Text normalization
# ------------------------------------------------------------


_ARABIC_DIACRITICS_RE = re.compile(
    r"[\u0617-\u061A\u064B-\u0652\u0670\u06D6-\u06ED]"
)

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_text(text: str) -> str:
    """
    Normalize Arabic/English user text for deterministic routing.
    """

    text = text.lower().strip()

    # Remove Arabic diacritics.
    text = _ARABIC_DIACRITICS_RE.sub("", text)

    # Normalize Arabic characters.
    replacements = {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ى": "ي",
        "ؤ": "و",
        "ئ": "ي",
        "ـ": "",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return _WHITESPACE_RE.sub(" ", text)


def _latest_user_message(request: ChatCompletionRequest) -> str:
    """
    Routing should primarily inspect the latest user turn.

    Using the entire conversation can incorrectly activate agent mode because
    an old user message mentioned files/tools/search.
    """

    for message in reversed(request.messages):
        if message.role == "user" and message.content:
            return _normalize_text(str(message.content))

    return ""


def _recent_user_messages(
    request: ChatCompletionRequest,
    limit: int = 3,
) -> list[str]:
    """
    Return a small recent user-message window.

    Useful for detecting follow-up commands such as:
        user: ابحث عن أفضل المكتبات
        assistant: ...
        user: الآن قارن بينها واحفظ النتيجة في ملف
    """

    messages: list[str] = []

    for message in reversed(request.messages):
        if message.role == "user" and message.content:
            messages.append(_normalize_text(str(message.content)))

            if len(messages) >= limit:
                break

    messages.reverse()
    return messages


# ------------------------------------------------------------
# Fast chat rules
# ------------------------------------------------------------


FAST_CHAT_EXACT = frozenset(
    {
        # Arabic greetings
        "مرحبا",
        "مرحبا بك",
        "اهلا",
        "اهلين",
        "السلام عليكم",
        "وعليكم السلام",
        "هلا",
        "صباح الخير",
        "مساء الخير",

        # Arabic conversational
        "شكرا",
        "شكرا لك",
        "كيف حالك",
        "من انت",
        "ما اسمك",
        "ماذا تستطيع ان تفعل",

        # English
        "hi",
        "hello",
        "hey",
        "thanks",
        "thank you",
        "how are you",
        "who are you",
        "what can you do",
    }
)


CHAT_PREFIXES = (
    "اشرح ",
    "فسر ",
    "وضح ",
    "ما هو ",
    "ما هي ",
    "ما الفرق ",
    "لماذا ",
    "كيف يعمل ",
    "اعطني رايك ",
    "لخص لي ",
    "translate ",
    "explain ",
    "what is ",
    "what are ",
    "why is ",
    "how does ",
)


# ------------------------------------------------------------
# Strong agent rules
# ------------------------------------------------------------


# Explicit action verbs. These do NOT activate agent mode alone.
ACTION_VERBS = (
    "انشئ",
    "اصنع",
    "جهز",
    "عدل",
    "حول",
    "ارسل",
    "ارفع",
    "نزل",
    "حمل",
    "احفظ",
    "استخرج",
    "ادمج",
    "اقطع",
    "قص",
    "احذف",
    "اضف",
    "اربط",
    "نفذ",
    "شغل",
    "افتح",
    "حدث",
    "انشر",
    "جدول",
    "راقب",
    "create",
    "generate",
    "modify",
    "convert",
    "send",
    "upload",
    "download",
    "save",
    "extract",
    "merge",
    "execute",
    "run",
    "publish",
    "schedule",
    "monitor",
)


# Resources usually requiring tools.
TOOL_RESOURCES = (
    # Documents
    "pdf",
    "docx",
    "xlsx",
    "pptx",
    "word",
    "excel",
    "powerpoint",
    "ملف",
    "مستند",
    "جدول بيانات",
    "عرض تقديمي",

    # Media
    "فيديو",
    "صوت",
    "اغنيه",
    "صوره",
    "subtitle",
    "srt",
    "video",
    "audio",
    "image",

    # External services
    "gmail",
    "email",
    "بريد",
    "سلاك",
    "slack",
    "github",
    "جيت هاب",
    "composio",

    # Web/browser
    "الويب",
    "الانترنت",
    "موقع",
    "صفحه",
    "رابط",
    "web",
    "website",
    "browser",
)


# Strong phrases indicating external execution.
STRONG_AGENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # Web/search/navigation
        r"\b(ابحث|تصفح|افتح)\b.{0,30}\b(الويب|الانترنت|موقع|صفحه|رابط)\b",
        r"\b(search|browse|open)\b.{0,30}\b(web|website|url|page)\b",

        # File generation/manipulation
        r"\b(انشئ|اصنع|جهز|حول|عدل)\b.{0,40}\b(pdf|docx|xlsx|pptx|word|excel|ملف|مستند)\b",
        r"\b(create|generate|convert|modify)\b.{0,40}\b(pdf|docx|xlsx|pptx|file|document)\b",

        # Media processing
        r"\b(قص|اقطع|ادمج|حول|عدل|استخرج|اضف|احذف)\b.{0,40}\b(فيديو|صوت|صوره|subtitle|srt)\b",
        r"\b(cut|trim|merge|convert|modify|extract|add|remove)\b.{0,40}\b(video|audio|image|subtitle)\b",

        # External communication
        r"\b(ارسل|انشر|ارفع)\b.{0,40}\b(بريد|email|gmail|slack|github|جيت هاب)\b",
        r"\b(send|publish|upload)\b.{0,40}\b(email|gmail|slack|github)\b",

        # Automation
        r"\b(جدول|راقب|تابع)\b.{0,60}\b(كل يوم|كل ساعه|يوميا|اسبوعيا|تلقائيا|باستمرار)\b",
        r"\b(schedule|monitor|watch)\b.{0,60}\b(daily|hourly|weekly|automatically|continuously)\b",

        # Scraping
        r"\b(اكشط|اسحب|اجلب|استخرج)\b.{0,40}\b(موقع|صفحه|بيانات|محتوى)\b",
        r"\b(scrape|crawl|extract)\b.{0,40}\b(website|page|data|content)\b",
    )
)


# Multi-step connectors.
MULTI_STEP_CONNECTORS = (
    " ثم ",
    " وبعدها ",
    " بعد ذلك ",
    " ومن ثم ",
    " ثم قم ",
    " وبعد ذلك ",
    " and then ",
    " then ",
    " after that ",
)


def _contains_any(text: str, values: Iterable[str]) -> bool:
    return any(value in text for value in values)


def _count_occurrences(text: str, values: Iterable[str]) -> int:
    return sum(text.count(value) for value in values)


def _has_strong_agent_pattern(text: str) -> bool:
    return any(pattern.search(text) for pattern in STRONG_AGENT_PATTERNS)


def _looks_like_multi_step_agent_task(text: str) -> bool:
    """
    Detect multi-step tasks conservatively.

    Example:
        ابحث في الويب عن المشروع ثم استخرج النتائج واحفظها في ملف

    A connector alone is not enough.
    We require:
    - multiple action verbs
    - at least one tool resource
    """

    connector_count = _count_occurrences(text, MULTI_STEP_CONNECTORS)
    action_count = _count_occurrences(text, ACTION_VERBS)

    return (
        connector_count >= 1
        and action_count >= 2
        and _contains_any(text, TOOL_RESOURCES)
    )


def _looks_like_action_on_resource(text: str) -> bool:
    """
    Generic fallback detector.

    Requires BOTH:
    - an action verb
    - a resource associated with external tools

    This is safer than matching broad words like "اكتب" alone.
    """

    return (
        _contains_any(text, ACTION_VERBS)
        and _contains_any(text, TOOL_RESOURCES)
    )


def _route_request(request: ChatCompletionRequest) -> RoutingDecision:
    """
    Deterministic request router.

    Priority:
    1. Explicit API tools/functions.
    2. Empty/no-user-message -> chat.
    3. Exact conversational fast path.
    4. Strong agent patterns.
    5. Multi-step tool-oriented tasks.
    6. Generic action + tool resource.
    7. Default -> chat.

    Important:
    Agent mode must be conservative.

    False negatives are usually cheaper than false positives because the
    normal model can still answer conversationally, while unnecessarily
    starting AgentRunner increases latency and cost.
    """

    # Highest-confidence signal.
    if request.tools or getattr(request, "functions", None):
        return RoutingDecision(
            mode=ExecutionMode.AGENT,
            reason="explicit_tools",
            confidence=1.0,
        )

    latest_text = _latest_user_message(request)

    if not latest_text:
        return RoutingDecision(
            mode=ExecutionMode.CHAT,
            reason="no_user_text",
            confidence=1.0,
        )

    # Exact fast conversational path.
    if latest_text in FAST_CHAT_EXACT:
        return RoutingDecision(
            mode=ExecutionMode.CHAT,
            reason="fast_chat_exact",
            confidence=1.0,
        )

    # Short informational questions should remain chat unless a strong
    # external-action rule matches.
    word_count = len(latest_text.split())

    if (
        word_count <= 12
        and latest_text.startswith(CHAT_PREFIXES)
        and not _has_strong_agent_pattern(latest_text)
    ):
        return RoutingDecision(
            mode=ExecutionMode.CHAT,
            reason="short_informational_request",
            confidence=0.95,
        )

    # Strong deterministic patterns.
    if _has_strong_agent_pattern(latest_text):
        return RoutingDecision(
            mode=ExecutionMode.AGENT,
            reason="strong_agent_pattern",
            confidence=0.98,
        )

    # Detect follow-up agent commands using a small recent window.
    recent_text = " ".join(_recent_user_messages(request, limit=3))

    if _looks_like_multi_step_agent_task(recent_text):
        return RoutingDecision(
            mode=ExecutionMode.AGENT,
            reason="multi_step_agent_task",
            confidence=0.93,
        )

    # Generic action + resource fallback.
    if _looks_like_action_on_resource(latest_text):
        return RoutingDecision(
            mode=ExecutionMode.AGENT,
            reason="action_on_tool_resource",
            confidence=0.88,
        )

    # Safe default.
    return RoutingDecision(
        mode=ExecutionMode.CHAT,
        reason="default_chat",
        confidence=0.90,
    )


def _should_use_agent_mode(request: ChatCompletionRequest) -> bool:
    """
    Backward-compatible helper.
    """

    return _route_request(request).mode is ExecutionMode.AGENT


# ============================================================
# Model endpoints
# ============================================================


@router.get("/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": name,
                "object": "model",
                "owned_by": "g4f",
            }
            for name in registry.healthy_models()
        ],
    }


# ============================================================
# Chat Completions endpoint
# ============================================================


@router.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    model = request.model or settings.DEFAULT_MODEL

    _validate_model(model)

    decision = _route_request(request)

    logger.info(
        "request_routed mode=%s reason=%s confidence=%.2f stream=%s model=%s",
        decision.mode.value,
        decision.reason,
        decision.confidence,
        request.stream,
        model,
    )

    if request.stream:
        return StreamingResponse(
            _stream_response(
                request=request,
                model=model,
                decision=decision,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return await _non_stream_response(
        request=request,
        model=model,
        decision=decision,
    )


# ============================================================
# Validation
# ============================================================


def _validate_model(model: str) -> None:
    if (
        registry.models
        and model in registry.models
        and not registry.models[model].healthy
    ):
        raise HTTPException(
            status_code=400,
            detail=f"النموذج '{model}' غير صحي حالياً",
        )


# ============================================================
# Non-stream execution
# ============================================================


async def _non_stream_response(
    request: ChatCompletionRequest,
    model: str,
    decision: RoutingDecision,
) -> ChatCompletionResponse:

    if decision.mode is ExecutionMode.AGENT:
        return await _run_agent_non_stream(request, model)

    return await _run_chat_non_stream(request, model)


async def _run_agent_non_stream(
    request: ChatCompletionRequest,
    model: str,
) -> ChatCompletionResponse:
    """
    Run the full agent loop.

    AgentRunner is responsible for:
    - model calls
    - tool calls
    - tool execution
    - iterative reasoning loop
    - final answer generation
    """

    content, tool_calls, finish_reason = await runner.run(request)

    message = ChatMessage(
        role="assistant",
        content=content or None,
    )

    if tool_calls:
        message.tool_calls = tool_calls

    return ChatCompletionResponse(
        model=model,
        choices=[
            Choice(
                message=message,
                finish_reason=finish_reason or "stop",
            )
        ],
    )


async def _run_chat_non_stream(
    request: ChatCompletionRequest,
    model: str,
) -> ChatCompletionResponse:
    """
    Direct single model call without AgentRunner.

    Important:
    Ordinary stream=False requests should NOT enter the agent loop.
    """

    messages = runner._prepare_messages(request, strict=False)

    response = await client.chat.completions.create(
        model=model,
        messages=messages,
    )

    if not response.choices:
        raise HTTPException(
            status_code=502,
            detail="لم يُرجع مزود النموذج أي اختيارات",
        )

    source_choice = response.choices[0]
    source_message = source_choice.message

    content = getattr(source_message, "content", None)
    finish_reason = getattr(source_choice, "finish_reason", None) or "stop"

    message = ChatMessage(
        role="assistant",
        content=content,
    )

    return ChatCompletionResponse(
        model=model,
        choices=[
            Choice(
                message=message,
                finish_reason=finish_reason,
            )
        ],
    )


# ============================================================
# Streaming execution
# ============================================================


async def _stream_response(
    request: ChatCompletionRequest,
    model: str,
    decision: RoutingDecision,
) -> AsyncIterator[str]:

    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    try:
        if decision.mode is ExecutionMode.AGENT:
            async for event in _stream_agent_result(
                request=request,
                model=model,
                chunk_id=chunk_id,
            ):
                yield event

            return

        async for event in _stream_direct_chat(
            request=request,
            model=model,
            chunk_id=chunk_id,
        ):
            yield event

    except Exception as exc:
        logger.exception(
            "stream_failed mode=%s model=%s",
            decision.mode.value,
            model,
        )

        yield _sse_error(exc)
        yield _sse_done()


async def _stream_direct_chat(
    request: ChatCompletionRequest,
    model: str,
    chunk_id: str,
) -> AsyncIterator[str]:
    """
    True token streaming for normal conversational requests.
    """

    logger.debug("starting direct chat stream")

    messages = runner._prepare_messages(request, strict=False)

    stream = client.chat.completions.stream(
        model=model,
        messages=messages,
    )

    role_sent = False

    async for part in stream:
        if not part.choices:
            continue

        source_choice = part.choices[0]
        source_delta = source_choice.delta

        delta_content = getattr(source_delta, "content", None)

        if delta_content:
            yield _sse_chunk(
                ChatCompletionChunk(
                    id=chunk_id,
                    model=model,
                    choices=[
                        StreamChoice(
                            delta=DeltaMessage(
                                role=None if role_sent else "assistant",
                                content=delta_content,
                            ),
                            finish_reason=None,
                        )
                    ],
                )
            )

            role_sent = True

        finish_reason = getattr(source_choice, "finish_reason", None)

        if finish_reason:
            yield _sse_chunk(
                ChatCompletionChunk(
                    id=chunk_id,
                    model=model,
                    choices=[
                        StreamChoice(
                            delta=DeltaMessage(),
                            finish_reason=finish_reason,
                        )
                    ],
                )
            )

            yield _sse_done()
            return

    # Provider ended without explicit finish_reason.
    yield _sse_chunk(
        ChatCompletionChunk(
            id=chunk_id,
            model=model,
            choices=[
                StreamChoice(
                    delta=DeltaMessage(),
                    finish_reason="stop",
                )
            ],
        )
    )

    yield _sse_done()


async def _stream_agent_result(
    request: ChatCompletionRequest,
    model: str,
    chunk_id: str,
) -> AsyncIterator[str]:
    """
    Agent execution intentionally does not stream intermediate reasoning.

    The complete agent loop runs first, then the final answer is emitted
    through an OpenAI-compatible SSE response.

    Tool calls are emitted structurally when supported by the schema.
    They are NOT serialized into assistant-visible text.
    """

    logger.debug("starting buffered agent execution")

    content, tool_calls, finish_reason = await runner.run(request)

    role_sent = False

    # First chunk: assistant role.
    yield _sse_chunk(
        ChatCompletionChunk(
            id=chunk_id,
            model=model,
            choices=[
                StreamChoice(
                    delta=DeltaMessage(role="assistant"),
                    finish_reason=None,
                )
            ],
        )
    )

    role_sent = True

    # Final assistant content.
    if content:
        yield _sse_chunk(
            ChatCompletionChunk(
                id=chunk_id,
                model=model,
                choices=[
                    StreamChoice(
                        delta=DeltaMessage(
                            content=content,
                        ),
                        finish_reason=None,
                    )
                ],
            )
        )

    # Emit tool calls structurally if DeltaMessage supports them.
    if tool_calls:
        tool_delta = DeltaMessage()

        if hasattr(tool_delta, "tool_calls"):
            tool_delta.tool_calls = tool_calls

            yield _sse_chunk(
                ChatCompletionChunk(
                    id=chunk_id,
                    model=model,
                    choices=[
                        StreamChoice(
                            delta=tool_delta,
                            finish_reason=None,
                        )
                    ],
                )
            )
        else:
            logger.warning(
                "DeltaMessage schema does not support tool_calls; "
                "tool calls omitted from streaming response"
            )

    yield _sse_chunk(
        ChatCompletionChunk(
            id=chunk_id,
            model=model,
            choices=[
                StreamChoice(
                    delta=DeltaMessage(),
                    finish_reason=finish_reason or "stop",
                )
            ],
        )
    )

    yield _sse_done()


# ============================================================
# SSE helpers
# ============================================================


def _sse_chunk(chunk: ChatCompletionChunk) -> str:
    return f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"


def _sse_done() -> str:
    return "data: [DONE]\n\n"


def _sse_error(exc: Exception) -> str:
    """
    OpenAI-style error envelope.

    Avoid leaking traceback/internal implementation details to clients.
    """

    payload = {
        "error": {
            "message": str(exc),
            "type": "server_error",
            "param": None,
            "code": None,
        }
    }

    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"