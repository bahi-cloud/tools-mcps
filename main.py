# main.py - التعديلات الجراحية النهائية لإصلاح مشكلة user_id + إزالة تكرار الأدوات
# + إضافة خاصية "الوكيل الخارق" (Super Agent Switch) لتفعيل/تعطيل كل الأدوات دفعة واحدة
import os
import io
import json
import base64
import ipaddress
import socket
import uuid
import asyncio
import logging
import traceback
import shutil
from contextlib import asynccontextmanager, AsyncExitStack
from pathlib import Path
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse
import httpx
from fastapi import FastAPI, HTTPException, Header, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logger = logging.getLogger("main")
logging.basicConfig(level=logging.INFO)

# ══════════════════════════════════════════════════════════
# استيراد Hub Core مع عرض الخطأ الحقيقي
# ══════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).parent

try:
    from hub import create_services, AccessContext
    logger.info("✅ تم استيراد hub.py بنجاح.")
    registry, auth = create_services()
except Exception as e:
    logger.error(f"❌ فشل استيراد hub.py: {e}")
    traceback.print_exc()
    
    class AccessContext:
        def __init__(self, client_id="internal", allowed_servers=None, allowed_domains=None, allowed_tags=None, allowed_tools=None):
            self.client_id = client_id
            self.allowed_servers = allowed_servers
            self.allowed_domains = allowed_domains
            self.allowed_tags = allowed_tags
            self.allowed_tools = allowed_tools
    
    class MockRegistry:
        async def startup_from_config(self):
            return {"started": [], "errors": {}}
        async def shutdown(self):
            pass
        async def call_tool(self, tool_name, arguments, context):
            return {"error": f"Tool '{tool_name}' not found"}
        @property
        def servers(self):
            return {}
        @property
        def tool_routes(self):
            return {}
    
    class MockAuth:
        async def reload(self):
            pass
    
    registry = MockRegistry()
    auth = MockAuth()

def internal_context():
    return AccessContext(client_id="internal")

# ══════════════════════════════════════════════════════════
# إعداد عام وتكوين البيئة
# ══════════════════════════════════════════════════════════
FILES_DIR = BASE_DIR / "files"
FILES_DIR.mkdir(exist_ok=True)

FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
CONTEXT7_API_KEY = os.environ.get("CONTEXT7_API_KEY", "")
COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY", "")
COMPOSIO_DEFAULT_USER_ID = os.environ.get("COMPOSIO_DEFAULT_USER_ID", "default")
COMPOSIO_BASE_URL = "https://backend.composio.dev/api/v3"
COMPOSIO_BASE_URL_V31 = "https://backend.composio.dev/api/v3.1"
APP_SECRET = os.environ.get("APP_SECRET", "")
SPACE_BASE_URL = os.environ.get("SPACE_BASE_URL", "")

# ══════════════════════════════════════════════════════════
# ★ ★ ★ الوكيل الخارق (Super Agent Switch) ★ ★ ★
# مفتاح واحد يفعّل أو يعطّل كل الأدوات دفعة واحدة (المضمّنة + أدوات الفيديو + أدوات Hub)
# ══════════════════════════════════════════════════════════
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)
SUPER_AGENT_STATE_FILE = STATE_DIR / "super_agent.json"

# القيمة الافتراضية: كل الأدوات مفعّلة
_super_agent_enabled: bool = True

def _load_super_agent_state() -> None:
    """تحميل حالة الوكيل الخارق من القرص عند بدء التشغيل (لتبقى الحالة محفوظة بعد إعادة التشغيل)."""
    global _super_agent_enabled
    try:
        if SUPER_AGENT_STATE_FILE.exists():
            data = json.loads(SUPER_AGENT_STATE_FILE.read_text(encoding="utf-8"))
            _super_agent_enabled = bool(data.get("enabled", True))
            logger.info(f"🔌 تم تحميل حالة الوكيل الخارق من القرص: enabled={_super_agent_enabled}")
    except Exception as e:
        logger.warning(f"تعذّر تحميل حالة الوكيل الخارق، سيتم استخدام القيمة الافتراضية (مفعّل): {e}")
        _super_agent_enabled = True

