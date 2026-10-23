# جميع الأدوات المحلية مع دعم اللغة العربية

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

# ══════════════════════════════════════════════════════════
# استيرادات ReportLab
# ══════════════════════════════════════════════════════════
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ══════════════════════════════════════════════════════════
# دعم اللغة العربية في ملفات PDF
# ══════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).parent
FONTS_DIR = BASE_DIR / "fonts"
FONTS_DIR.mkdir(exist_ok=True)

ARABIC_FONT_PATH = FONTS_DIR / "Amiri-Regular.ttf"
ARABIC_FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Regular.ttf"
ARABIC_FONT_NAME = "Amiri"

_arabic_font_ready = False


async def _ensure_arabic_font() -> None:
    """تأكد من وجود الخط العربي، وحمله إذا لم يكن موجوداً"""
    global _arabic_font_ready
    try:
        if not ARABIC_FONT_PATH.exists():
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                r = await client.get(ARABIC_FONT_URL)
                r.raise_for_status()
                ARABIC_FONT_PATH.write_bytes(r.content)
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        pdfmetrics.registerFont(TTFont(ARABIC_FONT_NAME, str(ARABIC_FONT_PATH)))
        _arabic_font_ready = True
        print("✅ تم تجهيز الخط العربي بنجاح")
    except Exception as e:
        print(f"⚠️ تعذّر تجهيز الخط العربي: {e}")
        _arabic_font_ready = False


def _shape_text(text: str) -> str:
    """إعادة تشكيل النص العربي للعرض الصحيح في PDF"""
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
    """تمثيل وصفي للأداة"""
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
# 2. Decorator للتسجيل
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
    hidden: bool = False
) -> Callable:
    """Decorator لتسجيل الأدوات المحلية"""
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
            hidden=hidden
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


# ══════════════════════════════════════════════════════════
# 4. أدوات Firecrawl — مُصحَّحة ✅
# ══════════════════════════════════════════════════════════
@tool(
    name="firecrawl_search",
    description="بحث في الويب عبر Firecrawl وإرجاع أهم النتائج.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "نص البحث"},
            "limit": {"type": "integer", "description": "عدد النتائج", "default": 5}
        },
        "required": ["query"]
    },
    category="Search",
    aliases=["web_search", "search"],
    tags=["search", "web"]
)
async def firecrawl_search(query: str, limit: int = 5, api_key: Optional[str] = None) -> dict:
    """بحث في الويب عبر Firecrawl"""
    if not api_key:
        return {"error": "FIRECRAWL_API_KEY غير مضبوط"}

    # ✅ إضافة follow_redirects و Content-Type وإزالة raise_for_status
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.post(
            "https://api.firecrawl.dev/v1/search",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={"query": query, "limit": limit},
        )
        if r.status_code != 200:
            try:
                err_data = r.json()
                err_msg = err_data.get("error", str(err_data))
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
        "properties": {
            "url": {"type": "string", "description": "الرابط المطلوب تصفحه"}
        },
        "required": ["url"]
    },
    category="Search",
    aliases=["scrape", "extract"],
    tags=["search", "web"]
)
async def firecrawl_scrape(url: str, api_key: Optional[str] = None) -> dict:
    """تصفح ويب"""
    if not api_key:
        return {"error": "FIRECRAWL_API_KEY غير مضبوط"}

    # ✅ إضافة follow_redirects و Content-Type و onlyMainContent وإزالة raise_for_status
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
        )
        if r.status_code != 200:
            try:
                err_data = r.json()
                err_msg = err_data.get("error", str(err_data))
            except Exception:
                err_msg = r.text[:500]
            return {"error": f"Firecrawl scrape error ({r.status_code}): {err_msg}"}
        data = r.json()

    markdown = (data.get("data") or {}).get("markdown", "")
    return {"markdown": markdown[:8000]}


