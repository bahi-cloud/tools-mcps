# main.py - التعديلات الجراحية النهائية
# + إصلاح user_id + إزالة تكرار الأدوات
# + عزل المستخدمين في /call + composio_revoke_account + /composio/disconnect
# + قراءة كسولة لـ COMPOSIO_API_KEY و COMPOSIO_DEFAULT_USER_ID
# + revoke_account على v3.1 مع revoke_on_delete=true (الحل النهائي)
# + عزل الملفات لكل مستخدم (client_id) + روابط كاملة للملفات
# ★ عزل المستخدمين: مساحة لكل دردشة + sandbox للمسارات + files_changed + list_my_files
#   + تقييد اختياري لأدوات الكود الحر (ADMIN_SECRET) + إلغاء «الوكيل الخارق» نهائياً
# ★ FIX (هذه النسخة): /call لم يعد يعلّق التطبيق
#     1) الدوال المتزامنة تعمل في thread بدل الـ event loop
#     2) مهلة زمنية CALL_TIMEOUT لكل استدعاء (504 بدل التعليق)
#     3) _is_url_safe (DNS متزامن) يعمل في thread
#     4) إزالة طباعة قائمة المسارات الكاملة في كل استدعاء
#     5) الإقلاع الثقيل (video + hub + font) في الخلفية → المنفذ يفتح فوراً
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
import hashlib
import hmac
import re
import time
import contextvars  # ★ NEW: لعزل الملفات
from contextlib import asynccontextmanager, AsyncExitStack
from pathlib import Path
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse, quote
import httpx
from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field

logger = logging.getLogger("main")
logging.basicConfig(level=logging.INFO)

# ══════════════════════════════════════════════════════════
# ★ متغير سياق لعزل الملفات بين المستخدمين
# ══════════════════════════════════════════════════════════
_current_client_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_client_id", default="anonymous"
)

# ══════════════════════════════════════════════════════════
# استيراد Hub Core
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

# ★ COMPOSIO_BASE_URL ثابت (URL) — لا يحتاج قراءة كسولة
COMPOSIO_BASE_URL = "https://backend.composio.dev/api/v3"
COMPOSIO_BASE_URL_V31 = "https://backend.composio.dev/api/v3.1"

APP_SECRET = os.environ.get("APP_SECRET", "")
# مفتاح اختياري للمدير: عند ضبطه لا تعمل أدوات الكود الحر (RESTRICTED_TOOLS) إلا مع الهيدر X-Admin-Key
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
# REQUIRE_AUTH=1 → يرفض الخادم /call و/upload إذا لم يُضبط APP_SECRET (fail-closed). الافتراضي: معطّل مؤقتاً.
REQUIRE_AUTH = os.environ.get("REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes")
SPACE_BASE_URL = os.environ.get("SPACE_BASE_URL", "")

# ★ FIX: المهلة القصوى (بالثواني) لأي استدعاء في /call — قابلة للتعديل من متغير بيئة
try:
    CALL_TIMEOUT = int(os.environ.get("CALL_TIMEOUT", "120"))
except ValueError:
    CALL_TIMEOUT = 120


# ══════════════════════════════════════════════════════════
# ★ قراءة كسولة لمتغيرات Composio الحساسة (آمنة لـ HuggingFace)
# ══════════════════════════════════════════════════════════

def _get_composio_api_key() -> str:
    """★ تُقرأ في كل استدعاء — لا تُثبَّت وقت الاستيراد."""
    return os.environ.get("COMPOSIO_API_KEY", "") or ""


def _get_composio_default_user_id() -> str:
    """★ تُقرأ في كل استدعاء — لا تُثبَّت وقت الاستيراد."""
    return os.environ.get("COMPOSIO_DEFAULT_USER_ID", "default") or "default"


# ══════════════════════════════════════════════════════════
# ★ دوال عزل الملفات والروابط الكاملة
# ══════════════════════════════════════════════════════════

def _full_file_url(relative_path: str) -> str:
    """★ يحول المسار النسبي إلى رابط كامل باستخدام SPACE_BASE_URL."""
    if not relative_path:
        return ""
    if relative_path.startswith("http"):
        return relative_path
    base = SPACE_BASE_URL.rstrip("/") if SPACE_BASE_URL else ""
    return f"{base}{relative_path}" if base else relative_path


def save_bytes(data: bytes, ext: str, client_id: str = None) -> str:
    """
    ★ حفظ آمن: يعزل ملفات كل مستخدم في مجلد خاص به.
    يعيد المسار النسبي (وليس الكامل) — استخدم _full_file_url للرابط الكامل.
    """
    if client_id is None:
        client_id = _current_client_id.get()

    # تنظيف client_id لمنع أي محاولة اختراق مسار (Path Traversal)
    safe_client = "".join(c for c in client_id if c.isalnum() or c in "-_")[:32] or "anonymous"

    # إنشاء مجلد المستخدم إذا لم يكن موجوداً
    user_dir = FILES_DIR / safe_client
    user_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
    (user_dir / filename).write_bytes(data)

    return f"/files/{safe_client}/{filename}"


# ══════════════════════════════════════════════════════════
# TOOLS_MANIFEST (قبل استخدامه في _connect_video_mcp)
# ══════════════════════════════════════════════════════════
TOOLS_MANIFEST: List[Dict[str, Any]] = []


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
        logger.warning("video-audio-mcp/server.py غير موجود — تخطي أدوات الفيديو")
        return
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        logger.warning("حزمة mcp غير مثبّتة — تخطي أدوات الفيديو")
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
                "خارجي إلى مسار محلي. اكتب المخرجات دائماً باسم ملف نسبي فقط "
                "(مثل out.mp4) وسيُحفظ داخل مساحة هذه الدردشة؛ أي مسار خارج "
                "مساحتك مرفوض. رابط التحميل = workspace_url (من رد /call) + اسم الملف."
            )
            TOOLS_MANIFEST.append({
                "type": "function",
                "function": {"name": t.name, "description": desc, "parameters": schema},
            })
            video_tool_names.add(t.name)
            video_tool_schemas[t.name] = schema
        video_session = session
        logger.info(f"Video-Audio MCP connected: {len(video_tool_names)} tools")
    except Exception as e:
        logger.exception(f"فشل الاتصال بـ video-audio-mcp: {e}")
        video_session = None
        video_tool_names.clear()