def _save_super_agent_state() -> None:
    try:
        SUPER_AGENT_STATE_FILE.write_text(
            json.dumps({"enabled": _super_agent_enabled}, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"تعذّر حفظ حالة الوكيل الخارق على القرص: {e}")

def is_super_agent_enabled() -> bool:
    return _super_agent_enabled

def set_super_agent_enabled(enabled: bool) -> Dict[str, Any]:
    """تفعيل/تعطيل كل الأدوات دفعة واحدة. تُرجع ملخصاً بعدد الأدوات المتأثرة."""
    global _super_agent_enabled
    _super_agent_enabled = bool(enabled)
    _save_super_agent_state()

    builtin_count = len(TOOLS_MANIFEST) if "TOOLS_MANIFEST" in globals() else 0
    video_count = len(video_tool_names)
    hub_count = len(registry.tool_routes) if hasattr(registry, "tool_routes") else 0

    logger.info(
        f"🔁 تم {'تفعيل' if _super_agent_enabled else 'تعطيل'} الوكيل الخارق — "
        f"الأدوات المضمّنة={builtin_count}، أدوات الفيديو={video_count}، أدوات Hub={hub_count}"
    )
    return {
        "enabled": _super_agent_enabled,
        "affected_tools_count": builtin_count + video_count + hub_count,
        "builtin_tools_count": builtin_count,
        "video_tools_count": video_count,
        "hub_tools_count": hub_count,
    }

# ══════════════════════════════════════════════════════════
# ★ ★ ★ تعريف TOOLS_MANIFEST (قبل استخدامه في _connect_video_mcp) ★ ★ ★
# ══════════════════════════════════════════════════════════
TOOLS_MANIFEST = [
    # سيتم إضافة أدوات الفيديو لاحقاً في _connect_video_mcp
]

# ══════════════════════════════════════════════════════════
# الاتصال بخادم أدوات الفيديو/الصوت
# ══════════════════════════════════════════════════════════
VIDEO_MCP_SCRIPT = BASE_DIR / "video-audio-mcp" / "server.py"

video_session: Optional[Any] = None
video_tool_names: set = set()
video_tool_schemas: Dict[str, dict] = {}
_video_exit_stack: Optional[AsyncExitStack] = None

async def _connect_video_mcp() -> None:
    global video_session, video_tool_names, _video_exit_stack
    if not VIDEO_MCP_SCRIPT.exists():
        logger.warning("video-audio-mcp/server.py غير موجود على القرص — سيتم تخطي أدوات الفيديو")
        return
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        logger.warning("حزمة mcp غير مثبّتة — سيتم تخطي أدوات الفيديو")
        return
    try:
        _video_exit_stack = AsyncExitStack()
        params = StdioServerParameters(command="python", args=[str(VIDEO_MCP_SCRIPT)])
        read, write = await _video_exit_stack.enter_async_context(stdio_client(params))
        session = await _video_exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listed = await session.list_tools()
        for t in listed.tools:
            schema = t.inputSchema or {"type": "object", "properties": {}}
            desc = (t.description or "").strip()
            desc += (
                "\n\n[ملاحظة مهمة]: هذه أداة فيديو/صوت تعمل على مسارات ملفات محلية "
                "على الخادم فقط. استخدم fetch_media_to_file أولاً لتنزيل أي رابط "
                "خارجي إلى مسار محلي، واكتب المخرجات دائماً داخل مجلد "
                "/app/files/<اسم-فريد>.<امتداد> ليصبح الملف قابلاً للتحميل عبر "
                "/files/<اسم-الملف>."
            )
            TOOLS_MANIFEST.append({
                "type": "function",
                "function": {"name": t.name, "description": desc, "parameters": schema},
            })
            video_tool_names.add(t.name)
            video_tool_schemas[t.name] = schema
        video_session = session
        logger.info(f"Video-Audio MCP connected successfully: discovered {len(video_tool_names)} tools")
    except Exception as e:
        logger.exception(f"فشل الاتصال بخادم video-audio-mcp: {e}")
        video_session = None
        video_tool_names.clear()

async def _disconnect_video_mcp() -> None:
    global _video_exit_stack
    if _video_exit_stack:
        try:
            await _video_exit_stack.aclose()
        except Exception as e:
            logger.warning(f"خطأ أثناء إغلاق اتصال video-audio-mcp: {e}")
        _video_exit_stack = None

def _resolve_video_args(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    schema = video_tool_schemas.get(name) or {}
    props = list((schema.get("properties") or {}).keys())
    if not props:
        return arguments
    resolved = dict(arguments)
    used_targets = {k for k in resolved if k in props}
    for key in list(resolved.keys()):
        if key in props:
            continue
        key_tokens = set(t for t in key.lower().replace("-", "_").split("_") if t)
        best, best_score = None, 0
        for p in props:
            if p in used_targets:
                continue
            p_tokens = set(t for t in p.lower().replace("-", "_").split("_") if t)
            score = len(key_tokens & p_tokens) * 10
            score += sum(
                1 for kt in key_tokens for pt in p_tokens
                if len(kt) >= 2 and (kt in pt or pt in kt)
            )
            if score > best_score:
                best, best_score = p, score
        if best and best_score > 0:
            resolved[best] = resolved.pop(key)
            used_targets.add(best)
    return resolved

async def _call_video_tool(name: str, arguments: Dict[str, Any]) -> str:
    if not video_session:
        raise RuntimeError("خادم أدوات الفيديو غير متصل حالياً")
    arguments = _resolve_video_args(name, arguments)
    result = await video_session.call_tool(name, arguments)
    parts = [getattr(c, "text", None) or str(c) for c in (result.content or [])]
    return "\n".join(p for p in parts if p)

# ══════════════════════════════════════════════════════════
# دعم اللغة العربية في ملفات PDF
# ══════════════════════════════════════════════════════════
FONTS_DIR = BASE_DIR / "fonts"
FONTS_DIR.mkdir(exist_ok=True)
ARABIC_FONT_PATH = FONTS_DIR / "Amiri-Regular.ttf"
ARABIC_FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Regular.ttf"
ARABIC_FONT_NAME = "Amiri"
_arabic_font_ready = False

async def _ensure_arabic_font() -> None:
    global _arabic_font_ready
    try:
        if not ARABIC_FONT_PATH.exists():
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(ARABIC_FONT_URL)
                r.raise_for_status()
                ARABIC_FONT_PATH.write_bytes(r.content)
        
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        pdfmetrics.registerFont(TTFont(ARABIC_FONT_NAME, str(ARABIC_FONT_PATH)))
        _arabic_font_ready = True
        logger.info("Arabic PDF font (Amiri) registered successfully")
        
    except Exception as e:
        logger.warning(f"تعذّر تجهيز الخط العربي لملفات PDF: {e}")
        _arabic_font_ready = False

def _shape_text(text: str) -> str:
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text

# ══════════════════════════════════════════════════════════
# lifespan
# ══════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_super_agent_state()
    await _connect_video_mcp()
    await auth.reload()
    result = await registry.startup_from_config()
    logger.info(f"Hub startup result: {result}")
    logger.info(f"Registered routes: {list(registry.tool_routes.keys())}")
    logger.info(f"🔌 حالة الوكيل الخارق عند الإقلاع: enabled={_super_agent_enabled}")
    await _ensure_arabic_font()
    yield
    await _disconnect_video_mcp()
    await registry.shutdown()

app = FastAPI(title="9Router Tools Server", version="1.0.2", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

app.mount("/files", StaticFiles(directory=str(FILES_DIR)), name="files")

def save_bytes(data: bytes, ext: str) -> str:
    filename = f"{uuid.uuid4().hex}.{ext}"
    (FILES_DIR / filename).write_bytes(data)
    return f"/files/{filename}"

# ══════════════════════════════════════════════════════════
# ★ ★ ★ دالة move_to_files ★ ★ ★
# ══════════════════════════════════════════════════════════
def move_to_files(source_path: str, filename: str = None) -> str:
    FILES_DIR = Path("/app/files")
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    if filename is None:
        filename = Path(source_path).name
    dest_path = FILES_DIR / filename
    shutil.copy2(source_path, dest_path)
    return f"/files/{filename}"

# ══════════════════════════════════════════════════════════
# حماية SSRF
# ══════════════════════════════════════════════════════════
_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}

def _is_url_safe(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    if host.lower() in _BLOCKED_HOSTNAMES:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True

async def _download(url: Optional[str], base64_data: Optional[str]) -> bytes:
    if base64_data:
        return base64.b64decode(base64_data)
    if url:
        if not _is_url_safe(url):
            raise ValueError("الرابط غير آمن أو محظور (حماية SSRF)")
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            r = await client.get(url)
            if r.is_redirect:
                raise ValueError("إعادة التوجيه (redirect) غير مسموح بها لأسباب أمنية (SSRF)")
            r.raise_for_status()
            return r.content
    raise ValueError("يجب توفير url أو base64_data")

# ══════════════════════════════════════════════════════════
# 1) FIRECRAWL
# ══════════════════════════════════════════════════════════
async def firecrawl_search(query: str, limit: int = 5) -> dict:
    if not FIRECRAWL_API_KEY:
        return {"error": "FIRECRAWL_API_KEY غير مضبوط على الخادم"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.firecrawl.dev/v1/search",
            headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}"},
            json={"query": query, "limit": limit},
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("data") or data.get("results") or []
        out = []
        for item in results[:limit]:
            out.append({
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("description") or (item.get("markdown") or "")[:300],
            })
        return {"results": out}

async def firecrawl_scrape(url: str) -> dict:
    if not FIRECRAWL_API_KEY:
        return {"error": "FIRECRAWL_API_KEY غير مضبوط على الخادم"}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}"},
            json={"url": url, "formats": ["markdown"]},
        )
        r.raise_for_status()
        data = r.json()
        markdown = (data.get("data") or {}).get("markdown", "")
        return {"markdown": markdown[:8000]}

# ══════════════════════════════════════════════════════════
# 2) CONTEXT7
# ══════════════════════════════════════════════════════════
async def context7_resolve_library(query: str) -> dict:
    if not CONTEXT7_API_KEY:
        return {"error": "CONTEXT7_API_KEY غير مضبوط على الخادم"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://context7.com/api/v1/search",
            params={"query": query},
            headers={"Authorization": f"Bearer {CONTEXT7_API_KEY}"},
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("results", data if isinstance(data, list) else [])
        libs = [{"id": it.get("id"), "title": it.get("title") or it.get("name")} for it in results[:8]]
        return {"libraries": libs}

async def context7_get_docs(library_id: str, topic: Optional[str] = None) -> dict:
    if not CONTEXT7_API_KEY:
        return {"error": "CONTEXT7_API_KEY غير مضبوط على الخادم"}
    params = {"topic": topic} if topic else {}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"https://context7.com/api/v1/{library_id}",
            params=params,
            headers={"Authorization": f"Bearer {CONTEXT7_API_KEY}"},
        )
        r.raise_for_status()
        data = r.json()
        content = data.get("content") or data.get("text") or json.dumps(data, ensure_ascii=False)
        return {"docs": content[:6000]}

# ══════════════════════════════════════════════════════════
# 3) COMPOSIO — التعديلات الجراحية النهائية
# ══════════════════════════════════════════════════════════
async def composio_list_connected_tools() -> dict:
    if not COMPOSIO_API_KEY:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{COMPOSIO_BASE_URL}/connected_accounts",
            headers={"x-api-key": COMPOSIO_API_KEY},
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
        
        # ✅ تسجيل العينة الخام للتشخيص
        if items:
            logger.info(f"📋 Raw connected_account sample: {items[0]}")
        
        tools = [{
            "slug": (it.get("toolkit") or {}).get("slug"),
            "id": it.get("id"),
            "alias": it.get("alias"),
            "status": it.get("status"),
            # ✅ إضافة user_id من الحقل الصحيح
            "user_id": it.get("user_id") or it.get("entity_id") or it.get("entityId"),
        } for it in items]
        
        return {
            "connected_tools": tools,
            "hint": (
                "استخدم حقل 'user_id' هنا بالضبط عند استدعاء composio_execute_tool — "
                "لا تخترع قيمة عشوائية مثل 'user_123'. اختر حساباً بحالة status='ACTIVE' فقط."
            ),
        }

async def composio_list_tools(toolkit_slug: str, limit: int = 20) -> dict:
    if not COMPOSIO_API_KEY:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not toolkit_slug or not toolkit_slug.strip():
        return {
            "error": "المعامل 'toolkit_slug' مطلوب ولا يمكن أن يكون فارغاً.",
            "suggestion": "استخدم composio_list_connected_tools أولاً للحصول على قائمة toolkits المتصلة.",
            "example": "مثال: toolkit_slug='gmail' أو toolkit_slug='github'"
        }
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{COMPOSIO_BASE_URL_V31}/tools",
            params={
                "toolkit_slug": toolkit_slug,
                "toolkit_versions": "latest",
                "limit": limit,
            },
            headers={"x-api-key": COMPOSIO_API_KEY},
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("items", data if isinstance(data, list) else [])
        tools = [{
            "slug": it.get("slug") or it.get("name"),
            "description": (it.get("description") or "")[:200],
        } for it in items[:limit]]
        
        logger.info(f"📋 Tools found for '{toolkit_slug}': {[t['slug'] for t in tools]}")
        
        return {"toolkit": toolkit_slug, "tools": tools}

async def composio_execute_tool(
    slug: str,
    arguments: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
    version: str = "latest",
) -> dict:
    if not COMPOSIO_API_KEY:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not slug or not slug.strip():
        return {
            "error": "المعامل 'slug' مطلوب ولا يمكن أن يكون فارغاً.",
            "suggestion": "استخدم composio_list_tools أولاً لمعرفة الـ slug الصحيح."
        }
    
    effective_user_id = user_id or COMPOSIO_DEFAULT_USER_ID
    payload: Dict[str, Any] = {
        "arguments": arguments or {},
        "version": version,
        "user_id": effective_user_id,
    }
    
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.post(
            f"{COMPOSIO_BASE_URL_V31}/tools/execute/{slug}",
            headers={"x-api-key": COMPOSIO_API_KEY, "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code == 404:
            error_text = r.text.lower()
            toolkit_guess = slug.split("_")[0].lower()
            
            # ✅ حالة 1: لا يوجد حساب متصل بهذا الـ user_id
            if "no connected account" in error_text or "not found for user" in error_text:
                connected = await composio_list_connected_tools()
                active_accounts = [
                    t for t in connected.get("connected_tools", [])
                    if t.get("slug") == toolkit_guess and t.get("status") == "ACTIVE"
                ]
                return {
                    "error": f"لا يوجد حساب '{toolkit_guess}' نشط مرتبط بـ user_id='{effective_user_id}'.",
                    "status_code": 404,
                    "composio_detail": r.text[:500],
                    "used_user_id": effective_user_id,
                    "active_accounts_found": active_accounts,
                    "instruction": (
                        "إن كانت active_accounts_found غير فارغة، أعد الاستدعاء بنفس user_id "
                        "الموجود في أحد عناصرها بالضبط. إن كانت فارغة، لا يوجد حساب نشط إطلاقاً "
                        "ويجب إعادة الربط عبر composio_authorize_toolkit أولاً."
                    ),
                }
            
            # ✅ حالة 2: اسم slug غير صحيح (احتياطي)
            suggestion = await composio_list_tools(toolkit_slug=toolkit_guess, limit=50)
            return {
                "error": f"الأداة '{slug}' غير موجودة (404).",
                "status_code": 404,
                "composio_detail": r.text[:500],
                "correct_slugs_for_this_toolkit": suggestion.get("tools", []),
                "instruction": "استخدم أحد الأسماء في 'correct_slugs_for_this_toolkit' حرفياً، لا تخمّن الاسم."
            }
        
        r.raise_for_status()
        return r.json()

async def composio_create_session(
    user_id: str,
    toolkits: Optional[List[str]] = None,
    wait_for_connections: bool = False,
    callback_url: Optional[str] = None,
) -> dict:
    if not COMPOSIO_API_KEY:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not user_id or not user_id.strip():
        return {
            "error": "المعامل 'user_id' مطلوب ولا يمكن أن يكون فارغاً.",
            "suggestion": "قدم معرف مستخدم صالح (مثل 'user_123' أو معرف البريد الإلكتروني)."
        }

    payload: Dict[str, Any] = {"user_id": user_id}

    if toolkits is not None:
        toolkits_lower = [str(tk).lower().strip() for tk in toolkits if tk is not None]
        all_keywords = {"all", "select all", "select_all", "كل", "الكل", "جميع", "all toolkits"}
        is_all_selected = any(k in toolkits_lower for k in all_keywords) or toolkits == []

        if not is_all_selected:
            cleaned_tools = [tk for tk in toolkits_lower if tk and tk not in all_keywords]
            if cleaned_tools:
                payload["toolkits"] = {"enable": cleaned_tools}

    payload["manage_connections"] = {
        "enable": True,
        "enable_wait_for_connections": wait_for_connections,
        "enable_connection_removal": True,
    }

    if callback_url:
        payload["manage_connections"]["callback_url"] = callback_url

    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        r = await client.post(
            f"{COMPOSIO_BASE_URL_V31}/tool_router/session",
            headers={"x-api-key": COMPOSIO_API_KEY, "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code == 400:
            return {
                "error": "خطأ في التحقق من صحة الطلب",
                "detail": r.text[:500],
                "payload_sent": payload,
                "suggestion": "تأكد من أن جميع الحقول متوافقة مع إصدار API المستخدم"
            }
        r.raise_for_status()
        data = r.json()
        
        tool_router_tools = data.get("tool_router_tools", [])
        session_id = data.get("session_id")
        logger.info(f"📋 Session {session_id} created successfully")
        logger.info(f"📋 Tool router tools count: {len(tool_router_tools)}")
        
        if tool_router_tools:
            sample = tool_router_tools[:10]
            logger.info(f"📋 Sample tools (first 10): {sample}")
            
            execute_tools = [t for t in tool_router_tools if "EXECUTE" in str(t).upper() or "SEARCH" in str(t).upper()]
            if execute_tools:
                logger.info(f"✅ Found execute/search meta-tools: {execute_tools}")
            else:
                logger.warning("⚠️ No execute/search meta-tools found in session response!")
        else:
            logger.warning("⚠️ No tool_router_tools returned in session response!")
        
        return {
            "session_id": session_id,
            "mcp_url": (data.get("mcp") or {}).get("url"),
            "tool_router_tools": tool_router_tools,
            "config": data.get("config", {}),
            "tools_count": len(tool_router_tools),
        }

async def composio_get_session(session_id: str) -> dict:
    if not COMPOSIO_API_KEY:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not session_id or not session_id.strip():
        return {"error": "المعامل 'session_id' مطلوب ولا يمكن أن يكون فارغاً."}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{COMPOSIO_BASE_URL_V31}/tool_router/session/{session_id}",
            headers={"x-api-key": COMPOSIO_API_KEY},
        )
        if r.status_code == 404:
            return {"error": f"الجلسة '{session_id}' غير موجودة.", "status_code": 404}
        r.raise_for_status()
        return r.json()

async def composio_session_toolkits(session_id: str) -> dict:
    if not COMPOSIO_API_KEY:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not session_id or not session_id.strip():
        return {"error": "المعامل 'session_id' مطلوب ولا يمكن أن يكون فارغاً."}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{COMPOSIO_BASE_URL_V31}/tool_router/session/{session_id}/toolkits",
            headers={"x-api-key": COMPOSIO_API_KEY},
        )
        if r.status_code == 404:
            return {"error": f"الجلسة '{session_id}' غير موجودة.", "status_code": 404}
        r.raise_for_status()
        return r.json()

async def composio_authorize_toolkit(
    session_id: str,
    toolkit: str,
    callback_url: Optional[str] = None,
) -> dict:
    if not COMPOSIO_API_KEY:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not session_id or not session_id.strip():
        return {"error": "المعامل 'session_id' مطلوب ولا يمكن أن يكون فارغاً."}
    if not toolkit or not toolkit.strip():
        return {"error": "المعامل 'toolkit' مطلوب ولا يمكن أن يكون فارغاً."}
    
    effective_callback = callback_url or (
        f"{SPACE_BASE_URL}/oauth-callback-close" if SPACE_BASE_URL else None
    )
    
    payload: Dict[str, Any] = {"toolkit": toolkit}
    if effective_callback:
        payload["callback_url"] = effective_callback
    
    url = f"{COMPOSIO_BASE_URL}/tool_router/session/{session_id}/link"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.post(
            url,
            headers={"x-api-key": COMPOSIO_API_KEY, "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code == 404:
            return {
                "error": f"الجلسة '{session_id}' غير موجودة أو التطبيق '{toolkit}' غير صالح (404).",
                "status_code": 404,
            }
        r.raise_for_status()
        data = r.json()
        redirect_url = data.get("redirect_url") or data.get("redirectUrl")
        return {"redirect_url": redirect_url, "toolkit": toolkit, "raw": data}

async def composio_close_session(session_id: str) -> dict:
    if not COMPOSIO_API_KEY:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not session_id or not session_id.strip():
        return {"error": "المعامل 'session_id' مطلوب ولا يمكن أن يكون فارغاً."}
    
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.delete(
            f"{COMPOSIO_BASE_URL_V31}/tool_router/session/{session_id}",
            headers={"x-api-key": COMPOSIO_API_KEY},
        )
        if r.status_code == 404:
            return {"error": f"الجلسة '{session_id}' غير موجودة أو محذوفة مسبقاً.", "status_code": 404}
        r.raise_for_status()
        return r.json()

# ══════════════════════════════════════════════════════════
# 4) المستندات — DOCX / PPTX / PDF / XLSX / Markdown
# ══════════════════════════════════════════════════════════
def _apply_markdown_lines(add_heading, add_paragraph, add_bullet, text: str):
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("### "):
            add_heading(line[4:], 3)
        elif line.startswith("## "):
            add_heading(line[3:], 2)
        elif line.startswith("# "):
            add_heading(line[2:], 1)
        elif line.startswith(("- ", "* ")):
            add_bullet(line[2:])
        else:
            add_paragraph(line)

def create_docx(title: str, content_markdown: str) -> dict:
    from docx import Document
    doc = Document()
    doc.add_heading(title, level=1)
    _apply_markdown_lines(
        lambda t, lvl: doc.add_heading(t, level=lvl),
        lambda t: doc.add_paragraph(t),
        lambda t: doc.add_paragraph(t, style="List Bullet"),
        content_markdown,
    )
    buf = io.BytesIO()
    doc.save(buf)
    return {"file_url": save_bytes(buf.getvalue(), "docx")}

def create_pptx(title: str, slides: List[Dict[str, Any]]) -> dict:
    from pptx import Presentation
    prs = Presentation()
    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = title
    bullet_layout = prs.slide_layouts[1]
    for s in slides:
        slide = prs.slides.add_slide(bullet_layout)
        slide.shapes.title.text = s.get("title", "")
        tf = slide.placeholders[1].text_frame
        bullets = s.get("bullets", [])
        for i, b in enumerate(bullets):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = b
    buf = io.BytesIO()
    prs.save(buf)
    return {"file_url": save_bytes(buf.getvalue(), "pptx")}

def create_pdf(title: str, content_markdown: str) -> dict:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_RIGHT
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    base_styles = getSampleStyleSheet()
    font = ARABIC_FONT_NAME if _arabic_font_ready else "Helvetica"
    styles = {
        "Title": ParagraphStyle("TitleAr", parent=base_styles["Title"], fontName=font, alignment=TA_RIGHT, fontSize=20),
        "Heading1": ParagraphStyle("H1Ar", parent=base_styles["Heading1"], fontName=font, alignment=TA_RIGHT),
        "Heading2": ParagraphStyle("H2Ar", parent=base_styles["Heading2"], fontName=font, alignment=TA_RIGHT),
        "Normal": ParagraphStyle("NormalAr", parent=base_styles["Normal"], fontName=font, alignment=TA_RIGHT, fontSize=12, leading=18),
    }
    story = [Paragraph(_shape_text(title), styles["Title"]), Spacer(1, 14)]
    _apply_markdown_lines(
        lambda t, lvl: story.append(Paragraph(_shape_text(t), styles["Heading1"] if lvl == 1 else styles["Heading2"])),
        lambda t: story.append(Paragraph(_shape_text(t), styles["Normal"])),
        lambda t: story.append(Paragraph(_shape_text("• " + t), styles["Normal"])),
        content_markdown,
    )
    doc.build(story)
    return {"file_url": save_bytes(buf.getvalue(), "pdf")}

async def read_pdf(url: Optional[str] = None, base64_data: Optional[str] = None, max_pages: int = 20) -> dict:
    data = await _download(url, base64_data)
    text_parts: List[str] = []
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:max_pages]:
                text_parts.append(page.extract_text() or "")
    except Exception:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages[:max_pages]:
            text_parts.append(page.extract_text() or "")
    full_text = "\n".join(text_parts)
    return {"text": full_text[:12000], "pages_read": len(text_parts)}

async def pdf_info(url: Optional[str] = None, base64_data: Optional[str] = None) -> dict:
    data = await _download(url, base64_data)
    import fitz
    doc = fitz.open(stream=data, filetype="pdf")
    return {"pages": doc.page_count, "metadata": doc.metadata}

def create_xlsx(title: str, headers: Optional[List[str]] = None, rows: Optional[List[List[Any]]] = None) -> dict:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = (title or "Sheet1")[:31]
    if headers:
        ws.append(headers)
    for row in (rows or []):
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return {"file_url": save_bytes(buf.getvalue(), "xlsx")}

async def read_xlsx(url: Optional[str] = None, base64_data: Optional[str] = None, sheet: Optional[str] = None) -> dict:
    data = await _download(url, base64_data)
    import pandas as pd
    df = pd.read_excel(io.BytesIO(data), sheet_name=sheet or 0)
    return {
        "columns": [str(c) for c in df.columns],
        "rows": df.head(200).values.tolist(),
        "total_rows": int(len(df)),
    }

def markdown_to_html(markdown_text: str) -> dict:
    import markdown as md
    html = md.markdown(markdown_text, extensions=["tables", "fenced_code"])
    return {"html": html}

async def fetch_media_to_file(url: Optional[str] = None, base64_data: Optional[str] = None, ext: str = "mp4") -> dict:
    data = await _download(url, base64_data)
    filename = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
    path = FILES_DIR / filename
    path.write_bytes(data)
    return {"local_path": str(path), "file_url": f"/files/{filename}"}

def write_text_file(content: str, ext: str = "srt") -> dict:
    filename = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
    path = FILES_DIR / filename
    path.write_text(content, encoding="utf-8")
    return {"local_path": str(path), "file_url": f"/files/{filename}"}

# ══════════════════════════════════════════════════════════
# سجل الخرائط والدوال الداخلية لـ /call
# ══════════════════════════════════════════════════════════
TOOL_FUNCTIONS = {
    "firecrawl_search": firecrawl_search,
    "firecrawl_scrape": firecrawl_scrape,
    "context7_resolve_library": context7_resolve_library,
    "context7_get_docs": context7_get_docs,
    "composio_list_connected_tools": composio_list_connected_tools,
    "composio_list_tools": composio_list_tools,
    "composio_execute_tool": composio_execute_tool,
    "composio_create_session": composio_create_session,
    "composio_get_session": composio_get_session,
    "composio_session_toolkits": composio_session_toolkits,
    "composio_authorize_toolkit": composio_authorize_toolkit,
    "composio_close_session": composio_close_session,
    "create_docx": create_docx,
    "create_pptx": create_pptx,
    "create_pdf": create_pdf,
    "read_pdf": read_pdf,
    "pdf_info": pdf_info,
    "create_xlsx": create_xlsx,
    "read_xlsx": read_xlsx,
    "markdown_to_html": markdown_to_html,
    "fetch_media_to_file": fetch_media_to_file,
    "write_text_file": write_text_file,
}

# ══════════════════════════════════════════════════════════
# قائمة الأدوات المتاحة للنظام الخارجي (OpenAI Manifest)
# ══════════════════════════════════════════════════════════
# تم تعريف TOOLS_MANIFEST مسبقاً كقائمة فارغة، وسيتم ملؤها في _connect_video_mcp
# لكن نحتاج إلى إضافة الأدوات المضمّنة الأساسية هنا (باستثناء أدوات الفيديو التي ستُضاف لاحقاً)

# نضيف الأدوات المضمّنة الأساسية (باستثناء أدوات الفيديو)
TOOLS_MANIFEST.extend([
    {"type": "function", "function": {
        "name": "firecrawl_search",
        "description": "بحث في الويب عبر Firecrawl وإرجاع أهم النتائج.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "نص البحث"},
            "limit": {"type": "integer", "description": "عدد النتائج", "default": 5},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "firecrawl_scrape",
        "description": "تصفح رابط ويب محدد وإرجاع محتواه كـ Markdown نظيف.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
        }, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "context7_resolve_library",
        "description": "البحث عن معرّف مكتبة برمجية في Context7.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "context7_get_docs",
        "description": "جلب توثيق مكتبة برمجية من Context7.",
        "parameters": {"type": "object", "properties": {
            "library_id": {"type": "string"},
            "topic": {"type": "string"},
        }, "required": ["library_id"]},
    }},
    {"type": "function", "function": {
        "name": "composio_list_connected_tools",
        "description": "✅ عرض قائمة التطبيقات المرتبطة بحساب Composio الحالي مع user_id الحقيقي لكل حساب.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "composio_list_tools",
        "description": "جلب قائمة الأدوات القابلة للتنفيذ فعلياً داخل تطبيق معيّن. يجب استدعاء هذه الدالة أولاً للحصول على slugs صالحة قبل استخدام composio_execute_tool.",
        "parameters": {"type": "object", "properties": {
            "toolkit_slug": {"type": "string", "description": "✅ مطلوب: اسم التطبيق بصيغته الصغيرة (مثل 'gmail'، 'github'، 'slack') — لا يمكن أن يكون فارغاً."},
            "limit": {"type": "integer", "default": 20},
        }, "required": ["toolkit_slug"]},
    }},
    {"type": "function", "function": {
        "name": "composio_execute_tool",
        "description": (
            "✅ تنفيذ أداة Composio. ⚠️ إلزامي: لا تخترع قيمة user_id عشوائية أبداً. "
            "استدعِ composio_list_connected_tools أولاً، واستخدم حقل 'user_id' الموجود فيه "
            "لحساب بحالة status='ACTIVE' فقط ونفس toolkit المطلوب."
        ),
        "parameters": {"type": "object", "properties": {
            "slug": {"type": "string", "description": "✅ مطلوب: المعرف الفريد للأداة — لا يمكن أن يكون فارغاً."},
            "arguments": {"type": "object", "description": "معاملات الأداة المطلوبة (تعتمد على الأداة المحددة)."},
            "user_id": {"type": "string", "description": "⚠️ إلزامي: القيمة الحقيقية من composio_list_connected_tools — ليس معرّفاً تخترعه."},
        }, "required": ["slug", "user_id"]},
    }},
    {"type": "function", "function": {
        "name": "composio_create_session",
        "description": "✅ إنشاء جلسة Composio Tool Router مع تفعيل جميع الأدوات التنفيذية تلقائياً. بعد الإنشاء، استخدم composio_authorize_toolkit لكل تطبيق يحتاج مصادقة.",
        "parameters": {"type": "object", "properties": {
            "user_id": {"type": "string", "description": "✅ مطلوب: معرف المستخدم — لا يمكن أن يكون فارغاً."},
            "toolkits": {"type": "array", "items": {"type": "string"}, "description": "قائمة التطبيقات المطلوب تفعيلها (مثل ['gmail', 'github']). اتركها فارغة أو أرسل ['all'] لتفعيل كل شيء."},
            "wait_for_connections": {"type": "boolean", "default": False, "description": "انتظار اكتمال المصادقات قبل إنهاء الجلسة."},
            "callback_url": {"type": "string", "description": "رابط إعادة التوجيه بعد المصادقة."},
        }, "required": ["user_id"]},
    }},
    {"type": "function", "function": {
        "name": "composio_get_session",
        "description": "جلب حالة جلسة Tool Router موجودة مسبقاً.",
        "parameters": {"type": "object", "properties": {
            "session_id": {"type": "string", "description": "✅ مطلوب: معرف الجلسة — لا يمكن أن يكون فارغاً."},
        }, "required": ["session_id"]},
    }},
    {"type": "function", "function": {
        "name": "composio_session_toolkits",
        "description": "عرض توليكيتات جلسة معيّنة وحالة اتصال كل منها.",
        "parameters": {"type": "object", "properties": {
            "session_id": {"type": "string", "description": "✅ مطلوب: معرف الجلسة — لا يمكن أن يكون فارغاً."},
        }, "required": ["session_id"]},
    }},
    {"type": "function", "function": {
        "name": "composio_authorize_toolkit",
        "description": "✅ إنشاء رابط تسجيل دخول (Connect Link) لتطبيق واحد (toolkit) داخل جلسة Tool Router موجودة. بعد نجاح المصادقة، سيُغلق النافذة تلقائياً.",
        "parameters": {"type": "object", "properties": {
            "session_id": {"type": "string", "description": "✅ مطلوب: معرّف الجلسة الناتج عن composio_create_session — لا يمكن أن يكون فارغاً."},
            "toolkit": {"type": "string", "description": "✅ مطلوب: اسم التطبيق بصيغته الصغيرة، مثل gmail أو slack أو github — لا يمكن أن يكون فارغاً."},
            "callback_url": {"type": "string", "description": "رابط اختياري يُعاد توجيه المستخدم إليه بعد إتمام تسجيل الدخول (سيُستخدم رابط الإغلاق التلقائي افتراضياً)."},
        }, "required": ["session_id", "toolkit"]},
    }},
    {"type": "function", "function": {
        "name": "composio_close_session",
        "description": "✅ إغلاق/حذف جلسة Tool Router بشكل نهائي. استخدمها بعد انتهاء المحادثة أو المهمة لتحرير الموارد، لأن جلسات Tool Router لا تنتهي صلاحيتها تلقائياً.",
        "parameters": {"type": "object", "properties": {
            "session_id": {"type": "string", "description": "✅ مطلوب: معرّف الجلسة المراد إغلاقها — لا يمكن أن يكون فارغاً."},
        }, "required": ["session_id"]},
    }},
    {"type": "function", "function": {
        "name": "create_docx",
        "description": "إنشاء ملف Word (.docx).",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "content_markdown": {"type": "string"},
        }, "required": ["title", "content_markdown"]},
    }},
    {"type": "function", "function": {
        "name": "create_pptx",
        "description": "إنشاء عرض تقديمي (.pptx).",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "slides": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"},
                "bullets": {"type": "array", "items": {"type": "string"}},
            }}},
        }, "required": ["title", "slides"]},
    }},
    {"type": "function", "function": {
        "name": "create_pdf",
        "description": "إنشاء ملف PDF.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "content_markdown": {"type": "string"},
        }, "required": ["title", "content_markdown"]},
    }},
    {"type": "function", "function": {
        "name": "read_pdf",
        "description": "استخراج النص من ملف PDF.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "base64_data": {"type": "string"},
            "max_pages": {"type": "integer", "default": 20},
        }},
    }},
    {"type": "function", "function": {
        "name": "pdf_info",
        "description": "معلومات سريعة عن ملف PDF.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "base64_data": {"type": "string"},
        }},
    }},
    {"type": "function", "function": {
        "name": "create_xlsx",
        "description": "إنشاء ملف Excel (.xlsx).",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "headers": {"type": "array", "items": {"type": "string"}},
            "rows": {"type": "array", "items": {"type": "array"}},
        }, "required": ["title"]},
    }},
    {"type": "function", "function": {
        "name": "read_xlsx",
        "description": "قراءة بيانات من ملف Excel.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "base64_data": {"type": "string"},
            "sheet": {"type": "string"},
        }},
    }},
    {"type": "function", "function": {
        "name": "markdown_to_html",
        "description": "تحويل نص Markdown إلى HTML.",
        "parameters": {"type": "object", "properties": {
            "markdown_text": {"type": "string"},
        }, "required": ["markdown_text"]},
    }},
    {"type": "function", "function": {
        "name": "fetch_media_to_file",
        "description": "ينزّل رابط فيديو/صوت (أو base64) ويحفظه كملف فعلي على الخادم، ويُرجع local_path لاستخدامه كمدخل لأدوات الفيديو/الصوت (video_path, input_audio_path...). استخدمها دائماً قبل أي أداة فيديو تحتاج ملفاً من رابط خارجي.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "base64_data": {"type": "string"},
            "ext": {"type": "string", "description": "امتداد الملف الناتج، مثل mp4 أو mp3 أو wav", "default": "mp4"},
        }},
    }},
    {"type": "function", "function": {
        "name": "write_text_file",
        "description": "يكتب نصاً مباشرة (بدون رابط أو base64) إلى ملف حقيقي على الخادم، ويُرجع local_path. استخدمها لإنشاء ملفات ترجمة SRT قبل add_subtitles، أو أي نص آخر تحتاجه أدوات الفيديو كمسار ملف.",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string", "description": "المحتوى النصي الكامل المطلوب كتابته، مثل نص SRT كاملاً"},
            "ext": {"type": "string", "description": "امتداد الملف الناتج، مثل srt أو txt", "default": "srt"},
        }, "required": ["content"]},
    }},
])

