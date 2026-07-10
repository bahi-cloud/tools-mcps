"""تطبيع استدعاءات الأدوات، إصلاح JSON المكسور، والتحقق ضد المخططات."""
import json
import logging
import re
from typing import Any, Optional

from jsonschema import Draft7Validator
from models.canonical import CanonicalToolCall
from models.openai_schemas import ToolDef

logger = logging.getLogger("gateway.tools")

# نمط لالتقاط كتل JSON من نص حر (عندما يكتب النموذج الاستدعاء داخل content)
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```|(\{[^{}]*\"tool\"[^{}]*\})", re.DOTALL)


def repair_json(raw: str) -> Optional[dict]:
    """إصلاح تلقائي لـ JSON مكسور: فواصل زائدة، اقتباسات مفردة، أقواس ناقصة."""
    candidates = [raw]
    fixed = raw.strip()
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)                 # إزالة الفواصل الزائدة
    fixed = re.sub(r"'([^']*)'\s*:", r'"\1":', fixed)            # مفاتيح باقتباس مفرد
    fixed = re.sub(r":\s*'([^']*)'", r': "\1"', fixed)           # قيم باقتباس مفرد
    open_diff = fixed.count("{") - fixed.count("}")
    if open_diff > 0:
        fixed += "}" * open_diff                                 # إغلاق الأقواس الناقصة
    candidates.append(fixed)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    logger.warning("تعذر إصلاح JSON: %.120s", raw)
    return None


def normalize_tool_calls(message: Any) -> list[CanonicalToolCall]:
    """تطبيع الاستدعاءات من tool_calls الرسمية أو من JSON مضمّن في النص."""
    calls: list[CanonicalToolCall] = []

    # المصدر 1: tool_calls البنيوية
    for tc in (getattr(message, "tool_calls", None) or []):
        fn = tc.function if hasattr(tc, "function") else tc.get("function", {})
        name = getattr(fn, "name", None) or fn.get("name", "")
        raw_args = getattr(fn, "arguments", None) or fn.get("arguments", "{}")
        args = repair_json(raw_args) if isinstance(raw_args, str) else raw_args
        calls.append(CanonicalToolCall(
            name=name,
            arguments=args or {},
            valid=args is not None,
            rejection_reason=None if args is not None else "وسائط JSON غير قابلة للإصلاح",
        ))

    # المصدر 2: JSON مضمّن في المحتوى النصي
    content = getattr(message, "content", "") or ""
    if not calls and content:
        for match in _JSON_BLOCK.finditer(content):
            block = match.group(1) or match.group(2)
            parsed = repair_json(block)
            if parsed and "tool" in parsed:
                calls.append(CanonicalToolCall(
                    name=parsed.get("tool", ""),
                    arguments=parsed.get("arguments", parsed.get("args", {})),
                ))
    return calls


def validate_call(call: CanonicalToolCall, tools: list[ToolDef]) -> CanonicalToolCall:
    """التحقق من الاستدعاء ضد JSON Schema الخاص بالأداة، مع سبب رفض واضح."""
    schema_map = {t.function.name: t.function.parameters for t in tools}
    if call.name not in schema_map:
        call.valid = False
        call.rejection_reason = f"أداة غير معروفة: '{call.name}'. الأدوات المتاحة: {list(schema_map)}"
        return call
    errors = sorted(Draft7Validator(schema_map[call.name]).iter_errors(call.arguments), key=str)
    if errors:
        call.valid = False
        call.rejection_reason = "فشل التحقق من المخطط: " + "; ".join(e.message for e in errors[:3])
    return call