async def _disconnect_video_mcp() -> None:
    global _video_exit_stack
    if _video_exit_stack:
        try:
            await _video_exit_stack.aclose()
        except Exception as e:
            logger.warning(f"خطأ أثناء إغلاق video-audio-mcp: {e}")
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
        raise RuntimeError("خادم أدوات الفيديو غير متصل")
    arguments = _resolve_video_args(name, arguments)
    result = await video_session.call_tool(name, arguments)
    parts = [getattr(c, "text", None) or str(c) for c in (result.content or [])]
    return "\n".join(p for p in parts if p)


# ══════════════════════════════════════════════════════════
# دعم اللغة العربية في PDF
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
        logger.warning(f"تعذّر تجهيز الخط العربي: {e}")
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
# ★ FIX: الإقلاع الثقيل (video-audio-mcp ≈ 28 ثانية + 15 سيرفر MCP + تحميل الخط)
#   ينفَّذ في الخلفية. هكذا يفتح uvicorn المنفذ 7860 فوراً بدل أن يرفض الطلبات
#   (timeout) طوال ~50 ثانية بعد كل إقلاع/restart.
#   كل اتصالات MCP تُفتح وتُغلق داخل task واحد (متطلب anyio لـ stdio_client).
# ══════════════════════════════════════════════════════════
_hub_stop: Optional[asyncio.Event] = None
_hub_task: Optional[asyncio.Task] = None
_font_task: Optional[asyncio.Task] = None
_hub_state: Dict[str, Any] = {"ready": False, "result": None}
_watchdog_task: Optional[asyncio.Task] = None


def _read_cgroup_memory() -> Optional[tuple]:
    """يعيد (المستعمل, الحد) بالبايت من cgroup v2 أو v1 إن توفر."""
    pairs = [
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ]
    for cur_p, max_p in pairs:
        try:
            cur = int(Path(cur_p).read_text().strip())
            mx_raw = Path(max_p).read_text().strip()
            mx = int(mx_raw) if mx_raw.isdigit() else None
            return cur, mx
        except Exception:
            continue
    return None


async def _watchdog() -> None:
    """★ تشخيص: يسجل أي تجمّد في الـ event loop (>1s) وضغط الذاكرة (>80%)."""
    tick = 0
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(1)
        lag = time.monotonic() - t0 - 1
        if lag > 1.0:
            logger.warning(f"⚠️ Event loop lag: {lag:.1f}s (التطبيق كان مجمّداً)")
        tick += 1
        if tick % 30 == 0:
            mem = _read_cgroup_memory()
            if mem and mem[1]:
                pct = mem[0] / mem[1] * 100
                level = logger.warning if pct > 80 else logger.info
                level(f"🧠 Memory: {mem[0] / 1e9:.2f}GB / {mem[1] / 1e9:.2f}GB ({pct:.0f}%)")


async def _background_startup() -> None:
    try:
        await _connect_video_mcp()
        result = await registry.startup_from_config()
        _hub_state["result"] = result
        _hub_state["ready"] = True
        logger.info(f"Hub startup result: {result}")
        logger.info(f"Hub tools registered: {len(registry.tool_routes)}")
        await _hub_stop.wait()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("فشل الإقلاع في الخلفية")
    finally:
        try:
            await _disconnect_video_mcp()
        except BaseException as e:
            logger.warning(f"خطأ أثناء إغلاق الفيديو: {e!r}")
        try:
            await registry.shutdown()
        except BaseException as e:
            logger.warning(f"خطأ أثناء إغلاق الـ hub: {e!r}")