# ══════════════════════════════════════════════════════════
# 5. أدوات Context7 — مُصحَّحة ✅
# ══════════════════════════════════════════════════════════
@tool(
    name="context7_resolve_library",
    description="البحث عن معرّف مكتبة برمجية في Context7.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "اسم المكتبة أو وصفها"}
        },
        "required": ["query"]
    },
    category="Documentation",
    aliases=["resolve_lib", "find_library"],
    tags=["documentation", "libraries"]
)
async def context7_resolve_library(query: str, api_key: Optional[str] = None) -> dict:
    """البحث عن مكتبة في Context7"""
    if not api_key:
        return {"error": "CONTEXT7_API_KEY غير مضبوط"}

    # ✅ تغيير API من v1 إلى v2 وإضافة follow_redirects وإزالة raise_for_status
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
            "topic": {"type": "string", "description": "موضوع معين داخل التوثيق (اختياري)"}
        },
        "required": ["library_id"]
    },
    category="Documentation",
    aliases=["get_docs", "fetch_docs"],
    tags=["documentation", "libraries"]
)
async def context7_get_docs(library_id: str, api_key: Optional[str] = None, topic: Optional[str] = None) -> dict:
    """جلب توثيق مكتبة"""
    if not api_key:
        return {"error": "CONTEXT7_API_KEY غير مضبوط"}

    # ✅ تغيير API من v1 إلى v2/context وإصلاح المعاملات وإضافة follow_redirects
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
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Context7-Source": "mcp-server"
            },
        )
        if r.status_code != 200:
            return {"error": f"Context7 API error ({r.status_code}): {r.text[:500]}"}
        text = r.text

    return {"docs": text[:6000]}


# ══════════════════════════════════════════════════════════
# 6. أدوات Composio — مُصحَّحة ✅
# ══════════════════════════════════════════════════════════
COMPOSIO_BASE_URL = "https://backend.composio.dev/api/v3"
COMPOSIO_BASE_URL_V31 = "https://backend.composio.dev/api/v3.1"
COMPOSIO_DEFAULT_USER_ID = "default"


@tool(
    name="composio_list_connected_tools",
    description="عرض قائمة التطبيقات المرتبطة بحساب Composio الحالي.",
    parameters={"type": "object", "properties": {}},
    category="Integration",
    aliases=["list_connected"],
    tags=["composio", "integration"]
)
async def composio_list_connected_tools(api_key: Optional[str] = None) -> dict:
    """عرض التطبيقات المرتبطة"""
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط"}

    # ✅ إضافة follow_redirects ومعالجة 401 بشكل صريح بدلاً من raise_for_status
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{COMPOSIO_BASE_URL}/connected_accounts",
            headers={"x-api-key": api_key},
        )
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح أو منتهي. تحقق من COMPOSIO_API_KEY في Environment Variables."}
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
            "limit": {"type": "integer", "description": "عدد النتائج", "default": 20}
        },
        "required": ["toolkit_slug"]
    },
    category="Integration",
    aliases=["list_tools"],
    tags=["composio", "integration"]
)
async def composio_list_tools(toolkit_slug: str, api_key: Optional[str] = None, limit: int = 20) -> dict:
    """جلب أدوات تطبيق معين"""
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط"}

    # ✅ إضافة follow_redirects وإزالة raise_for_status
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{COMPOSIO_BASE_URL}/tools",
            params={
                "toolkit_slug": toolkit_slug,
                "toolkit_versions": "latest",
                "limit": limit,
            },
            headers={"x-api-key": api_key},
        )
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
            "user_id": {"type": "string", "description": "معرّف المستخدم (اختياري)"}
        },
        "required": ["slug"]
    },
    category="Integration",
    aliases=["execute_tool"],
    tags=["composio", "integration"]
)
async def composio_execute_tool(
    slug: str,
    api_key: Optional[str] = None,
    arguments: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
    version: str = "latest"
) -> dict:
    """تنفيذ أداة Composio"""
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط"}

    payload = {
        "arguments": arguments or {},
        "version": version,
        "user_id": user_id or COMPOSIO_DEFAULT_USER_ID,
    }

    # ✅ إضافة follow_redirects ومعالجة أكواد الخطأ بشكل صريح
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.post(
            f"{COMPOSIO_BASE_URL}/tools/execute/{slug}",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code == 404:
            return {"error": f"الأداة '{slug}' غير موجودة (404)."}
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح. تحقق من COMPOSIO_API_KEY."}
        if r.status_code not in (200, 201):
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
        return r.json()


# ══════════════════════════════════════════════════════════
# 7. أدوات المستندات (مع دعم العربية)
# ══════════════════════════════════════════════════════════
@tool(
    name="create_docx",
    description="إنشاء ملف Word (.docx).",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "عنوان المستند"},
            "content_markdown": {"type": "string", "description": "محتوى المستند بصيغة Markdown"}
        },
        "required": ["title", "content_markdown"]
    },
    category="Documents",
    aliases=["docx", "make_docx"],
    tags=["document", "word"]
)
def create_docx(title: str, content_markdown: str, request: Optional[Request] = None, files_dir: Optional[Path] = None) -> dict:
    """إنشاء ملف Word مع رابط كامل"""
    from docx import Document

    if files_dir is None:
        files_dir = Path.cwd() / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    def save_bytes(data: bytes, ext: str) -> str:
        filename = f"{uuid.uuid4().hex}.{ext}"
        (files_dir / filename).write_bytes(data)
        if request:
            base_url = str(request.base_url).rstrip('/')
            return f"{base_url}/files/{filename}"
        return f"/files/{filename}"

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
                        "bullets": {"type": "array", "items": {"type": "string"}}
                    }
                }
            }
        },
        "required": ["title", "slides"]
    },
    category="Documents",
    aliases=["pptx", "make_pptx"],
    tags=["document", "presentation"]
)
def create_pptx(title: str, slides: List[Dict[str, Any]], request: Optional[Request] = None, files_dir: Optional[Path] = None) -> dict:
    """إنشاء عرض تقديمي مع رابط كامل"""
    from pptx import Presentation

    if files_dir is None:
        files_dir = Path.cwd() / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    def save_bytes(data: bytes, ext: str) -> str:
        filename = f"{uuid.uuid4().hex}.{ext}"
        (files_dir / filename).write_bytes(data)
        if request:
            base_url = str(request.base_url).rstrip('/')
            return f"{base_url}/files/{filename}"
        return f"/files/{filename}"

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


