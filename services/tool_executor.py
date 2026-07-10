import datetime
import json
import logging
import aiohttp

from services.agent_loop import register_tool

logger = logging.getLogger("gateway.tools.executor")

# الرابط الموحد للباك اند الخارجي للأدوات
TOOLS_BACKEND_URL = "https://ayb-bh92-tools.hf.space/call"

# ===== الأدوات الأساسية (تعمل محلياً بالكامل) =====

async def _get_current_datetime(arguments: dict) -> str:
    """الوقت والتاريخ الحاليان (محلياً)."""
    tz = arguments.get("timezone", "UTC")
    now = datetime.datetime.now(datetime.timezone.utc)
    return json.dumps({"utc_datetime": now.isoformat(), "requested_timezone": tz}, ensure_ascii=False)

async def _calculator(arguments: dict) -> str:
    """حاسبة آمنة لعمليات حسابية مبنية على الشجرة المعجمية (AST)."""
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


# ===== المحرك الذكي والموحد لاستدعاء أدوات الباك اند الخارجي =====

async def _smart_tool_router(tool_name: str, arguments: dict) -> str:
    """
    الموجه الذكي: 
    1. يفحص الحقول بحثاً عن روابط ويب (HTTP/HTTPS) في أدوات الوسائط ويقوم بتنزيلها تلقائياً.
    2. يحدد المهلة الزمنية المناسبة بناءً على نوع الأداة.
    3. يرسل الحمولة المهيأة إلى الباك اند الخارجي.
    """
    tool_args = dict(arguments)  # أخذ نسخة لتجنب تعديل البيانات الأصلية بالخطأ

    # الحقول المتوقع أن تحتوي على روابط ملفات تحتاج تنزيل مسبق على الخادم
    media_path_fields = ["video_path", "input_audio_path", "media_path", "image_path", "srt_file_path"]
    
    # قائمة الأدوات الثقيلة (الفيديو/الصوت) التي تتطلب وقتاً أطول للمعالجة
    heavy_video_tools = [
        "trim_video", "extract_audio_from_video", "add_subtitles", "concatenate_videos",
        "convert_video_properties", "change_aspect_ratio", "convert_video_format",
        "set_video_resolution", "set_video_codec", "set_video_bitrate", "set_video_frame_rate",
        "add_text_overlay", "add_image_overlay", "change_video_speed", "remove_silence", "add_b_roll"
    ]

    # تحديد المهلة الزمنية (90 ثانية للفيديو، و30 ثانية للأدوات العادية كالبحث وComposio)
    timeout_duration = 90 if tool_name in heavy_video_tools else 30

    try:
        async with aiohttp.ClientSession() as session:
            
            # --- المرحلة الأولى: الفحص والتحويل التلقائي للروابط الخارجية ---
            for field in media_path_fields:
                if field in tool_args and str(tool_args[field]).startswith(("http://", "https://")):
                    logger.info(f"🔄 جلب وتنزيل الملف الخارجي للحقل '{field}' تلقائياً قبل تنفيذ أداة: {tool_name}")
                    
                    download_payload = {
                        "tool": "fetch_media_to_file",
                        "arguments": {"url_or_base64": tool_args[field]}
                    }
                    
                    async with session.post(TOOLS_BACKEND_URL, json=download_payload, timeout=45) as dl_resp:
                        if dl_resp.status == 200:
                            dl_data = await dl_resp.json()
                            if dl_data.get("ok") and "local_path" in dl_data.get("result", {}):
                                # استبدال رابط الويب بمسار الملف المحلي الفعلي على الخادم
                                local_path = dl_data["result"]["local_path"]
                                tool_args[field] = local_path
                                logger.info(f"✅ تم تنزيل الملف بنجاح وحفظه في: {local_path}")
                            else:
                                error_msg = dl_data.get("error", "فشل استخراج مسار الملف المحلي.")
                                return json.dumps({"status": "error", "error": f"فشل تنزيل ملف الحقل {field}: {error_msg}"}, ensure_ascii=False)
                        else:
                            return json.dumps({"status": "error", "error": f"خطأ في شبكة خادم التنزيل: استجابة {dl_resp.status}"}, ensure_ascii=False)

            # --- المرحلة الثانية: إرسال الطلب الفعلي للأداة المطلوبة ---
            payload = {
                "tool": tool_name,
                "arguments": tool_args
            }
            
            logger.info(f"🚀 إرسال طلب تنفيذ الأداة [{tool_name}] بمهلة زمنية {timeout_duration} ثانية...")
            
            async with session.post(
                TOOLS_BACKEND_URL, 
                json=payload, 
                timeout=aiohttp.ClientTimeout(total=timeout_duration)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("ok"):
                        result = data.get("result", {})
                        
                        # إذا كانت النتيجة تحتوي على رابط ملف جاهز للمستخدم، صغها بشكل مقروء ونظيف
                        if isinstance(result, dict) and "file_url" in result:
                            return json.dumps({
                                "status": "success",
                                "file_url": result["file_url"],
                                "message": f"تم تنفيذ {tool_name} بنجاح. رابط الملف الناتج هو: {result['file_url']}"
                            }, ensure_ascii=False)
                            
                        return json.dumps({"status": "success", "result": result}, ensure_ascii=False)
                    else:
                        return json.dumps({"status": "error", "error": data.get("error", f"فشل تنفيذ {tool_name}")}, ensure_ascii=False)
                else:
                    return json.dumps({"status": "error", "error": f"خطأ في الاتصال بالباك اند: استجابة {response.status}"}, ensure_ascii=False)

    except aiohttp.ClientTimeout:
        return json.dumps({"status": "error", "error": f"انتهت المهلة الزمنية المحددة ({timeout_duration} ثانية) أثناء معالجة أداة {tool_name}"}, ensure_ascii=False)
    except aiohttp.ClientError as e:
        return json.dumps({"status": "error", "error": f"خطأ في الشبكة أثناء الاتصال بالخادم الخارجي: {str(e)}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": f"خطأ غير متوقع في معالج الأدوات: {str(e)}"}, ensure_ascii=False)


# ===== هندسة التغليف والتسجيل التلقائي (التكامل مع الـ Agent Loop) =====

def make_tool_callback(name: str):
    """دالة مصنع (Factory Function) لربط اسم الأداة المستدعاة ديناميكياً."""
    async def callback(arguments: dict) -> str:
        return await _smart_tool_router(name, arguments)
    return callback

# تسجيل الأدوات المحلية الأساسية
register_tool("get_current_datetime", _get_current_datetime)
register_tool("calculate", _calculator)

# قائمة جميع أدوات الباك اند المتاحة ليراها ويستدعيها الوكيل بشكل مستقل
all_backend_tools = [
    # البحث والتصفح
    "firecrawl_search", "firecrawl_scrape",
    
    # التوثيق البرمجي
    "context7_resolve_library", "context7_get_docs",
    
    # منصة ومجموعة أدوات Composio
    "composio_list_connected_tools", "composio_list_tools", 
    "composio_execute_tool", "composio_create_session",
    "composio_get_session", "composio_session_toolkits", "composio_authorize_toolkit",
    
    # المستندات والمكاتب
    "create_docx", "create_pptx", "create_pdf", "read_pdf", "pdf_info", "create_xlsx", "read_xlsx",
    
    # التحويلات والوسائط الوسيطة
    "markdown_to_html", "fetch_media_to_file", "write_text_file", "health_check",
    
    # أدوات هندسة الفيديو والصوت المتقدمة
    "extract_audio_from_video", "trim_video", "convert_audio_properties",
    "convert_video_properties", "change_aspect_ratio", "convert_audio_format",
    "set_audio_bitrate", "set_audio_sample_rate", "set_audio_channels",
    "convert_video_format", "set_video_resolution", "set_video_codec",
    "set_video_bitrate", "set_video_frame_rate", "set_video_audio_track_codec",
    "set_video_audio_track_bitrate", "set_video_audio_track_sample_rate",
    "set_video_audio_track_channels", "add_subtitles", "add_text_overlay",
    "add_image_overlay", "concatenate_videos", "change_video_speed",
    "remove_silence", "add_b_roll", "add_basic_transitions"
]

# تسجيل كافة أدوات الباك اند تلقائياً عبر المصنع الديناميكي لتعمل كأسماء مستقلة ومباشرة للوكيل
for tool in all_backend_tools:
    register_tool(tool, make_tool_callback(tool))

logger.info(f"🚀 [Gateway] تم تسجيل وتأمين {len(all_backend_tools) + 2} أداة بنجاح في بيئة نظام الوكيل الذكي.")