def _log_runtime_versions() -> None:
    """★ يسجل نسخ المكتبات الأساسية عند كل إقلاع: يكشف تغيّر الاعتماديات بعد rebuild."""
    try:
        from importlib.metadata import version, PackageNotFoundError
        parts = []
        for pkg in ("mcp", "fastapi", "starlette", "uvicorn", "pydantic", "httpx", "reportlab", "python-docx", "python-pptx", "openpyxl"):
            try:
                parts.append(f"{pkg}={version(pkg)}")
            except PackageNotFoundError:
                parts.append(f"{pkg}=?")
        import sys, subprocess
        try:
            node_v = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            node_v = "?"
        logger.info(f"📌 Versions: python={sys.version.split()[0]} node={node_v} " + " ".join(parts))
    except Exception as e:
        logger.warning(f"تعذّر تسجيل النسخ: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _hub_stop, _hub_task, _font_task, _watchdog_task
    _log_runtime_versions()
    await auth.reload()
    if not APP_SECRET:
        logger.warning("⚠️ APP_SECRET غير مضبوط → /call و/upload مفتوحان لأي شخص. اضبطه كـ Secret في HF.")

    _hub_stop = asyncio.Event()
    _hub_task = asyncio.create_task(_background_startup())
    _font_task = asyncio.create_task(_ensure_arabic_font())
    _watchdog_task = asyncio.create_task(_watchdog())

    yield  # ← التطبيق يستقبل الطلبات هنا مباشرة

    _hub_stop.set()
    _, pending = await asyncio.wait({_hub_task}, timeout=20)
    if pending:
        _hub_task.cancel()
    if _font_task and not _font_task.done():
        _font_task.cancel()
    if _watchdog_task:
        _watchdog_task.cancel()


app = FastAPI(title="9Router Tools Server", version="1.0.5", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# ★ FIX: /tools يرجع JSON ضخم (300+ أداة بـ schemas) → ضغط gzip يصغّره بشكل كبير
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def _security_headers(request, call_next):
    t0 = time.monotonic()
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    dur = time.monotonic() - t0
    if dur > 3.0:
        logger.warning(f"🐢 Slow request: {request.method} {request.url.path} took {dur:.1f}s")
    return response


app.mount("/files", StaticFiles(directory=str(FILES_DIR)), name="files")


def move_to_files(source_path: str, filename: str = None) -> str:
    dest_dir = Path("/app/files")
    dest_dir.mkdir(parents=True, exist_ok=True)
    if filename is None:
        filename = Path(source_path).name
    dest_path = dest_dir / filename
    shutil.copy2(source_path, dest_path)
    return _full_file_url(f"/files/{filename}")


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
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            return False
    return True


async def _download(url: Optional[str], base64_data: Optional[str]) -> bytes:
    if base64_data:
        return base64.b64decode(base64_data)
    if url:
        # ★ FIX: getaddrinfo متزامن (قد يعلق على DNS) → ننفذه في thread
        if not await asyncio.to_thread(_is_url_safe, url):
            raise ValueError("الرابط غير آمن أو محظور (حماية SSRF)")
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            r = await client.get(url)
            if r.is_redirect:
                raise ValueError("إعادة التوجيه غير مسموحة لأسباب أمنية (SSRF)")
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
# 3) COMPOSIO — قراءة كسولة للمفتاح + revoke
# ══════════════════════════════════════════════════════════

async def composio_list_connected_tools() -> dict:
    api_key = _get_composio_api_key()
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{COMPOSIO_BASE_URL}/connected_accounts",
            headers={"x-api-key": api_key},
        )
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح أو منتهي. تحقق من COMPOSIO_API_KEY."}
        if r.status_code != 200:
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
        data = r.json()
        items = data.get("items", [])

        if items:
            logger.info(f"📋 Raw connected_account sample: {items[0]}")

        tools = [{
            "slug": (it.get("toolkit") or {}).get("slug"),
            "id": it.get("id"),
            "alias": it.get("alias"),
            "status": it.get("status"),
            "user_id": it.get("user_id") or it.get("entity_id") or it.get("entityId"),
        } for it in items]

        return {
            "connected_tools": tools,
            "hint": (
                "استخدم حقل 'user_id' هنا بالضبط عند استدعاء composio_execute_tool — "
                "لا تخترع قيمة عشوائية. اختر حساباً بحالة status='ACTIVE' فقط."
            ),
        }


async def composio_list_tools(toolkit_slug: str, limit: int = 20) -> dict:
    api_key = _get_composio_api_key()
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not toolkit_slug or not toolkit_slug.strip():
        return {
            "error": "المعامل 'toolkit_slug' مطلوب ولا يمكن أن يكون فارغاً.",
            "suggestion": "استخدم composio_list_connected_tools أولاً.",
            "example": "toolkit_slug='gmail' أو 'github'",
        }

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{COMPOSIO_BASE_URL_V31}/tools",
            params={
                "toolkit_slug": toolkit_slug,
                "toolkit_versions": "latest",
                "limit": limit,
            },
            headers={"x-api-key": api_key},
        )
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح."}
        if r.status_code != 200:
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
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
    api_key = _get_composio_api_key()
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not slug or not slug.strip():
        return {
            "error": "المعامل 'slug' مطلوب ولا يمكن أن يكون فارغاً.",
            "suggestion": "استخدم composio_list_tools أولاً.",
        }

    effective_user_id = user_id or _get_composio_default_user_id()
    payload: Dict[str, Any] = {
        "arguments": arguments or {},
        "version": version,
        "user_id": effective_user_id,
    }

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.post(
            f"{COMPOSIO_BASE_URL_V31}/tools/execute/{slug}",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code == 404:
            error_text = r.text.lower()
            toolkit_guess = slug.split("_")[0].lower()

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
                        "الموجود في أحد عناصرها. إن كانت فارغة، يجب إعادة الربط."
                    ),
                }

            suggestion = await composio_list_tools(toolkit_slug=toolkit_guess, limit=50)
            return {
                "error": f"الأداة '{slug}' غير موجودة (404).",
                "status_code": 404,
                "composio_detail": r.text[:500],
                "correct_slugs_for_this_toolkit": suggestion.get("tools", []),
                "instruction": "استخدم أحد الأسماء في 'correct_slugs_for_this_toolkit' حرفياً.",
            }

        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح."}
        if r.status_code not in (200, 201):
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
        return r.json()


async def composio_create_session(
    user_id: str,
    toolkits: Optional[List[str]] = None,
    wait_for_connections: bool = False,
    callback_url: Optional[str] = None,
) -> dict:
    api_key = _get_composio_api_key()
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not user_id or not user_id.strip():
        return {
            "error": "المعامل 'user_id' مطلوب ولا يمكن أن يكون فارغاً.",
            "suggestion": "قدم معرف مستخدم صالح.",
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
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code == 400:
            return {
                "error": "خطأ في التحقق من صحة الطلب",
                "detail": r.text[:500],
                "payload_sent": payload,
            }
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح."}
        if r.status_code not in (200, 201):
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
        data = r.json()

        tool_router_tools = data.get("tool_router_tools", [])
        session_id = data.get("session_id")
        logger.info(f"📋 Session {session_id} created. Tools: {len(tool_router_tools)}")

        if tool_router_tools:
            logger.info(f"📋 Sample tools: {tool_router_tools[:10]}")

        return {
            "session_id": session_id,
            "mcp_url": (data.get("mcp") or {}).get("url"),
            "tool_router_tools": tool_router_tools,
            "config": data.get("config", {}),
            "tools_count": len(tool_router_tools),
        }


async def composio_get_session(session_id: str) -> dict:
    api_key = _get_composio_api_key()
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not session_id or not session_id.strip():
        return {"error": "المعامل 'session_id' مطلوب."}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{COMPOSIO_BASE_URL_V31}/tool_router/session/{session_id}",
            headers={"x-api-key": api_key},
        )
        if r.status_code == 404:
            return {"error": f"الجلسة '{session_id}' غير موجودة.", "status_code": 404}
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح."}
        if r.status_code != 200:
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
        return r.json()


async def composio_session_toolkits(session_id: str) -> dict:
    api_key = _get_composio_api_key()
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not session_id or not session_id.strip():
        return {"error": "المعامل 'session_id' مطلوب."}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{COMPOSIO_BASE_URL_V31}/tool_router/session/{session_id}/toolkits",
            headers={"x-api-key": api_key},
        )
        if r.status_code == 404:
            return {"error": f"الجلسة '{session_id}' غير موجودة.", "status_code": 404}
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح."}
        if r.status_code != 200:
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
        return r.json()


