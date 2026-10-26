# tools.py
# جميع الأدوات المحلية مع دعم اللغة العربية
# ★ نسخة نهائية: قراءة كسولة + read_pdf حقيقية + composio_revoke_account
# ★ إصلاح: دعم filename في دوال إنشاء الملفات

import os
import io
import json
import base64
import uuid
import asyncio
import httpx
from typing import Dict, Any, Callable, Optional, List
from dataclasses import dataclass, field
from pathlib import Path
from fastapi import Request

from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


# ══════════════════════════════════════════════════════════
# ★ قراءة متغيرات البيئة (كسولة)
# ══════════════════════════════════════════════════════════

def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        val = os.environ.get(name)
        if val and val.strip():
            return val.strip()
    return default


_COMPOSIO_KEY_ALIASES = (
    "COMPOSIO_API_KEY",
    "COMPOSIO_KEY",
    "COMPOSIO_TOKEN",
    "COMPOSIO_API_TOKEN",
)


def _get_composio_key(api_key: Optional[str] = None) -> Optional[str]:
    if api_key and str(api_key).strip():
        return str(api_key).strip()
    return _env(*_COMPOSIO_KEY_ALIASES)


def _get_composio_base_url() -> str:
    return _env(
        "COMPOSIO_BASE_URL",
        "COMPOSIO_API_URL",
        default="https://backend.composio.dev/api/v3"
    )


def _get_composio_default_user_id() -> str:
    return _env("COMPOSIO_USER_ID", default="default")


# ══════════════════════════════════════════════════════════
# دعم اللغة العربية في PDF
# ══════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).parent
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
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                r = await client.get(ARABIC_FONT_URL)
                r.raise_for_status()
                ARABIC_FONT_PATH.write_bytes(r.content)
        pdfmetrics.registerFont(TTFont(ARABIC_FONT_NAME, str(ARABIC_FONT_PATH)))
        _arabic_font_ready = True
        print("✅ تم تجهيز الخط العربي بنجاح")
    except Exception as e:
        print(f"⚠️ تعذّر تجهيز الخط العربي: {e}")
        _arabic_font_ready = False


def _shape_text(text: str) -> str:
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        if not text:
            return text
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except Exception as e:
        print(f"⚠️ فشل تشكيل النص العربي: {e}")
        return text


# ══════════════════════════════════════════════════════════
# 1. تعريف بنية الأداة
# ══════════════════════════════════════════════════════════
@dataclass
class LocalTool:
    name: str
    description: str
    parameters: Dict[str, Any]
    callable: Callable
    category: str = "General"
    aliases: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    enabled: bool = True
    hidden: bool = False


# ══════════════════════════════════════════════════════════
# 2. Decorator
# ══════════════════════════════════════════════════════════
TOOLS: Dict[str, LocalTool] = {}


def tool(
    *,
    name: Optional[str] = None,
    description: str,
    parameters: Dict[str, Any],
    category: str = "General",
    aliases: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    enabled: bool = True,
    hidden: bool = False,
) -> Callable:
    def wrapper(fn: Callable) -> Callable:
        tool_name = name or fn.__name__
        TOOLS[tool_name] = LocalTool(
            name=tool_name,
            description=description,
            parameters=parameters,
            callable=fn,
            category=category,
            aliases=aliases or [],
            tags=tags or [],
            enabled=enabled,
            hidden=hidden,
        )
        return fn
    return wrapper


# ══════════════════════════════════════════════════════════
# 3. دوال مساعدة
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


def _pick_content(*values) -> str:
    for v in values:
        if v and isinstance(v, str) and v.strip():
            return v
    return ""


def _safe_filename(name: str, default_ext: str) -> str:
    """ينظف اسم الملف ويضمن الامتداد الصحيح."""
    if not name or not name.strip():
        return ""
    safe = "".join(c for c in name.strip() if c.isalnum() or c in "-_.")[:60]
    if not safe:
        return ""
    if not safe.lower().endswith("." + default_ext):
        safe += "." + default_ext
    return safe