# ══════════════════════════════════════════════════════════
# نقاط النهاية العامة (Endpoints)
# ══════════════════════════════════════════════════════════
class CallRequest(BaseModel):
    tool: str
    arguments: Dict[str, Any] = Field(default_factory=dict)

class SuperAgentToggleRequest(BaseModel):
    enabled: bool

def _check_auth(x_api_key: str) -> None:
    if APP_SECRET and x_api_key != APP_SECRET:
        raise HTTPException(401, "غير مصرح: مفتاح API غير صحيح أو مفقود (X-API-Key)")

@app.get("/")
async def root():
    return {"status": "ok", "service": "9Router Tools Server", "tools_count": len(TOOLS_MANIFEST)}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "firecrawl_configured": bool(FIRECRAWL_API_KEY),
        "context7_configured": bool(CONTEXT7_API_KEY),
        "composio_configured": bool(COMPOSIO_API_KEY),
        "auth_enforced": bool(APP_SECRET),
        "video_mcp_connected": bool(video_session),
        "video_tools_count": len(video_tool_names),
        "hub_ready": True,
        "hub_servers": len(registry.servers) if hasattr(registry, 'servers') else 0,
        "super_agent_enabled": is_super_agent_enabled(),
    }

# ══════════════════════════════════════════════════════════
# ★ ★ ★ نقاط نهاية "الوكيل الخارق" ★ ★ ★
# ══════════════════════════════════════════════════════════
@app.get("/super-agent/status")
async def super_agent_status(x_api_key: str = Header(default="")):
    _check_auth(x_api_key)
    builtin_count = len(TOOLS_MANIFEST)
    hub_count = len(registry.tool_routes) if hasattr(registry, "tool_routes") else 0
    return {
        "enabled": is_super_agent_enabled(),
        "builtin_tools_count": builtin_count,
        "video_tools_count": len(video_tool_names),
        "hub_tools_count": hub_count,
        "total_tools_count": builtin_count + hub_count,
    }

