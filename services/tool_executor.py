"""
أدوات حقيقية تُنفَّذ على جانب الخادم.

سابقاً: SERVER_TOOLS = {} فارغ دائماً — الوكيل كان يستدعي أدوات لكن لا شيء
ينفذها فعلياً على الخادم (كل الأدوات كانت تُعاد للعميل عبر finish_reason="tool_calls"
حتى لو كانت أدوات بسيطة يفترض أن يديرها الخادم نفسه، مثل الوقت الحالي أو حاسبة).

الآن: أضفنا أداة عامة واحدة (call_tool) تنفذ أي أداة عبر الباك اند الخارجي
(https://ayb-bh92-tools.hf.space/call)، مما يغطي جميع الأدوات الأساسية (27 أداة)
وجميع أدوات الفيديو/الصوت (100+ أداة) وجميع أدوات Composio، دون الحاجة لتسجيل
كل أداة على حدة.

طريقة الاستخدام:
1) عرّف دالة async(arguments: dict) -> str لكل أداة.
2) سجّلها عبر register_tool("اسم_الأداة", الدالة).
3) استورد هذا الملف مرة واحدة عند إقلاع التطبيق (في main.py):
       from services import tool_executor  # noqa: F401  (يكفي الاستيراد لتفعيل التسجيل)
"""
import datetime
import json
import logging
import aiohttp

from services.agent_loop import register_tool

logger = logging.getLogger("gateway.tools.executor")

# ===== الأدوات الأساسية (تعمل محلياً) =====
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

# ===== الأداة العامة للاتصال بجميع أدوات الباك اند =====
async def _call_any_tool(arguments: dict) -> str:
    """
    ★ الأداة الشاملة ★
    تنفذ أي أداة عبر الباك اند الخارجي (https://ayb-bh92-tools.hf.space/call).
    تغطي هذه الأداة جميع أدوات الباك اند:
    - أدوات البحث والتصفح (Firecrawl)
    - أدوات التوثيق (Context7)
    - أدوات Composio (Gmail, Slack, GitHub, إلخ)
    - أدوات المستندات (PDF, DOCX, PPTX, XLSX)
    - أدوات الفيديو/الصوت (أكثر من 100 أداة)
    - وأي أداة جديدة تُضاف للباك اند مستقبلاً.

    المتطلبات:
        arguments: {
            "tool": "اسم_الأداة_في_الباك_اند",  # مثل create_pdf, cut_video, composio_execute_tool
            "arguments": { ... }  # وسائط الأداة المطلوبة
        }
    
    مثال:
        {
            "tool": "create_pdf",
            "arguments": {
                "title": "الذكاء الاصطناعي",
                "content_markdown": "# مقدمة ..."
            }
        }
    """
    url = "https://ayb-bh92-tools.hf.space/call"
    tool_name = arguments.get("tool")
    tool_args = arguments.get("arguments", {})

    if not tool_name:
        return json.dumps({"error": "الحقل 'tool' مطلوب في الوسائط"}, ensure_ascii=False)

    payload = {"tool": tool_name, "arguments": tool_args}
    
    # ✅ التعديل 1: إضافة مهلة زمنية 30 ثانية
    timeout = aiohttp.ClientTimeout(total=30)
    session = None

    try:
        session = aiohttp.ClientSession(timeout=timeout)
        async with session.post(url, json=payload) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("ok"):
                    result = data.get("result", {})
                    if isinstance(result, dict) and "file_url" in result:
                        return json.dumps({
                            "status": "success",
                            "file_url": result["file_url"],
                            "message": f"تم تنفيذ {tool_name} بنجاح. رابط الملف: {result['file_url']}"
                        }, ensure_ascii=False)
                    return json.dumps({
                        "status": "success",
                        "result": result
                    }, ensure_ascii=False)
                else:
                    return json.dumps({
                        "status": "error",
                        "error": data.get("error", f"فشل تنفيذ الأداة {tool_name}")
                    }, ensure_ascii=False)
            else:
                return json.dumps({
                    "status": "error",
                    "error": f"خطأ في الاتصال بالباك اند: {response.status}"
                }, ensure_ascii=False)
    except aiohttp.ClientTimeout as e:
        return json.dumps({
            "status": "error",
            "error": f"المهلة الزمنية انتهت (30 ثانية): {str(e)}"
        }, ensure_ascii=False)
    except aiohttp.ClientError as e:
        return json.dumps({
            "status": "error",
            "error": f"خطأ في الشبكة: {str(e)}"
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": f"استثناء غير متوقع: {str(e)}"
        }, ensure_ascii=False)
    finally:
        # ✅ التعديل 2: إغلاق الجلسة يدوياً في النهاية
        if session and not session.closed:
            await session.close()

# ===== تسجيل الأدوات في SERVER_TOOLS =====
# الأدوات الأساسية (تعمل محلياً)
register_tool("get_current_datetime", _get_current_datetime)
register_tool("calculate", _calculator)
register_tool("web_search", _web_search_placeholder)

# ★ الأداة الشاملة (تغطي جميع أدوات الباك اند) ★
register_tool("call_tool", _call_any_tool)

logger.info("✅ تم تسجيل أدوات الخادم: get_current_datetime, calculate, web_search(placeholder), call_tool (للتعامل مع جميع أدوات الباك اند)")
logger.info("✅ أداة call_tool تغطي أكثر من 120 أداة في الباك اند (بما فيها الفيديو و Composio)")