async def composio_authorize_toolkit(
    session_id: str,
    toolkit: str,
    callback_url: Optional[str] = None,
) -> dict:
    api_key = _get_composio_api_key()
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not session_id or not session_id.strip():
        return {"error": "المعامل 'session_id' مطلوب."}
    if not toolkit or not toolkit.strip():
        return {"error": "المعامل 'toolkit' مطلوب."}

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
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code == 404:
            return {
                "error": f"الجلسة '{session_id}' غير موجودة أو التطبيق '{toolkit}' غير صالح (404).",
                "status_code": 404,
            }
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح."}
        if r.status_code not in (200, 201):
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
        data = r.json()
        redirect_url = data.get("redirect_url") or data.get("redirectUrl")
        return {"redirect_url": redirect_url, "toolkit": toolkit, "raw": data}


async def composio_close_session(session_id: str) -> dict:
    api_key = _get_composio_api_key()
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not session_id or not session_id.strip():
        return {"error": "المعامل 'session_id' مطلوب."}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.delete(
            f"{COMPOSIO_BASE_URL_V31}/tool_router/session/{session_id}",
            headers={"x-api-key": api_key},
        )
        if r.status_code == 404:
            return {"error": f"الجلسة '{session_id}' غير موجودة أو محذوفة مسبقاً.", "status_code": 404}
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح."}
        if r.status_code not in (200, 201, 204):
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
        return r.json() if r.content else {"ok": True, "closed": session_id}


# ★ ★ ★ composio_revoke_account (تسجيل الخروج الفعلي — v3.1 + DELETE) ★ ★ ★
async def composio_revoke_account(connected_account_id: str) -> dict:
    """
    ✅ تسجيل خروج فعلي: حذف الحساب من Composio وإلغاء التوثيق.
    يستخدم v3.1 مع revoke_on_delete=true.
    """
    api_key = _get_composio_api_key()
    if not api_key:
        return {"ok": False, "error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}

    cid = (connected_account_id or "").strip()
    if not cid:
        return {"ok": False, "error": "connected_account_id مطلوب"}

    url = f"{COMPOSIO_BASE_URL_V31}/connected_accounts/{cid}?revoke_on_delete=true"
    headers = {"x-api-key": api_key}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.delete(url, headers=headers)
        info = {
            "upstream_status": r.status_code,
            "upstream_body": r.text[:300],
            "url": url,
        }
        if r.status_code == 404:
            return {"ok": True, "revoked": cid, "note": "already_deleted", **info}
        if r.status_code not in (200, 204):
            return {"ok": False, "error": f"Composio رفض الحذف ({r.status_code})", **info}
        try:
            body = r.json()
            success = r.status_code == 204 or body.get("success") is True
        except Exception:
            success = r.status_code == 204
        if not success:
            return {"ok": False, "error": "رد غير متوقع من Composio", **info}

        status_after = "UNKNOWN"
        try:
            g = await client.get(
                f"{COMPOSIO_BASE_URL_V31}/connected_accounts/{cid}",
                headers=headers,
            )
            if g.status_code == 404:
                status_after = "DELETED"
            elif g.status_code == 200:
                status_after = str(g.json().get("status", "")).upper()
        except Exception:
            pass

    if status_after == "ACTIVE":
        return {
            "ok": False,
            "error": "حُذف لكن الحساب ما زال ACTIVE — revoke_on_delete يعمل في الخلفية",
            "status_after": status_after,
            **info,
        }
    return {"ok": True, "revoked": cid, "status_after": status_after, **info}


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
    rel = save_bytes(buf.getvalue(), "docx")
    return {"file_url": _full_file_url(rel), "relative_path": rel}


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
    rel = save_bytes(buf.getvalue(), "pptx")
    return {"file_url": _full_file_url(rel), "relative_path": rel}


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
    rel = save_bytes(buf.getvalue(), "pdf")
    return {"file_url": _full_file_url(rel), "relative_path": rel}


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
    rel = save_bytes(buf.getvalue(), "xlsx")
    return {"file_url": _full_file_url(rel), "relative_path": rel}


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
    rel = save_bytes(data, ext)
    return {"local_path": str(FILES_DIR / rel.lstrip("/").replace("files/", "", 1)), "file_url": _full_file_url(rel), "relative_path": rel}


def write_text_file(content: str, ext: str = "srt") -> dict:
    rel = save_bytes(content.encode("utf-8"), ext)
    # local_path يبقى بنفس التنسيق القديم للتوافق مع أدوات الفيديو
    local = str(FILES_DIR / rel.lstrip("/").replace("files/", "", 1))
    return {"local_path": local, "file_url": _full_file_url(rel), "relative_path": rel}


# ══════════════════════════════════════════════════════════
# ★ تتبّع ملفات الدردشة: ما الذي تغيّر في هذا الاستدعاء + قائمة ملفاتي
#   يحل مشكلة «الوكيل يخطئ في تحديد الملف المستعمل»: كل رد يذكر الملفات الجديدة/المعدّلة بروابطها.
# ══════════════════════════════════════════════════════════
def _snapshot(ws: Path, limit: int = 5000) -> Dict[str, tuple]:
    """لقطة {المسار النسبي: (mtime_ns, size)} لكل ملفات المساحة (بدون تتبّع symlinks)."""
    out: Dict[str, tuple] = {}
    stack = [ws]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        out[str(Path(e.path).relative_to(ws))] = (st.st_mtime_ns, st.st_size)
                        if len(out) >= limit:
                            return out
        except OSError:
            continue
    return out


def _diff_snapshots(before: Dict[str, tuple], after: Dict[str, tuple], ws_name: str, limit: int = 50) -> List[Dict[str, Any]]:
    changed: List[Dict[str, Any]] = []
    for rel, meta in after.items():
        old = before.get(rel)
        if old == meta:
            continue
        changed.append({
            "path": rel,
            "size": meta[1],
            "status": "new" if old is None else "modified",
            "url": _full_file_url(f"/files/{ws_name}/{quote(rel)}"),
        })
        if len(changed) >= limit:
            break
    return changed


def list_my_files(limit: int = 100) -> dict:
    """ملفات هذه الدردشة فقط (الأحدث أولاً) مع المسار المحلي والرابط."""
    from datetime import datetime, timezone
    ws = workspace_for(_current_client_id.get())
    snap = _snapshot(ws)
    n = max(1, min(int(limit), 500))
    items = sorted(snap.items(), key=lambda kv: kv[1][0], reverse=True)[:n]
    return {
        "workspace_url": _full_file_url(f"/files/{ws.name}/"),
        "total_files": len(snap),
        "files": [
            {
                "path": rel,
                "local_path": str(ws / rel),
                "size": meta[1],
                "modified": datetime.fromtimestamp(meta[0] / 1e9, tz=timezone.utc).isoformat(),
                "url": _full_file_url(f"/files/{ws.name}/{quote(rel)}"),
            }
            for rel, meta in items
        ],
    }


# ══════════════════════════════════════════════════════════
# سجل TOOL_FUNCTIONS
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
    "composio_revoke_account": composio_revoke_account,
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
    "list_my_files": list_my_files,
}