def _save_bytes_and_url(
    data: bytes, ext: str,
    request: Optional[Request],
    files_dir: Optional[Path],
    filename: str = "",
) -> dict:
    """دالة موحدة لحفظ الملفات. تدعم filename المخصص."""
    if files_dir is None:
        files_dir = Path.cwd() / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    safe = _safe_filename(filename, ext)
    if safe:
        path = files_dir / safe
        path.write_bytes(data)
        fname = safe
    else:
        fname = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
        path = files_dir / fname
        path.write_bytes(data)

    if request:
        base_url = str(request.base_url).rstrip("/")
        file_url = f"{base_url}/files/{fname}"
    else:
        file_url = f"/files/{fname}"
    return {"local_path": str(path), "file_url": file_url}


# ══════════════════════════════════════════════════════════
# 4. Firecrawl
# ══════════════════════════════════════════════════════════
@tool(
    name="firecrawl_search",
    description="بحث في الويب عبر Firecrawl وإرجاع أهم النتائج.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "نص البحث"},
            "limit": {"type": "integer", "description": "عدد النتائج", "default": 5},
        },
        "required": ["query"],
    },
    category="Search",
    aliases=["web_search", "search"],
    tags=["search", "web"],
)
async def firecrawl_search(query: str, limit: int = 5, api_key: Optional[str] = None) -> dict:
    api_key = api_key or _env("FIRECRAWL_API_KEY")
    if not api_key:
        return {"error": "FIRECRAWL_API_KEY غير مضبوط"}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.post(
            "https://api.firecrawl.dev/v1/search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"query": query, "limit": limit},
        )
        if r.status_code != 200:
            try:
                err_msg = r.json().get("error", str(r.json()))
            except Exception:
                err_msg = r.text[:500]
            return {"error": f"Firecrawl search error ({r.status_code}): {err_msg}"}
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


@tool(
    name="firecrawl_scrape",
    description="تصفح رابط ويب محدد وإرجاع محتواه كـ Markdown نظيف.",
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "الرابط المطلوب تصفحه"}},
        "required": ["url"],
    },
    category="Search",
    aliases=["scrape", "extract"],
    tags=["search", "web"],
)
async def firecrawl_scrape(url: str, api_key: Optional[str] = None) -> dict:
    api_key = api_key or _env("FIRECRAWL_API_KEY")
    if not api_key:
        return {"error": "FIRECRAWL_API_KEY غير مضبوط"}

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
        )
        if r.status_code != 200:
            try:
                err_msg = r.json().get("error", str(r.json()))
            except Exception:
                err_msg = r.text[:500]
            return {"error": f"Firecrawl scrape error ({r.status_code}): {err_msg}"}
        data = r.json()

    markdown = (data.get("data") or {}).get("markdown", "")
    return {"markdown": markdown[:8000]}


# ══════════════════════════════════════════════════════════
# 5. Context7
# ══════════════════════════════════════════════════════════
@tool(
    name="context7_resolve_library",
    description="البحث عن معرّف مكتبة برمجية في Context7.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "اسم المكتبة أو وصفها"}},
        "required": ["query"],
    },
    category="Documentation",
    aliases=["resolve_lib", "find_library"],
    tags=["documentation", "libraries"],
)
async def context7_resolve_library(query: str, api_key: Optional[str] = None) -> dict:
    api_key = api_key or _env("CONTEXT7_API_KEY")
    if not api_key:
        return {"error": "CONTEXT7_API_KEY غير مضبوط"}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            "https://context7.com/api/v2/libs/search",
            params={"libraryName": query, "query": query},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if r.status_code != 200:
            return {"error": f"Context7 API error ({r.status_code}): {r.text[:500]}"}
        data = r.json()

    results = data.get("results", data if isinstance(data, list) else [])
    libs = [{"id": it.get("id"), "title": it.get("title") or it.get("name")} for it in results[:8]]
    return {"libraries": libs}