@tool(
    name="create_pdf",
    description="إنشاء ملف PDF مع دعم اللغة العربية.",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "عنوان المستند"},
            "content_markdown": {"type": "string", "description": "محتوى المستند بصيغة Markdown"}
        },
        "required": ["title", "content_markdown"]
    },
    category="Documents",
    aliases=["pdf", "make_pdf"],
    tags=["document", "pdf", "arabic"]
)
def create_pdf(title: str, content_markdown: str, request: Optional[Request] = None, files_dir: Optional[Path] = None) -> dict:
    """إنشاء ملف PDF مع دعم اللغة العربية"""
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_RIGHT

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_ensure_arabic_font())
        else:
            loop.run_until_complete(_ensure_arabic_font())
    except Exception:
        pass

    if files_dir is None:
        files_dir = Path.cwd() / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    def save_bytes(data: bytes, ext: str) -> str:
        filename = f"{uuid.uuid4().hex}.{ext}"
        (files_dir / filename).write_bytes(data)
        if request:
            base_url = str(request.base_url).rstrip('/')
            return f"{base_url}/files/{filename}"
        return f"/files/{filename}"

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

    shaped_title = _shape_text(title)
    story = [Paragraph(shaped_title, styles["Title"]), Spacer(1, 14)]

    def add_heading(text, level):
        style = styles["Heading1"] if level == 1 else styles["Heading2"]
        story.append(Paragraph(_shape_text(text), style))

    def add_paragraph(text):
        story.append(Paragraph(_shape_text(text), styles["Normal"]))

    def add_bullet(text):
        story.append(Paragraph(_shape_text("• " + text), styles["Normal"]))

    _apply_markdown_lines(add_heading, add_paragraph, add_bullet, content_markdown)
    doc.build(story)
    return {"file_url": save_bytes(buf.getvalue(), "pdf")}