# ══════════════════════════════════════════════════════════
# TOOLS_MANIFEST — الأدوات المضمّنة
# ══════════════════════════════════════════════════════════
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
        "description": "جلب قائمة الأدوات القابلة للتنفيذ داخل تطبيق معيّن. يجب استدعاؤها أولاً للحصول على slugs صالحة.",
        "parameters": {"type": "object", "properties": {
            "toolkit_slug": {"type": "string", "description": "✅ اسم التطبيق (مثل 'gmail')"},
            "limit": {"type": "integer", "default": 20},
        }, "required": ["toolkit_slug"]},
    }},
    {"type": "function", "function": {
        "name": "composio_execute_tool",
        "description": (
            "✅ تنفيذ أداة Composio. ⚠️ إلزامي: لا تخترع user_id عشوائياً. "
            "استدعِ composio_list_connected_tools أولاً."
        ),
        "parameters": {"type": "object", "properties": {
            "slug": {"type": "string"},
            "arguments": {"type": "object"},
            "user_id": {"type": "string"},
        }, "required": ["slug", "user_id"]},
    }},
    {"type": "function", "function": {
        "name": "composio_create_session",
        "description": "إنشاء جلسة Composio Tool Router.",
        "parameters": {"type": "object", "properties": {
            "user_id": {"type": "string"},
            "toolkits": {"type": "array", "items": {"type": "string"}},
            "wait_for_connections": {"type": "boolean", "default": False},
            "callback_url": {"type": "string"},
        }, "required": ["user_id"]},
    }},
    {"type": "function", "function": {
        "name": "composio_get_session",
        "description": "جلب حالة جلسة Tool Router.",
        "parameters": {"type": "object", "properties": {
            "session_id": {"type": "string"},
        }, "required": ["session_id"]},
    }},
    {"type": "function", "function": {
        "name": "composio_session_toolkits",
        "description": "عرض توليكيتات جلسة معيّنة وحالة اتصال كل منها.",
        "parameters": {"type": "object", "properties": {
            "session_id": {"type": "string"},
        }, "required": ["session_id"]},
    }},
    {"type": "function", "function": {
        "name": "composio_authorize_toolkit",
        "description": "إنشاء رابط تسجيل دخول (Connect Link) لتطبيق واحد.",
        "parameters": {"type": "object", "properties": {
            "session_id": {"type": "string"},
            "toolkit": {"type": "string"},
            "callback_url": {"type": "string"},
        }, "required": ["session_id", "toolkit"]},
    }},
    {"type": "function", "function": {
        "name": "composio_close_session",
        "description": "إغلاق/حذف جلسة Tool Router نهائياً.",
        "parameters": {"type": "object", "properties": {
            "session_id": {"type": "string"},
        }, "required": ["session_id"]},
    }},
    {"type": "function", "function": {
        "name": "composio_revoke_account",
        "description": (
            "✅ تسجيل الخروج الفعلي من تطبيق Composio — يلغي التفويض "
            "ويُبقي السجل قابلاً للاستعادة. "
            "⚠️ استدعِ composio_list_connected_tools أولاً للحصول على 'id'."
        ),
        "parameters": {"type": "object", "properties": {
            "connected_account_id": {
                "type": "string",
                "description": "معرّف الحساب من حقل 'id' في composio_list_connected_tools.",
            },
        }, "required": ["connected_account_id"]},
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
        "description": "ينزّل رابط فيديو/صوت (أو base64) ويحفظه كملف فعلي على الخادم، ويُرجع local_path.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "base64_data": {"type": "string"},
            "ext": {"type": "string", "default": "mp4"},
        }},
    }},
    {"type": "function", "function": {
        "name": "write_text_file",
        "description": "يكتب نصاً مباشرة إلى ملف حقيقي على الخادم، ويُرجع local_path.",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string"},
            "ext": {"type": "string", "default": "srt"},
        }, "required": ["content"]},
    }},
    {"type": "function", "function": {
        "name": "list_my_files",
        "description": "يعرض ملفات هذه الدردشة فقط (الأحدث أولاً) مع المسار المحلي والرابط. استعمله لمعرفة الملف الصحيح بدل التخمين.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "default": 100},
        }},
    }},
])


# ══════════════════════════════════════════════════════════
# Pydantic models
# ══════════════════════════════════════════════════════════
class CallRequest(BaseModel):
    tool: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


def _check_auth(x_api_key: str) -> None:
    if not APP_SECRET:
        if REQUIRE_AUTH:
            raise HTTPException(503, "APP_SECRET غير مضبوط على الخادم (REQUIRE_AUTH=1)")
        return
    # مقارنة ثابتة الزمن
    if not hmac.compare_digest((x_api_key or "").encode(), APP_SECRET.encode()):
        raise HTTPException(401, "غير مصرح: مفتاح API غير صحيح أو مفقود (X-API-Key)")