@tool(
    name="context7_get_docs",
    description="جلب توثيق مكتبة برمجية من Context7.",
    parameters={
        "type": "object",
        "properties": {
            "library_id": {"type": "string", "description": "معرّف المكتبة (من context7_resolve_library)"},
            "topic": {"type": "string", "description": "موضوع معين داخل التوثيق (اختياري)"},
        },
        "required": ["library_id"],
    },
    category="Documentation",
    aliases=["get_docs", "fetch_docs"],
    tags=["documentation", "libraries"],
)
async def context7_get_docs(library_id: str, api_key: Optional[str] = None, topic: Optional[str] = None) -> dict:
    api_key = api_key or _env("CONTEXT7_API_KEY")
    if not api_key:
        return {"error": "CONTEXT7_API_KEY غير مضبوط"}

    if library_id.startswith("/"):
        library_id = library_id[1:]

    params = {
        "libraryId": f"/{library_id}",
        "query": topic or "documentation",
        "type": "txt",
    }

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            "https://context7.com/api/v2/context",
            params=params,
            headers={"Authorization": f"Bearer {api_key}", "X-Context7-Source": "mcp-server"},
        )
        if r.status_code != 200:
            return {"error": f"Context7 API error ({r.status_code}): {r.text[:500]}"}
        text = r.text

    return {"docs": text[:6000]}


# ══════════════════════════════════════════════════════════
# 6. Composio
# ══════════════════════════════════════════════════════════

@tool(
    name="composio_list_connected_tools",
    description="عرض قائمة التطبيقات المرتبطة بحساب Composio الحالي.",
    parameters={"type": "object", "properties": {}},
    category="Integration",
    aliases=["list_connected"],
    tags=["composio", "integration"],
)
async def composio_list_connected_tools(api_key: Optional[str] = None) -> dict:
    api_key = _get_composio_key(api_key)
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{_get_composio_base_url()}/connected_accounts",
            headers={"x-api-key": api_key},
        )
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح أو منتهي. تحقق من COMPOSIO_API_KEY."}
        if r.status_code != 200:
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
        data = r.json()

    items = data.get("items", [])
    tools = [{
        "slug": (it.get("toolkit") or {}).get("slug"),
        "id": it.get("id"),
        "alias": it.get("alias"),
        "status": it.get("status"),
    } for it in items]
    return {"connected_tools": tools}


@tool(
    name="composio_list_tools",
    description="جلب قائمة الأدوات القابلة للتنفيذ فعلياً داخل تطبيق معيّن.",
    parameters={
        "type": "object",
        "properties": {
            "toolkit_slug": {"type": "string", "description": "اسم التطبيق"},
            "limit": {"type": "integer", "description": "عدد النتائج", "default": 20},
        },
        "required": ["toolkit_slug"],
    },
    category="Integration",
    aliases=["list_tools"],
    tags=["composio", "integration"],
)
async def composio_list_tools(toolkit_slug: str, api_key: Optional[str] = None, limit: int = 20) -> dict:
    api_key = _get_composio_key(api_key)
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{_get_composio_base_url()}/tools",
            params={"toolkit_slug": toolkit_slug, "toolkit_versions": "latest", "limit": limit},
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
    return {"toolkit": toolkit_slug, "tools": tools}