@app.post("/super-agent/toggle")
async def super_agent_toggle(req: SuperAgentToggleRequest, x_api_key: str = Header(default="")):
    """
    تفعيل enabled=true أو تعطيل enabled=false لكل الأدوات دفعة واحدة (نسخة JSON body — للاستخدام البرمجي).
    """
    _check_auth(x_api_key)
    summary = set_super_agent_enabled(req.enabled)
    return {"ok": True, **summary}

# ★ ★ ★ نسختان مبسّطتان بـ GET (بدون body) — سهلتا الاستدعاء من رابط مباشر أو زر ★ ★ ★
@app.get("/super-agent/enable")
async def super_agent_enable(x_api_key: str = Header(default="")):
    _check_auth(x_api_key)
    summary = set_super_agent_enabled(True)
    return {"ok": True, **summary}

@app.get("/super-agent/disable")
async def super_agent_disable(x_api_key: str = Header(default="")):
    _check_auth(x_api_key)
    summary = set_super_agent_enabled(False)
    return {"ok": True, **summary}

# ★ ★ ★ صفحة بها زرّان فعليان: "تفعيل الكل" و "تعطيل الكل" ★ ★ ★
@app.get("/super-agent", response_class=HTMLResponse)
async def super_agent_page():
    return """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl"><head><meta charset="utf-8">
    <title>الوكيل الخارق</title>
    <style>
        body{font-family:sans-serif;text-align:center;padding-top:60px;background:#0f0f0f;color:#eee;}
        button{font-size:18px;padding:14px 28px;margin:10px;border:none;border-radius:8px;cursor:pointer;}
        #on{background:#1e8e3e;color:#fff;}
        #off{background:#c62828;color:#fff;}
        #status{margin-top:20px;font-size:16px;color:#aaa;}
    </style></head>
    <body>
        <h2>⚡ الوكيل الخارق</h2>
        <p id="status">...جاري التحقق من الحالة</p>
        <div>
            <button id="on" onclick="setState(true)">✅ تفعيل الكل</button>
            <button id="off" onclick="setState(false)">🛑 تعطيل الكل</button>
        </div>
        <script>
        async function refresh(){
            const r = await fetch('/super-agent/status');
            const d = await r.json();
            document.getElementById('status').innerText =
                (d.enabled ? '🟢 الحالة: مفعّل — ' : '🔴 الحالة: معطّل — ') +
                'إجمالي الأدوات: ' + (d.total_tools_count ?? '?');
        }
        async function setState(enabled){
            document.getElementById('status').innerText = '...جاري التنفيذ';
            await fetch(enabled ? '/super-agent/enable' : '/super-agent/disable');
            await refresh();
        }
        refresh();
        </script>
    </body></html>
    """