# ══════════════════════════════════════════════════════════
# ★ هوية العميل (عزل الملفات)
#   بدون X-Client-Id ولا X-API-Key كل المستخدمين يُحشرون في مجلد واحد: files/anonymous/
#   REQUIRE_CLIENT_ID=1  → يرفض /call بدون هوية (400) بدل الكتابة في anonymous.
# ══════════════════════════════════════════════════════════
_CLIENT_ID_HEADERS = ("x-client-id", "x-session-id", "x-user-id", "x-conversation-id", "x-thread-id")
REQUIRE_CLIENT_ID = os.environ.get("REQUIRE_CLIENT_ID", "").strip().lower() in ("1", "true", "yes")
_stats: Dict[str, int] = {"anonymous_calls": 0}


def _safe_client_name(client_id: str) -> str:
    return "".join(c for c in (client_id or "") if c.isalnum() or c in "-_")[:32] or "anonymous"


def _resolve_client_id(request: Request, x_api_key: str):
    """يعيد (client_id, مصدر الهوية). المصدر 'none' = لا هوية → anonymous."""
    for h in _CLIENT_ID_HEADERS:
        v = (request.headers.get(h) or "").strip()
        if v:
            return v, h
    if x_api_key:
        return hashlib.sha256(x_api_key.encode()).hexdigest()[:16], "api-key-hash"
    return "anonymous", "none"


# ══════════════════════════════════════════════════════════
# ★ عزل المستخدمين: مساحة عمل لكل دردشة + sandbox للمسارات + تقييد اختياري لأدوات الكود الحر
#   نقطة فرض واحدة داخل /call — لا تغيير في الأدوات نفسها.
#   (هوية العميل _resolve_client_id وأدوات tools.py المحلية معزولة أصلاً أعلاه وفي hub.py)
#   ملاحظة: هذا عزل ضد الخلط والأخطاء (best-effort)، وليس حاجزاً ضد مستخدم خبيث.
# ══════════════════════════════════════════════════════════
# أدوات تنفّذ كوداً حراً (تكسر العزل). تعمل لأي أحد ما لم تضبط ADMIN_SECRET؛
# وعند ضبطه لا تعمل إلا لمن يرسل الهيدر X-Admin-Key بنفس القيمة.
RESTRICTED_TOOLS = {t.strip() for t in os.environ.get("RESTRICTED_TOOLS", "run__run-code,ken__eval_js").split(",") if t.strip()}

# مجلدات إضافية مسموح استعمالها كما هي (مثلاً الخطوط): SANDBOX_EXTRA_ROOTS=/usr/share/fonts:/app/assets
_EXTRA_ROOTS = [Path(x).resolve() for x in os.environ.get("SANDBOX_EXTRA_ROOTS", "").split(":") if x.strip()]

_PATH_TOKEN = re.compile(
    r"(?:^|_)(?:path|paths|dir|dirs|directory|folder|file|files|source|destination|dest|clips|input|output)$"
)


def _is_admin(x_admin_key: str) -> bool:
    return bool(ADMIN_SECRET) and hmac.compare_digest((x_admin_key or "").encode(), ADMIN_SECRET.encode())


def _is_restricted_denied(tool: str, x_admin_key: str) -> bool:
    """True إذا كانت الأداة مقيّدة والمتصل ليس admin (يُطبَّق فقط عند ضبط ADMIN_SECRET)."""
    return bool(ADMIN_SECRET) and tool in RESTRICTED_TOOLS and not _is_admin(x_admin_key)


def workspace_for(client_id: str) -> Path:
    """مساحة الدردشة: files/<نفس اسم المجلد الذي تستعمله save_bytes و/upload و hub.py>."""
    ws = (FILES_DIR / _safe_client_name(client_id)).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _is_path_key(key: str, extra=frozenset()) -> bool:
    if key in extra:
        return True
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()
    return bool(_PATH_TOKEN.search(snake))


def _schema_path_keys(schema) -> frozenset:
    """أسماء الوسائط التي يصفها schema الأداة بأنها مسار/مجلد (مكمّل للتخمين بالاسم)."""
    props = (schema or {}).get("properties") or {}
    out = set()
    for k, v in props.items():
        desc = (v.get("description") or "").lower() if isinstance(v, dict) else ""
        if any(w in desc for w in ("path", "directory", "folder", "filesystem", "file system")):
            out.add(k)
    return frozenset(out)


def _confine(value: str, ws: Path) -> str:
    v = value.strip()
    if not v:
        return value
    low = v.lower()
    if low.startswith(("http://", "https://", "data:")):
        return value
    if "://" in v or low.startswith("file:"):
        raise HTTPException(403, "بروتوكول غير مسموح في المسارات")
    p = Path(v)
    if p.is_absolute():
        rp = p.resolve()
        files_root = FILES_DIR.resolve()
        if rp == ws or ws in rp.parents:
            return str(rp)
        if any(r == rp or r in rp.parents for r in _EXTRA_ROOTS):
            return str(rp)
        if rp == files_root or files_root in rp.parents:
            # مسار تحت /app/files لكن خارج مساحة هذه الدردشة → يُعاد توجيهه داخل مساحتها
            p = Path(*rp.relative_to(files_root).parts)
        else:
            raise HTTPException(403, "مسار خارج مساحة هذه الدردشة")
    rp = (ws / p).resolve()
    if rp != ws and ws not in rp.parents:
        raise HTTPException(403, "مسار خارج مساحة هذه الدردشة")
    return str(rp)


def _sandbox(obj, ws: Path, key: str = "", extra=frozenset()):
    """يحصر كل قيمة مسار داخل الوسائط في مساحة الدردشة. extra يُطبَّق على المستوى الأعلى فقط."""
    if isinstance(obj, dict):
        child_extra = extra if key == "" else frozenset()
        return {k: _sandbox(v, ws, str(k), child_extra) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sandbox(v, ws, key, frozenset()) for v in obj]
    if isinstance(obj, str):
        if key == "timeline":
            t = obj.strip()
            if t and t[0] in "{[":
                try:
                    return json.dumps(_sandbox(json.loads(t), ws, "", frozenset()), ensure_ascii=False)
                except ValueError:
                    return obj
            return _confine(obj, ws)
        if _is_path_key(key, extra):
            return _confine(obj, ws)
    return obj