@tool(
    name="composio_execute_tool",
    description="تنفيذ أداة معيّنة عبر Composio باستخدام slug الأداة الفعلي.",
    parameters={
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "معرّف الأداة"},
            "arguments": {"type": "object", "description": "معاملات الأداة"},
            "user_id": {"type": "string", "description": "معرّف المستخدم (اختياري)"},
        },
        "required": ["slug"],
    },
    category="Integration",
    aliases=["execute_tool"],
    tags=["composio", "integration"],
)
async def composio_execute_tool(
    slug: str,
    api_key: Optional[str] = None,
    arguments: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
    version: str = "latest",
) -> dict:
    api_key = _get_composio_key(api_key)
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}

    payload = {
        "arguments": arguments or {},
        "version": version,
        "user_id": user_id or _get_composio_default_user_id(),
    }

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.post(
            f"{_get_composio_base_url()}/tools/execute/{slug}",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code == 404:
            return {"error": f"الأداة '{slug}' غير موجودة (404)."}
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح."}
        if r.status_code not in (200, 201):
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
        return r.json()


@tool(
    name="composio_revoke_account",
    description=(
        "✅ تسجيل الخروج الفعلي من تطبيق Composio — يلغي التفويض ويُبقي السجل "
        "قابلاً للاستعادة لاحقاً. ⚠️ استدعِ composio_list_connected_tools أولاً للحصول على 'id'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "connected_account_id": {
                "type": "string",
                "description": "معرّف الحساب من حقل 'id' في composio_list_connected_tools.",
            }
        },
        "required": ["connected_account_id"],
    },
    category="Integration",
    aliases=["revoke_account", "logout_composio"],
    tags=["composio", "integration", "logout"],
)
async def composio_revoke_account(connected_account_id: str, api_key: Optional[str] = None) -> dict:
    api_key = _get_composio_key(api_key)
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}

    if not connected_account_id or not connected_account_id.strip():
        return {
            "error": "المعامل 'connected_account_id' مطلوب.",
            "suggestion": "استدعِ composio_list_connected_tools أولاً.",
        }

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.post(
            f"{_get_composio_base_url()}/connected_accounts/{connected_account_id}/revoke",
            headers={"x-api-key": api_key},
        )

    if r.status_code == 404:
        return {
            "error": f"الحساب '{connected_account_id}' غير موجود.",
            "status_code": 404,
            "suggestion": "تحقق من الـ id عبر composio_list_connected_tools.",
        }
    if r.status_code == 401:
        return {"error": "مفتاح Composio API غير صالح."}
    if r.status_code not in (200, 201, 204):
        return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}

    return {"ok": True, "revoked": connected_account_id}


# ══════════════════════════════════════════════════════════
# 7. أدوات المستندات (معاملات مرنة + filename)
# ══════════════════════════════════════════════════════════

@tool(
    name="create_docx",
    description="إنشاء ملف Word (.docx).",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "عنوان المستند"},
            "content_markdown": {"type": "string", "description": "محتوى المستند بصيغة Markdown"},
            "filename": {"type": "string", "description": "اسم الملف (اختياري)"},
        },
        "required": ["title", "content_markdown"],
    },
    category="Documents",
    aliases=["docx", "make_docx"],
    tags=["document", "word"],
)
def create_docx(
    title: str,
    content_markdown: str = "",
    content: str = "",
    text: str = "",
    markdown: str = "",
    filename: str = "",
    request: Optional[Request] = None,
    files_dir: Optional[Path] = None,
) -> dict:
    from docx import Document

    final_content = _pick_content(content_markdown, content, text, markdown)
    if not final_content:
        raise ValueError("المعامل 'content_markdown' مطلوب لإنشاء DOCX.")

    doc = Document()
    doc.add_heading(title, level=1)
    _apply_markdown_lines(
        lambda t, lvl: doc.add_heading(t, level=lvl),
        lambda t: doc.add_paragraph(t),
        lambda t: doc.add_paragraph(t, style="List Bullet"),
        final_content,
    )
    buf = io.BytesIO()
    doc.save(buf)
    return _save_bytes_and_url(buf.getvalue(), "docx", request, files_dir, filename)


