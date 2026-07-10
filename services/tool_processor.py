"""تطبيع استدعاءات الأدوات، إصلاح JSON المكسور، والتحقق ضد المخططات."""
import json
import logging
import re
from typing import Any, Iterator, Optional

from jsonschema import Draft7Validator
from models.canonical import CanonicalToolCall
from models.openai_schemas import ToolDef

logger = logging.getLogger("gateway.tools")

# مفاتيح مرادفة شائعة يستخدمها النماذج المختلفة للتعبير عن اسم الأداة/الوسائط
_NAME_KEYS = ("tool", "name", "function_name")
_ARGS_KEYS = ("arguments", "args", "parameters", "params")


def repair_json(raw: Any) -> Optional[dict]:
    """إصلاح تلقائي لـ JSON مكسور: فواصل زائدة، اقتباسات مفردة، أقواس ناقصة."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    candidates = [raw]
    fixed = raw.strip()
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)  # إزالة الفواصل الزائدة
    fixed = re.sub(r"'([^']*)'\s*:", r'"\1":', fixed)  # مفاتيح باقتباس مفرد
    fixed = re.sub(r":\s*'([^']*)'", r': "\1"', fixed)  # قيم باقتباس مفرد
    open_diff = fixed.count("{") - fixed.count("}")
    if open_diff > 0:
        fixed += "}" * open_diff  # إغلاق الأقواس الناقصة
    candidates.append(fixed)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    logger.warning("تعذر إصلاح JSON: %.120s", raw if isinstance(raw, str) else str(raw))
    return None


def _iter_balanced_json_objects(text: str) -> Iterator[dict]:
    """
    ماسح JSON متوازن الأقواس (يستبدل الـ regex القديم).

    السابق: r'(\\{[^{}]*"tool"[^{}]*\\})' كان يرفض أي أقواس متداخلة داخل الوسائط
    (أي معطى على شكل كائن أو قائمة كان يكسر الالتقاط بالكامل).

    الحل: استخدام json.JSONDecoder().raw_decode الذي يفهم قواعد JSON فعلياً
    (بما فيها التداخل والسلاسل النصية المحتوية على أقواس) بدل نمط نصي سطحي.
    يعمل أيضاً حتى لو كان الكائن ملفوفاً داخل ```json ... ``` لأننا نبحث فقط عن أول '{'.
    """
    decoder = json.JSONDecoder()
    idx = 0
    length = len(text)
    while idx < length:
        brace = text.find("{", idx)
        if brace == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, brace)
            if isinstance(obj, dict):
                yield obj
            idx = max(end, brace + 1)
        except json.JSONDecodeError:
            idx = brace + 1


def _extract_name_and_args(obj: dict) -> Optional[tuple[str, Any]]:
    """يستخرج (name, arguments) من كائن JSON بأي من الأشكال الشائعة التي تكتبها النماذج:
    {"tool": "x", "arguments": {...}}
    {"name": "x", "arguments": {...}}
    {"function": {"name": "x", "arguments": "{...}"}}   <- شكل OpenAI عند التضمين النصي
    """
    # الشكل المتداخل: {"function": {"name": ..., "arguments": ...}}
    fn = obj.get("function")
    if isinstance(fn, dict):
        obj = fn

    name = None
    for key in _NAME_KEYS:
        if isinstance(obj.get(key), str) and obj.get(key):
            name = obj[key]
            break
    if not name:
        return None

    args: Any = {}
    for key in _ARGS_KEYS:
        if key in obj:
            args = obj[key]
            break
    return name, args


def extract_tool_json_from_text(content: str) -> list[dict]:
    """يمسح النص الحر بحثاً عن كل استدعاءات الأدوات المحتملة (قد يكون أكثر من واحد)."""
    found: list[dict] = []
    for obj in _iter_balanced_json_objects(content):
        # دعم {"tool_calls": [ {...}, {...} ]}
        wrapped = obj.get("tool_calls")
        candidates = wrapped if isinstance(wrapped, list) else [obj]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            parsed = _extract_name_and_args(candidate)
            if parsed:
                name, args = parsed
                args = repair_json(args) if isinstance(args, str) else (args or {})
                found.append({"name": name, "arguments": args if isinstance(args, dict) else {}})
    return found


def extract_final_answer(content: str) -> Optional[str]:
    """يبحث عن {"final": "..."} صريح — يُستخدم في البروتوكول الصارم الجديد بدل heuristic الطول."""
    for obj in _iter_balanced_json_objects(content):
        if isinstance(obj.get("final"), str) and obj["final"].strip():
            return obj["final"]
    return None


def normalize_tool_calls(message: Any) -> list[CanonicalToolCall]:
    """تطبيع الاستدعاءات من tool_calls الرسمية أو من JSON مضمّن في النص."""
    calls: list[CanonicalToolCall] = []

    # المصدر 1: tool_calls البنيوية (تُملأ فقط إذا مُرر tools= فعلياً للـ API - انظر agent_loop.py)
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

    # المصدر 2: JSON مضمّن في المحتوى النصي (fallback لمزودي g4f الذين لا يدعمون tool_calls البنيوية)
    content = getattr(message, "content", "") or ""
    if not calls and content:
        for found in extract_tool_json_from_text(content):
            calls.append(CanonicalToolCall(
                name=found["name"],
                arguments=found["arguments"],
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
    call.valid = True
    call.rejection_reason = None
    return call