# ══════════════════════════════════════════════════════════
# ★ FIX: غلاف المهلة الزمنية لاستدعاءات /call
# ══════════════════════════════════════════════════════════
async def _run(coro, tool: str):
    """ينفّذ coroutine بمهلة CALL_TIMEOUT ويحوّل التعليق إلى 504 واضح."""
    try:
        return await asyncio.wait_for(coro, timeout=CALL_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"الأداة {tool} تجاوزت المهلة ({CALL_TIMEOUT} ثانية)")


# ══════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════
@app.get("/")
async def root():
    return {"status": "ok", "service": "9Router Tools Server", "tools_count": len(TOOLS_MANIFEST)}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "firecrawl_configured": bool(FIRECRAWL_API_KEY),
        "context7_configured": bool(CONTEXT7_API_KEY),
        "composio_configured": bool(_get_composio_api_key()),
        "auth_enforced": bool(APP_SECRET),
        "video_mcp_connected": bool(video_session),
        "video_tools_count": len(video_tool_names),
        "hub_ready": _hub_state["ready"],
        "hub_startup_errors": (_hub_state.get("result") or {}).get("errors", {}),
        "hub_servers": len(registry.servers) if hasattr(registry, 'servers') else 0,
        "files_isolation": True,
        "anonymous_calls": _stats["anonymous_calls"],
        "require_client_id": REQUIRE_CLIENT_ID,
        "auth_required": REQUIRE_AUTH,
        "restricted_tools_need_admin": bool(ADMIN_SECRET),
        "space_base_url_set": bool(SPACE_BASE_URL),
        "call_timeout_seconds": CALL_TIMEOUT,
    }


# ══════════════════════════════════════════════════════════
# ★ FIX: /tools مضغوط ومخزّن مؤقتاً
#   الواجهة تقطع الطلب بعد ~10 ثوانٍ (signal timed out). الرد كان ضخماً (300+ أداة)،
#   والسيرفر يرد 200 لكن الجسم لا يصل في الوقت. نصغّره ونبنيه مرة واحدة.
#   متغيرات بيئة اختيارية:
#     TOOLS_DESC_MAX=400           أقصى طول لوصف أدوات الـ hub (0 = بلا اقتطاع)
#     HIDE_TOOL_PREFIXES=pt__,kino__glitch_   أدوات تُخفى من القائمة فقط (تبقى قابلة للاستدعاء في /call)
# ══════════════════════════════════════════════════════════
try:
    TOOLS_DESC_MAX = int(os.environ.get("TOOLS_DESC_MAX", "400"))
except ValueError:
    TOOLS_DESC_MAX = 400
HIDE_TOOL_PREFIXES = tuple(
    p.strip() for p in os.environ.get("HIDE_TOOL_PREFIXES", "").split(",") if p.strip()
)
_tools_cache: Dict[Any, bytes] = {}


def _slim_schema(node, is_props: bool = False):
    """يحذف \"$schema\" و\"title\" (حشو) من JSON Schema دون المساس بأسماء الخصائص."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if not is_props and k in ("$schema", "title"):
                continue
            out[k] = _slim_schema(v, is_props=(k == "properties" and not is_props))
        return out
    if isinstance(node, list):
        return [_slim_schema(x) for x in node]
    return node


def _shorten(text: str) -> str:
    if TOOLS_DESC_MAX and len(text) > TOOLS_DESC_MAX:
        return text[:TOOLS_DESC_MAX].rstrip() + "…"
    return text


def _build_tools_body(include_restricted: bool = True) -> bytes:
    all_tools = list(TOOLS_MANIFEST)
    seen_names = {t["function"]["name"] for t in all_tools}

    if hasattr(registry, "tool_routes"):
        for tool_name, route in registry.tool_routes.items():
            if tool_name in seen_names:
                continue
            if HIDE_TOOL_PREFIXES and tool_name.startswith(HIDE_TOOL_PREFIXES):
                continue
            if not include_restricted and tool_name in RESTRICTED_TOOLS:
                continue
            # أدوات video-audio-mcp تظهر مرتين (بدون prefix من الاتصال المباشر + video__* من الـ hub):
            # نخفي نسخة الـ hub من القائمة فقط (تبقى قابلة للاستدعاء) إن كان الاتصال المباشر شغالاً.
            if video_session and getattr(route, "server_name", "") == "video-audio-mcp":
                continue
            try:
                all_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": _shorten(getattr(route, "description", "") or ""),
                        "parameters": _slim_schema(
                            getattr(route, "input_schema", None) or {"type": "object", "properties": {}}
                        ),
                    },
                })
                seen_names.add(tool_name)
            except Exception as e:
                logger.warning(f"Skipping tool {tool_name} due to error: {e}")

    body = json.dumps(
        {"tools": all_tools},
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    logger.info(f"📦 /tools payload: {len(body) / 1024:.0f}KB, {len(all_tools)} tools")
    return body


@app.get("/tools")
async def list_tools(x_admin_key: str = Header(default="")):
    # عند ضبط ADMIN_SECRET تُخفى أدوات الكود الحر عمّن لا يملك X-Admin-Key
    include_restricted = (not ADMIN_SECRET) or _is_admin(x_admin_key)
    key = (
        len(TOOLS_MANIFEST),
        len(registry.tool_routes) if hasattr(registry, "tool_routes") else 0,
        bool(video_session), TOOLS_DESC_MAX, HIDE_TOOL_PREFIXES, include_restricted,
    )
    body = _tools_cache.get(key)
    if body is None:
        if len(_tools_cache) > 8:
            _tools_cache.clear()
        body = _tools_cache[key] = _build_tools_body(include_restricted)
    return Response(content=body, media_type="application/json")


@app.get("/oauth-callback-close")
async def oauth_callback_close():
    html_content = """
    <!DOCTYPE html>
    <html><head><meta charset="utf-8"></head>
    <body style="font-family:sans-serif;text-align:center;padding-top:50px;">
        <h2>✅ تم الربط بنجاح</h2>
        <p>يمكنك إغلاق هذه النافذة الآن.</p>
        <script>setTimeout(function() { window.close(); }, 1500);</script>
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
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    x_api_key: str = Header(default=""),
    x_client_id: str = Header(default=""),
):
    _check_auth(x_api_key)
    orig_ext = Path(file.filename or "").suffix.lower() or ""
    if orig_ext not in ALLOWED_UPLOAD_EXTS:
        raise HTTPException(
            400,
            f"امتداد الملف '{orig_ext or '(بدون امتداد)'}' غير مسموح. "
            f"المسموحة: {', '.join(sorted(ALLOWED_UPLOAD_EXTS))}",
        )

    # ★ عزل الملفات بحسب client_id
    cid, _src = _resolve_client_id(request, x_api_key)
    safe_client = "".join(c for c in cid if c.isalnum() or c in "-_")[:32] or "anonymous"
    user_dir = FILES_DIR / safe_client
    user_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid.uuid4().hex}{orig_ext}"
    dest = user_dir / filename
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
                        f"الملف أكبر من الحد ({MAX_UPLOAD_BYTES // (1024 * 1024)}MB)",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(500, f"فشل حفظ الملف: {e}")
    finally:
        await file.close()

    rel = f"/files/{safe_client}/{filename}"
    return {
        "ok": True,
        "filename": filename,
        "original_filename": file.filename,
        "local_path": str(dest),
        "file_url": _full_file_url(rel),
        "relative_path": rel,
        "size_bytes": size,
    }