@tool(
    name="create_pptx",
    description="إنشاء عرض تقديمي (.pptx).",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "عنوان العرض"},
            "slides": {
                "type": "array",
                "description": "قائمة الشرائح",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "عنوان الشريحة"},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "filename": {"type": "string", "description": "اسم الملف (اختياري)"},
        },
        "required": ["title", "slides"],
    },
    category="Documents",
    aliases=["pptx", "make_pptx"],
    tags=["document", "presentation"],
)
def create_pptx(
    title: str,
    slides: Optional[List[Dict[str, Any]]] = None,
    content: Optional[List[Dict[str, Any]]] = None,
    content_markdown: str = "",
    filename: str = "",
    request: Optional[Request] = None,
    files_dir: Optional[Path] = None,
) -> dict:
    from pptx import Presentation

    final_slides = slides or content
    if final_slides is None and content_markdown:
        final_slides = [{"title": title, "bullets": [content_markdown]}]
    if not final_slides:
        raise ValueError("المعامل 'slides' مطلوب لإنشاء PPTX.")

    prs = Presentation()
    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = title
    bullet_layout = prs.slide_layouts[1]
    for s in final_slides:
        slide = prs.slides.add_slide(bullet_layout)
        slide.shapes.title.text = s.get("title", "")
        tf = slide.placeholders[1].text_frame
        bullets = s.get("bullets", [])
        for i, b in enumerate(bullets):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = b
    buf = io.BytesIO()
    prs.save(buf)
    return _save_bytes_and_url(buf.getvalue(), "pptx", request, files_dir, filename)


@tool(
    name="create_pdf",
    description="إنشاء ملف PDF مع دعم اللغة العربية.",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "عنوان المستند"},
            "content_markdown": {"type": "string", "description": "محتوى المستند بصيغة Markdown"},
            "filename": {"type": "string", "description": "اسم الملف (اختياري)"},
        },
        "required": ["title", "content_markdown"],
    },
    category="Documents",
    aliases=["pdf", "make_pdf"],
    tags=["document", "pdf", "arabic"],
)
def create_pdf(
    title: str,
    content_markdown: str = "",
    content: str = "",
    text: str = "",
    markdown: str = "",
    filename: str = "",
    request: Optional[Request] = None,
    files_dir: Optional[Path] = None,
) -> dict:
    final_content = _pick_content(content_markdown, content, text, markdown)
    if not final_content:
        raise ValueError("المعامل 'content_markdown' مطلوب لإنشاء PDF.")

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_ensure_arabic_font())
        else:
            loop.run_until_complete(_ensure_arabic_font())
    except Exception:
        pass

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

    def add_heading(text, level):
        style = styles["Heading1"] if level == 1 else styles["Heading2"]
        story.append(Paragraph(_shape_text(text), style))

    def add_paragraph(text):
        story.append(Paragraph(_shape_text(text), styles["Normal"]))

    def add_bullet(text):
        story.append(Paragraph(_shape_text("• " + text), styles["Normal"]))

    _apply_markdown_lines(add_heading, add_paragraph, add_bullet, final_content)
    doc.build(story)
    return _save_bytes_and_url(buf.getvalue(), "pdf", request, files_dir, filename)


@tool(
    name="read_pdf",
    description="استخراج النص من ملف PDF (من URL أو base64).",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "رابط ملف PDF"},
            "base64_data": {"type": "string", "description": "بيانات PDF مشفرة بـ base64"},
            "max_pages": {"type": "integer", "description": "عدد الصفحات", "default": 20},
        },
    },
    category="Documents",
    aliases=["extract_pdf"],
    tags=["document", "pdf"],
)
async def read_pdf(url: Optional[str] = None, base64_data: Optional[str] = None, max_pages: int = 20) -> dict:
    try:
        import pdfplumber
    except ImportError:
        return {"error": "مكتبة pdfplumber غير مثبتة."}

    if base64_data:
        try:
            data = base64.b64decode(base64_data)
        except Exception as e:
            return {"error": f"فشل فك base64: {e}"}
    elif url:
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.content
        except Exception as e:
            return {"error": f"فشل تحميل PDF: {e}"}
    else:
        return {"error": "يجب توفير url أو base64_data"}

    try:
        text_parts: List[str] = []
        pages_read = 0
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages):
                if i >= max_pages:
                    break
                page_text = page.extract_text() or ""
                if page_text:
                    text_parts.append(page_text)
                pages_read += 1

        full_text = "\n\n".join(text_parts)
        return {
            "text": full_text[:50000],
            "pages_read": pages_read,
            "total_chars": len(full_text),
            "truncated": len(full_text) > 50000,
        }
    except Exception as e:
        return {"error": f"PDF reading failed: {e}"}