# ══════════════════════════════════════════════════════════
# ✅ التعديل الجراحي: إزالة تكرار الأدوات من registry.tool_routes
# + احترام مفتاح الوكيل الخارق: لا نُرجع أي أداة إن كان معطّلاً
# ══════════════════════════════════════════════════════════
@app.get("/tools")
async def list_tools():
    if not is_super_agent_enabled():
        return {
            "tools": [],
            "super_agent_enabled": False,
            "message": "الوكيل الخارق معطّل حالياً — تم إيقاف جميع الأدوات. فعّله عبر /super-agent/toggle.",
        }

    # نبدأ بقائمة TOOLS_MANIFEST (نسخة main.py)
    all_tools = list(TOOLS_MANIFEST)
    seen_names = {t["function"]["name"] for t in all_tools}  # تتبع الأسماء المسجلة

    if hasattr(registry, "tool_routes"):
        for tool_name, route in registry.tool_routes.items():
            # إذا كان الاسم موجوداً بالفعل في TOOLS_MANIFEST، نتجاوز نسخة hub.py
            if tool_name in seen_names:
                continue
            try:
                all_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": getattr(route, "description", ""),
                        "parameters": getattr(route, "input_schema", {"type": "object", "properties": {}})
                    }
                })
                seen_names.add(tool_name)
            except Exception as e:
                logger.warning(f"Skipping tool {tool_name} due to error: {e}")

    return {"tools": all_tools, "super_agent_enabled": True}

