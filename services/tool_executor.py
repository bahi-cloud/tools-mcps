"""
أدوات حقيقية تُنفَّذ على جانب الخادم.

تدعم الآن جميع الأدوات:
- البحث والتصفح (Firecrawl)
- التوثيق (Context7)
- Composio (Gmail, Slack, GitHub, إلخ)
- المستندات (PDF, DOCX, PPTX, XLSX)
- التحويلات (Markdown → HTML)
- الوسائط (تحميل/كتابة ملفات)
- أدوات الفيديو/الصوت (أكثر من 90 أداة)
"""
import datetime
import json
import logging
import aiohttp

from services.agent_loop import register_tool

logger = logging.getLogger("gateway.tools.executor")

# ===== الأدوات الأساسية (تعمل محلياً) =====
async def _get_current_datetime(arguments: dict) -> str:
    """الوقت والتاريخ الحاليان."""
    tz = arguments.get("timezone", "UTC")
    now = datetime.datetime.now(datetime.timezone.utc)
    return json.dumps({"utc_datetime": now.isoformat(), "requested_timezone": tz}, ensure_ascii=False)

async def _calculator(arguments: dict) -> str:
    """حاسبة آمنة لعمليات حسابية بسيطة."""
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

# ===== الأداة العامة للاتصال بجميع أدوات الباك اند (مع إغلاق تلقائي للجلسات) =====
async def _call_any_tool(arguments: dict) -> str:
    """
    تنفذ أي أداة عبر الباك اند الخارجي مع إغلاق تلقائي للجلسات.
    """
    url = "https://ayb-bh92-tools.hf.space/call"
    tool_name = arguments.get("tool")
    tool_args = arguments.get("arguments", {})

    if not tool_name:
        return json.dumps({"error": "الحقل 'tool' مطلوب"}, ensure_ascii=False)

    # قائمة أدوات الفيديو التي تحتاج وقتاً أطول
    video_tools = [
        "trim_video", "extract_audio_from_video", "add_subtitles", "concatenate_videos",
        "convert_video_properties", "add_text_overlay", "add_image_overlay",
        "change_video_speed", "remove_silence", "add_b_roll", "add_basic_transitions"
    ]
    
    payload = {"tool": tool_name, "arguments": tool_args}
    
    # مهلة أطول لأدوات الفيديو (60 ثانية)، و30 ثانية للباقي
    timeout_duration = 60 if tool_name in video_tools else 30

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, 
                json=payload, 
                timeout=aiohttp.ClientTimeout(total=timeout_duration)
            ) as response:
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
                            "error": data.get("error", f"فشل تنفيذ {tool_name}")
                        }, ensure_ascii=False)
                else:
                    return json.dumps({
                        "status": "error",
                        "error": f"خطأ في الاتصال: {response.status}"
                    }, ensure_ascii=False)
    except aiohttp.ClientTimeout:
        return json.dumps({
            "status": "error",
            "error": f"انتهت المهلة ({timeout_duration} ثانية) لأداة {tool_name}"
        }, ensure_ascii=False)
    except aiohttp.ClientError as e:
        return json.dumps({
            "status": "error",
            "error": f"خطأ في الشبكة: {str(e)}"
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": f"خطأ غير متوقع: {str(e)}"
        }, ensure_ascii=False)

# ===== تسجيل الأدوات =====
register_tool("get_current_datetime", _get_current_datetime)
register_tool("calculate", _calculator)
register_tool("call_tool", _call_any_tool)

# أدوات شائعة كاختصارات
tools = [
    "firecrawl_search", "firecrawl_scrape",
    "context7_resolve_library", "context7_get_docs",
    "composio_list_connected_tools", "composio_list_tools", 
    "composio_execute_tool", "composio_create_session",
    "composio_get_session", "composio_session_toolkits", "composio_authorize_toolkit",
    "create_docx", "create_pptx", "create_pdf", "read_pdf", 
    "pdf_info", "create_xlsx", "read_xlsx",
    "markdown_to_html", "fetch_media_to_file", "write_text_file",
    "extract_audio_from_video", "trim_video", "add_subtitles",
    "concatenate_videos", "change_video_speed", "remove_silence"
]

for name in tools:
    register_tool(name, _call_any_tool)

logger.info(f"✅ تم تسجيل {len(tools) + 3} أداة (جميعها تغلق الجلسات تلقائياً)")