@tool(
    name="create_xlsx",
    description="إنشاء ملف Excel (.xlsx).",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "اسم الورقة"},
            "headers": {"type": "array", "items": {"type": "string"}},
            "rows": {"type": "array", "items": {"type": "array"}},
            "filename": {"type": "string", "description": "اسم الملف (اختياري)"},
        },
        "required": ["title"],
    },
    category="Documents",
    aliases=["xlsx", "make_xlsx"],
    tags=["document", "excel"],
)
def create_xlsx(
    title: str,
    headers: Optional[List[str]] = None,
    rows: Optional[List[List[Any]]] = None,
    filename: str = "",
    request: Optional[Request] = None,
    files_dir: Optional[Path] = None,
) -> dict:
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
    return _save_bytes_and_url(buf.getvalue(), "xlsx", request, files_dir, filename)


@tool(
    name="markdown_to_html",
    description="تحويل نص Markdown إلى HTML.",
    parameters={
        "type": "object",
        "properties": {"markdown_text": {"type": "string", "description": "نص Markdown"}},
        "required": ["markdown_text"],
    },
    category="Documents",
    aliases=["md_to_html"],
    tags=["document", "markdown", "html"],
)
def markdown_to_html(markdown_text: str = "", text: str = "", content: str = "") -> dict:
    import markdown as md
    final_text = _pick_content(markdown_text, text, content)
    if not final_text:
        raise ValueError("المعامل 'markdown_text' مطلوب.")
    html = md.markdown(final_text, extensions=["tables", "fenced_code"])
    return {"html": html}


# ══════════════════════════════════════════════════════════
# 8. أدوات الوسائط
# ══════════════════════════════════════════════════════════
@tool(
    name="fetch_media_to_file",
    description="ينزّل رابط فيديو/صوت (أو base64) ويحفظه كملف فعلي على الخادم.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "رابط الملف"},
            "base64_data": {"type": "string", "description": "بيانات الملف مشفرة بـ base64"},
            "ext": {"type": "string", "description": "امتداد الملف", "default": "mp4"},
            "filename": {"type": "string", "description": "اسم الملف (اختياري)"},
        },
    },
    category="Media",
    aliases=["download_media"],
    tags=["media", "file"],
)
async def fetch_media_to_file(
    url: Optional[str] = None,
    base64_data: Optional[str] = None,
    ext: str = "mp4",
    filename: str = "",
    request: Optional[Request] = None,
    files_dir: Optional[Path] = None,
) -> dict:
    if base64_data:
        data = base64.b64decode(base64_data)
    elif url:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.content
    else:
        return {"error": "يجب توفير url أو base64_data"}

    return _save_bytes_and_url(data, ext, request, files_dir, filename)


@tool(
    name="write_text_file",
    description="يكتب نصاً مباشرة إلى ملف حقيقي على الخادم.",
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "المحتوى النصي"},
            "ext": {"type": "string", "description": "امتداد الملف", "default": "txt"},
            "filename": {"type": "string", "description": "اسم الملف (اختياري)"},
        },
        "required": ["content"],
    },
    category="Media",
    aliases=["write_file"],
    tags=["file", "text"],
)
def write_text_file(
    content: str = "",
    text: str = "",
    data: str = "",
    ext: str = "txt",
    filename: str = "",
    request: Optional[Request] = None,
    files_dir: Optional[Path] = None,
) -> dict:
    final_content = _pick_content(content, text, data)
    if not final_content:
        raise ValueError("المعامل 'content' مطلوب.")
    return _save_bytes_and_url(final_content.encode("utf-8"), ext, request, files_dir, filename)


# ══════════════════════════════════════════════════════════
# 9. Ping
# ══════════════════════════════════════════════════════════
@tool(
    name="ping",
    description="أداة بسيطة للتحقق من أن البنية تعمل",
    parameters={"type": "object", "properties": {}, "required": []},
    category="Utility",
    aliases=["health", "check"],
    tags=["utility", "diagnostic"],
)
async def ping() -> Dict[str, Any]:
    return {"ok": True, "message": "pong"}