@app.post("/composio/disconnect")
async def composio_disconnect_endpoint(
    connected_account_id: str,
    x_api_key: str = Header(default=""),
):
    """
    ★ endpoint برمجي مباشر لتسجيل الخروج من Composio.
    لا يحتاج وكيل — يمكن استدعاؤه من أي كود خارجي.
    """
    _check_auth(x_api_key)
    return await composio_revoke_account(connected_account_id)


@app.post("/call")
async def call_tool(
    req: CallRequest,
    request: Request,
    response: Response,
    x_api_key: str = Header(default=""),
    x_client_id: str = Header(default=""),
    x_admin_key: str = Header(default=""),
):
    _check_auth(x_api_key)

    # ★ حساب client_id وضبطه في السياق لتعزيل الملفات
    client_id, id_source = _resolve_client_id(request, x_api_key)
    if id_source == "none":
        _stats["anonymous_calls"] += 1
        if REQUIRE_CLIENT_ID:
            raise HTTPException(
                400,
                "هوية العميل مفقودة: أرسل الهيدر X-Client-Id (معرّف المستخدم/الجلسة) مع كل طلب /call.",
            )
        if _stats["anonymous_calls"] % 100 == 1:
            logger.warning(
                "⚠️ /call بدون X-Client-Id ولا X-API-Key → الملفات تُحفظ في files/anonymous "
                f"(مشتركة بين كل المستخدمين). عدد هذه الطلبات: {_stats['anonymous_calls']}"
            )
    response.headers["X-Client-Id-Used"] = _safe_client_name(client_id)
    _current_client_id.set(client_id)

    # ★ أدوات الكود الحر: لمن يملك X-Admin-Key فقط (عند ضبط ADMIN_SECRET)
    if _is_restricted_denied(req.tool, x_admin_key):
        raise HTTPException(403, f"الأداة {req.tool} مقيّدة (تتطلب X-Admin-Key)")

    ws = workspace_for(client_id)
    ws_url = _full_file_url(f"/files/{ws.name}/")

    before = await asyncio.to_thread(_snapshot, ws)

    async def ok(result):
        after = await asyncio.to_thread(_snapshot, ws)
        return {
            "ok": True,
            "result": result,
            "workspace_url": ws_url,
            "files_changed": _diff_snapshots(before, after, ws.name),
        }

    # 1. أدوات الفيديو
    if req.tool in video_tool_names:
        if not video_session:
            raise HTTPException(503, "خادم أدوات الفيديو غير متصل")
        try:
            # ★ FIX: مهلة زمنية
            # نحلّ أسماء الوسائط أولاً ثم نحصر المسارات (حتى لا يتجاوز التخمين الـ sandbox)
            v_args = _sandbox(
                _resolve_video_args(req.tool, req.arguments), ws,
                extra=_schema_path_keys(video_tool_schemas.get(req.tool)),
            )
            result_text = await _run(_call_video_tool(req.tool, v_args), req.tool)
            return await ok(result_text)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"خطأ في أداة الفيديو {req.tool}: {e}")

    # 2. الأدوات المضمّنة
    fn = TOOL_FUNCTIONS.get(req.tool)
    if fn:
        try:
            if asyncio.iscoroutinefunction(fn):
                result = await _run(fn(**req.arguments), req.tool)
            else:
                # ★ FIX: الدوال المتزامنة (pdf/docx/xlsx/...) في thread حتى لا تبلوكي الـ event loop
                # (to_thread ينسخ contextvars، فعزل الملفات per-client يبقى يعمل)
                result = await _run(asyncio.to_thread(fn, **req.arguments), req.tool)
            return await ok(result)
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(400, str(e))
        except TypeError as e:
            raise HTTPException(400, f"معاملات غير صحيحة لأداة {req.tool}: {e}")
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:500] if e.response is not None else ""
            raise HTTPException(502, f"خطأ من الخدمة الخارجية: HTTP {e.response.status_code} - {detail}")
        except Exception as e:
            raise HTTPException(500, f"خطأ أثناء تنفيذ {req.tool}: {e}")

    # 3. أدوات Hub — مع عزل المستخدمين
    # ★ FIX: لا نطبع قائمة المسارات الكاملة (250+ اسم) في كل استدعاء
    logger.info(f"Incoming tool = {req.tool}")

    route = registry.tool_routes.get(req.tool)
    if route is None:
        logger.warning(f"Tool '{req.tool}' not found in hub routes")
    # ★ حصر كل مسار في مساحة الدردشة قبل وصوله لأي سيرفر MCP
    h_args = _sandbox(
        req.arguments, ws,
        extra=_schema_path_keys(getattr(route, "input_schema", None)),
    )

    context = AccessContext(client_id=client_id)

    try:
        # ★ FIX: مهلة زمنية للأدوات الثقيلة (render / nmap / ...)
        result = await _run(registry.call_tool(req.tool, h_args, context), req.tool)
        return await ok(result)
    except HTTPException:
        raise
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