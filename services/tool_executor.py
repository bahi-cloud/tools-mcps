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

# ===== الأداة العامة للاتصال بجميع أدوات الباك اند =====
async def _call_any_tool(arguments: dict) -> str:
    """تنفيذ أي أداة عبر الباك اند الخارجي."""
    url = "https://ayb-bh92-tools.hf.space/call"
    tool_name = arguments.get("tool")
    tool_args = arguments.get("arguments", {})

    if not tool_name:
        return json.dumps({"error": "الحقل 'tool' مطلوب في الوسائط"}, ensure_ascii=False)

    payload = {"tool": tool_name, "arguments": tool_args}
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
        if session and not session.closed:
            await session.close()

# ===== تسجيل الأدوات في SERVER_TOOLS =====
# الأدوات الأساسية (تعمل محلياً)
register_tool("get_current_datetime", _get_current_datetime)
register_tool("calculate", _calculator)

# ★ الأداة الشاملة (تغطي جميع أدوات الباك اند) ★
# هذه الأداة تغطي: البحث، التوثيق، Composio، المستندات، الوسائط، وأدوات الفيديو/الصوت
register_tool("call_tool", _call_any_tool)

# ★ تسجيل أدوات محددة كاختصارات ★
# هذه تسمح للنموذج باستدعاء الأدوات مباشرة دون الحاجة لاستخدام call_tool
# ولكنها تستخدم نفس الدالة العامة للتنفيذ
tools_with_shortcuts = [
    # أدوات البحث والتصفح
    "firecrawl_search", "firecrawl_scrape",
    # أدوات التوثيق
    "context7_resolve_library", "context7_get_docs",
    # أدوات Composio
    "composio_list_connected_tools", "composio_list_tools", 
    "composio_execute_tool", "composio_create_session",
    "composio_get_session", "composio_session_toolkits", "composio_authorize_toolkit",
    # أدوات المستندات
    "create_docx", "create_pptx", "create_pdf", "read_pdf", 
    "pdf_info", "create_xlsx", "read_xlsx",
    # أدوات التحويل
    "markdown_to_html",
    # أدوات الوسائط
    "fetch_media_to_file", "write_text_file",
    # أدوات الفيديو/الصوت (الأكثر شيوعاً)
    "extract_audio_from_video", "trim_video", "convert_audio_properties",
    "convert_video_properties", "change_aspect_ratio", "convert_audio_format",
    "set_audio_bitrate", "set_audio_sample_rate", "set_audio_channels",
    "convert_video_format", "set_video_resolution", "set_video_codec",
    "set_video_bitrate", "set_video_frame_rate", "set_video_audio_track_codec",
    "set_video_audio_track_bitrate", "set_video_audio_track_sample_rate",
    "set_video_audio_track_channels", "add_subtitles", "add_text_overlay",
    "add_image_overlay", "concatenate_videos", "change_video_speed",
    "remove_silence", "add_b_roll", "add_basic_transitions",
]

# تسجيل الاختصارات
async def _tool_wrapper(arguments: dict) -> str:
    """غلاف لأي أداة باستخدام call_tool."""
    return await _call_any_tool(arguments)

for tool_name in tools_with_shortcuts:
    register_tool(tool_name, _tool_wrapper)

logger.info(f"✅ تم تسجيل {len(tools_with_shortcuts) + 2} أداة في SERVER_TOOLS (بما فيها أدوات الفيديو والبحث وComposio)")
logger.info("✅ جميع الأدوات تغطي أكثر من 110 أداة في الباك اند")