# ✅ صفحة الإغلاق التلقائي لـ OAuth
@app.get("/oauth-callback-close")
async def oauth_callback_close():
    html_content = """
    <!DOCTYPE html>
    <html><head><meta charset="utf-8"></head>
    <body style="font-family:sans-serif;text-align:center;padding-top:50px;">
        <h2>✅ تم الربط بنجاح</h2>
        <p>يمكنك إغلاق هذه النافذة الآن.</p>
        <script>
            setTimeout(function() {
                window.close();
            }, 1500);
        </script>
    </body></html>
    """
    return HTMLResponse(content=html_content)

MAX_UPLOAD_BYTES = 500 * 1024 * 1024

ALLOWED_UPLOAD_EXTS = {
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v",
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac",
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".txt", ".srt",
}

@app.post("/upload")
async def upload_file(file: UploadFile = File(...), x_api_key: str = Header(default="")):
    _check_auth(x_api_key)
    orig_ext = Path(file.filename or "").suffix.lower() or ""
    if orig_ext not in ALLOWED_UPLOAD_EXTS:
        raise HTTPException(
            400,
            f"امتداد الملف '{orig_ext or '(بدون امتداد)'}' غير مسموح به. "
            f"الامتدادات المسموحة: {', '.join(sorted(ALLOWED_UPLOAD_EXTS))}",
        )
    filename = f"{uuid.uuid4().hex}{orig_ext}"
    dest = FILES_DIR / filename
    size = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        413,
                        f"الملف أكبر من الحد المسموح ({MAX_UPLOAD_BYTES // (1024 * 1024)}MB)",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(500, f"فشل حفظ الملف المرفوع: {e}")
    finally:
        await file.close()
    return {
        "ok": True,
        "filename": filename,
        "original_filename": file.filename,
        "local_path": str(dest),
        "file_url": f"/files/{filename}",
        "size_bytes": size,
    }