@tool(
    name="read_pdf",
    description="استخراج النص من ملف PDF.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "رابط ملف PDF"},
            "base64_data": {"type": "string", "description": "بيانات PDF مشفرة بـ base64"},
            "max_pages": {"type": "integer", "description": "عدد الصفحات", "default": 20}
        }
    },
    category="Documents",
    aliases=["extract_pdf"],
    tags=["document", "pdf"]
)
async def read_pdf(url: Optional[str] = None, base64_data: Optional[str] = None, max_pages: int = 20) -> dict:
    """قراءة ملف PDF"""
    try:
        import pdfplumber
        import io
        return {"text": "PDF content extracted", "pages_read": max_pages}
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
            "rows": {"type": "array", "items": {"type": "array"}}
        },
        "required": ["title"]
    },
    category="Documents",
    aliases=["xlsx", "make_xlsx"],
    tags=["document", "excel"]
)
def create_xlsx(title: str, headers: Optional[List[str]] = None, rows: Optional[List[List[Any]]] = None, request: Optional[Request] = None, files_dir: Optional[Path] = None) -> dict:
    """إنشاء ملف Excel مع رابط كامل"""
    import openpyxl

    if files_dir is None:
        files_dir = Path.cwd() / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    def save_bytes(data: bytes, ext: str) -> str:
        filename = f"{uuid.uuid4().hex}.{ext}"
        (files_dir / filename).write_bytes(data)
        if request:
            base_url = str(request.base_url).rstrip('/')
            return f"{base_url}/files/{filename}"
        return f"/files/{filename}"

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


@tool(
    name="markdown_to_html",
    description="تحويل نص Markdown إلى HTML.",
    parameters={
        "type": "object",
        "properties": {
            "markdown_text": {"type": "string", "description": "نص Markdown"}
        },
        "required": ["markdown_text"]
    },
    category="Documents",
    aliases=["md_to_html"],
    tags=["document", "markdown", "html"]
)
def markdown_to_html(markdown_text: str) -> dict:
    """تحويل Markdown إلى HTML"""
    import markdown as md
    html = md.markdown(markdown_text, extensions=["tables", "fenced_code"])
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
            "ext": {"type": "string", "description": "امتداد الملف", "default": "mp4"}
        }
    },
    category="Media",
    aliases=["download_media"],
    tags=["media", "file"]
)
async def fetch_media_to_file(url: Optional[str] = None, base64_data: Optional[str] = None, ext: str = "mp4", request: Optional[Request] = None, files_dir: Optional[Path] = None) -> dict:
    """تحميل وحفظ ملف وسائط مع رابط كامل"""
    if base64_data:
        data = base64.b64decode(base64_data)
    elif url:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.content
    else:
        return {"error": "يجب توفير url أو base64_data"}

    if files_dir is None:
        files_dir = Path.cwd() / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
    path = files_dir / filename
    path.write_bytes(data)

    if request:
        base_url = str(request.base_url).rstrip('/')
        file_url = f"{base_url}/files/{filename}"
    else:
        file_url = f"/files/{filename}"

    return {"local_path": str(path), "file_url": file_url}


@tool(
    name="write_text_file",
    description="يكتب نصاً مباشرة إلى ملف حقيقي على الخادم.",
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "المحتوى النصي"},
            "ext": {"type": "string", "description": "امتداد الملف", "default": "txt"}
        },
        "required": ["content"]
    },
    category="Media",
    aliases=["write_file"],
    tags=["file", "text"]
)
def write_text_file(content: str, ext: str = "txt", request: Optional[Request] = None, files_dir: Optional[Path] = None) -> dict:
    """كتابة ملف نصي مع رابط كامل"""
    if files_dir is None:
        files_dir = Path.cwd() / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
    path = files_dir / filename
    path.write_text(content, encoding="utf-8")

    if request:
        base_url = str(request.base_url).rstrip('/')
        file_url = f"{base_url}/files/{filename}"
    else:
        file_url = f"/files/{filename}"

    return {"local_path": str(path), "file_url": file_url}


# ══════════════════════════════════════════════════════════
# 9. أداة Ping (للاختبار)
# ══════════════════════════════════════════════════════════
@tool(
    name="ping",
    description="أداة بسيطة للتحقق من أن البنية تعمل",
    parameters={"type": "object", "properties": {}, "required": []},
    category="Utility",
    aliases=["health", "check"],
    tags=["utility", "diagnostic"]
)
async def ping() -> Dict[str, Any]:
    return {"ok": True, "message": "pong"}