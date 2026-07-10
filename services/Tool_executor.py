"""
أدوات حقيقية تُنفَّذ على جانب الخادم.

سابقاً: SERVER_TOOLS = {} فارغ دائماً — الوكيل كان يستدعي أدوات لكن لا شيء
ينفذها فعلياً على الخادم (كل الأدوات كانت تُعاد للعميل عبر finish_reason="tool_calls"
حتى لو كانت أدوات بسيطة يفترض أن يديرها الخادم نفسه، مثل الوقت الحالي أو حاسبة).

طريقة الاستخدام:
1) عرّف دالة async(arguments: dict) -> str لكل أداة.
2) سجّلها عبر register_tool("اسم_الأداة", الدالة).
3) استورد هذا الملف مرة واحدة عند إقلاع التطبيق (في main.py):
       from services import tool_executor  # noqa: F401  (يكفي الاستيراد لتفعيل التسجيل)
"""
import datetime
import json
import logging

from services.agent_loop import register_tool

logger = logging.getLogger("gateway.tools.executor")


async def _get_current_datetime(arguments: dict) -> str:
    """مثال أداة حقيقية بسيطة: الوقت والتاريخ الحاليان (بدون اتصال خارجي)."""
    tz = arguments.get("timezone", "UTC")
    now = datetime.datetime.now(datetime.timezone.utc)
    return json.dumps({"utc_datetime": now.isoformat(), "requested_timezone": tz}, ensure_ascii=False)


async def _calculator(arguments: dict) -> str:
    """
    حاسبة آمنة (بدون eval الخام) لعمليات حسابية بسيطة.
    arguments المتوقعة: {"expression": "2 + 2 * 3"}
    """
    import ast
    import operator

    expr = str(arguments.get("expression", ""))
    ops = {
        ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg,
    }

    def _eval(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](_eval(node.operand))
        raise ValueError(f"تعبير غير مدعوم: {expr}")

    try:
        result = _eval(ast.parse(expr, mode="eval").body)
        return json.dumps({"expression": expr, "result": result}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"تعذر حساب '{expr}': {exc}"}, ensure_ascii=False)


async def _web_search_placeholder(arguments: dict) -> str:
    """
    قالب جاهز لربط بحث ويب حقيقي (SerpAPI / Tavily / Brave Search API إلخ).
    حالياً يرجع خطأ واضحاً بدل أن يتظاهر بالعمل.
    """
    logger.warning("تم استدعاء web_search لكن لا يوجد مزود بحث مُهيّأ فعلياً")
    return json.dumps({
        "error": "أداة البحث غير مفعّلة على الخادم بعد. أضف مفتاح API لمزود بحث "
                 "(مثل Tavily أو SerpAPI) في core/config.py ونفّذ الاستدعاء الفعلي هنا."
    }, ensure_ascii=False)


# --- التسجيل الفعلي في SERVER_TOOLS (يجعل هذه الأدوات تُنفَّذ على الخادم مباشرة) ---
register_tool("get_current_datetime", _get_current_datetime)
register_tool("calculate", _calculator)
register_tool("web_search", _web_search_placeholder)

logger.info("تم تسجيل أدوات الخادم: get_current_datetime, calculate, web_search(placeholder)")