@app.post("/call")
async def call_tool(req: CallRequest, x_api_key: str = Header(default="")):
    _check_auth(x_api_key)

    # ★ ★ ★ فحص مفتاح الوكيل الخارق أولاً — قبل أي منطق آخر ★ ★ ★
    if not is_super_agent_enabled():
        raise HTTPException(
            403,
            "الوكيل الخارق معطّل حالياً — جميع الأدوات (المضمّنة، الفيديو، وأدوات Hub) متوقفة. "
            "فعّله عبر POST /super-agent/toggle {\"enabled\": true}."
        )
    
    # 1. أدوات الفيديو المحلية
    if req.tool in video_tool_names:
        if not video_session:
            raise HTTPException(503, "خادم أدوات الفيديو (video-audio-mcp) غير متصل حالياً")
        try:
            result_text = await _call_video_tool(req.tool, req.arguments)
            return {"ok": True, "result": result_text}
        except Exception as e:
            raise HTTPException(500, f"خطأ أثناء تنفيذ أداة الفيديو {req.tool}: {e}")
    
    # 2. الأدوات المضمّنة في TOOL_FUNCTIONS
    fn = TOOL_FUNCTIONS.get(req.tool)
    if fn:
        try:
            if asyncio.iscoroutinefunction(fn):
                result = await fn(**req.arguments)
            else:
                result = fn(**req.arguments)
            return {"ok": True, "result": result}
        except ValueError as e:
            raise HTTPException(400, str(e))
        except TypeError as e:
            raise HTTPException(400, f"معاملات غير صحيحة لأداة {req.tool}: {e}")
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:500] if e.response is not None else ""
            raise HTTPException(502, f"خطأ من الخدمة الخارجية: HTTP {e.response.status_code} - {detail}")
        except Exception as e:
            raise HTTPException(500, f"خطأ أثناء تنفيذ {req.tool}: {e}")
    
    # 3. أدوات Hub (مع سجلات التشخيص)
    logger.info(f"Incoming tool = {req.tool}")
    logger.info(f"Available routes = {list(registry.tool_routes.keys())}")
    
    if req.tool not in registry.tool_routes:
        logger.warning(
            f"Tool '{req.tool}' not found. Available: {list(registry.tool_routes.keys())}"
        )
    
    context = internal_context()
    try:
        result = await registry.call_tool(req.tool, req.arguments, context)
        return {"ok": True, "result": result}
    except Exception as e:
        error_msg = str(e)
        if "ToolNotFound" in error_msg or "not found" in error_msg.lower():
            raise HTTPException(404, error_msg)
        elif "PermissionDenied" in error_msg or "not allowed" in error_msg.lower():
            raise HTTPException(403, error_msg)
        elif "AuthenticationRequired" in error_msg or "authentication required" in error_msg.lower():
            raise HTTPException(401, error_msg)
        elif "ServerNotReady" in error_msg or "not ready" in error_msg.lower():
            raise HTTPException(503, error_msg)
        elif "OperationTimeout" in error_msg or "timeout" in error_msg.lower():
            raise HTTPException(504, error_msg)
        else:
            raise HTTPException(500, f"خطأ في أداة Hub: {error_msg}")

# ══════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "7860")),
        reload=False,
        access_log=True,
    )